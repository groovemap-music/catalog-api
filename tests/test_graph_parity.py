"""Row-level parity between the Neo4j and PostgreSQL collaborators backends.

Opt-in, and the only test in this repository that needs PostgreSQL 19: SQL/PGQ and the
`graph.catalog` property graph exist nowhere else. Run it with::

    just test-integration-pg19

which starts the digest-pinned `postgres:19beta3-alpine` image beside the usual Neo4j
container, applies the `groovemap-database-schema` initializer with
`SCHEMA_PROPERTY_GRAPH` enabled so the property graph is declared, and points
`scripts/test-integration.sh` at this file.

The claim under test is not "the SQL runs". It is that the two engines return the same
rows, in the same order, with the same Python types, from the same fixture — which is
what lets the graph-backend seam switch `GRAPH_BACKEND` without the API response moving.
So the fixture is seeded once, projected into both engines, and every assertion compares
the two results to each other rather than to a hand-written expectation. A hand-written
expectation would only prove both halves agree with whatever the author believed.

The fixture is deliberately free of ties on `(distance, collaboration_count)`. Both
implementations order by exactly those two keys and neither adds a tiebreaker, so a tie
would make row order legitimately unspecified on both sides and the comparison would
test the planners rather than the queries.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from common import AsyncPostgreSQLPool, AsyncResilientNeo4jDriver, parse_postgres_host_port
from groovemap_schema.postgres import (
    PROPERTY_GRAPH_MINIMUM_SERVER_VERSION,
    PROPERTY_GRAPH_NAME,
    create_postgres_schema,
    property_graph_enabled,
)

from api.queries import network_pg_queries as postgres_backend
from api.queries import network_queries as neo4j_backend


pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


# ── The shared fixture ───────────────────────────────────────────────────────
# Anchor artist "1" reaches three collaborators in one hop over three, two, and one shared
# release, and three more in two hops bridged by three, two, and one intermediary. Six
# distinct `(distance, collaboration_count)` pairs, so the ordering both engines produce is
# total. Artists 2-4 are the one-hop ring; 5-7 are two-hop only and must never be reported
# at distance 1, which is what the anti-join is for.
ANCHOR_ARTIST_ID = "1"

ARTISTS: dict[str, str] = {
    "1": "Anchor",
    "2": "Three Shared",
    "3": "Two Shared",
    "4": "One Shared",
    "5": "Three Bridges",
    "6": "Two Bridges",
    "7": "One Bridge",
}

# release id -> the artist ids credited on it.
RELEASES: dict[str, tuple[str, str]] = {
    "101": ("1", "2"),
    "102": ("1", "2"),
    "103": ("1", "2"),
    "104": ("1", "3"),
    "105": ("1", "3"),
    "106": ("1", "4"),
    "107": ("2", "5"),
    "108": ("3", "5"),
    "109": ("4", "5"),
    "110": ("2", "6"),
    "111": ("3", "6"),
    "112": ("4", "7"),
}

_TRUNCATE_ENTITIES = "TRUNCATE artists, releases CASCADE"

_SEED_ARTIST = "INSERT INTO artists (data_id, hash, data) VALUES (%s, %s, %s::jsonb)"
_SEED_RELEASE = "INSERT INTO releases (data_id, hash, data) VALUES (%s, %s, %s::jsonb)"

_SEED_NEO4J = """
UNWIND $artists AS artist
MERGE (a:Artist {id: artist.id})
SET a.name = artist.name
WITH count(*) AS _seeded
UNWIND $releases AS release
MERGE (r:Release {id: release.id})
WITH r, release
UNWIND release.artists AS artist_id
MATCH (a:Artist {id: artist_id})
MERGE (r)-[:BY]->(a)
"""

_SERVER_VERSION = "SELECT current_setting('server_version_num')::int"


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.fail(f"{name} must be supplied by `just test-integration-pg19`")
    return value


async def _consume(driver: AsyncResilientNeo4jDriver, cypher: str, **params: Any) -> None:
    async with driver.session(database="neo4j") as session:
        result = await session.run(cypher, params)
        await result.consume()


@pytest_asyncio.fixture
async def neo4j_driver() -> AsyncIterator[AsyncResilientNeo4jDriver]:
    """A Neo4j driver holding the shared fixture as the graph enrichers project it."""
    driver = AsyncResilientNeo4jDriver(
        uri=_required_env("NEO4J_HOST"),
        auth=(_required_env("NEO4J_USERNAME"), _required_env("NEO4J_PASSWORD")),
        max_retries=1,
    )
    await _consume(driver, "MATCH (n) DETACH DELETE n")
    await _consume(
        driver,
        _SEED_NEO4J,
        artists=[{"id": artist_id, "name": name} for artist_id, name in ARTISTS.items()],
        releases=[{"id": release_id, "artists": list(credits)} for release_id, credits in RELEASES.items()],
    )
    try:
        yield driver
    finally:
        await driver.close()


@pytest_asyncio.fixture
async def postgres_pool() -> AsyncIterator[AsyncPostgreSQLPool]:
    """A PostgreSQL 19 pool holding the same fixture as Discogs documents.

    The schema is the producer's own — `create_postgres_schema` — so `graph.catalog` and
    the 52 views underneath it are declared exactly as a deployment would get them, and the
    only thing this fixture writes is rows in `artists` and `releases`. The two gates are
    asserted rather than skipped past: this suite exists to run on PostgreSQL 19 with the
    property graph on, so a container that cannot serve it is a failure, not a no-op.
    """
    assert property_graph_enabled(), "SCHEMA_PROPERTY_GRAPH must be enabled; use `just test-integration-pg19`"

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

    async with pool.connection() as conn, conn.cursor() as cursor:
        await cursor.execute(_SERVER_VERSION)
        row = await cursor.fetchone()
        server_version_num = int(row[0]) if row else 0
    assert server_version_num >= PROPERTY_GRAPH_MINIMUM_SERVER_VERSION, (
        f"this suite needs server_version_num >= {PROPERTY_GRAPH_MINIMUM_SERVER_VERSION}; "
        f"the container reports {server_version_num}. Check POSTGRES_INTEGRATION_IMAGE."
    )

    failures = await create_postgres_schema(pool)
    assert failures == 0, f"{failures} schema statements failed against the integration container"

    async with pool.connection() as conn, conn.cursor() as cursor:
        await cursor.execute(_TRUNCATE_ENTITIES)
        for artist_id, name in ARTISTS.items():
            await cursor.execute(_SEED_ARTIST, (artist_id, "parity-fixture", json.dumps({"name": name})))
        for release_id, credits in RELEASES.items():
            document = {"title": f"Release {release_id}", "artists": [{"id": int(each)} for each in credits]}
            await cursor.execute(_SEED_RELEASE, (release_id, "parity-fixture", json.dumps(document)))
    try:
        yield pool
    finally:
        await pool.close()


async def test_the_property_graph_is_declared_on_the_integration_container(
    postgres_pool: AsyncPostgreSQLPool,
) -> None:
    """Everything below reads `graph.catalog`; this is the one test that says so out loud."""
    async with postgres_pool.connection() as conn, conn.cursor() as cursor:
        await cursor.execute(
            "SELECT relkind FROM pg_class AS relation "
            "JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace "
            "WHERE namespace.nspname = %s AND relation.relname = %s",
            tuple(PROPERTY_GRAPH_NAME.split(".")),
        )
        row = await cursor.fetchone()
    assert row is not None, f"{PROPERTY_GRAPH_NAME} was not declared; is SCHEMA_PROPERTY_GRAPH enabled?"
    # `g` is the relkind PostgreSQL 19 gives a property graph.
    assert row[0] == "g"


async def test_artist_identity_is_identical_on_both_backends(
    neo4j_driver: AsyncResilientNeo4jDriver,
    postgres_pool: AsyncPostgreSQLPool,
) -> None:
    cypher = await neo4j_backend.get_artist_identity(neo4j_driver, ANCHOR_ARTIST_ID)
    sqlpgq = await postgres_backend.get_artist_identity(postgres_pool, ANCHOR_ARTIST_ID)

    assert cypher == sqlpgq
    assert sqlpgq == {"artist_id": "1", "artist_name": "Anchor"}


async def test_artist_identity_is_none_on_both_backends_for_an_unknown_artist(
    neo4j_driver: AsyncResilientNeo4jDriver,
    postgres_pool: AsyncPostgreSQLPool,
) -> None:
    assert await neo4j_backend.get_artist_identity(neo4j_driver, "does-not-exist") is None
    assert await postgres_backend.get_artist_identity(postgres_pool, "does-not-exist") is None


@pytest.mark.parametrize("depth", [1, 2, 3])
async def test_multi_hop_collaborators_agree_row_for_row(
    neo4j_driver: AsyncResilientNeo4jDriver,
    postgres_pool: AsyncPostgreSQLPool,
    depth: int,
) -> None:
    """Same rows, same order, same types — at every depth the endpoint accepts.

    Depth 3 is included because the endpoint accepts it and the Cypher treats it as depth 2:
    its second UNION branch is the only one that adds hops. The PostgreSQL side has to make
    the same choice, and a parity run is the only place that shows it does.
    """
    cypher = await neo4j_backend.get_multi_hop_collaborators(neo4j_driver, ANCHOR_ARTIST_ID, depth=depth, limit=50)
    sqlpgq = await postgres_backend.get_multi_hop_collaborators(postgres_pool, ANCHOR_ARTIST_ID, depth=depth, limit=50)

    assert sqlpgq == cypher
    assert [type(value) for value in sqlpgq[0].values()] == [type(value) for value in cypher[0].values()]


async def test_the_depth_two_result_is_the_expected_shape(
    neo4j_driver: AsyncResilientNeo4jDriver,
    postgres_pool: AsyncPostgreSQLPool,
) -> None:
    """Parity is worthless if both sides agree on nothing, so pin the fixture's answer once.

    Artists 5-7 are reachable only in two hops and are bridged by three, two, and one
    intermediary; artists 2-4 share three, two, and one release. Every collaboration_count
    is distinct within its distance, so this list is the only order either engine may
    return.
    """
    expected = [
        {"artist_id": "2", "artist_name": "Three Shared", "distance": 1, "collaboration_count": 3},
        {"artist_id": "3", "artist_name": "Two Shared", "distance": 1, "collaboration_count": 2},
        {"artist_id": "4", "artist_name": "One Shared", "distance": 1, "collaboration_count": 1},
        {"artist_id": "5", "artist_name": "Three Bridges", "distance": 2, "collaboration_count": 3},
        {"artist_id": "6", "artist_name": "Two Bridges", "distance": 2, "collaboration_count": 2},
        {"artist_id": "7", "artist_name": "One Bridge", "distance": 2, "collaboration_count": 1},
    ]

    assert await neo4j_backend.get_multi_hop_collaborators(neo4j_driver, ANCHOR_ARTIST_ID, depth=2, limit=50) == expected
    assert await postgres_backend.get_multi_hop_collaborators(postgres_pool, ANCHOR_ARTIST_ID, depth=2, limit=50) == expected


async def test_depth_one_excludes_every_two_hop_collaborator_on_both_backends(
    neo4j_driver: AsyncResilientNeo4jDriver,
    postgres_pool: AsyncPostgreSQLPool,
) -> None:
    cypher = await neo4j_backend.get_multi_hop_collaborators(neo4j_driver, ANCHOR_ARTIST_ID, depth=1, limit=50)
    sqlpgq = await postgres_backend.get_multi_hop_collaborators(postgres_pool, ANCHOR_ARTIST_ID, depth=1, limit=50)

    assert sqlpgq == cypher
    assert [row["artist_id"] for row in sqlpgq] == ["2", "3", "4"]
    assert all(row["distance"] == 1 for row in sqlpgq)


@pytest.mark.parametrize("limit", [1, 4, 50])
async def test_the_limit_truncates_the_same_prefix_on_both_backends(
    neo4j_driver: AsyncResilientNeo4jDriver,
    postgres_pool: AsyncPostgreSQLPool,
    limit: int,
) -> None:
    cypher = await neo4j_backend.get_multi_hop_collaborators(neo4j_driver, ANCHOR_ARTIST_ID, depth=2, limit=limit)
    sqlpgq = await postgres_backend.get_multi_hop_collaborators(postgres_pool, ANCHOR_ARTIST_ID, depth=2, limit=limit)

    assert sqlpgq == cypher
    assert len(sqlpgq) == min(limit, 6)


@pytest.mark.parametrize("depth", [1, 2, 3])
async def test_the_collaborator_count_agrees_on_both_backends(
    neo4j_driver: AsyncResilientNeo4jDriver,
    postgres_pool: AsyncPostgreSQLPool,
    depth: int,
) -> None:
    cypher = await neo4j_backend.count_multi_hop_collaborators(neo4j_driver, ANCHOR_ARTIST_ID, depth=depth)
    sqlpgq = await postgres_backend.count_multi_hop_collaborators(postgres_pool, ANCHOR_ARTIST_ID, depth=depth)

    assert sqlpgq == cypher
    assert sqlpgq == (3 if depth == 1 else 6)
    # The count is a count of collaborators, not of paths, and the limit never applies to it.
    assert sqlpgq >= len(await postgres_backend.get_multi_hop_collaborators(postgres_pool, ANCHOR_ARTIST_ID, depth=depth, limit=1))


async def test_every_artist_in_the_fixture_agrees_on_both_backends(
    neo4j_driver: AsyncResilientNeo4jDriver,
    postgres_pool: AsyncPostgreSQLPool,
) -> None:
    """Parity from every vantage point in the graph, not only the anchor.

    Ordering is not asserted here: read from a leaf, the fixture does produce ties on
    `(distance, collaboration_count)`, and neither implementation promises an order for
    those. The rows themselves must still match exactly, which is the part that is defined.
    """
    for artist_id in ARTISTS:
        cypher = await neo4j_backend.get_multi_hop_collaborators(neo4j_driver, artist_id, depth=2, limit=50)
        sqlpgq = await postgres_backend.get_multi_hop_collaborators(postgres_pool, artist_id, depth=2, limit=50)

        key = ("distance", "collaboration_count")
        assert [tuple(row[column] for column in key) for row in sqlpgq] == [tuple(row[column] for column in key) for row in cypher], (
            f"sort keys diverged for artist {artist_id}"
        )
        assert sorted(sqlpgq, key=lambda row: str(row["artist_id"])) == sorted(cypher, key=lambda row: str(row["artist_id"])), (
            f"rows diverged for artist {artist_id}"
        )
        assert await postgres_backend.count_multi_hop_collaborators(
            postgres_pool, artist_id, depth=2
        ) == await neo4j_backend.count_multi_hop_collaborators(neo4j_driver, artist_id, depth=2)
