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

**The credits component** (ids 701-710 / 801-804 / 502-503) belongs to the credits family
(`gm-catalog-api-dl8.1`) and is disconnected from the other three: its releases credit only
its own people and are credited to only its own artists and labels, so no walk from an
anchor of another component can reach it and no walk from it can leave.

Its shape is dictated by what the credits family orders by, one function at a time — every
registered parity call is made from a vantage point where that function's own `ORDER BY` is
total, because neither backend adds a tiebreaker and a tie would make row order legitimately
unspecified on both sides. Four consequences are worth naming, because each of them is the
reason some row is here:

- **A release carrying one person twice under two roles** (`DUAL_ROLE_RELEASE_ID`, two
  different categories) is what separates `count(c)` from `count(DISTINCT r)`: the profile
  counts credits, the leaderboard counts releases, and only a duplicated release tells the
  two apart. A second such release carries the same person twice in *one* category, which is
  what `get_person_profile`'s `total_credits` is read from.
- **A person with a `SAME_AS` artist** (`SAME_AS_PERSON`, linked to `SAME_AS_ARTIST_ID`)
  and people without one share `DUAL_ROLE_RELEASE_ID`, so `get_release_credits`' outer join
  is exercised on both sides in a single call.
- **Two credited people on the same release whose names are the only tiebreaker**
  (`SESSION_RELEASE_ID`) is what proves `ORDER BY c.category, p.name` rather than just
  `ORDER BY c.category`.
- **A release crediting four artists and two labels** (`OVERFLOWING_RELEASE_ID`) is the only
  place `collect(DISTINCT a.name)[..3]` and `collect(DISTINCT l.name)[..1]` are capped at
  all. It is deliberately *not* reachable from a registered `get_person_credits` parity
  call: Cypher's `collect` has no defined order, so which three names survive the cap is
  unspecified on the Neo4j side and the two engines can only be asked how *many* survive.
  `tests/test_graph_parity.py` asks them exactly that.

Every other credits release carries at most one artist and at most one label for the same
reason — a one-element list has only one order.
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
from common.credit_roles import categorize_role
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

# ── Coverage spike family 1 fixture data (gm-catalog-api-91a.2) ─────────────────────────
# Vertex lookups and store statistics need entities the pilot's collaborators fixture never
# seeded: a label and a master to look up by id, a year on every release so `get_year_range`
# has a real min and max to agree on, and a genre and a style so `get_graph_stats` counts
# something other than zero for those two labels. The years are otherwise arbitrary — chosen
# only so the extremes are unambiguous (release "101" is the sole minimum, "203" the sole
# maximum) — and every release gets one, so no release is excluded by the `year > 0` guard
# both engines apply, and none is excluded by `one_hop_collaborators`' `year ~ '^[0-9]{4}$'`
# guard either. A separate mapping rather than a field on `RELEASES` is also what the
# `one_hop_collaborators` family (`gm-catalog-api-91a.3`) needs it for: the two-hop family's
# `len(RELEASES[...])` / membership checks (`tests/test_graph_parity.py`) keep reading a
# plain tuple of credited artist ids either way.
#
# The genre and style are deliberately not left at zero. A totally absent label is a
# pathological case neither engine treats the same way once you look past the trivial "both
# report 0": this Neo4j build's `CALL { ... UNION ALL ... }` drops the branch's row entirely
# rather than reporting `count(g) = 0` when no node has ever carried the `Genre` label in the
# database, while the SQL side's `count(*)` over an empty `graph.genre` view still returns a
# row of `0` — a real divergence, but one that cannot happen against production data, where
# `graphinator` has always created at least one node of every label before either endpoint is
# ever called. Giving both engines one real genre and one real style keeps the fixture inside
# the case the two backends actually have to agree on.
#
# Ids are chosen clear of the full-text component's 301-303 (artists) and 401-403 (labels)
# below.
LABEL_ID = "501"
LABEL_NAME = "Fixture Label"
MASTER_ID = "601"
MASTER_NAME = "Fixture Master"
GENRE_NAME = "Fixture Genre"
STYLE_NAME = "Fixture Style"
# The one release whose document carries the genre/style tags above, so `graph.genre` and
# `graph.style` (DISTINCT over every release's and master's tags) each get exactly one row.
_TAGGED_RELEASE_ID = "101"

