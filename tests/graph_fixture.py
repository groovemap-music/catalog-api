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

**The full-text component** (ids 301+) belongs to the autocomplete family and shares no
row with the other two. It traverses nothing: the five relations it is read from are
`graph.artist`, `graph.label`, `graph.genre`, `graph.style`, and `graph.person`, and only
the first two are reachable from a release at all. Its releases therefore credit no artist
and name no label — they exist solely to carry the `genres`, `styles`, and `extraartists`
blocks the three name-keyed vertex tables are projected from, so nothing it adds can reach
an anchor of the other two components.

Its names are chosen for three jobs. Each registered query matches its relation under
*both* engines' rules — every term a prefix of a word in the name — so the row sets agree
and only the ranking differs. Each matches more than one name where the family's limit and
ordering are worth exercising. And three of them carry characters a query string has no
business carrying. Two (`AC/DC`, `Charles "Chuck" Berry`) are Lucene syntax, which is what
the escaping this family retires existed for, and are searched for by the hazard tests
rather than by a parity call because the Lucene side does not return the same row.
`Sinéad O'Connor` is the third and is different: an apostrophe survives Lucene's tokenizer
intact, so both engines answer and it is a parity call — it is there for the character that
would have broken a hand-built SQL string rather than a query parser.
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
from groovemap_schema.neo4j import create_neo4j_schema
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

# ── The full-text component ─────────────────────────────────────────────────
# Read by the autocomplete family. Ids start at 301 so no row here can collide with the
# two traversal components above.

# Query "radio" reaches both; query "acdc" reaches neither, and query "AC/DC" is what the
# Lucene escaping mishandled.
AUTOCOMPLETE_ARTISTS: dict[str, str] = {
    "301": "Radiohead",
    "302": "Radio Birdman",
    "303": "AC/DC",
}

# Query "warp" reaches the first two; "Warped Vinyl" is there so the prefix is a prefix of
# a *word* rather than of the whole name, which is the rule both engines apply.
AUTOCOMPLETE_LABELS: dict[str, str] = {
    "401": "Warp Records",
    "402": "Warped Vinyl",
    "403": "Mute",
}

# release id -> the tag blocks and credits it carries. `graph.genre`, `graph.style`, and
# `graph.person` are projections of exactly these three document keys, so this is the whole
# input for three of the family's five relations. No `artists` and no `labels` key: an edge
# out of these releases would join the full-text component to nothing and is not wanted.
AUTOCOMPLETE_RELEASES: dict[str, dict[str, Any]] = {
    "901": {
        "genres": ["Rock"],
        "styles": ["Ambient"],
        "extraartists": [
            {"name": 'Charles "Chuck" Berry', "role": "Guitar"},
            {"name": "Bob Ludwig", "role": "Mastered By"},
        ],
    },
    "902": {
        "genres": ["Electronic"],
        "styles": ["Ambient House", "Techno"],
        "extraartists": [
            {"name": "Sinéad O'Connor", "role": "Vocals"},
            {"name": "Bob Power", "role": "Mixed By"},
        ],
    },
}

# The names the two vertex tables above are projected to, restated so a test can name one
# without re-deriving it from the documents.
AUTOCOMPLETE_GENRES: tuple[str, ...] = ("Electronic", "Rock")
AUTOCOMPLETE_STYLES: tuple[str, ...] = ("Ambient", "Ambient House", "Techno")
AUTOCOMPLETE_PEOPLE: tuple[str, ...] = (
    "Bob Ludwig",
    "Bob Power",
    'Charles "Chuck" Berry',
    "Sinéad O'Connor",
)


_TRUNCATE_ENTITIES = "TRUNCATE artists, labels, releases CASCADE"

# What turns the seeded documents into graph rows. From the phase 2 schema revision the
# `graph` relations a loader owns — every edge, and the `genre`, `style`, and `person`
# vertices — are tables rather than views over `public.releases`, so seeding a document no
# longer projects an edge on its own. `graph.bootstrap_fill()` is the producer's own
# one-off fill: it truncates each of those relations and refills it from the same phase 0
# body the view used to publish, in one transaction, and returns a row count per relation.
# It is exactly what the contract says it is for — populating an environment before a
# loader has run — which is what a fixture is.
_BOOTSTRAP_FILL = "SELECT relation, row_count FROM graph.bootstrap_fill()"

_SEED_ARTIST = "INSERT INTO artists (data_id, hash, data) VALUES (%s, %s, %s::jsonb)"
_SEED_LABEL = "INSERT INTO labels (data_id, hash, data) VALUES (%s, %s, %s::jsonb)"
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

