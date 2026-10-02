"""Engine-backed regressions for catalog API query and write boundaries."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from time import perf_counter
from uuid import UUID

import httpx
import pytest
import pytest_asyncio
import respx
from common import AsyncPostgreSQLPool, AsyncResilientNeo4jDriver, parse_postgres_host_port
from groovemap_schema.postgres import create_postgres_schema
from neo4j.exceptions import Neo4jError
from psycopg.rows import dict_row

from api.queries.credits_queries import get_person_connections
from api.queries.helpers import run_count, run_query, run_single
from api.syncer import DISCOGS_API_BASE, sync_collection


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
    ``57b43be2f087771914f7c8408ed0f0a35a672b91`` — the same producer revision
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