RELEASE_YEARS: dict[str, int] = {
    "101": 1959,
    "102": 1962,
    "103": 1965,
    "104": 1968,
    "105": 1971,
    "106": 1974,
    "107": 1977,
    "108": 1980,
    "109": 1983,
    "110": 1986,
    "111": 1989,
    "112": 1992,
    THREE_CREDIT_RELEASE_ID: 1995,
    "202": 1998,
    "203": 2001,
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


# ── The credits component (gm-catalog-api-dl8.1) ─────────────────────────────
# Read by the credits family, which walks `graph.credited_on` and `graph.same_as`. Ids
# start at 701 (releases), 801 (artists) and 502 (labels) so nothing here can collide with
# the three components above, and no release here credits an artist or names a label that
# any of them uses — the component is reachable only from its own people.
#
# The years are distinct from every year in `RELEASE_YEARS` and lie strictly inside it, so
# `catalog_overview`'s `get_year_range` still reads its minimum off release "101" and its
# maximum off release "203".

SAME_AS_PERSON = "Tessa Vance"
SAME_AS_ARTIST_ID = "801"
# One person, two roles, two categories: `get_release_credits` orders by (category, name)
# and still sees two unambiguous rows, while `get_role_leaderboard` sees one release.
DUAL_ROLE_RELEASE_ID = "701"
# Two different people, same category, so `p.name` is the only thing separating them.
SESSION_RELEASE_ID = "705"
# Four artists and two labels — the only release either `collect(...)` cap applies to.
OVERFLOWING_RELEASE_ID = "707"

CREDITS_ARTISTS: dict[str, str] = {
    "801": "Vance Machine",
    "802": "Hale Combo",
    "803": "Quill Ensemble",
    "804": "Okonkwo Trio",
}

CREDITS_LABELS: dict[str, str] = {
    "502": "Provenance Records",
    "503": "Second Pressing",
}

# release id -> the document it is seeded as. `artists` and `labels` are Discogs id lists
# (rendered into the `{"id": ...}` blocks the projections read); `extraartists` is the
# credit block verbatim, and an entry's optional `id` is what both engines turn into a
# `SAME_AS` edge to that artist.
CREDITS_RELEASES: dict[str, dict[str, Any]] = {
    DUAL_ROLE_RELEASE_ID: {
        "year": 1963,
        "artists": ["801"],
        "labels": ["502"],
        "extraartists": [
            {"name": SAME_AS_PERSON, "role": "Mastered By", "id": 801},
            {"name": "Rex Quill", "role": "Producer"},
            {"name": "Rex Quill", "role": "Mixed By"},
        ],
    },
    "702": {
        "year": 1966,
        "artists": ["802"],
        "labels": ["502"],
        "extraartists": [
            {"name": SAME_AS_PERSON, "role": "Mastered By", "id": 801},
            {"name": "Marlon Hale", "role": "Guitar"},
        ],
    },
    "703": {
        "year": 1969,
        "artists": ["802"],
        "labels": [],
        "extraartists": [
            {"name": SAME_AS_PERSON, "role": "Mastered By", "id": 801},
            {"name": "Marlon Hale", "role": "Bass"},
        ],
    },
    "704": {
        "year": 1972,
        "artists": [],
        "labels": ["502"],
        "extraartists": [
            {"name": SAME_AS_PERSON, "role": "Mastered By", "id": 801},
            {"name": "Marlon Hale", "role": "Guitar"},
        ],
    },
    SESSION_RELEASE_ID: {
        "year": 1975,
        "artists": [],
        "labels": [],
        "extraartists": [
            {"name": "Marlon Hale", "role": "Guitar"},
            {"name": "Ida Okonkwo", "role": "Vocals"},
        ],
    },
    # The same person twice in ONE category, which is what makes `get_person_profile`'s
    # `count(c)` differ from the leaderboard's `count(DISTINCT r)`. It is deliberately not
    # a `get_release_credits` parity call: two rows agreeing on (category, name) have no
    # defined order on either side.
    "706": {
        "year": 1978,
        "artists": ["801"],
        "labels": ["503"],
        "extraartists": [
            {"name": "Rex Quill", "role": "Producer"},
            {"name": "Rex Quill", "role": "Executive Producer"},
            {"name": "Nadia Brightwater", "role": "Artwork"},
        ],
    },
    OVERFLOWING_RELEASE_ID: {
        "year": 1981,
        "artists": ["801", "802", "803", "804"],
        "labels": ["502", "503"],
        "extraartists": [{"name": "Owen Fairweather", "role": "A&R"}],
    },
    # A second 1966 release, so one person has two credits in one year and
    # `get_person_timeline` reports a count above one without reporting two rows for a year.
    # Its title is what keeps `get_person_credits`' (year DESC, title) order total.
    "708": {"year": 1966, "artists": [], "labels": [], "extraartists": [{"name": "Marlon Hale", "role": "Bass"}]},
    "709": {
        "year": 1984,
        "artists": [],
        "labels": [],
        "extraartists": [
            {"name": "Wren Halloway", "role": "Mastered By"},
            {"name": "Wren Halloway", "role": "Lacquer Cut By"},
        ],
    },
    "710": {"year": 1987, "artists": [], "labels": [], "extraartists": [{"name": "Wren Halloway", "role": "Mastered By"}]},
}


def _release_document(release_id: str, release: dict[str, Any]) -> dict[str, Any]:
    """Render one credits release as the Discogs document both sides are projected from."""
    return {
        "title": f"Release {release_id}",
        "year": release["year"],
        "artists": [{"id": int(artist_id)} for artist_id in release["artists"]],
        "labels": [{"id": int(label_id)} for label_id in release["labels"]],
        "extraartists": release["extraartists"],
    }


def credit_edges() -> list[dict[str, Any]]:
    """Return every `CREDITED_ON` edge the seeded documents imply, as the enricher writes it.

    Both the credits component and the full-text component contribute: `graph.person` and
    `graph.credited_on` are filled from *every* `extraartists` block in `public.releases`,
    so a document whose credits Neo4j was never told about is a divergence the credits
    family reads as a missing person. `category` is computed with the same
    `categorize_role` the graph enricher calls, which is also what the schema producer
    renders `graph.credit_role_category` from — one taxonomy, not three copies of one.
    """
    documents: dict[str, Any] = {
        **{release_id: release["extraartists"] for release_id, release in CREDITS_RELEASES.items()},
        **{release_id: tags["extraartists"] for release_id, tags in AUTOCOMPLETE_RELEASES.items()},
    }
    return [
        {"release_id": release_id, "name": credit["name"], "role": credit["role"], "category": categorize_role(credit["role"])}
        for release_id, credits in documents.items()
        for credit in credits
    ]


def same_as_edges() -> list[dict[str, str]]:
    """Return every `SAME_AS` edge the seeded credits imply, deduplicated as the graph is."""
    seen = {
        (credit["name"], str(credit["id"]))
        for release in CREDITS_RELEASES.values()
        for credit in release["extraartists"]
        if credit.get("id")
    }
    return [{"name": name, "artist_id": artist_id} for name, artist_id in sorted(seen)]


def _endpoint_pairs(key: str, column: str) -> list[dict[str, str]]:
    """Return the (release, endpoint) pairs one document key implies, flattened for UNWIND."""
    return [
        {"release_id": release_id, column: endpoint_id} for release_id, release in CREDITS_RELEASES.items() for endpoint_id in release[key]
    ]


# Reconciled from the two branches' TRUNCATEs: family 1 needs `masters` truncated too, on
# top of the autocomplete family's `artists, labels, releases`.
_TRUNCATE_ENTITIES = "TRUNCATE artists, labels, releases, masters CASCADE"

# What turns the seeded documents into graph rows. From the phase 2 schema revision the
# `graph` relations a loader owns — every edge, and the `genre`, `style`, and `person`
# vertices — are tables rather than views over `public.releases`, so seeding a document no
# longer projects an edge on its own. `graph.bootstrap_fill()` is the producer's own
# one-off fill: it truncates each of those relations and refills it from the same phase 0
# body the view used to publish, in one transaction, and returns a row count per relation.
# It is exactly what the contract says it is for — populating an environment before a
# loader has run — which is what a fixture is. Family 1's genre/style counts, seeded above
# as a tag on release "101", depend on this the same way the full-text component's do: both
# `graph.genre` and `graph.style` are among the relations it fills.
_BOOTSTRAP_FILL = "SELECT relation, row_count FROM graph.bootstrap_fill()"

_SEED_ARTIST = "INSERT INTO artists (data_id, hash, data) VALUES (%s, %s, %s::jsonb)"
_SEED_LABEL = "INSERT INTO labels (data_id, hash, data) VALUES (%s, %s, %s::jsonb)"
_SEED_RELEASE = "INSERT INTO releases (data_id, hash, data) VALUES (%s, %s, %s::jsonb)"
_SEED_MASTER = "INSERT INTO masters (data_id, hash, data) VALUES (%s, %s, %s::jsonb)"

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
WITH count(*) AS _credited
MERGE (l:Label {id: $label_id})
SET l.name = $label_name
MERGE (m:Master {id: $master_id})
SET m.title = $master_name
MERGE (g:Genre {name: $genre_name})
MERGE (s:Style {name: $style_name})
"""

# The full-text component's Neo4j half. `graphinator` writes these five node kinds from the
# same document keys `graph.bootstrap_fill` projects the PostgreSQL tables from, so the two
# sides are seeded from one set of constants and diverge only where the engines do.
#
# The bare `:Release` nodes are the one addition that is not one of the five: the PostgreSQL
# side's `releases` table is the single base table every family's `graph.release` view or
# fill reads from, so `AUTOCOMPLETE_RELEASES`' two rows are visible to `catalog_overview`'s
# `get_graph_stats` there whether the autocomplete family "needs" them counted or not — the
# document exists, so the row does. Neo4j has no such single shared table, so without a
# matching `:Release {id: ...}` here its `MATCH (r:Release)` would legitimately answer two
# fewer than PostgreSQL's `count(*) FROM graph.release`, not because the two engines
# disagree but because only one of them was told about these two releases. No `BY`/`ON` edge
# is added — that disconnection is what the full-text component still needs — and no `year`
# is set, matching the PostgreSQL document, which carries none either.
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
WITH count(*) AS _people
UNWIND $releases AS release
MERGE (:Release {id: release})
"""

# ── The credits component's Neo4j half (gm-catalog-api-dl8.1) ────────────────
# Five statements rather than one, because `UNWIND` of an empty list drops the row it was
# unwinding from: a release with no artists would take itself out of the stream and never
# get its `:Release` node. Flattening each edge kind into its own list keeps every
# statement's input non-empty and independent.
_SEED_NEO4J_CREDITS_ENTITIES = """
UNWIND $artists AS artist
MERGE (a:Artist {id: artist.id}) SET a.name = artist.name
WITH count(*) AS _artists
UNWIND $labels AS label
MERGE (l:Label {id: label.id}) SET l.name = label.name
WITH count(*) AS _labels
UNWIND $releases AS release
MERGE (r:Release {id: release.id}) SET r.title = release.title, r.year = release.year
"""

_SEED_NEO4J_BY = """
UNWIND $edges AS edge
MATCH (r:Release {id: edge.release_id})
MATCH (a:Artist {id: edge.artist_id})
MERGE (r)-[:BY]->(a)
"""

_SEED_NEO4J_ON = """
UNWIND $edges AS edge
MATCH (r:Release {id: edge.release_id})
MATCH (l:Label {id: edge.label_id})
MERGE (r)-[:ON]->(l)
"""

# Both credit statements are `discogs-graph-enricher`'s own, copied from
# `graphinator/batch_projection.py` rather than paraphrased: `CREDITED_ON` MERGEs on
# `{role}` alone and SETs `category` afterwards, which is what makes one person credited
# twice on one release two edges instead of one.
_SEED_NEO4J_CREDITED_ON = """
UNWIND $credits AS credit
MATCH (r:Release {id: credit.release_id})
MERGE (p:Person {name: credit.name})
MERGE (p)-[c:CREDITED_ON {role: credit.role}]->(r)
SET c.category = credit.category
"""

_SEED_NEO4J_SAME_AS = """
UNWIND $credits AS credit
MATCH (p:Person {name: credit.name})
MATCH (a:Artist {id: credit.artist_id})
MERGE (p)-[:SAME_AS]->(a)
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
        releases=[{"id": release_id, "artists": list(credits), "year": RELEASE_YEARS[release_id]} for release_id, credits in RELEASES.items()],
        label_id=LABEL_ID,
        label_name=LABEL_NAME,
        master_id=MASTER_ID,
        master_name=MASTER_NAME,
        genre_name=GENRE_NAME,
        style_name=STYLE_NAME,
    )
    await consume(
        driver,
        _SEED_NEO4J_FULLTEXT,
        artists=[{"id": artist_id, "name": name} for artist_id, name in AUTOCOMPLETE_ARTISTS.items()],
        labels=[{"id": label_id, "name": name} for label_id, name in AUTOCOMPLETE_LABELS.items()],
        genres=list(AUTOCOMPLETE_GENRES),
        styles=list(AUTOCOMPLETE_STYLES),
        people=list(AUTOCOMPLETE_PEOPLE),
        releases=list(AUTOCOMPLETE_RELEASES),
    )
    # ── credits component (gm-catalog-api-dl8.1) ─────────────────────────────
    # Last, because `CREDITED_ON` and `SAME_AS` MATCH the `:Release` and `:Artist` nodes
    # the two statements above create. `credit_edges()` covers the full-text component's
    # documents as well as this one's: those two releases carry an `extraartists` block,
    # so PostgreSQL derives four `graph.credited_on` rows from them whether Neo4j was told
    # about them or not, and a credits query that scans a whole category reads the gap as
    # a person PostgreSQL has and Neo4j does not.
    await consume(
        driver,
        _SEED_NEO4J_CREDITS_ENTITIES,
        artists=[{"id": artist_id, "name": name} for artist_id, name in CREDITS_ARTISTS.items()],
        labels=[{"id": label_id, "name": name} for label_id, name in CREDITS_LABELS.items()],
        releases=[{"id": release_id, "title": f"Release {release_id}", "year": release["year"]} for release_id, release in CREDITS_RELEASES.items()],
    )
    await consume(driver, _SEED_NEO4J_BY, edges=_endpoint_pairs("artists", "artist_id"))
    await consume(driver, _SEED_NEO4J_ON, edges=_endpoint_pairs("labels", "label_id"))
    await consume(driver, _SEED_NEO4J_CREDITED_ON, credits=credit_edges())
    await consume(driver, _SEED_NEO4J_SAME_AS, credits=same_as_edges())
    # ── end credits component ────────────────────────────────────────────────
    await consume(driver, _AWAIT_NEO4J_INDEXES)


async def seed_postgres(pool: AsyncPostgreSQLPool) -> None:
    """Write the fixture as Discogs documents, then project it into the graph relations.

    The documents are still the whole input — nothing is written to a `graph` relation by
    hand. They are not the whole projection any more, though: the loader-owned relations
    are tables from the phase 2 schema revision onward, so `graph.bootstrap_fill()` runs
    afterwards to derive them from the documents just written. It truncates first, so a
    re-seed converges rather than accumulating, and it covers the traversal component's
    `graph.by_artist` and the full-text component's `graph.genre`, `graph.style`, and
    `graph.person` in the same pass — which is also where family 1's genre and style come
    from now: `graph.master` stays a plain view, so `masters` needs no fill of its own, but
    `graph.genre`/`graph.style` are only populated once `graph.bootstrap_fill()` has run.
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
            if release_id == _TAGGED_RELEASE_ID:
                document["genres"] = [GENRE_NAME]
                document["styles"] = [STYLE_NAME]
            await cursor.execute(_SEED_RELEASE, (release_id, "parity-fixture", json.dumps(document)))
        await cursor.execute(_SEED_LABEL, (LABEL_ID, "parity-fixture", json.dumps({"name": LABEL_NAME})))
        await cursor.execute(_SEED_MASTER, (MASTER_ID, "parity-fixture", json.dumps({"title": MASTER_NAME})))
        for artist_id, name in AUTOCOMPLETE_ARTISTS.items():
            await cursor.execute(_SEED_ARTIST, (artist_id, "parity-fixture", json.dumps({"name": name})))
        for label_id, name in AUTOCOMPLETE_LABELS.items():
            await cursor.execute(_SEED_LABEL, (label_id, "parity-fixture", json.dumps({"name": name})))
        for release_id, tags in AUTOCOMPLETE_RELEASES.items():
            document = {"title": f"Release {release_id}", **tags}
            await cursor.execute(_SEED_RELEASE, (release_id, "parity-fixture", json.dumps(document)))
        # ── credits component (gm-catalog-api-dl8.1) ─────────────────────────
        for artist_id, name in CREDITS_ARTISTS.items():
            await cursor.execute(_SEED_ARTIST, (artist_id, "parity-fixture", json.dumps({"name": name})))
        for label_id, name in CREDITS_LABELS.items():
            await cursor.execute(_SEED_LABEL, (label_id, "parity-fixture", json.dumps({"name": name})))
        for release_id, release in CREDITS_RELEASES.items():
            await cursor.execute(_SEED_RELEASE, (release_id, "parity-fixture", json.dumps(_release_document(release_id, release))))
        # ── end credits component ────────────────────────────────────────────
        await cursor.execute(_BOOTSTRAP_FILL)
        await cursor.fetchall()


async def open_postgres_pool() -> AsyncPostgreSQLPool:
    """Open a pool on the integration container with the producer's own schema applied.

    Both property-graph gates are asserted rather than skipped past *when the switch asks
    for the graph*: a container that has been told to declare `graph.catalog` and then
    cannot is a failure, not a no-op, because the whole point of the PostgreSQL 19 tier is
    that the graph is there.

    With the switch off there is nothing to gate. A family registered
    `requires_property_graph=False` — coverage spike family 1 (`gm-catalog-api-91a.2`) and
    autocomplete both are — is ordinary SQL over the `graph` relations, which are
    unconditional on every tier, so it runs here on PostgreSQL 18 too, and asserting a graph
    it never reads would be the fixture failing a run the family is fine on. Which gate
    applies is read from `SCHEMA_PROPERTY_GRAPH` itself rather than passed in by the caller:
    one pool is shared by every family a given test run seeds, and the switch is a property
    of the *tier*, not of any one family's call.
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
