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

**The rarity component** (ids 701+) belongs to the rarity family (`gm-catalog-api-wpku.1`) and
is the first component that needs the graph to be *complete* rather than merely sufficient for
one traversal. The rarity batch reads every release there is and asks eight questions about
each, one of which is its degree — so a release whose document implies an edge the Neo4j side
was never told about is a divergence, whatever family seeded it. Everything below is therefore
projected into both engines from one set of documents (:func:`release_documents`), including
the genre, style, and credit edges the earlier components' documents always implied and only
PostgreSQL ever had.

The component itself carries what the signals need and nothing else: a label on four releases
so `label_catalog` and `label_max_catalog` are non-zero, a genre on three so
`genre_max_release_count` is, a master with three pressings and a second master with exactly
one (the `groovemap-cu2.75` case, where a unique pressing must score 100.0 and not the 90.0 a
release with no master link scores), a standalone release with no master at all, a canonical
`vinyl_12` medium on every one of them so the grooved family applies and `formats`,
`media_families` and the `ISSUED_ON` edges are all exercised, and one collection row and one
wantlist row on release 731 — which is the only way `graph.release_degree`'s live half, the
pair of lateral counts that is the reason it is not folded onto the release vertex, is
counted at all.

Release 735 credits two artists deliberately: `artist_name` is the alphabetically first
credit on both engines, and a single-credit fixture would never show that the two agree.

The counters `graphinator` writes as node properties — `Genre.release_count`,
`Style.release_count`, `Label.release_count` — are computed here from the same documents and
set on the Neo4j nodes, because that is what PostgreSQL's `graph.bootstrap_fill()` derives on
its side. The degrees are not: Neo4j counts those live, and the fixture's edges are what makes
the two agree.

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

# A disconnected six-edge chain for the procedural path family. Alternating artist and
# release vertices gives one unambiguous target at every distance from one through six.
PATH_ANCHOR_ARTIST_ID = "1201"
PATH_UNREACHABLE_ARTIST_ID = "1299"
PATH_DISTANCE_TARGETS: tuple[tuple[str, str], ...] = (
    ("release", "1301"),
    ("artist", "1202"),
    ("release", "1302"),
    ("artist", "1203"),
    ("release", "1303"),
    ("artist", "1204"),
)
PATH_ALIAS_ARTIST_ID = "1298"

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
    "1201": "Path Zero",
    "1202": "Path Two",
    "1203": "Path Four",
    "1204": "Path Six",
    PATH_ALIAS_ARTIST_ID: "Path Alias",
    PATH_UNREACHABLE_ARTIST_ID: "Path Unreachable",
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
    "1301": ("1201", "1202"),
    "1302": ("1202", "1203"),
    "1303": ("1203", "1204"),
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
    "1301": 2002,
    "1302": 2003,
    "1303": 2004,
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


# ── MusicBrainz component (gm-catalog-api-wpku.3) ──────────────────────────
# Reuses Discogs ids 1, 2, 501, and 101 deliberately: the feature is the mapping
# between the two catalogs. Its MusicBrainz UUIDs occupy their own all-9 prefix so
# they cannot overlap any other fixture identifier.
MB_ARTIST_ONE = "99999999-0000-0000-0000-000000000001"
MB_ARTIST_TWO = "99999999-0000-0000-0000-000000000002"
MB_LABEL = "99999999-0000-0000-0000-000000000003"
MB_RELEASE = "99999999-0000-0000-0000-000000000004"
MB_LINK = ("wikidata", "https://www.wikidata.org/wiki/Q999999")

MUSICBRAINZ_ARTISTS: tuple[dict[str, Any], ...] = (
    {
        "mbid": MB_ARTIST_ONE,
        "discogs_id": 1,
        "name": ARTISTS["1"],
        "sort_name": "Anchor",
        "type": "Person",
        "gender": None,
        "begin_date": "1970",
        "end_date": None,
        "area": "Fixture Area",
        "begin_area": "Fixture Beginning",
        "disambiguation": "fixture anchor",
    },
    {
        "mbid": MB_ARTIST_TWO,
        "discogs_id": 2,
        "name": ARTISTS["2"],
        "sort_name": "Shared, Three",
        "type": "Group",
        "gender": None,
        "begin_date": "1980",
        "end_date": None,
        "area": "Fixture Area",
        "begin_area": None,
        "disambiguation": "fixture band",
    },
)

MUSICBRAINZ_RELATIONSHIPS: tuple[dict[str, Any], ...] = (
    {
        "source_mbid": MB_ARTIST_ONE,
        "target_mbid": MB_ARTIST_TWO,
        "raw_type": "member of band",
        "mapped_type": "MEMBER_OF",
        "begin_date": "1990",
        "end_date": None,
        "attributes": ["vocals"],
    },
    # PostgreSQL preserves this as a second relationship instance. The Neo4j enricher's
    # MERGE key is only (source, mapped type, target), so it collapses both instances into
    # one edge. Keeping the dates equal makes ``attributes`` the required final ordering
    # key on the PostgreSQL response.
    {
        "source_mbid": MB_ARTIST_ONE,
        "target_mbid": MB_ARTIST_TWO,
        "raw_type": "member of band",
        "mapped_type": "MEMBER_OF",
        "begin_date": "1990",
        "end_date": None,
        "attributes": ["guitar"],
    },
    # The loader retains unknown raw vocabulary, while graph.mb_relationship_type
    # publishes NULL and the enricher writes no edge. Keeping it in the shared
    # fixture proves the API reads the mapped column and filters it, without hiding
    # the raw row from source-table status totals.
    {
        "source_mbid": MB_ARTIST_ONE,
        "target_mbid": MB_ARTIST_TWO,
        "raw_type": "producer",
        "mapped_type": None,
        "begin_date": None,
        "end_date": None,
        "attributes": [],
    },
)


# ── The rarity component (gm-catalog-api-wpku.1) ────────────────────────────
# Ids start at 701, clear of every range above. Names are chosen so none of them is reached by
# an autocomplete parity query: a new row that matched "radio", "warp", "roc", "elec", "ambi",
# "tech", "bob" or "chuck" would change that family's answer on both engines and the harness
# would be proving the fixture rather than the SQL.

RARITY_ARTISTS: dict[str, str] = {
    "701": "Sole Presser",
    "702": "Deep Cut Duo",
}

RARITY_LABEL_ID = "711"
RARITY_LABEL_NAME = "Silent Grooves"

# Two masters: one with three pressings, one with exactly one. The second is the
# groovemap-cu2.75 case — a release that IS linked to a master but is that master's only
# pressing must score 100.0 (unique pressing), not the 90.0 a release with no master link
# scores — and it is the reason the pressing query keeps two separate optional lookups.
RARITY_MASTER_ID = "721"
RARITY_MASTER_NAME = "Grooved Master"
RARITY_LONE_MASTER_ID = "722"
RARITY_LONE_MASTER_NAME = "Only Press Master"

# The insights family needs one real anniversary result, including its optional artist
# credit.  Keeping it on the pre-existing fixture master makes the earlier metadata calls
# observe the same entity while giving both engines a non-empty anniversary comparison.
MASTER_YEARS: dict[str, int] = {MASTER_ID: 2000}
MASTER_ARTISTS: dict[str, tuple[str, ...]] = {MASTER_ID: (ANCHOR_ARTIST_ID,)}

