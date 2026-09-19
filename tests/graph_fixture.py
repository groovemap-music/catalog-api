"""The one graph fixture both engines answer the parity harness from.

A parity suite is only worth the containers it starts if both engines are looking at the
same graph, so the fixture lives here rather than in either test module: the harness in
`tests/test_real_databases.py` and the SQL/PGQ predicate suite in
`tests/test_graph_parity.py` seed from these constants and nothing else.

Two components, and they are deliberately disconnected from each other.

**The ordering component** (artists 1-7) is the pilot's original fixture. Anchor artist
"1" reaches three collaborators in one hop over three, two, and one shared release, and
three more in two hops bridged by three, two, and one intermediary. That is six distinct
`(distance, collaboration_count)` pairs, so the order both engines produce reading from
the anchor is total — neither implementation adds a tiebreaker, and a tie would make row
order legitimately unspecified on both sides.

**The predicate component** (artists 8-11) exists to make the four no-revisit predicates
in the depth-2 SQL observable. Release "201" credits three artists, which is the whole
point of it: SQL/PGQ walk semantics let the same edge bind to two edge patterns, so
without `far.release_id <> near.release_id` the walk can turn around inside one release
and report a third artist credited on it as a two-hop collaborator. A release crediting
only two artists cannot show that — the only artists to turn around onto are the anchor
and the bridge, which two of the other predicates already exclude. Artist "10" is
credited on exactly one release for the same reason: it leaves the turn-around the only
way to reach it as a bridge.

Anchor "8" is tie-free too: artist 9 at distance 1 over two shared releases, artist 10 at
distance 1 over one, artist 11 at distance 2 over one bridge.

**Release years** (`RELEASE_YEARS`) exist for the one-hop `collaborator_queries` family
(`gm-catalog-api-91a.3`), which Cypher-filters on `r.year > 0` and groups its result by
year. Every release above gets a distinct year so `RELEASE_YEARS` needs no component of its
own: under anchor "1" the one-hop family orders by `release_count` exactly as the two-hop
family orders by `collaboration_count` at distance 1, so it inherits the same tie-free
vantage point; under anchor "8" the three-credit release is what makes the one-hop family's
own walk-semantics guard (`peer.artist_id <> anchor.artist_id`) observable too — without it,
a walk from "8" over release 201 can turn around and report "8" as its own collaborator.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import pytest
from common import AsyncPostgreSQLPool, AsyncResilientNeo4jDriver, parse_postgres_host_port
from groovemap_schema.postgres import (
    PROPERTY_GRAPH_MINIMUM_SERVER_VERSION,
    create_postgres_schema,
    property_graph_enabled,
)


# The vantage point the ordered parity calls are made from.
ANCHOR_ARTIST_ID = "1"

# The vantage point the no-revisit predicate probes are made from.
PROBE_ANCHOR_ARTIST_ID = "8"

# The release whose third credit is what a revisiting walk reaches.
THREE_CREDIT_RELEASE_ID = "201"

ARTISTS: dict[str, str] = {
    "1": "Anchor",
    "2": "Three Shared",
    "3": "Two Shared",
    "4": "One Shared",
    "5": "Three Bridges",
    "6": "Two Bridges",
    "7": "One Bridge",
    "8": "Probe Anchor",
    "9": "Probe Bridge",
    "10": "Probe Third Credit",
    "11": "Probe Two Hop",
}

# release id -> the artist ids credited on it.
RELEASES: dict[str, tuple[str, ...]] = {
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
    # The predicate component. 201 is the three-credit release; 202 gives the anchor and
    # the bridge a second shared release so a walk has somewhere else to turn around;
    # 203 hangs one genuine two-hop collaborator off the bridge.
    THREE_CREDIT_RELEASE_ID: ("8", "9", "10"),
    "202": ("8", "9"),
    "203": ("9", "11"),
}

# release id -> the year it was released. A separate mapping rather than a field on
# `RELEASES` so the two-hop family's `len(RELEASES[...])` / membership checks
# (`tests/test_graph_parity.py`) keep reading a plain tuple of credited artist ids. Every
# release gets its own year; nothing in either family's ordering depends on which.
RELEASE_YEARS: dict[str, int] = {
    "101": 2001,
    "102": 2002,
    "103": 2003,
    "104": 2004,
    "105": 2005,
    "106": 2006,
    "107": 2007,
    "108": 2008,
    "109": 2009,
    "110": 2010,
    "111": 2011,
    "112": 2012,
    THREE_CREDIT_RELEASE_ID: 2013,
    "202": 2014,
    "203": 2015,
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
SET r.year = release.year
WITH r, release
UNWIND release.artists AS artist_id
MATCH (a:Artist {id: artist_id})
MERGE (r)-[:BY]->(a)
"""

