"""Engine-backed regressions for catalog API query and write boundaries."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import datetime
from time import perf_counter
from uuid import UUID

import httpx
import pytest
import pytest_asyncio
import respx
from common import AsyncPostgreSQLPool, AsyncResilientNeo4jDriver, parse_postgres_host_port
from groovemap_schema.postgres import create_postgres_schema
from neo4j.exceptions import ClientError as Neo4jClientError
from neo4j.exceptions import Neo4jError
from psycopg.errors import QueryCanceled
from psycopg.rows import dict_row

from api.queries import strata_queries
from api.queries.credits_queries import get_person_connections
from api.queries.helpers import run_count, run_query, run_single
from api.queries.insights_neo4j_queries import query_style_year_counts
from api.queries.strata_queries import get_shared_personnel, get_strata_catalog, get_strata_labels, get_strata_marks, get_strata_root
from api.strata import MAX_ROOT_RELEASES, Budget, apply_labels, build_strata, label_targets
from api.syncer import DISCOGS_API_BASE, sync_collection, sync_wantlist


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
    ``f21b3e1482a0f74f79d7802c7fcd7b22e074b7cd`` — the same producer revision
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


def _dated_collection_payload(date_added: str | None) -> dict[str, object]:
    payload = _collection_payload(labels=[{"name": "Integration Label"}], formats=[])
    (release,) = payload["releases"]  # type: ignore[misc]
    if date_added is None:
        del release["date_added"]
    else:
        release["date_added"] = date_added
    return payload


def _dated_wantlist_payload(date_added: str | None) -> dict[str, object]:
    want: dict[str, object] = {
        "id": TEST_RELEASE_ID,
        "rating": 0,
        "notes": "integration want",
        "basic_information": {
            "title": "Integration Record",
            "year": 2026,
            "artists": [{"name": "Integration Artist"}],
            "formats": [{"name": "Vinyl", "qty": "1"}],
        },
    }
    if date_added is not None:
        want["date_added"] = date_added
    return {"wants": [want], "pagination": {"page": 1, "pages": 1}}


async def test_date_added_survives_a_sync_that_omits_it_and_still_updates(
    postgres_pool: AsyncPostgreSQLPool,
    neo4j_driver: AsyncResilientNeo4jDriver,
) -> None:
    """A stored date_added is kept when a re-sync lacks it, and replaced when it differs.

    Covers every store the field is written to: ``user_collections``, ``user_wantlists``,
    ``owned_copies.acquired_at``, and the Neo4j ``COLLECTED`` and ``WANTS`` edges.
    """
    await _consume(neo4j_driver, "CREATE (:Release {id: $release_id})", release_id=str(TEST_RELEASE_ID))
    first, later = "2026-01-02T03:04:05Z", "2026-03-04T05:06:07Z"
    collection_url = f"{DISCOGS_API_BASE}/users/integration-user/collection/folders/0/releases"
    wantlist_url = f"{DISCOGS_API_BASE}/users/integration-user/wants"
    credentials = ("consumer-key", "consumer-secret", "access-token", "token-secret", "catalog-api-integration-test")

    async def read_stores() -> dict[str, object]:
        async with postgres_pool.connection() as conn, conn.cursor(row_factory=dict_row) as cursor:
            await cursor.execute("SELECT date_added FROM user_collections WHERE user_id = %s", (TEST_USER_ID,))
            collection = [row["date_added"] for row in await cursor.fetchall()]
            await cursor.execute("SELECT date_added FROM user_wantlists WHERE user_id = %s", (TEST_USER_ID,))
            wantlist = [row["date_added"] for row in await cursor.fetchall()]
            await cursor.execute("SELECT acquired_at FROM owned_copies WHERE user_id = %s", (TEST_USER_ID,))
            copies = [row["acquired_at"] for row in await cursor.fetchall()]
        collected = await run_query(
            neo4j_driver,
            "MATCH (:User {id: $user_id})-[c:COLLECTED]->(:Release {id: $release_id}) RETURN c.date_added AS date_added",
            user_id=str(TEST_USER_ID),
            release_id=str(TEST_RELEASE_ID),
        )
        wants = await run_query(
            neo4j_driver,
            "MATCH (:User {id: $user_id})-[w:WANTS]->(:Release {id: $release_id}) RETURN w.date_added AS date_added",
            user_id=str(TEST_USER_ID),
            release_id=str(TEST_RELEASE_ID),
        )
        return {
            "collection": collection,
            "wantlist": wantlist,
            "owned_copies": copies,
            "collected": [row["date_added"] for row in collected],
            "wants": [row["date_added"] for row in wants],
        }

    async def sync_both(date_added: str | None) -> None:
        with respx.mock(assert_all_called=True) as router:
            router.get(collection_url).respond(200, json=_dated_collection_payload(date_added))
            router.get(wantlist_url).respond(200, json=_dated_wantlist_payload(date_added))
            await sync_collection(TEST_USER_ID, "integration-user", *credentials, postgres_pool, neo4j_driver)
            await sync_wantlist(TEST_USER_ID, "integration-user", *credentials, postgres_pool, neo4j_driver)

    def expected(iso: str) -> dict[str, object]:
        moment = datetime.fromisoformat(iso)
        return {"collection": [moment], "wantlist": [moment], "owned_copies": [moment], "collected": [iso], "wants": [iso]}

    await sync_both(first)
    assert await read_stores() == expected(first)

    await sync_both(None)
    assert await read_stores() == expected(first)

    await sync_both(later)
    assert await read_stores() == expected(later)


# A small discography written the way the enrichers write it. Master 501 has no year, and
# no `(:Master {id: '0'})` exists: masterless releases simply have no DERIVED_FROM edge, as
# discogs-graph-enricher writes them since the sentinel fix.
_STRATA_GRAPH = """
CREATE (root:Artist {id: '1', name: 'Strata Root'}),
       (guest:Artist {id: '2', name: 'Strata Guest'}),
       (m500:Master {id: '500', title: 'Master A', year: 1971}),
       (m501:Master {id: '501', title: 'Master B', year: 0}),
       (r10:Release {id: '10', title: 'A, first press', year: 1971}),
       (r11:Release {id: '11', title: 'A, undated press', year: 0}),
       (r12:Release {id: '12', title: 'B, joint', year: 1980}),
       (r13:Release {id: '13', title: 'No master, dated', year: 1985}),
       (r14:Release {id: '14', title: 'No master, undated', year: 0}),
       (r10)-[:BY]->(root), (r11)-[:BY]->(root), (r13)-[:BY]->(root), (r14)-[:BY]->(root),
       (r12)-[:BY]->(root), (r12)-[:BY]->(guest),
       (r10)-[:DERIVED_FROM]->(m500), (r11)-[:DERIVED_FROM]->(m500), (r12)-[:DERIVED_FROM]->(m501),
       (p:Person {name: 'Shared Producer'}),
       (p)-[:CREDITED_ON {role: 'Producer [Assistant]', category: 'Production'}]->(r10),
       (p)-[:CREDITED_ON {role: 'Producer', category: 'Production'}]->(r12),
       (u:User {id: $user_id}),
       (u)-[:COLLECTED {instance_id: 1, date_added: '2019-05-01T12:00:00-07:00'}]->(r10),
       (u)-[:COLLECTED {instance_id: 2, date_added: '2021-01-01T00:00:00-08:00'}]->(r10),
       (u)-[:WANTS {date_added: '2018-02-03T04:05:06-08:00'}]->(r10),
       (u)-[:WANTS {date_added: '2020-06-07T08:09:10-07:00'}]->(r14)