RARITY_GENRE_NAME = "Spiritual Jazz"
RARITY_STYLE_NAME = "Free Improvisation"

# The canonical medium every rarity release is issued on. `vinyl` is a grooved family, so the
# grooved extension applies and `pressing_scarcity` is actually contributed; a CD would score
# the whole component through the core alone and prove nothing about the family seam.
RARITY_MEDIUM_ID = "vinyl_12"
RARITY_MEDIUM_FAMILY = "vinyl"
# What `graph.release.formats` projects from the document — `data->'formats'[].name`, so the
# `LP` description is not one of them.
RARITY_FORMAT_NAME = "Vinyl"

# The one release with a collection row and a wantlist row, which is what exercises the live
# half of `graph.release_degree` — the pair of lateral counts over `user_collections` and
# `user_wantlists` that is the whole reason that counter is not folded onto the release vertex.
RARITY_COLLECTED_RELEASE_ID = "731"

# The account those two rows belong to. Distinct from `tests/test_real_databases.py`'s own
# TEST_USER_ID so a sync test's TRUNCATE and this fixture cannot be mistaken for each other.
RARITY_USER_ID = "00000000-0000-0000-0000-000000000701"
RARITY_USER_EMAIL = "rarity-fixture@catalog-api.invalid"

# release id -> (credited artist ids, label ids, master id, genres, styles, year).
RARITY_RELEASES: dict[str, dict[str, Any]] = {
    "731": {
        "artists": ("701",),
        "labels": (RARITY_LABEL_ID,),
        "master": RARITY_MASTER_ID,
        "genres": (RARITY_GENRE_NAME,),
        "styles": (RARITY_STYLE_NAME,),
        "year": 1972,
    },
    "732": {
        "artists": ("701",),
        "labels": (RARITY_LABEL_ID,),
        "master": RARITY_MASTER_ID,
        "genres": (RARITY_GENRE_NAME,),
        "styles": (),
        "year": 1975,
    },
    "733": {"artists": ("702",), "labels": (RARITY_LABEL_ID,), "master": RARITY_MASTER_ID, "genres": (), "styles": (), "year": 1978},
    "734": {
        "artists": ("702",),
        "labels": (RARITY_LABEL_ID,),
        "master": RARITY_LONE_MASTER_ID,
        "genres": (RARITY_GENRE_NAME,),
        "styles": (),
        "year": 1981,
    },
    # No master and no label: the standalone case, and the one release credited to two artists,
    # so `artist_name` has an alphabetically-first credit to pick rather than an only one.
    "735": {"artists": ("701", "702"), "labels": (), "master": None, "genres": (), "styles": (), "year": 1984},
}

# The stored rarity rows the two vertex lookups page over. `insights.release_rarity` is
# written by the batch, not by a loader, so a lookup has nothing to read until something has
# scored — and both backends read this same table, which is the point: ADR 0012 moves the
# graph reads, not the results table. Scores are distinct so the `rarity_score DESC,
# release_id` order is total and neither engine is asked to break a tie it has no rule for.
RARITY_STORED_ROWS: tuple[dict[str, Any], ...] = (
    {
        "release_id": 731,
        "title": "Release 731",
        "artist_name": "Sole Presser",
        "year": 1972,
        "rarity_score": 91.5,
        "tier": "ultra-rare",
        "hidden_gem_score": 44.0,
    },
    {
        "release_id": 732,
        "title": "Release 732",
        "artist_name": "Sole Presser",
        "year": 1975,
        "rarity_score": 72.25,
        "tier": "rare",
        "hidden_gem_score": 31.5,
    },
    {
        "release_id": 733,
        "title": "Release 733",
        "artist_name": "Deep Cut Duo",
        "year": 1978,
        "rarity_score": 58.0,
        "tier": "scarce",
        "hidden_gem_score": 22.75,
    },
    {
        "release_id": 734,
        "title": "Release 734",
        "artist_name": "Deep Cut Duo",
        "year": 1981,
        "rarity_score": 44.5,
        "tier": "scarce",
        "hidden_gem_score": 12.0,
    },
    {
        "release_id": 735,
        "title": "Release 735",
        "artist_name": "Deep Cut Duo",
        "year": 1984,
        "rarity_score": 33.75,
        "tier": "uncommon",
        "hidden_gem_score": 5.25,
    },
)

# One community row, so the batch's `collection_prevalence` is computed from a real have/want
# pair on at least one release rather than falling back to the neutral 50.0 everywhere.
# `insights.community_counts` is PostgreSQL on both backends and is not part of the migration.
RARITY_COMMUNITY_COUNTS: tuple[tuple[int, int, int], ...] = ((731, 12, 340),)


def release_documents() -> dict[str, dict[str, Any]]:
    """Return the Discogs document of every release in the fixture, keyed by release id.

    One builder for both engines. PostgreSQL stores these verbatim and `graph.bootstrap_fill()`
    projects the graph relations out of them; :func:`seed_neo4j` projects the same documents
    into nodes and relationships. Anything derived — the flattened `formats` names, the media
    families, the counter properties — is derived here, once, so the two sides cannot disagree
    about what a document implies.
    """
    documents: dict[str, dict[str, Any]] = {}
    for release_id, credits in RELEASES.items():
        document: dict[str, Any] = {
            "title": f"Release {release_id}",
            "year": RELEASE_YEARS[release_id],
            "artists": [{"id": int(each)} for each in credits],
        }
        if release_id == _TAGGED_RELEASE_ID:
            document["genres"] = [GENRE_NAME]
            document["styles"] = [STYLE_NAME]
        documents[release_id] = document

    for release_id, tags in AUTOCOMPLETE_RELEASES.items():
        documents[release_id] = {"title": f"Release {release_id}", **tags}

    for release_id, spec in RARITY_RELEASES.items():
        document = {
            "title": f"Release {release_id}",
            "year": spec["year"],
            "artists": [{"id": int(each)} for each in spec["artists"]],
            "formats": [{"name": RARITY_FORMAT_NAME, "descriptions": ["LP"]}],
        }
        if spec["labels"]:
            document["labels"] = [{"id": int(each), "catno": f"SG-{release_id}"} for each in spec["labels"]]
        if spec["master"] is not None:
            document["master_id"] = spec["master"]
        if spec["genres"]:
            document["genres"] = list(spec["genres"])
        if spec["styles"]:
            document["styles"] = list(spec["styles"])
        documents[release_id] = document
    return documents


def release_media() -> dict[str, dict[str, Any]]:
    """Return the canonical media block of every release that has one, by release id.

    The `releases.media` column, which `graph.release.media_families` and the `issued_on` /
    `medium` relations are projected from. Only the rarity component carries one; the earlier
    components' releases have no media evidence at all, which is a case the batch has to score
    too.
    """
    return {
        release_id: {"items": [{"medium": RARITY_MEDIUM_ID, "family": RARITY_MEDIUM_FAMILY, "qty": 1}], "families": [RARITY_MEDIUM_FAMILY]}
        for release_id in RARITY_RELEASES
    }


def _tag_counts(key: str) -> dict[str, int]:
    """Return how many releases carry each value of the document's *key* tag list.

    What `graph.bootstrap_fill()` computes into `genre_stats.release_count` and
    `style_stats.release_count`: one row per edge, counted per tag. `graphinator` writes the
    same number onto the Neo4j node as a property, which is what the Cypher signal queries
    read, so the fixture has to write it too.
    """
    counts: dict[str, int] = {}
    for document in release_documents().values():
        for value in document.get(key, ()):
            counts[value] = counts.get(value, 0) + 1
    return counts


