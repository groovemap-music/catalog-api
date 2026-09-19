"""Engine-backed regressions for catalog API query and write boundaries.

The second half of this module is the Neo4j-versus-PostgreSQL parity harness every
migrating query family reuses: a family registers its calls, and the harness runs each of
them on both backends against one shared fixture and compares the results to each other.
See the banner below, and `docs/graph-table-migration-template.md` for where that sits in
migrating a family.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from time import perf_counter
from types import ModuleType
from typing import Any, get_protocol_members
from uuid import UUID

import httpx
import pytest
import pytest_asyncio
import respx
from common import AsyncPostgreSQLPool, AsyncResilientNeo4jDriver, parse_postgres_host_port
from groovemap_schema.postgres import create_postgres_schema, property_graph_enabled
from neo4j.exceptions import Neo4jError
from psycopg.rows import dict_row

from api.graph_backend import CollaboratorsBackend, get_backend
from api.queries.credits_queries import get_person_connections
from api.queries.helpers import run_count, run_query, run_single
from api.syncer import DISCOGS_API_BASE, sync_collection
from tests import graph_fixture


pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

TEST_USER_ID = UUID("00000000-0000-0000-0000-000000000042")
TEST_RELEASE_ID = 4828001
TEST_USER_EMAIL = "integration@catalog-api.invalid"

# The tables a single test must start clean on. `users` and `catalog_items` are the
# roots of the foreign keys the sync writes through, so truncating them with CASCADE
# also empties user_collections, user_wantlists, owned_copies, artifacts, observations,
# and collection_snapshots. `provider_aliases` is named explicitly because nothing
# references it and CASCADE therefore never reaches it.
_RESET_TABLES = "TRUNCATE users, catalog_items, provider_aliases CASCADE"

# The collection sync writes through `user_collections.user_id`, which the real schema
# constrains to `users(id)`. The fixture seeds the account the tests sync as; the
# password column is NOT NULL and is never read by anything under test.
_SEED_USER = """
    INSERT INTO users (id, email, hashed_password, is_active)
    VALUES (%s, %s, %s, TRUE)
    ON CONFLICT (id) DO NOTHING