# The full-text component's Neo4j half. `graphinator` writes these five node kinds from the
# same document keys `graph.bootstrap_fill` projects the PostgreSQL tables from, so the two
# sides are seeded from one set of constants and diverge only where the engines do.
_SEED_NEO4J_FULLTEXT = """
UNWIND $artists AS artist
MERGE (a:Artist {id: artist.id}) SET a.name = artist.name
WITH count(*) AS _artists
UNWIND $labels AS label
MERGE (l:Label {id: label.id}) SET l.name = label.name
WITH count(*) AS _labels
UNWIND $genres AS genre
MERGE (:Genre {name: genre})
WITH count(*) AS _genres
UNWIND $styles AS style
MERGE (:Style {name: style})
WITH count(*) AS _styles
UNWIND $people AS person
MERGE (:Person {name: person})
"""

# Lucene indexes are populated in the background, so a search issued the moment the seed
# commits can read an index that is still building and answer with fewer rows than the
# graph holds. Every full-text call in the suite is downstream of this.
_AWAIT_NEO4J_INDEXES = "CALL db.awaitIndexes()"

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
    """Project the fixture into Neo4j as the graph enrichers would.

    The producer's own constraints and indexes are applied first, for the same reason
    `seed_postgres` applies the producer's DDL rather than a hand-rolled subset: the five
    `*_name_fulltext` indexes the autocomplete family reads are declared in
    `groovemap_schema.neo4j`, and a hand-written `CREATE FULLTEXT INDEX` here would be a
    second spelling of them that can drift. `db.awaitIndexes()` then makes the seed
    readable rather than merely committed.
    """
    failures = await create_neo4j_schema(driver)
    assert failures == 0, f"{failures} Neo4j schema statements failed against the integration container"
    await consume(driver, "MATCH (n) DETACH DELETE n")
    await consume(
        driver,
        _SEED_NEO4J,
        artists=[{"id": artist_id, "name": name} for artist_id, name in ARTISTS.items()],
        releases=[{"id": release_id, "artists": list(credits)} for release_id, credits in RELEASES.items()],
    )
    await consume(
        driver,
        _SEED_NEO4J_FULLTEXT,
        artists=[{"id": artist_id, "name": name} for artist_id, name in AUTOCOMPLETE_ARTISTS.items()],
        labels=[{"id": label_id, "name": name} for label_id, name in AUTOCOMPLETE_LABELS.items()],
        genres=list(AUTOCOMPLETE_GENRES),
        styles=list(AUTOCOMPLETE_STYLES),
        people=list(AUTOCOMPLETE_PEOPLE),
    )
    await consume(driver, _AWAIT_NEO4J_INDEXES)


async def seed_postgres(pool: AsyncPostgreSQLPool) -> None:
    """Write the fixture as Discogs documents, then project it into the graph relations.

    The documents are still the whole input — nothing is written to a `graph` relation by
    hand. They are not the whole projection any more, though: the loader-owned relations
    are tables from the phase 2 schema revision onward, so `graph.bootstrap_fill()` runs
    afterwards to derive them from the documents just written. It truncates first, so a
    re-seed converges rather than accumulating, and it covers the traversal component's
    `graph.by_artist` and the full-text component's `graph.genre`, `graph.style`, and
    `graph.person` in the same pass.
    """
    async with pool.connection() as conn, conn.cursor() as cursor:
        await cursor.execute(_TRUNCATE_ENTITIES)
        for artist_id, name in ARTISTS.items():
            await cursor.execute(_SEED_ARTIST, (artist_id, "parity-fixture", json.dumps({"name": name})))
        for release_id, credits in RELEASES.items():
            document = {"title": f"Release {release_id}", "artists": [{"id": int(each)} for each in credits]}
            await cursor.execute(_SEED_RELEASE, (release_id, "parity-fixture", json.dumps(document)))
        for artist_id, name in AUTOCOMPLETE_ARTISTS.items():
            await cursor.execute(_SEED_ARTIST, (artist_id, "parity-fixture", json.dumps({"name": name})))
        for label_id, name in AUTOCOMPLETE_LABELS.items():
            await cursor.execute(_SEED_LABEL, (label_id, "parity-fixture", json.dumps({"name": name})))
        for release_id, tags in AUTOCOMPLETE_RELEASES.items():
            document = {"title": f"Release {release_id}", **tags}
            await cursor.execute(_SEED_RELEASE, (release_id, "parity-fixture", json.dumps(document)))
        await cursor.execute(_BOOTSTRAP_FILL)
        await cursor.fetchall()


async def open_postgres_pool() -> AsyncPostgreSQLPool:
    """Open a pool on the integration container with the producer's own schema applied.

    Both property-graph gates are asserted rather than skipped past *when the switch asks
    for the graph*: a container that has been told to declare `graph.catalog` and then
    cannot is a failure, not a no-op, because the whole point of the PostgreSQL 19 tier is
    that the graph is there.

    With the switch off there is nothing to gate. A family registered
    `requires_property_graph=False` is ordinary SQL over the `graph` relations — which are
    unconditional, on every tier — so it runs here on PostgreSQL 18 too, and asserting a
    graph it never reads would be the fixture failing a run the family is fine on.
    """
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

    if property_graph_enabled():
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