def _label_release_counts() -> dict[str, int]:
    """Return how many releases each label id appears on — `label_stats.release_count`."""
    counts: dict[str, int] = {}
    for document in release_documents().values():
        for entry in document.get("labels", ()):
            label_id = str(entry["id"])
            counts[label_id] = counts.get(label_id, 0) + 1
    return counts


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


# ── The label-DNA component (gm-catalog-api-dl8.2) ──────────────────────────
# This component is intentionally disconnected from autocomplete labels 401-403 and the
# credits labels 502-503. Its target has six releases, so every full-profile query runs;
# the candidate has five releases sharing a style, so the two-phase similarity query has
# a real result; the low-release label proves the MIN_RELEASES short-circuit; and the
# fallback label has a media family but no ISSUED_ON edge.
LABEL_DNA_TARGET_ID = "1101"
LABEL_DNA_CANDIDATE_ID = "1102"
LABEL_DNA_LOW_RELEASE_ID = "1103"
LABEL_DNA_FALLBACK_ID = "1104"
COLLECTION_MASTER_ID = "1401"
COLLECTION_USER_ID = "00000000-0000-0000-0000-000000000001"
COLLECTION_OTHER_USER_ID = "00000000-0000-0000-0000-000000000002"
COLLECTION_THIRD_USER_ID = "00000000-0000-0000-0000-000000000003"
COLLECTION_RECOMMEND_USER_ID = "00000000-0000-0000-0000-000000000004"

LABEL_DNA_LABELS: dict[str, str] = {
    LABEL_DNA_TARGET_ID: "Label DNA Target",
    LABEL_DNA_CANDIDATE_ID: "Label DNA Candidate",
    LABEL_DNA_LOW_RELEASE_ID: "Label DNA Tiny",
    LABEL_DNA_FALLBACK_ID: "Label DNA Fallback",
    "1150": "Recommendation Test Label",
}

LABEL_DNA_ARTISTS: dict[str, str] = {
    "1301": "Label DNA Artist One",
    "1302": "Label DNA Artist Two",
    "1303": "Recommendation Test Artist",
}

# Graphinator emits ALIAS_OF from alias to primary. The primary artist document
# lists its aliases, while the Neo4j edge below follows alias -> primary.
ALIAS_ARTIST_ID = "1301"
PRIMARY_ARTIST_ID = "1302"


def _label_dna_release(
    *,
    year: int | None,
    artists: list[str],
    label: str,
    genres: list[str],
    styles: list[str],
    formats: list[str],
    families: list[str],
    items: list[dict[str, Any]],
    master_id: str | None = None,
) -> dict[str, Any]:
    return {
        "year": year,
        "artists": artists,
        "label": label,
        "genres": genres,
        "styles": styles,
        "formats": formats,
        "media": {"families": families, "items": items},
        "master_id": master_id,
    }


LABEL_DNA_RELEASES: dict[str, dict[str, Any]] = {
    **{
        str(1201 + index): _label_dna_release(
            year=1991 + index if index < 5 else None,
            artists=["1301"] if index < 3 else ["1302"],
            label=LABEL_DNA_TARGET_ID,
            genres=["Electronic"] if index < 4 else ["Jazz"],
            styles=["Label DNA Shared Style"],
            formats=["Vinyl"] if index < 4 else ["CD"],
            families=["vinyl"] if index < 4 else ["optical"],
            items=[
                {
                    "medium": "vinyl_12" if index < 4 else "optical_cd",
                    "family": "vinyl" if index < 4 else "optical",
                    "label": '12" vinyl' if index < 4 else "CD",
                }
            ],
            master_id=COLLECTION_MASTER_ID if index >= 2 else None,
        )
        for index in range(6)
    },
    **{
        str(1211 + index): _label_dna_release(
            year=2001 + index,
            artists=["1301"],
            label=LABEL_DNA_CANDIDATE_ID,
            genres=["Electronic"] if index < 4 else ["Ambient"],
            styles=["Label DNA Shared Style"],
            formats=["Vinyl"],
            families=[],
            items=[],
        )
        for index in range(5)
    },
    "1221": _label_dna_release(
        year=2010,
        artists=["1301"],
        label=LABEL_DNA_LOW_RELEASE_ID,
        genres=["Rock"],
        styles=["Label DNA Tiny Style"],
        formats=["Vinyl"],
        families=[],
        items=[],
    ),
    "1222": _label_dna_release(
        year=2011,
        artists=["1302"],
        label=LABEL_DNA_LOW_RELEASE_ID,
        genres=["Rock"],
        styles=["Label DNA Tiny Style"],
        formats=["CD"],
        families=[],
        items=[],
    ),
    "1231": _label_dna_release(
        year=2015,
        artists=["1301"],
        label=LABEL_DNA_FALLBACK_ID,
        genres=["Ambient"],
        styles=["Label DNA Fallback Style"],
        formats=["Vinyl"],
        families=["vinyl"],
        items=[],
    ),
    **{
        str(1241 + index): _label_dna_release(
            year=2016 + index,
            artists=["1303"],
            label="1150",
            genres=["Electronic"],
            styles=["Label DNA Shared Style"],
            formats=["Vinyl"],
            families=["vinyl"],
            items=[],
        )
        for index in range(3)
    },
    "1244": _label_dna_release(
        year=2019,
        artists=["1303"],
        label="1150",
        genres=["Rock"],
        styles=["Label DNA Shared Style"],
        formats=["Vinyl"],
        families=["vinyl"],
        items=[],
    ),
}

COLLECTION_ROWS = (
    {"user_id": COLLECTION_USER_ID, "release_id": "1201", "instance_id": 1, "rating": 5, "folder_id": 1, "date_added": "2020-01-01T00:00:00Z"},
    {"user_id": COLLECTION_USER_ID, "release_id": "1201", "instance_id": 2, "rating": 4, "folder_id": 2, "date_added": "2020-02-01T00:00:00Z"},
    {"user_id": COLLECTION_USER_ID, "release_id": "1202", "instance_id": 3, "rating": 4, "folder_id": 1, "date_added": "2021-01-01T00:00:00Z"},
    {"user_id": COLLECTION_USER_ID, "release_id": "1203", "instance_id": 4, "rating": 0, "folder_id": 2, "date_added": "2022-01-01T00:00:00Z"},
    {"user_id": COLLECTION_OTHER_USER_ID, "release_id": "1201", "instance_id": 5, "rating": 0, "folder_id": 1, "date_added": "2023-01-01T00:00:00Z"},
    {"user_id": COLLECTION_OTHER_USER_ID, "release_id": "1202", "instance_id": 6, "rating": 0, "folder_id": 1, "date_added": "2023-02-01T00:00:00Z"},
    {"user_id": COLLECTION_THIRD_USER_ID, "release_id": "1201", "instance_id": 7, "rating": 0, "folder_id": 1, "date_added": "2024-01-01T00:00:00Z"},
    {
        "user_id": COLLECTION_RECOMMEND_USER_ID,
        "release_id": "1204",
        "instance_id": 8,
        "rating": 0,
        "folder_id": 1,
        "date_added": "2024-02-01T00:00:00Z",
    },
    {
        "user_id": COLLECTION_RECOMMEND_USER_ID,
        "release_id": "1241",
        "instance_id": 9,
        "rating": 0,
        "folder_id": 1,
        "date_added": "2024-03-01T00:00:00Z",
    },
)