_SERVER_VERSION = "SELECT current_setting('server_version_num')::int"


@dataclass(frozen=True)
class ParityBackends:
    """Both engines, holding the same fixture, ready to be asked the same question."""

    neo4j: AsyncResilientNeo4jDriver
    postgres: AsyncPostgreSQLPool


def required_env(name: str) -> str:
    """Return *name* from the environment, failing the test when the recipe did not set it."""
    value = os.environ.get(name)
    if not value:
        pytest.fail(f"{name} must be supplied by `just test-integration-pg19`")
    return value


async def consume(driver: AsyncResilientNeo4jDriver, cypher: str, **params: Any) -> None:
    """Run *cypher* for its effect."""
    async with driver.session(database="neo4j") as session:
        result = await session.run(cypher, params)
        await result.consume()


async def seed_neo4j(driver: AsyncResilientNeo4jDriver) -> None:
    """Project the fixture into Neo4j as the graph enrichers would."""
    await consume(driver, "MATCH (n) DETACH DELETE n")
    await consume(
        driver,
        _SEED_NEO4J,
        artists=[{"id": artist_id, "name": name} for artist_id, name in ARTISTS.items()],
        releases=[{"id": release_id, "artists": list(credits), "year": RELEASE_YEARS[release_id]} for release_id, credits in RELEASES.items()],
    )


async def seed_postgres(pool: AsyncPostgreSQLPool) -> None:
    """Write the fixture into `artists` and `releases` as Discogs documents.

    Nothing else is written: `graph.catalog` and the views underneath it are declarations
    over these two tables, so the documents are the whole projection.
    """
    async with pool.connection() as conn, conn.cursor() as cursor:
        await cursor.execute(_TRUNCATE_ENTITIES)
        for artist_id, name in ARTISTS.items():
            await cursor.execute(_SEED_ARTIST, (artist_id, "parity-fixture", json.dumps({"name": name})))
        for release_id, credits in RELEASES.items():
            document = {
                "title": f"Release {release_id}",
                "year": RELEASE_YEARS[release_id],
                "artists": [{"id": int(each)} for each in credits],
            }
            await cursor.execute(_SEED_RELEASE, (release_id, "parity-fixture", json.dumps(document)))


async def open_postgres_pool() -> AsyncPostgreSQLPool:
    """Open a pool on the integration container with the producer's own schema applied.

    The two gates are asserted rather than skipped past. A caller reaches this only once
    the property graph has been *requested*, and a container that then cannot serve it is
    a failure, not a no-op: the whole point of the PostgreSQL 19 tier is that the graph is
    there.
    """
    assert property_graph_enabled(), "SCHEMA_PROPERTY_GRAPH must be enabled; use `just test-integration-pg19`"

    host, port = parse_postgres_host_port(required_env("POSTGRES_HOST"))
    pool = AsyncPostgreSQLPool(
        connection_params={
            "host": host,
            "port": port,
            "dbname": required_env("POSTGRES_DATABASE"),
            "user": required_env("POSTGRES_USERNAME"),
            "password": required_env("POSTGRES_PASSWORD"),
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
    return pool


@asynccontextmanager
async def seeded_backends() -> AsyncIterator[ParityBackends]:
    """Yield both engines holding the fixture, and close them afterwards."""
    driver = AsyncResilientNeo4jDriver(
        uri=required_env("NEO4J_HOST"),
        auth=(required_env("NEO4J_USERNAME"), required_env("NEO4J_PASSWORD")),
        max_retries=1,
    )
    pool = await open_postgres_pool()
    try:
        await seed_neo4j(driver)
        await seed_postgres(pool)
        yield ParityBackends(neo4j=driver, postgres=pool)
    finally:
        await driver.close()
        await pool.close()