"""


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.fail(f"{name} must be supplied by `just test-integration`")
    return value


async def _consume(driver: AsyncResilientNeo4jDriver, cypher: str, **params: object) -> None:
    async with driver.session(database="neo4j") as session:
        result = await session.run(cypher, params)
        await result.consume()


@pytest_asyncio.fixture
async def neo4j_driver() -> AsyncIterator[AsyncResilientNeo4jDriver]:
    driver = AsyncResilientNeo4jDriver(
        uri=_required_env("NEO4J_HOST"),
        auth=(_required_env("NEO4J_USERNAME"), _required_env("NEO4J_PASSWORD")),
        max_retries=1,
    )
    await _consume(driver, "MATCH (n) DETACH DELETE n")
    try:
        yield driver
    finally:
        await driver.close()


@pytest_asyncio.fixture
async def postgres_pool() -> AsyncIterator[AsyncPostgreSQLPool]:
    """Apply the real PostgreSQL schema to the integration container, then reset it.

    The schema comes from ``groovemap_schema.postgres.create_postgres_schema``, pinned as
    a dev dependency on database-schema revision
    ``ea36cfa66672cb1e3f565165fea56d01b9b19c95`` — the same producer revision
    ``contracts/persistence/v1/source.json`` records this repository as tested against.
    Applying the producer's own DDL is what keeps the fixture from drifting behind the
    tables the syncer, identity, and projection paths read; a hand-rolled subset is what
    let ``provider_aliases`` go missing after ADR 0009.
    """
    host, port = parse_postgres_host_port(_required_env("POSTGRES_HOST"))
    pool = AsyncPostgreSQLPool(
        connection_params={
            "host": host,
            "port": port,
            "dbname": _required_env("POSTGRES_DATABASE"),
            "user": _required_env("POSTGRES_USERNAME"),
            "password": _required_env("POSTGRES_PASSWORD"),
        },
        min_connections=1,
        max_connections=2,
        max_retries=1,
        health_check_interval=3600,
    )
    await pool.initialize()
    # The authoritative DDL, not a hand-rolled subset: every statement is IF NOT EXISTS,
    # so this is a no-op once the container already carries the schema. A non-zero
    # failure count means the producer and this image disagree, which is a fixture bug
    # rather than something a test should be left to discover as a missing relation.
    failures = await create_postgres_schema(pool)
    assert failures == 0, f"{failures} schema statements failed against the integration container"
    async with pool.connection() as conn, conn.cursor() as cursor:
        await cursor.execute(_RESET_TABLES)
        await cursor.execute(_SEED_USER, (TEST_USER_ID, TEST_USER_EMAIL, "integration-fixture-not-a-hash"))
    try:
        yield pool
    finally:
        await pool.close()


async def test_4828_connections_query_executes_with_valid_grouping(
    neo4j_driver: AsyncResilientNeo4jDriver,
) -> None:
    """The depth-two credits query must compile and preserve second-hop results."""
    await _consume(
        neo4j_driver,
        """
        CREATE (alice:Person {name: 'Alice'}),
               (bob:Person {name: 'Bob'}),
               (carol:Person {name: 'Carol'}),
               (shared:Release {id: 'shared'}),
               (second:Release {id: 'second'}),
               (alice)-[:CREDITED_ON]->(shared),
               (bob)-[:CREDITED_ON]->(shared),
               (bob)-[:CREDITED_ON]->(second),
               (carol)-[:CREDITED_ON]->(second)
        """,
    )

    rows = await get_person_connections(neo4j_driver, "Alice", depth=2, limit=50)

    assert len(rows) == 1
    assert rows[0]["name"] == "Bob"
    assert rows[0]["shared_count"] == 1
    assert rows[0]["second_hops"] == [{"name": "Carol", "via": "Bob", "shared": 1}]


async def test_query_helpers_surface_engine_results_errors_and_timeout(
    neo4j_driver: AsyncResilientNeo4jDriver,
) -> None:
    """Exercise helper iteration, single/count consumption, syntax errors, and Query timeout."""
    rows = await run_query(
        neo4j_driver,
        "UNWIND $values AS value RETURN value ORDER BY value",
        database="neo4j",
        values=[3, 1, 2],
    )
    single = await run_single(
        neo4j_driver,
        "RETURN $value AS value",
        database="neo4j",
        timeout=5.0,
        value="single",
    )
    count = await run_count(
        neo4j_driver,
        "UNWIND $values AS value RETURN count(value) AS total",
        database="neo4j",
        timeout=5.0,
        values=[1, 2, 3],
    )

    assert rows == [{"value": 1}, {"value": 2}, {"value": 3}]
    assert single == {"value": "single"}
    assert count == 3

    with pytest.raises(Neo4jError):
        await run_query(neo4j_driver, "RETURN missing_identifier AS value", database="neo4j")

    # Prove the workload is valid before scaling it high enough to hit the
    # server-side timeout. A pure Cypher aggregation keeps this portable across
    # Community images, where optional sleep procedures are not installed.
    assert await run_single(
        neo4j_driver,
        "UNWIND range(1, $upper) AS value RETURN sum(toFloat(value)) AS total",
        upper=10,
    ) == {"total": 55.0}

    started = perf_counter()
    with pytest.raises(Neo4jError):
        await run_query(
            neo4j_driver,
            "UNWIND range(1, $upper) AS value RETURN sum(toFloat(value)) AS total",
            timeout=0.05,
            upper=100_000_000,
        )
    assert perf_counter() - started < 2.0


def _collection_payload(*, labels: list[dict[str, str]], formats: list[dict[str, object]]) -> dict[str, object]:
    return {
        "releases": [
            {
                "instance_id": 7,
                "folder_id": 1,
                "rating": 4,
                "date_added": "2026-01-02T03:04:05Z",
                "basic_information": {
                    "id": TEST_RELEASE_ID,
                    "title": "Integration Record",
                    "year": 2026,
                    "artists": [{"name": "Integration Artist"}],
                    "labels": labels,
                    "formats": formats,
                },
            }
        ],
        "pagination": {"page": 1, "pages": 1},
    }


async def test_z7d3_collection_upsert_preserves_existing_optional_values(
    postgres_pool: AsyncPostgreSQLPool,
    neo4j_driver: AsyncResilientNeo4jDriver,
) -> None:
    """A sparse repeat sync must preserve prior PostgreSQL JSONB and Neo4j metadata."""
    await _consume(neo4j_driver, "CREATE (:Release {id: $release_id})", release_id=str(TEST_RELEASE_ID))
    formats = [{"name": "Vinyl", "qty": "1", "descriptions": ["LP"]}]
    first = _collection_payload(labels=[{"name": "Integration Label", "catno": "CAT-001"}], formats=formats)
    sparse = _collection_payload(labels=[{"name": "Integration Label"}], formats=[])
    url = f"{DISCOGS_API_BASE}/users/integration-user/collection/folders/0/releases"

    with respx.mock(assert_all_called=True) as router:
        route = router.get(url)
        route.side_effect = [httpx.Response(200, json=first), httpx.Response(200, json=sparse)]
        first_count = await sync_collection(
            TEST_USER_ID,
            "integration-user",
            "consumer-key",
            "consumer-secret",
            "access-token",
            "token-secret",
            "catalog-api-integration-test",
            postgres_pool,
            neo4j_driver,
        )
        second_count = await sync_collection(
            TEST_USER_ID,
            "integration-user",
            "consumer-key",
            "consumer-secret",
            "access-token",
            "token-secret",
            "catalog-api-integration-test",
            postgres_pool,
            neo4j_driver,
        )

    async with postgres_pool.connection() as conn, conn.cursor(row_factory=dict_row) as cursor:
        await cursor.execute(
            "SELECT formats, metadata FROM user_collections WHERE user_id = %s AND release_id = %s AND instance_id = %s",
            (TEST_USER_ID, TEST_RELEASE_ID, 7),
        )
        stored = await cursor.fetchone()
    neo4j_release = await run_single(
        neo4j_driver,
        "MATCH (release:Release {id: $release_id}) RETURN release.catalog_number AS catalog_number",
        release_id=str(TEST_RELEASE_ID),
    )

    assert first_count == second_count == 1
    assert route.call_count == 2
    assert stored == {"formats": formats, "metadata": {"catalog_number": "CAT-001"}}
    assert neo4j_release == {"catalog_number": "CAT-001"}


# ── The Neo4j-versus-PostgreSQL parity harness ───────────────────────────────
#
# ADR 0012 migrates the graph reads off Cypher one query family at a time, and parity with
# the Cypher is the gate each family crosses on. That gate is this harness, not a bespoke
# test module per family: registering a family below is step one of
# `docs/graph-table-migration-template.md`, and it is one line.
#
# What the harness asserts, for every registered call, is that the two backends returned
# equal rows, in the same order, with the same Python types and the same column order —
# because the router swaps the modules underneath an unchanged response schema, so
# anything that moves is a response that moved. It compares the two results to each other
# rather than to a hand-written expectation; a hand-written expectation only proves both
# halves agree with whatever the author believed. Pinning what the fixture's answer
# actually *is* happens once, in `tests/test_graph_parity.py`.
#
# A family that needs `graph.catalog` declares `requires_property_graph=True` (the
# default), and its calls skip themselves on every tier but PostgreSQL 19, because that is
# the only tier the graph is declared on. On that tier nothing skips: a container that
# cannot serve the graph fails the run rather than quietly passing it.


@dataclass(frozen=True)
class ParityCall:
    """One call both backends of a family must answer identically.

    `args` and `kwargs` are passed after the backend's own handle — a Neo4j driver or a
    PostgreSQL pool — which the harness supplies.
    """

    function: str
    args: tuple[Any, ...] = ()
    kwargs: Mapping[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        rendered = [repr(value) for value in self.args]
        rendered += [f"{name}={value!r}" for name, value in self.kwargs.items()]
        return f"{self.function}({', '.join(rendered)})"


@dataclass(frozen=True)
class ParityFamily:
    """A query family and the calls the harness proves parity on."""

    name: str
    calls: tuple[ParityCall, ...]
    requires_property_graph: bool = True

    @property
    def functions(self) -> frozenset[str]:
        """The distinct functions these calls cover."""
        return frozenset(call.function for call in self.calls)


# family name -> what the harness runs for it. Written by `register_parity_family`.
PARITY_FAMILIES: dict[str, ParityFamily] = {}


def register_parity_family(name: str, calls: Sequence[ParityCall], *, requires_property_graph: bool = True) -> None:
    """Register *name* — a family in `api/graph_backend.py` — with the calls to compare.

    The backends themselves are not passed: the harness resolves them through the same
    selector the router uses, so a family cannot be proven at parity against a module the
    router would not actually call.
    """
    PARITY_FAMILIES[name] = ParityFamily(name=name, calls=tuple(calls), requires_property_graph=requires_property_graph)


@dataclass(frozen=True)
class ExpectedDifference:
    """A divergence between two backends that has been looked at and accepted.

    `normalize` is applied to *both* results before they are compared again, so the
    declaration says exactly how much is tolerated rather than switching the assertion
    off. `reason` is what a reader gets when the normalised results still disagree.
    """

    reason: str
    normalize: Callable[[Any], Any]


# (family, function) -> the difference that is allowed to stand between its two backends.
#
# The harness fails on any divergence that is not in here, and names this mapping when it
# does: a diff is tolerated only once someone has written down what it is and why. It also
# fails on an entry whose difference did not materialise, so a tolerance cannot outlive
# the behaviour it was granted for.
#
# It is empty, and the collaborators family is why it should stay that way — the pilot
# reaches column-for-column agreement with no tolerance at all. An entry looks like:
#
#     ("label_dna", "get_label_profile"): ExpectedDifference(
#         reason="Neo4j returns float scores; the SQL sums numeric and rounds at 6 places",
#         normalize=lambda rows: [{**row, "score": round(row["score"], 6)} for row in rows],
#     ),
EXPECTED_DIFFERENCES: dict[tuple[str, str], ExpectedDifference] = {}


def _type_shape(result: Any) -> Any:
    """Reduce a result to its types and column order, dropping the values.

    `==` on two lists of dicts compares values and row order but not much else: it holds
    between `1` and `True`, between `1` and `1.0`, and between two dicts whose columns
    come back in a different order. Comparing this alongside the values is what turns
    "same rows" into "same rows, same types, same order".
    """
    if isinstance(result, list):
        return [_type_shape(row) for row in result]
    if isinstance(result, dict):
        return [(column, type(value).__name__) for column, value in result.items()]
    return type(result).__name__


def assert_parity(family: str, call: ParityCall, *, neo4j_result: Any, postgres_result: Any) -> None:
    """Fail unless the two backends agreed, or agreed as far as a declared difference allows."""
    declared = EXPECTED_DIFFERENCES.get((family, call.function))
    agrees = postgres_result == neo4j_result and _type_shape(postgres_result) == _type_shape(neo4j_result)

    if declared is None:
        if not agrees:
            pytest.fail(
                f"{family}.{call} diverged between the two backends.\n"
                f"  neo4j:          {neo4j_result!r}\n"
                f"  postgres:       {postgres_result!r}\n"
                f"  neo4j types:    {_type_shape(neo4j_result)!r}\n"
                f"  postgres types: {_type_shape(postgres_result)!r}\n"
                f"If this difference is intended, declare it in EXPECTED_DIFFERENCES under "
                f"the key ({family!r}, {call.function!r}); the harness tolerates nothing it "
                f"has not been told about."
            )
        return

    if agrees:
        pytest.fail(
            f"EXPECTED_DIFFERENCES declares a difference for ({family!r}, {call.function!r}) — "
            f"{declared.reason} — but the two backends agree on {call}. Delete the entry."
        )

    normalized_neo4j = declared.normalize(neo4j_result)
    normalized_postgres = declared.normalize(postgres_result)
    if normalized_postgres != normalized_neo4j or _type_shape(normalized_postgres) != _type_shape(normalized_neo4j):
        pytest.fail(
            f"{family}.{call} diverged by more than the declared difference ({declared.reason}).\n"
            f"  neo4j:    {normalized_neo4j!r}\n"
            f"  postgres: {normalized_postgres!r}"
        )


async def _invoke(backend: ModuleType, call: ParityCall, handle: Any) -> Any:
    """Run one registered call against one backend."""
    function = getattr(backend, call.function)
    return await function(handle, *call.args, **call.kwargs)


# ── The pilot family ─────────────────────────────────────────────────────────
# Collaborators, the family ADR 0012's pilot migrated. Every call is made from a vantage
# point the fixture keeps free of ties on `(distance, collaboration_count)`, because
# neither implementation adds a tiebreaker and a tie would make row order legitimately
# unspecified on both sides — the comparison would be testing the two planners.
#
# Depth 3 is registered because the endpoint accepts it and the Cypher treats it as
# depth 2: its second UNION branch is the only one that adds hops. The PostgreSQL side has
# to make the same choice, and a parity run is the only place that shows it does.

_COLLABORATOR_ANCHORS = (graph_fixture.ANCHOR_ARTIST_ID, graph_fixture.PROBE_ANCHOR_ARTIST_ID)

COLLABORATORS_CALLS: tuple[ParityCall, ...] = (
    ParityCall("get_artist_identity", (graph_fixture.ANCHOR_ARTIST_ID,)),
    ParityCall("get_artist_identity", (graph_fixture.PROBE_ANCHOR_ARTIST_ID,)),
    ParityCall("get_artist_identity", ("does-not-exist",)),
    *(ParityCall("get_multi_hop_collaborators", (anchor,), {"depth": depth, "limit": 50}) for anchor in _COLLABORATOR_ANCHORS for depth in (1, 2, 3)),
    *(ParityCall("get_multi_hop_collaborators", (graph_fixture.ANCHOR_ARTIST_ID,), {"depth": 2, "limit": limit}) for limit in (1, 4)),
    *(ParityCall("count_multi_hop_collaborators", (anchor,), {"depth": depth}) for anchor in _COLLABORATOR_ANCHORS for depth in (1, 2, 3)),
)

register_parity_family("collaborators", COLLABORATORS_CALLS)


# The protocol each family's two backends are bound to in `api/graph_backend.py`. It is
# what the coverage test below reads to check that registering a family did not quietly
# leave one of its functions unproven.
FAMILY_PROTOCOLS: dict[str, type] = {"collaborators": CollaboratorsBackend}


# `graph.catalog` exists only on a PostgreSQL 19 server whose initializer ran with the
# switch on, which is what `just test-integration-pg19` arranges. Off that tier the
# property-graph families are skipped at collection, so the default suite never starts a
# pool it cannot use; on it, `graph_fixture.open_postgres_pool` asserts rather than skips.
_NEEDS_PROPERTY_GRAPH = pytest.mark.skipif(
    not property_graph_enabled(),
    reason="needs graph.catalog on PostgreSQL 19; run `just test-integration-pg19`",
)


def _parity_params() -> list[Any]:
    """Expand every registered family into one parameter per call."""
    params: list[Any] = []
    for family in PARITY_FAMILIES.values():
        marks = (_NEEDS_PROPERTY_GRAPH,) if family.requires_property_graph else ()
        params.extend(pytest.param(family.name, call, marks=marks, id=f"{family.name}-{call}") for call in family.calls)
    return params


@pytest_asyncio.fixture
async def parity_backends() -> AsyncIterator[graph_fixture.ParityBackends]:
    """Both engines, holding the same fixture."""
    async with graph_fixture.seeded_backends() as backends:
        yield backends


@pytest.mark.parametrize(("family", "call"), _parity_params())
async def test_graph_query_family_agrees_on_both_backends(
    parity_backends: graph_fixture.ParityBackends,
    family: str,
    call: ParityCall,
) -> None:
    """Same rows, same order, same types, from the same fixture, for one registered call."""
    neo4j_result = await _invoke(get_backend(family, "neo4j"), call, parity_backends.neo4j)
    postgres_result = await _invoke(get_backend(family, "postgres"), call, parity_backends.postgres)

    assert_parity(family, call, neo4j_result=neo4j_result, postgres_result=postgres_result)


@pytest.mark.parametrize("family", sorted(PARITY_FAMILIES))
async def test_every_function_of_a_registered_family_is_covered_by_a_parity_call(family: str) -> None:
    """A family is at parity only if every function the seam exposes was compared.

    The seam's `Protocol` is the list of functions the router can reach, so it is also the
    list the harness owes a call. Registering a family and forgetting one of its three
    functions is otherwise a silent gap: the suite goes green having never run it.
    """
    expected = frozenset(get_protocol_members(FAMILY_PROTOCOLS[family]))
    covered = PARITY_FAMILIES[family].functions

    assert covered == expected, f"{family}: {sorted(expected - covered)} have no registered parity call"