WANT_ROWS = (
    {"user_id": COLLECTION_USER_ID, "release_id": "1204", "rating": 0, "date_added": "2023-03-01T00:00:00Z"},
    {"user_id": COLLECTION_USER_ID, "release_id": "1211", "rating": 0, "date_added": "2023-04-01T00:00:00Z"},
)


def label_dna_edges(field: str, endpoint: str) -> list[dict[str, str]]:
    """Flatten one list-valued label-DNA release field into Neo4j seed rows."""
    return [{"release_id": release_id, endpoint: str(value)} for release_id, release in LABEL_DNA_RELEASES.items() for value in release[field]]


def label_dna_media_edges() -> list[dict[str, str]]:
    """Return canonical media rows with labels for Neo4j's shared vertices."""
    return [
        {
            "release_id": release_id,
            "medium_id": str(item["medium"]),
            "family": str(item["family"]),
            "label": str(item["label"]),
        }
        for release_id, release in LABEL_DNA_RELEASES.items()
        for item in release["media"]["items"]
    ]


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
    seen = {(credit["name"], str(credit["id"])) for release in CREDITS_RELEASES.values() for credit in release["extraartists"] if credit.get("id")}
    return [{"name": name, "artist_id": artist_id} for name, artist_id in sorted(seen)]


def _endpoint_pairs(key: str, column: str) -> list[dict[str, str]]:
    """Return the (release, endpoint) pairs one document key implies, flattened for UNWIND."""
    return [{"release_id": release_id, column: endpoint_id} for release_id, release in CREDITS_RELEASES.items() for endpoint_id in release[key]]


# Reconciled from the two branches' TRUNCATEs: family 1 needs `masters` truncated too, on
# top of the autocomplete family's `artists, labels, releases`.
_TRUNCATE_ENTITIES = "TRUNCATE artists, labels, releases, masters, users CASCADE"
_TRUNCATE_MUSICBRAINZ = (
    "TRUNCATE musicbrainz.relationships, musicbrainz.external_links, "
    "musicbrainz.artists, musicbrainz.labels, musicbrainz.releases, "
    "musicbrainz.release_groups CASCADE"
)

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
_SEED_LABEL_DNA_RELEASE = "INSERT INTO releases (data_id, hash, data, media) VALUES (%s, %s, %s::jsonb, %s::jsonb)"
_SEED_RELEASE_WITH_MEDIA = _SEED_LABEL_DNA_RELEASE
_SEED_MASTER = "INSERT INTO masters (data_id, hash, data) VALUES (%s, %s, %s::jsonb)"
_SEED_MB_ARTIST = """
INSERT INTO musicbrainz.artists (
    mbid, name, sort_name, type, gender, begin_date, end_date,
    area, begin_area, disambiguation, discogs_artist_id
) VALUES (%s::uuid, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""
_SEED_MB_LABEL = """
INSERT INTO musicbrainz.labels (mbid, name, discogs_label_id)
VALUES (%s::uuid, %s, %s)
"""
_SEED_MB_RELEASE = """
INSERT INTO musicbrainz.releases (mbid, name, discogs_release_id)
VALUES (%s::uuid, %s, %s)
"""
_SEED_MB_RELATIONSHIP_ROW = """
INSERT INTO musicbrainz.relationships (
    source_mbid, target_mbid, source_entity_type, target_entity_type,
    relationship_type, begin_date, end_date, attributes
) VALUES (%s::uuid, %s::uuid, 'artist', 'artist', %s, %s, %s, %s::jsonb)
"""
_SEED_MB_LINK = """
INSERT INTO musicbrainz.external_links (mbid, entity_type, service_name, url)
VALUES (%s::uuid, 'artist', %s, %s)
"""
_SEED_COLLECTION = (
    "INSERT INTO user_collections (user_id, release_id, instance_id, rating, folder_id, date_added, media) "
    "VALUES (%s::uuid, %s::bigint, %s::bigint, %s, %s, %s::timestamptz, %s::jsonb)"
)
_SEED_WANT = "INSERT INTO user_wantlists (user_id, release_id, rating, date_added) VALUES (%s::uuid, %s::bigint, %s, %s::timestamptz)"

# The account the collection and wantlist rows belong to. `users.hashed_password` is NOT NULL
# and is never read by anything under test. The conflict target is left off so a re-seed
# converges whether the clash is on the id or on the unique email.
_SEED_USER = """
INSERT INTO users (id, email, hashed_password, is_active)
VALUES (%s, %s, 'not-a-real-hash', TRUE)
ON CONFLICT DO NOTHING
"""

_CLEAR_COLLECTION = "DELETE FROM user_collections WHERE user_id = %s"
_CLEAR_WANTLIST = "DELETE FROM user_wantlists WHERE user_id = %s"
_SEED_RARITY_COLLECTION = "INSERT INTO user_collections (user_id, release_id, instance_id) VALUES (%s, %s, %s)"
_SEED_RARITY_WANTLIST = "INSERT INTO user_wantlists (user_id, release_id) VALUES (%s, %s)"

# The stored rarity rows and community counts. Both tables are PostgreSQL on either backend —
# ADR 0012 migrates the graph reads, not the results table — so this is shared input to both
# sides of the comparison rather than one engine's half of it.
_CLEAR_STORED_RARITY = "DELETE FROM insights.release_rarity WHERE release_id = ANY(%s)"
_SEED_STORED_RARITY = """
INSERT INTO insights.release_rarity (release_id, title, artist_name, year, rarity_score, tier, hidden_gem_score)
VALUES (%s, %s, %s, %s, %s, %s, %s)
"""
_CLEAR_COMMUNITY_COUNTS = "DELETE FROM insights.community_counts WHERE release_id = ANY(%s)"
_SEED_COMMUNITY_COUNTS = "INSERT INTO insights.community_counts (release_id, have_count, want_count) VALUES (%s, %s, %s)"

# ── The Neo4j projection ────────────────────────────────────────────────────
# One statement per relation rather than one long chained MERGE. Chaining needs a
# `WITH count(*)` between every section to survive an empty UNWIND, and the nested per-release
# unwinds it forces make "which document key produced this edge" hard to read — which is
# exactly the question a reader comparing the two engines is asking. Each statement below
# takes a flat list that `seed_neo4j` derives from `release_documents()`, so the Cypher says
# what it writes and Python says where it came from.

_SEED_ARTIST_NODES = """
UNWIND $artists AS artist
MERGE (a:Artist {id: artist.id})
SET a.name = artist.name
"""

_SEED_NEO4J_EXPLORE_TAGS = """
UNWIND $rows AS row
MATCH (r:Release {id: row.id})
FOREACH (genre IN row.genres |
    MERGE (g:Genre {name: genre}) MERGE (r)-[:IS]->(g))
FOREACH (style IN row.styles |
    MERGE (s:Style {name: style}) MERGE (r)-[:IS]->(s))