"""


async def test_strata_read_on_a_seeded_graph(
    postgres_pool: AsyncPostgreSQLPool,
    neo4j_driver: AsyncResilientNeo4jDriver,
) -> None:
    """The strata reads compile on the real engines and fold the seeded ladder correctly.

    Covers a masterless credit lane, an undated release and master, the master-year
    fallback, a MusicBrainz-only release joined by native id and one joined by release-group
    siblings, shared personnel keyed on category across differing raw roles, and the
    caller's collector state with the recorded time.
    """
    user_id = str(TEST_USER_ID)
    await _consume(neo4j_driver, _STRATA_GRAPH, user_id=user_id)
    async with postgres_pool.connection() as conn, conn.cursor() as cursor:
        await cursor.execute("TRUNCATE musicbrainz.releases, musicbrainz.release_groups")
        await cursor.execute(
            """
            INSERT INTO musicbrainz.release_groups (mbid, name, discogs_master_id) VALUES
                ('00000000-0000-0000-0000-00000000a500', 'Group A', 500),
                ('00000000-0000-0000-0000-00000000b501', 'Group B', NULL),
                ('00000000-0000-0000-0000-00000000c999', 'Elsewhere', 999)
            """
        )
        await cursor.execute(
            """
            INSERT INTO musicbrainz.releases (mbid, name, release_group_mbid, discogs_release_id, data) VALUES
                ('00000000-0000-0000-0000-0000000000a1', 'A, MusicBrainz reissue',
                 '00000000-0000-0000-0000-00000000a500', NULL,
                 '{"release_events": [{"date": "2003-04"}, {"date": "1999-01-01"}]}'),
                ('00000000-0000-0000-0000-0000000000b1', 'B, linked sibling',
                 '00000000-0000-0000-0000-00000000b501', 12, '{}'),
                ('00000000-0000-0000-0000-0000000000b2', 'B, MusicBrainz only',
                 '00000000-0000-0000-0000-00000000b501', NULL, '{"release_events": []}'),
                ('00000000-0000-0000-0000-0000000000c1', 'Not this artist',
                 '00000000-0000-0000-0000-00000000c999', NULL, '{}')
            """
        )

    root = await get_strata_root(neo4j_driver, "1")
    assert root == {"id": "1", "name": "Strata Root", "releases": 5}
    assert await get_strata_root(neo4j_driver, "404") is None
    catalog = await get_strata_catalog(neo4j_driver, postgres_pool, "1", root["name"])
    marks = await get_strata_marks(neo4j_driver, user_id, "1")

    assert {(r.provider, r.id, r.master_id, r.year) for r in catalog.releases} == {
        ("discogs", "10", "500", 1971),
        ("discogs", "11", "500", None),
        ("discogs", "12", "501", 1980),
        ("discogs", "13", None, 1985),
        ("discogs", "14", None, None),
        ("musicbrainz", "00000000-0000-0000-0000-0000000000a1", "500", 1999),
        ("musicbrainz", "00000000-0000-0000-0000-0000000000b2", "501", None),
    }
    assert dict(catalog.master_years) == {"500": 1971, "501": None}
    assert marks["10"].state == "wanted_owned"
    assert (marks["10"].owned_at, marks["10"].wanted_at) == ("2019-05-01T12:00:00-07:00", "2018-02-03T04:05:06-08:00")
    assert marks["14"].state == "wanted"

    payload = build_strata(catalog, Budget(3, 12, 16), marks=marks)
    release_ids, master_ids, artist_ids = label_targets(payload)
    release_titles, master_titles, artist_names = await get_strata_labels(neo4j_driver, release_ids, master_ids, artist_ids)
    edges = await get_shared_personnel(neo4j_driver, master_ids, limit=8)
    assert edges == [{"source": "500", "target": "501", "categories": ["Production"], "people": 1}]
    apply_labels(payload, release_titles=release_titles, master_titles=master_titles, artist_names=artist_names, edges=edges)

    credits, masters, releases = payload["planes"]
    assert [(node["id"], [a["name"] for a in node["artists"]]) for node in credits["nodes"]] == [
        ("c:1", ["Strata Root"]),
        ("c:1+2", ["Strata Root", "Strata Guest"]),
    ]
    assert {node["id"]: (node["parent"], node["title"], node["year"], node["undated"]) for node in masters["nodes"]} == {
        "m:500": ("c:1", "Master A", 1971, False),
        "m:501": ("c:1+2", "Master B", 1980, False),
    }
    lane = masters["aggregates"]
    assert [(agg["id"], agg["kind"], agg["releases"], agg["undated_releases"]) for agg in lane] == [("nm:c:1", "no_master", 2, 1)]
    assert masters["edges"] == [{"source": "m:500", "target": "m:501", "kind": "shared_personnel", "categories": ["Production"], "people": 1}]

    nodes = {node["id"]: node for node in releases["nodes"]}
    assert releases["aggregates"] == []
    assert (nodes["r:10"]["title"], nodes["r:10"]["state"]) == ("A, first press", "wanted_owned")
    assert nodes["r:10"]["recorded_at"] == {"owned": "2019-05-01T12:00:00-07:00", "wanted": "2018-02-03T04:05:06-08:00"}
    assert (nodes["r:11"]["year"], nodes["r:11"]["year_source"]) == (1971, "master")
    assert (nodes["r:14"]["parent"], nodes["r:14"]["undated"], nodes["r:14"]["state"]) == ("nm:c:1", True, "wanted")
    assert nodes["r:13"]["parent"] == "nm:c:1"
    reissue = nodes["mb:00000000-0000-0000-0000-0000000000a1"]
    assert (reissue["parent"], reissue["title"], reissue["year"], reissue["state"]) == ("m:500", "A, MusicBrainz reissue", 1999, None)
    sibling_joined = nodes["mb:00000000-0000-0000-0000-0000000000b2"]
    # No release event and a master with no year: the MusicBrainz release lands in the undated lane.
    assert (sibling_joined["parent"], sibling_joined["year"], sibling_joined["undated"], sibling_joined["state"]) == ("m:501", None, True, None)
    assert payload["totals"] == {
        "credits": 2,
        "masters": 2,
        "releases": 7,
        "masterless_releases": 2,
        "undated_releases": 2,
        "musicbrainz_releases": 2,
    }

    anonymous = build_strata(catalog, Budget(3, 12, 16))
    assert anonymous["signed_in"] is False
    assert "state" not in anonymous["planes"][2]["nodes"][0]


async def _root_db_hits(driver: AsyncResilientNeo4jDriver, artist_id: str) -> tuple[int, set[str]]:
    """Total db hits and operator names of the strata root read, from a real PROFILE."""
    async with driver.session(database="neo4j") as session:
        result = await session.run(f"PROFILE {strata_queries._ROOT}", {"artist_id": artist_id})
        summary = await result.consume()
    assert summary.profile is not None
    hits, operators, stack = 0, set(), [summary.profile]
    while stack:
        plan = stack.pop()
        hits += plan.get("dbHits", 0)
        operators.add(plan["operatorType"].split("@")[0])
        stack.extend(plan.get("children", []))
    return hits, operators


async def test_a_root_above_the_strata_ceiling_is_sized_without_reading_its_releases(
    neo4j_driver: AsyncResilientNeo4jDriver,
) -> None:
    """The size check the route refuses on costs the same for one release as for 134,052.

    A root one past MAX_ROOT_RELEASES is seeded for real. Its BY degree comes back exact,
    and the root read's db hits match a five-release root's, so the check reads the node's
    degree count and never walks the root's releases.
    """
    above = MAX_ROOT_RELEASES + 1
    await _consume(neo4j_driver, "CREATE (:Artist {id: 'big', name: 'Placeholder'}), (:Artist {id: 'small', name: 'Small'})")
    await _consume(
        neo4j_driver,
        """
        UNWIND range(1, $count) AS i
        CALL (i) {
            MATCH (a:Artist {id: 'big'})
            CREATE (:Release {id: 'big-' + toString(i)})-[:BY]->(a)
        } IN TRANSACTIONS OF 20000 ROWS
        """,
        count=above,
    )
    await _consume(
        neo4j_driver,
        "MATCH (a:Artist {id: 'small'}) UNWIND range(1, 5) AS i CREATE (:Release {id: 'small-' + toString(i)})-[:BY]->(a)",
    )

    try:
        started = perf_counter()
        root = await get_strata_root(neo4j_driver, "big")
        elapsed = perf_counter() - started

        assert root == {"id": "big", "name": "Placeholder", "releases": above}
        assert root["releases"] > MAX_ROOT_RELEASES
        assert elapsed < 1.0
        big_hits, operators = await _root_db_hits(neo4j_driver, "big")
        small_hits, _ = await _root_db_hits(neo4j_driver, "small")
        assert big_hits == small_hits
        assert not {"Expand(All)", "Expand(Into)", "OptionalExpand(All)"} & operators
    finally:
        # The neo4j_driver fixture clears the graph in one transaction, which a 134,052-node
        # root would push past the integration container's 256 MB heap.
        await _consume(
            neo4j_driver,
            "MATCH (:Artist {id: 'big'})<-[:BY]-(r:Release) CALL (r) { DETACH DELETE r } IN TRANSACTIONS OF 20000 ROWS",
        )


async def test_strata_read_bounds_surface_as_the_errors_the_route_maps_to_504(
    postgres_pool: AsyncPostgreSQLPool,
    neo4j_driver: AsyncResilientNeo4jDriver,
) -> None:
    """A graph read past its timeout and a PostgreSQL read past its statement bound both fail fast.

    The route turns a Neo4j ClientError naming TransactionTimedOut, and a QueryCanceled,
    into a 504. The statement bound is transaction-local, so it never leaks onto the
    pooled connection.
    """
    started = perf_counter()
    with pytest.raises(Neo4jClientError, match="TransactionTimedOut"):
        await run_query(neo4j_driver, "UNWIND range(1, $upper) AS value RETURN sum(toFloat(value)) AS total", timeout=0.05, upper=100_000_000)
    assert perf_counter() - started < 2.0

    async with postgres_pool.connection() as conn:
        default = await (await conn.execute("SHOW statement_timeout")).fetchone()
        started = perf_counter()
        with pytest.raises(QueryCanceled):
            async with conn.transaction(), conn.cursor() as cursor:
                await cursor.execute(strata_queries._STATEMENT_TIMEOUT, {"timeout": "50ms"})
                await cursor.execute("SELECT pg_sleep(5)")
        assert perf_counter() - started < 2.0
        assert await (await conn.execute("SHOW statement_timeout")).fetchone() == default


async def test_the_pinned_schema_indexes_the_strata_release_group_join(postgres_pool: AsyncPostgreSQLPool) -> None:
    """database-schema f21b3e1 indexes musicbrainz.releases (release_group_mbid), and the sibling read uses it."""
    async with postgres_pool.connection() as conn, conn.cursor() as cursor:
        await cursor.execute("SELECT indexdef FROM pg_indexes WHERE schemaname = 'musicbrainz' AND indexname = 'idx_mb_releases_release_group_mbid'")
        assert await cursor.fetchone() == (
            "CREATE INDEX idx_mb_releases_release_group_mbid ON musicbrainz.releases USING btree (release_group_mbid)",
        )
        async with conn.transaction():
            # The fixture's tables are nearly empty, so a sequential scan would win on cost;
            # disabling it shows the index is one the sibling read can use at all.
            await cursor.execute("SET LOCAL enable_seqscan = off")
            await cursor.execute(f"EXPLAIN {strata_queries._MB_SIBLINGS}", {"group_mbids": ["00000000-0000-0000-0000-00000000b501"]})
            plan = "\n".join(row[0] for row in await cursor.fetchall())
    assert "idx_mb_releases_release_group_mbid" in plan


# Two styles over two genres. Bop has one master shared by two 1959 releases, a second 1959 master,
# a masterless 1960 release, and an undated release. Cool's only 1959 release derives from a master
# dated 1970, so a master-year bucket would put it in the wrong cell. A genre-only release and an
# unrelated style must not leak into either style's cells.
_STYLE_YEAR_GRAPH = """
CREATE (jazz:Genre {name: 'Jazz'}), (funk:Genre {name: 'Funk / Soul'}),
       (bop:Style {name: 'Bop'}), (cool:Style {name: 'Cool'}),
       (m1:Master {id: 'm1', year: 1959}), (m2:Master {id: 'm2', year: 1959}), (m3:Master {id: 'm3', year: 1970}),
       (r1:Release {id: 'r1', year: 1959}), (r2:Release {id: 'r2', year: 1959}), (r3:Release {id: 'r3', year: 1959}),
       (r4:Release {id: 'r4', year: 1960}), (r5:Release {id: 'r5', year: 0}),
       (r6:Release {id: 'r6', year: 1959}), (r7:Release {id: 'r7', year: 1960}),
       (r8:Release {id: 'r8', year: 1959}),
       (r1)-[:IS]->(bop), (r2)-[:IS]->(bop), (r3)-[:IS]->(bop), (r4)-[:IS]->(bop), (r5)-[:IS]->(bop),
       (r1)-[:IS]->(jazz), (r2)-[:IS]->(jazz), (r3)-[:IS]->(jazz), (r4)-[:IS]->(jazz), (r5)-[:IS]->(jazz),
       (r3)-[:IS]->(funk),
       (r1)-[:DERIVED_FROM]->(m1), (r2)-[:DERIVED_FROM]->(m1), (r3)-[:DERIVED_FROM]->(m2),
       (r6)-[:IS]->(cool), (r6)-[:IS]->(jazz), (r6)-[:DERIVED_FROM]->(m3),
       (r7)-[:IS]->(cool),
       (r8)-[:IS]->(jazz)