"""

_SEED_NEO4J_EXPLORE_COUNTERS = (
    """MATCH (g:Genre)
       OPTIONAL MATCH (g)<-[:IS]-(r:Release)
       WITH g, min(CASE WHEN r.year > 0 THEN r.year END) AS first_year
       SET g.first_year = first_year,
           g.release_count = COUNT { MATCH (release:Release)-[:IS]->(g) RETURN DISTINCT release },
           g.artist_count = COUNT { MATCH (release:Release)-[:IS]->(g), (release)-[:BY]->(artist:Artist) RETURN DISTINCT artist },
           g.label_count = COUNT { MATCH (release:Release)-[:IS]->(g), (release)-[:ON]->(label:Label) RETURN DISTINCT label },
           g.style_count = COUNT { MATCH (release:Release)-[:IS]->(g), (release)-[:IS]->(style:Style) RETURN DISTINCT style }
    """,
    """MATCH (s:Style)
       OPTIONAL MATCH (s)<-[:IS]-(r:Release)
       WITH s, min(CASE WHEN r.year > 0 THEN r.year END) AS first_year
       SET s.first_year = first_year,
           s.release_count = COUNT { MATCH (release:Release)-[:IS]->(s) RETURN DISTINCT release },
           s.artist_count = COUNT { MATCH (release:Release)-[:IS]->(s), (release)-[:BY]->(artist:Artist) RETURN DISTINCT artist },
           s.label_count = COUNT { MATCH (release:Release)-[:IS]->(s), (release)-[:ON]->(label:Label) RETURN DISTINCT label },
           s.genre_count = COUNT { MATCH (release:Release)-[:IS]->(s), (release)-[:IS]->(genre:Genre) RETURN DISTINCT genre }
    """,
    """MATCH (l:Label)
       SET l.release_count = COUNT { MATCH (release:Release)-[:ON]->(l) RETURN DISTINCT release },
           l.artist_count = COUNT { MATCH (release:Release)-[:ON]->(l), (release)-[:BY]->(artist:Artist) RETURN DISTINCT artist },
           l.genre_count = COUNT { MATCH (release:Release)-[:ON]->(l), (release)-[:IS]->(genre:Genre) RETURN DISTINCT genre }
    """,
)

# `release_count` is the counter `graphinator` writes in its post-import pass and the Cypher
# signal queries read as a node property. PostgreSQL derives the same number in
# `graph.bootstrap_fill()`; both come from the same documents.
_SEED_LABEL_NODES = """
UNWIND $labels AS label
MERGE (l:Label {id: label.id})
SET l.name = label.name, l.release_count = label.release_count
"""

_SEED_MASTER_NODES = """
UNWIND $masters AS master
MERGE (m:Master {id: master.id})
SET m.title = master.title, m.year = master.year
"""

_SEED_GENRE_NODES = """
UNWIND $genres AS genre
MERGE (g:Genre {name: genre.name})
SET g.release_count = genre.release_count
"""

_SEED_STYLE_NODES = """
UNWIND $styles AS style
MERGE (s:Style {name: style.name})
SET s.release_count = style.release_count
"""

_SEED_PERSON_NODES = """
UNWIND $people AS person
MERGE (:Person {name: person})
"""

_SEED_MEDIUM_NODES = """
UNWIND $mediums AS medium
MERGE (m:Medium {id: medium.id})
SET m.family = medium.family
MERGE (f:MediaFamily {name: medium.family})
MERGE (m)-[:IN_FAMILY]->(f)
"""

# `formats` and `media_families` are list properties on both engines, and `graph.release`
# projects each as an empty array rather than null when the document carries none — so an
# empty list is set here rather than the property being left off.
_SEED_RELEASE_NODES = """
UNWIND $releases AS release
MERGE (r:Release {id: release.id})
SET r.title = release.title,
    r.year = release.year,
    r.formats = release.formats,
    r.media_families = release.media_families
"""

_SEED_BY_EDGES = """
UNWIND $edges AS edge
MATCH (r:Release {id: edge.release_id})
MATCH (a:Artist {id: edge.artist_id})
MERGE (r)-[:BY]->(a)
"""

_SEED_MASTER_BY_EDGES = """
UNWIND $edges AS edge
MATCH (m:Master {id: edge.master_id})
MATCH (a:Artist {id: edge.artist_id})
MERGE (m)-[:BY]->(a)
"""

_SEED_ON_EDGES = """
UNWIND $edges AS edge
MATCH (r:Release {id: edge.release_id})
MATCH (l:Label {id: edge.label_id})
MERGE (r)-[:ON]->(l)
"""

_SEED_GENRE_EDGES = """
UNWIND $edges AS edge
MATCH (r:Release {id: edge.release_id})
MATCH (g:Genre {name: edge.name})
MERGE (r)-[:IS]->(g)
"""

_SEED_STYLE_EDGES = """
UNWIND $edges AS edge
MATCH (r:Release {id: edge.release_id})
MATCH (s:Style {name: edge.name})
MERGE (r)-[:IS]->(s)
"""

_SEED_MASTER_EDGES = """
UNWIND $edges AS edge
MATCH (r:Release {id: edge.release_id})
MATCH (m:Master {id: edge.master_id})
MERGE (r)-[:DERIVED_FROM]->(m)
"""

_SEED_ISSUED_ON_EDGES = """
UNWIND $edges AS edge
MATCH (r:Release {id: edge.release_id})
MATCH (m:Medium {id: edge.medium_id})
MERGE (r)-[i:ISSUED_ON {source: edge.source}]->(m)
SET i.qty = edge.qty
"""

_SEED_CREDITED_ON_EDGES = """
UNWIND $edges AS edge
MATCH (r:Release {id: edge.release_id})
MATCH (p:Person {name: edge.person_name})
MERGE (p)-[:CREDITED_ON {role: edge.role}]->(r)
"""

# The personal half of release degree. `graph.release_degree` counts these live out of
# `user_collections` and `user_wantlists`; Neo4j counts the edges. They have to be the same
# rows or the degree signal diverges on this release alone.
_SEED_COLLECTION_EDGES = """
MERGE (u:User {id: $user_id})
WITH u
UNWIND $collected AS release_id
MATCH (r:Release {id: release_id})
MERGE (u)-[:COLLECTED]->(r)
"""

_SEED_WANTLIST_EDGES = """
MERGE (u:User {id: $user_id})
WITH u
UNWIND $wanted AS release_id
MATCH (r:Release {id: release_id})
MERGE (u)-[:WANTS]->(r)
"""

_SEED_MB_ARTIST_PROPERTIES = """
UNWIND $artists AS artist
MATCH (a:Artist {id: artist.discogs_id})
SET a.mbid = artist.mbid,
    a.mb_type = artist.type,
    a.mb_gender = artist.gender,
    a.mb_begin_date = artist.begin_date,
    a.mb_end_date = artist.end_date,
    a.mb_area = artist.area,
    a.mb_begin_area = artist.begin_area,
    a.mb_disambiguation = artist.disambiguation
"""

_SEED_MB_ENTITY_PROPERTIES = """
MATCH (label:Label {id: $label_id})
SET label.mbid = $label_mbid
WITH label
MATCH (release:Release {id: $release_id})
SET release.mbid = $release_mbid
"""

_SEED_DISCOGS_MEMBER_EDGE = """
MATCH (member:Artist {id: $member_id})
MATCH (group:Artist {id: $group_id})
MERGE (member)-[:MEMBER_OF]->(group)
"""

_SEED_MB_EDGE = """
MATCH (source:Artist {id: $source_id})
MATCH (target:Artist {id: $target_id})
MERGE (source)-[relationship:MEMBER_OF]->(target)
SET relationship.source = 'musicbrainz'
"""

_SEED_ALIAS_EDGE = """
MATCH (alias:Artist {id: $alias_id})
MATCH (artist:Artist {id: $artist_id})
MERGE (alias)-[:ALIAS_OF]->(artist)
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

# ── The label-DNA component's Neo4j half (gm-catalog-api-dl8.2) ─────────────
_SEED_NEO4J_LABEL_DNA_ENTITIES = """
UNWIND $labels AS label
MERGE (l:Label {id: label.id})
SET l.name = label.name,
    l.release_count = label.release_count,
    l.artist_count = label.artist_count
WITH count(*) AS _labels
UNWIND $artists AS artist
MERGE (a:Artist {id: artist.id}) SET a.name = artist.name
WITH count(*) AS _artists
UNWIND $releases AS release
MERGE (r:Release {id: release.id})
SET r.title = release.title,
    r.year = release.year,
    r.formats = release.formats,
    r.media_families = release.media_families
"""

_SEED_NEO4J_LABEL_DNA_GENRE = """
UNWIND $edges AS edge
MATCH (r:Release {id: edge.release_id})
MERGE (genre:Genre {name: edge.name})
MERGE (r)-[:IS]->(genre)
"""

_SEED_NEO4J_LABEL_DNA_STYLE = """
UNWIND $edges AS edge
MATCH (r:Release {id: edge.release_id})
MERGE (style:Style {name: edge.name})
MERGE (r)-[:IS]->(style)
"""

_SEED_NEO4J_LABEL_DNA_MEDIA = """
UNWIND $edges AS edge
MATCH (r:Release {id: edge.release_id})
MERGE (m:Medium {id: edge.medium_id})
SET m.family = edge.family, m.label = edge.label
MERGE (f:MediaFamily {name: edge.family})
MERGE (r)-[:ISSUED_ON]->(m)
MERGE (m)-[:IN_FAMILY]->(f)
"""

_SEED_NEO4J_COLLECTION_USERS = """
UNWIND $users AS user
MERGE (:User {id: user.id})
"""

_SEED_NEO4J_COLLECTIONS = """
UNWIND $rows AS row
MATCH (u:User {id: row.user_id})
MATCH (r:Release {id: row.release_id})
MERGE (u)-[edge:COLLECTED {instance_id: row.instance_id}]->(r)
SET edge.rating = row.rating, edge.folder_id = row.folder_id, edge.date_added = row.date_added
"""

_SEED_NEO4J_WANTS = """
UNWIND $rows AS row
MATCH (u:User {id: row.user_id})
MATCH (r:Release {id: row.release_id})
MERGE (u)-[edge:WANTS]->(r)
SET edge.rating = row.rating, edge.date_added = row.date_added
"""