"""


async def test_style_year_counts_on_a_seeded_graph(neo4j_driver: AsyncResilientNeo4jDriver) -> None:
    """Counts bucket on release year and shares are raw fractions, on the real engine.

    The Cypher runs under the long server-side timeout, so this also proves the engine accepts
    the ``Query(..., timeout=...)`` wrapper for the three style-year reads.
    """
    await _consume(neo4j_driver, _STYLE_YEAR_GRAPH)

    result = await query_style_year_counts(neo4j_driver)

    assert result["style_years"] == [
        {"style": "Bop", "year": 1959, "release_count": 3, "master_count": 2},
        {"style": "Bop", "year": 1960, "release_count": 1, "master_count": 0},
        {"style": "Cool", "year": 1959, "release_count": 1, "master_count": 1},
        {"style": "Cool", "year": 1960, "release_count": 1, "master_count": 0},
    ]
    parents = {(row["style"], row["genre"]): row for row in result["style_parents"]}
    assert set(parents) == {("Bop", "Jazz"), ("Bop", "Funk / Soul"), ("Cool", "Jazz")}
    # The undated release counts toward membership: Bop has five releases, not four.
    assert parents["Bop", "Jazz"] == {"style": "Bop", "genre": "Jazz", "release_count": 5, "style_release_count": 5, "share": 1.0}
    # Below the lifecycle rule's 0.20 floor or exactly on it, nothing is dropped here.
    assert parents["Bop", "Funk / Soul"] == {
        "style": "Bop",
        "genre": "Funk / Soul",
        "release_count": 1,
        "style_release_count": 5,
        "share": 0.2,
    }
    # r7 carries the style and no genre, so Jazz covers one of Cool's two releases.
    assert parents["Cool", "Jazz"] == {"style": "Cool", "genre": "Jazz", "release_count": 1, "style_release_count": 2, "share": 0.5}