_SEED_NEO4J_DERIVED_FROM = """
MATCH (master:Master {id: $master_id})
UNWIND $release_ids AS release_id
MATCH (release:Release {id: release_id})
MERGE (release)-[:DERIVED_FROM]->(master)
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


def all_artists() -> dict[str, str]:
    """Return every artist in the fixture, by id."""
    return {**ARTISTS, **AUTOCOMPLETE_ARTISTS, **RARITY_ARTISTS}


def all_labels() -> dict[str, str]:
    """Return every label in the fixture, by id."""
    return {LABEL_ID: LABEL_NAME, **AUTOCOMPLETE_LABELS, RARITY_LABEL_ID: RARITY_LABEL_NAME}


def all_masters() -> dict[str, str]:
    """Return every master in the fixture, by id."""
    return {MASTER_ID: MASTER_NAME, RARITY_MASTER_ID: RARITY_MASTER_NAME, RARITY_LONE_MASTER_ID: RARITY_LONE_MASTER_NAME}


def master_documents() -> dict[str, dict[str, Any]]:
    """Return the Discogs document for every master in the shared graph fixture."""
    return {
        master_id: {
            "title": title,
            **({"year": MASTER_YEARS[master_id]} if master_id in MASTER_YEARS else {}),
            **({"artists": [{"id": int(artist_id)} for artist_id in MASTER_ARTISTS[master_id]]} if master_id in MASTER_ARTISTS else {}),
        }
        for master_id, title in all_masters().items()
    }


def _release_node(release_id: str, document: dict[str, Any], media: dict[str, Any] | None) -> dict[str, Any]:
    """Return the Neo4j properties `graph.release` projects for one document."""
    return {
        "id": release_id,
        "title": document.get("title"),
        "year": document.get("year"),
        "formats": [entry["name"] for entry in document.get("formats", ()) if entry.get("name")],
        "media_families": list((media or {}).get("families", ())),
    }


async def seed_neo4j(driver: AsyncResilientNeo4jDriver) -> None:
    """Project the fixture into Neo4j as the graph enrichers would.

    The producer's own constraints and indexes are applied first, for the same reason
    `seed_postgres` applies the producer's DDL rather than a hand-rolled subset: the five
    `*_name_fulltext` indexes the autocomplete family reads are declared in
    `groovemap_schema.neo4j`, and a hand-written `CREATE FULLTEXT INDEX` here would be a
    second spelling of them that can drift. `db.awaitIndexes()` then makes the seed
    readable rather than merely committed.

    Every relationship below is derived from :func:`release_documents`, which is the same
    input `graph.bootstrap_fill()` projects the PostgreSQL edge tables from. That is what the
    rarity family needs and no earlier family did: the batch reads every release's degree, so
    a genre or credit edge that exists on one side only is a divergence even when no query
    names it.
    """
    failures = await create_neo4j_schema(driver)
    assert failures == 0, f"{failures} Neo4j schema statements failed against the integration container"
    await consume(driver, "MATCH (n) DETACH DELETE n")

    documents = release_documents()
    media_blocks = release_media()
    genre_counts = _tag_counts("genres")
    style_counts = _tag_counts("styles")
    label_counts = _label_release_counts()

    await consume(driver, _SEED_ARTIST_NODES, artists=[{"id": artist_id, "name": name} for artist_id, name in all_artists().items()])
    await consume(
        driver,
        _SEED_LABEL_NODES,
        labels=[{"id": label_id, "name": name, "release_count": label_counts.get(label_id, 0)} for label_id, name in all_labels().items()],
    )
    await consume(
        driver,
        _SEED_MASTER_NODES,
        masters=[{"id": master_id, "title": document["title"], "year": document.get("year")} for master_id, document in master_documents().items()],
    )
    await consume(driver, _SEED_GENRE_NODES, genres=[{"name": name, "release_count": count} for name, count in genre_counts.items()])
    await consume(driver, _SEED_STYLE_NODES, styles=[{"name": name, "release_count": count} for name, count in style_counts.items()])
    await consume(driver, _SEED_PERSON_NODES, people=list(AUTOCOMPLETE_PEOPLE))
    await consume(driver, _SEED_MEDIUM_NODES, mediums=[{"id": RARITY_MEDIUM_ID, "family": RARITY_MEDIUM_FAMILY}])

    await consume(
        driver,
        _SEED_RELEASE_NODES,
        releases=[_release_node(release_id, document, media_blocks.get(release_id)) for release_id, document in documents.items()],
    )

    await consume(
        driver,
        _SEED_BY_EDGES,
        edges=[
            {"release_id": release_id, "artist_id": str(entry["id"])}
            for release_id, document in documents.items()
            for entry in document.get("artists", ())
        ],
    )
    await consume(
        driver,
        _SEED_MASTER_BY_EDGES,
        edges=[
            {"master_id": master_id, "artist_id": str(entry["id"])}
            for master_id, document in master_documents().items()
            for entry in document.get("artists", ())
        ],
    )
    await consume(
        driver,
        _SEED_ON_EDGES,
        edges=[
            {"release_id": release_id, "label_id": str(entry["id"])}
            for release_id, document in documents.items()
            for entry in document.get("labels", ())
        ],
    )
    await consume(
        driver,
        _SEED_GENRE_EDGES,
        edges=[{"release_id": release_id, "name": name} for release_id, document in documents.items() for name in document.get("genres", ())],
    )
    await consume(
        driver,
        _SEED_STYLE_EDGES,
        edges=[{"release_id": release_id, "name": name} for release_id, document in documents.items() for name in document.get("styles", ())],
    )
    await consume(
        driver,
        _SEED_MASTER_EDGES,
        edges=[
            {"release_id": release_id, "master_id": document["master_id"]} for release_id, document in documents.items() if document.get("master_id")
        ],
    )
    await consume(
        driver,
        _SEED_ISSUED_ON_EDGES,
        edges=[
            {"release_id": release_id, "medium_id": item["medium"], "source": "discogs", "qty": item.get("qty", 1)}
            for release_id, media in media_blocks.items()
            for item in media["items"]
        ],
    )
    await consume(
        driver,
        _SEED_CREDITED_ON_EDGES,
        edges=[
            {"release_id": release_id, "person_name": entry["name"], "role": entry.get("role")}
            for release_id, document in documents.items()
            for entry in document.get("extraartists", ())
        ],
    )
    await consume(driver, _SEED_COLLECTION_EDGES, user_id=RARITY_USER_ID, collected=[RARITY_COLLECTED_RELEASE_ID])
    await consume(driver, _SEED_WANTLIST_EDGES, user_id=RARITY_USER_ID, wanted=[RARITY_COLLECTED_RELEASE_ID])
    await consume(
        driver,
        _SEED_MB_ARTIST_PROPERTIES,
        artists=[{**artist, "discogs_id": str(artist["discogs_id"])} for artist in MUSICBRAINZ_ARTISTS],
    )
    await consume(
        driver,
        _SEED_MB_ENTITY_PROPERTIES,
        label_id=LABEL_ID,
        label_mbid=MB_LABEL,
        release_id="101",
        release_mbid=MB_RELEASE,
    )
    await consume(driver, _SEED_DISCOGS_MEMBER_EDGE, member_id="1", group_id="2")
    await consume(
        driver,
        _SEED_MB_EDGE,
        source_id="1",
        target_id="2",
    )
    await consume(driver, _SEED_ALIAS_EDGE, alias_id=PATH_ALIAS_ARTIST_ID, artist_id="1204")

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
    # ── label-DNA component (gm-catalog-api-dl8.2) ──────────────────────────
    await consume(
        driver,
        _SEED_NEO4J_LABEL_DNA_ENTITIES,
        labels=[
            {
                "id": label_id,
                "name": name,
                "release_count": sum(release["label"] == label_id for release in LABEL_DNA_RELEASES.values()),
                "artist_count": len(
                    {artist_id for release in LABEL_DNA_RELEASES.values() if release["label"] == label_id for artist_id in release["artists"]}
                ),
            }
            for label_id, name in LABEL_DNA_LABELS.items()
        ],
        artists=[{"id": artist_id, "name": name} for artist_id, name in LABEL_DNA_ARTISTS.items()],
        releases=[
            {
                "id": release_id,
                "title": f"Label DNA Release {release_id}",
                "year": release["year"],
                "formats": release["formats"],
                "media_families": release["media"]["families"],
            }
            for release_id, release in LABEL_DNA_RELEASES.items()
        ],
    )
    await consume(driver, _SEED_NEO4J_BY, edges=label_dna_edges("artists", "artist_id"))
    await consume(
        driver,
        "MATCH (alias:Artist {id: $alias_id}), (primary:Artist {id: $primary_id}) MERGE (alias)-[:ALIAS_OF]->(primary)",
        alias_id=ALIAS_ARTIST_ID,
        primary_id=PRIMARY_ARTIST_ID,
    )
    await consume(
        driver,
        _SEED_NEO4J_ON,
        edges=[{"release_id": release_id, "label_id": release["label"]} for release_id, release in LABEL_DNA_RELEASES.items()],
    )
    await consume(driver, _SEED_NEO4J_LABEL_DNA_GENRE, edges=label_dna_edges("genres", "name"))
    await consume(driver, _SEED_NEO4J_LABEL_DNA_STYLE, edges=label_dna_edges("styles", "name"))
    await consume(driver, _SEED_NEO4J_LABEL_DNA_MEDIA, edges=label_dna_media_edges())
    await consume(
        driver,
        _SEED_NEO4J_EXPLORE_TAGS,
        rows=[
            {"id": _TAGGED_RELEASE_ID, "genres": [GENRE_NAME], "styles": [STYLE_NAME]},
            *({"id": release_id, "genres": release["genres"], "styles": release["styles"]} for release_id, release in AUTOCOMPLETE_RELEASES.items()),
        ],
    )
    for cypher in _SEED_NEO4J_EXPLORE_COUNTERS:
        await consume(driver, cypher)
    await consume(driver, "MERGE (master:Master {id: $master_id}) SET master.title = 'Collection Master'", master_id=COLLECTION_MASTER_ID)
    await consume(
        driver,
        _SEED_NEO4J_DERIVED_FROM,
        master_id=COLLECTION_MASTER_ID,
        release_ids=[release_id for release_id, release in LABEL_DNA_RELEASES.items() if release["master_id"]],
    )
    await consume(
        driver,
        _SEED_NEO4J_COLLECTION_USERS,
        users=[{"id": user_id} for user_id in (COLLECTION_USER_ID, COLLECTION_OTHER_USER_ID, COLLECTION_THIRD_USER_ID, COLLECTION_RECOMMEND_USER_ID)],
    )
    await consume(driver, _SEED_NEO4J_COLLECTIONS, rows=list(COLLECTION_ROWS))
    await consume(driver, _SEED_NEO4J_WANTS, rows=list(WANT_ROWS))
    # ── end label-DNA component ─────────────────────────────────────────────
    await consume(driver, _AWAIT_NEO4J_INDEXES)


async def seed_postgres(pool: AsyncPostgreSQLPool) -> None:
    """Write the fixture as Discogs documents, then project it into the graph relations.

    The documents are still the whole input — nothing is written to a `graph` relation by
    hand. They are not the whole projection any more, though: the loader-owned relations
    are tables from the phase 2 schema revision onward, so `graph.bootstrap_fill()` runs
    afterwards to derive them from the documents just written. It truncates first, so a
    re-seed converges rather than accumulating, and it covers every edge relation the rarity
    batch reads as well as the `graph.genre`, `graph.style`, and `graph.person` vertices the
    full-text component needs — including the counter relations behind `l.release_count`,
    `g.release_count`, `a.degree` and `graph.release_degree`.

    Three things here are not documents and are not projected by the fill. The collection and
    wantlist rows are the live half of `graph.release_degree`, and Neo4j carries them as
    `COLLECTED` and `WANTS` edges. `insights.release_rarity` is what the two vertex lookups
    page over, and `insights.community_counts` is what the batch's `collection_prevalence`
    reads; both are PostgreSQL on either backend and are shared input rather than one side's
    half of the comparison.
    """
    documents = release_documents()
    media_blocks = release_media()
    stored_ids = [row["release_id"] for row in RARITY_STORED_ROWS]
    community_ids = [row[0] for row in RARITY_COMMUNITY_COUNTS]

    async with pool.connection() as conn, conn.cursor() as cursor:
        await cursor.execute(_TRUNCATE_ENTITIES)
        await cursor.execute(_TRUNCATE_MUSICBRAINZ)
        for artist_id, name in all_artists().items():
            document: dict[str, Any] = {"name": name}
            if artist_id == "1":
                # The MusicBrainz MEMBER_OF edge overlays this existing Discogs edge.
                # Both stores therefore count one relationship in artist_degree before
                # the enricher stamps it as MusicBrainz, rather than creating a second edge.
                document["groups"] = [{"id": 2, "name": ARTISTS["2"]}]
            if artist_id == "1204":
                document["aliases"] = [{"id": int(PATH_ALIAS_ARTIST_ID), "name": ARTISTS[PATH_ALIAS_ARTIST_ID]}]
            await cursor.execute(_SEED_ARTIST, (artist_id, "parity-fixture", json.dumps(document)))
        for label_id, name in all_labels().items():
            await cursor.execute(_SEED_LABEL, (label_id, "parity-fixture", json.dumps({"name": name})))
        for master_id, document in master_documents().items():
            await cursor.execute(_SEED_MASTER, (master_id, "parity-fixture", json.dumps(document)))
        for release_id, document in documents.items():
            media = media_blocks.get(release_id)
            await cursor.execute(
                _SEED_RELEASE_WITH_MEDIA,
                (release_id, "parity-fixture", json.dumps(document), json.dumps(media) if media is not None else None),
            )

        for artist in MUSICBRAINZ_ARTISTS:
            await cursor.execute(
                _SEED_MB_ARTIST,
                (
                    artist["mbid"],
                    artist["name"],
                    artist["sort_name"],
                    artist["type"],
                    artist["gender"],
                    artist["begin_date"],
                    artist["end_date"],
                    artist["area"],
                    artist["begin_area"],
                    artist["disambiguation"],
                    artist["discogs_id"],
                ),
            )
        await cursor.execute(_SEED_MB_LABEL, (MB_LABEL, LABEL_NAME, int(LABEL_ID)))
        await cursor.execute(_SEED_MB_RELEASE, (MB_RELEASE, "Release 101", 101))
        for relationship in MUSICBRAINZ_RELATIONSHIPS:
            await cursor.execute(
                _SEED_MB_RELATIONSHIP_ROW,
                (
                    relationship["source_mbid"],
                    relationship["target_mbid"],
                    relationship["raw_type"],
                    relationship["begin_date"],
                    relationship["end_date"],
                    json.dumps(relationship["attributes"]),
                ),
            )
        await cursor.execute(_SEED_MB_LINK, (MB_ARTIST_ONE, *MB_LINK))

        await cursor.execute(_SEED_USER, (RARITY_USER_ID, RARITY_USER_EMAIL))
        await cursor.execute(_CLEAR_COLLECTION, (RARITY_USER_ID,))
        await cursor.execute(_CLEAR_WANTLIST, (RARITY_USER_ID,))
        await cursor.execute(_SEED_RARITY_COLLECTION, (RARITY_USER_ID, int(RARITY_COLLECTED_RELEASE_ID), 1))
        await cursor.execute(_SEED_RARITY_WANTLIST, (RARITY_USER_ID, int(RARITY_COLLECTED_RELEASE_ID)))

        await cursor.execute(_CLEAR_STORED_RARITY, (stored_ids,))
        for row in RARITY_STORED_ROWS:
            await cursor.execute(
                _SEED_STORED_RARITY,
                (row["release_id"], row["title"], row["artist_name"], row["year"], row["rarity_score"], row["tier"], row["hidden_gem_score"]),
            )
        await cursor.execute(_CLEAR_COMMUNITY_COUNTS, (community_ids,))
        for release_id, have, want in RARITY_COMMUNITY_COUNTS:
            await cursor.execute(_SEED_COMMUNITY_COUNTS, (release_id, have, want))

        # ── credits component (gm-catalog-api-dl8.1) ─────────────────────────
        for artist_id, name in CREDITS_ARTISTS.items():
            await cursor.execute(_SEED_ARTIST, (artist_id, "parity-fixture", json.dumps({"name": name})))
        for label_id, name in CREDITS_LABELS.items():
            await cursor.execute(_SEED_LABEL, (label_id, "parity-fixture", json.dumps({"name": name})))
        for release_id, release in CREDITS_RELEASES.items():
            await cursor.execute(_SEED_RELEASE, (release_id, "parity-fixture", json.dumps(_release_document(release_id, release))))
        # ── end credits component ────────────────────────────────────────────
        # ── label-DNA component (gm-catalog-api-dl8.2) ──────────────────────
        for artist_id, name in LABEL_DNA_ARTISTS.items():
            document = {"name": name}
            if artist_id == PRIMARY_ARTIST_ID:
                document["aliases"] = [{"id": int(ALIAS_ARTIST_ID)}]
            await cursor.execute(_SEED_ARTIST, (artist_id, "parity-fixture", json.dumps(document)))
        for label_id, name in LABEL_DNA_LABELS.items():
            await cursor.execute(_SEED_LABEL, (label_id, "parity-fixture", json.dumps({"name": name})))
        for release_id, release in LABEL_DNA_RELEASES.items():
            document = {
                "title": f"Label DNA Release {release_id}",
                "year": release["year"],
                "artists": [{"id": int(artist_id)} for artist_id in release["artists"]],
                "labels": [{"id": int(release["label"])}],
                "genres": release["genres"],
                "styles": release["styles"],
                "formats": [{"name": name} for name in release["formats"]],
                "master_id": int(release["master_id"]) if release["master_id"] else None,
            }
            if release_id == "1201":
                document.update(
                    identifiers={"items": [{"type": "Barcode", "value": "1201-TEST"}]},
                    companies={"items": [{"name": "Fixture Pressing"}]},
                    country="US",
                )
            await cursor.execute(
                _SEED_LABEL_DNA_RELEASE,
                (release_id, "parity-fixture", json.dumps(document), json.dumps(release["media"])),
            )
        await cursor.execute(_SEED_MASTER, (COLLECTION_MASTER_ID, "parity-fixture", json.dumps({"title": "Collection Master"})))
        for user_id in (COLLECTION_USER_ID, COLLECTION_OTHER_USER_ID, COLLECTION_THIRD_USER_ID, COLLECTION_RECOMMEND_USER_ID):
            await cursor.execute(_SEED_USER, (user_id, f"{user_id[-1]}@fixture.invalid"))
        for row in COLLECTION_ROWS:
            await cursor.execute(
                _SEED_COLLECTION,
                (
                    row["user_id"],
                    row["release_id"],
                    row["instance_id"],
                    row["rating"],
                    row["folder_id"],
                    row["date_added"],
                    json.dumps(LABEL_DNA_RELEASES[row["release_id"]]["media"]),
                ),
            )
        for row in WANT_ROWS:
            await cursor.execute(_SEED_WANT, (row["user_id"], row["release_id"], row["rating"], row["date_added"]))
        # ── end label-DNA component ─────────────────────────────────────────
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
