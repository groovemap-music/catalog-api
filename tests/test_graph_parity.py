"""What SQL/PGQ does that Cypher does not, proven on PostgreSQL 19.

Family-level parity — same rows, same order, same types, for every function the graph
backend seam exposes — is the harness in `tests/test_real_databases.py`, and the
collaborators family is registered there. This module is the part that is about the SQL
rather than about the family: that `graph.catalog` is really declared, what the fixture's
answer actually is, and what each of the four no-revisit predicates in the depth-2
pattern is holding back.

Both modules are opt-in and both need PostgreSQL 19, which is where SQL/PGQ and
`graph.catalog` exist. Run them with::

    just test-integration-pg19

which starts the digest-pinned `postgres:19beta3-alpine` image beside the usual Neo4j
container, applies the `groovemap-database-schema` initializer with
`SCHEMA_PROPERTY_GRAPH` enabled so the property graph is declared, and points
`scripts/test-integration.sh` at both files.

The no-revisit predicates
-------------------------
Neo4j applies relationship isomorphism to a `MATCH` path: no relationship may bind twice,
which silently forbids the two-hop pattern from walking back down the edge it arrived on.
SQL/PGQ's default is walk semantics, so `api/queries/network_pg_queries.py` writes the
four constraints out. Proving they are load-bearing needs care, because in the *assembled*
statement the anti-join hides them: every walk they forbid arrives at an artist who shares
a release with the anchor, and the anti-join drops exactly those. So the walk is probed
where it is observable — the two-hop pattern on its own, compared to the same walk in
Cypher — and the assembled statement is probed separately, which is where the masking is
pinned as the fact it is rather than left as a surprise.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from groovemap_schema.postgres import PROPERTY_GRAPH_NAME

from api.queries import credits_pg_queries as credits_postgres_backend
from api.queries import credits_queries as credits_neo4j_backend
from api.queries import network_pg_queries as postgres_backend
from api.queries import network_queries as neo4j_backend
from api.queries.helpers import run_query
from tests import graph_fixture
from tests.graph_fixture import (
    ANCHOR_ARTIST_ID,
    ARTISTS,
    CREDITS_RELEASES,
    DUAL_ROLE_RELEASE_ID,
    OVERFLOWING_RELEASE_ID,
    PROBE_ANCHOR_ARTIST_ID,
    RELEASES,
    SAME_AS_PERSON,
    THREE_CREDIT_RELEASE_ID,
    ParityBackends,
)
from tests.test_real_databases import ParityCall, assert_parity


pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest_asyncio.fixture
async def parity_backends() -> AsyncIterator[ParityBackends]:
    """Both engines, holding the shared fixture."""
    async with graph_fixture.seeded_backends() as backends:
        yield backends


async def test_the_property_graph_is_declared_on_the_integration_container(
    parity_backends: ParityBackends,
) -> None:
    """Everything below reads `graph.catalog`; this is the one test that says so out loud."""
    async with parity_backends.postgres.connection() as conn, conn.cursor() as cursor:
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


# ── What the fixture's answer is ─────────────────────────────────────────────
# The harness proves the two engines agree with each other. That is worthless if both
# agree on nothing, so the answer itself is pinned here, once per anchor.


async def test_the_depth_two_result_is_the_expected_shape(parity_backends: ParityBackends) -> None:
    """Artists 2-4 share three, two, and one release; 5-7 are bridged by three, two, and one.

    Every `collaboration_count` is distinct within its distance, so this list is the only
    order either engine may return.
    """
    expected = [
        {"artist_id": "2", "artist_name": "Three Shared", "distance": 1, "collaboration_count": 3},
        {"artist_id": "3", "artist_name": "Two Shared", "distance": 1, "collaboration_count": 2},
        {"artist_id": "4", "artist_name": "One Shared", "distance": 1, "collaboration_count": 1},
        {"artist_id": "5", "artist_name": "Three Bridges", "distance": 2, "collaboration_count": 3},
        {"artist_id": "6", "artist_name": "Two Bridges", "distance": 2, "collaboration_count": 2},
        {"artist_id": "7", "artist_name": "One Bridge", "distance": 2, "collaboration_count": 1},
    ]

    assert await neo4j_backend.get_multi_hop_collaborators(parity_backends.neo4j, ANCHOR_ARTIST_ID, depth=2, limit=50) == expected
    assert await postgres_backend.get_multi_hop_collaborators(parity_backends.postgres, ANCHOR_ARTIST_ID, depth=2, limit=50) == expected


async def test_the_predicate_component_reads_the_same_from_its_own_anchor(
    parity_backends: ParityBackends,
) -> None:
    """The three-credit release must not change what the anchor sees, only what a walk can do.

    Artist 10 is credited on release 201 alongside the anchor and the bridge, so it is a
    one-hop collaborator over one release and nothing else. Reporting it at distance 2 —
    reached by turning around inside 201 — is exactly the mistake the `far <> near`
    predicate prevents, and it does not happen here.
    """
    expected = [
        {"artist_id": "9", "artist_name": "Probe Bridge", "distance": 1, "collaboration_count": 2},
        {"artist_id": "10", "artist_name": "Probe Third Credit", "distance": 1, "collaboration_count": 1},
        {"artist_id": "11", "artist_name": "Probe Two Hop", "distance": 2, "collaboration_count": 1},
    ]

    assert await neo4j_backend.get_multi_hop_collaborators(parity_backends.neo4j, PROBE_ANCHOR_ARTIST_ID, depth=2, limit=50) == expected
    assert await postgres_backend.get_multi_hop_collaborators(parity_backends.postgres, PROBE_ANCHOR_ARTIST_ID, depth=2, limit=50) == expected


async def test_depth_one_excludes_every_two_hop_collaborator_on_both_backends(
    parity_backends: ParityBackends,
) -> None:
    cypher = await neo4j_backend.get_multi_hop_collaborators(parity_backends.neo4j, ANCHOR_ARTIST_ID, depth=1, limit=50)
    sqlpgq = await postgres_backend.get_multi_hop_collaborators(parity_backends.postgres, ANCHOR_ARTIST_ID, depth=1, limit=50)

    assert sqlpgq == cypher
    assert [row["artist_id"] for row in sqlpgq] == ["2", "3", "4"]
    assert all(row["distance"] == 1 for row in sqlpgq)


async def test_every_artist_in_the_fixture_agrees_on_both_backends(parity_backends: ParityBackends) -> None:
    """Parity from every vantage point in the graph, not only the two anchors.

    Ordering is not asserted here: read from a leaf, the fixture does produce ties on
    `(distance, collaboration_count)`, and neither implementation promises an order for
    those. The rows themselves must still match exactly, which is the part that is
    defined. The registered harness calls are the ones that assert order, and they are
    made from the two anchors the fixture keeps tie-free.
    """
    for artist_id in ARTISTS:
        cypher = await neo4j_backend.get_multi_hop_collaborators(parity_backends.neo4j, artist_id, depth=2, limit=50)
        sqlpgq = await postgres_backend.get_multi_hop_collaborators(parity_backends.postgres, artist_id, depth=2, limit=50)

        key = ("distance", "collaboration_count")
        assert [tuple(row[column] for column in key) for row in sqlpgq] == [tuple(row[column] for column in key) for row in cypher], (
            f"sort keys diverged for artist {artist_id}"
        )
        assert sorted(sqlpgq, key=lambda row: str(row["artist_id"])) == sorted(cypher, key=lambda row: str(row["artist_id"])), (
            f"rows diverged for artist {artist_id}"
        )
        assert await postgres_backend.count_multi_hop_collaborators(
            parity_backends.postgres, artist_id, depth=2
        ) == await neo4j_backend.count_multi_hop_collaborators(parity_backends.neo4j, artist_id, depth=2)


# ── The four no-revisit predicates ───────────────────────────────────────────

# The predicates as they are written in `network_pg_queries._INDIRECT_COLLABORATORS`. The
# text is taken from the module rather than restated, so a rewording there fails these
# tests loudly instead of quietly mutating nothing.
NO_REVISIT_PREDICATES: dict[str, str] = {
    "bridge<>anchor": "bridge.artist_id <> anchor.artist_id",
    "peer<>anchor": "peer.artist_id <> anchor.artist_id",
    "peer<>bridge": "peer.artist_id <> bridge.artist_id",
    "far<>near": "far.release_id <> near.release_id",
}

# The walk each dropped predicate lets through, read from the probe anchor, as
# (collaborator, bridge) pairs. Each one is an artist pair the Cypher never produces:
#
#   bridge<>anchor  the walk goes down a release and straight back up to the anchor,
#                   then out again — a one-hop collaborator reported at distance 2.
#   peer<>anchor    it returns to the anchor over a second shared release, reporting the
#                   anchor as its own collaborator.
#   peer<>bridge    it binds the same far edge twice, reporting the bridge as the artist
#                   reached through the bridge.
#   far<>near       it turns around inside one release and reports a third artist credited
#                   on it. Only a release crediting three artists has a third artist to
#                   reach, which is why the fixture has one.
ADMITTED_BY_DROPPING: dict[str, set[tuple[str, str]]] = {
    "bridge<>anchor": {("9", "8"), ("10", "8")},
    "peer<>anchor": {("8", "9")},
    "peer<>bridge": {("9", "9")},
    "far<>near": {("9", "10")},
}

# The two-hop pattern on its own, before the grouping and the anti-join are wrapped around
# it. This is the level the predicates act at, so it is the level they are probed at.
_TWO_HOP_WALK = postgres_backend._INDIRECT_COLLABORATORS

# The same walk in Cypher, projecting the same two columns. Neo4j's relationship
# isomorphism is not written down anywhere in it — that is the point.
_CYPHER_TWO_HOP_WALK = """
MATCH (a:Artist {id: $artist_id})<-[:BY]-(:Release)-[:BY]->(mid:Artist)<-[:BY]-(:Release)-[:BY]->(hop2:Artist)
WHERE mid <> a AND hop2 <> a
RETURN hop2.id AS collaborator_id, mid.id AS bridge_id
"""


def _walk_dropping(predicate_name: str) -> str:
    """The two-hop walk with one predicate neutralised."""
    predicate = NO_REVISIT_PREDICATES[predicate_name]
    assert predicate in _TWO_HOP_WALK, f"{predicate!r} is no longer in the two-hop pattern"
    return _TWO_HOP_WALK.replace(predicate, "true")


def _walk_negating(predicate_name: str) -> str:
    """The two-hop walk with one predicate inverted."""
    predicate = NO_REVISIT_PREDICATES[predicate_name]
    assert predicate in _TWO_HOP_WALK, f"{predicate!r} is no longer in the two-hop pattern"
    return _TWO_HOP_WALK.replace(predicate, predicate.replace("<>", "=", 1))


def _assembled_with(walk: str) -> str:
    """`MULTI_HOP_COLLABORATORS_SQL` rebuilt around a mutated two-hop walk."""
    assembled = postgres_backend.MULTI_HOP_COLLABORATORS_SQL.replace(_TWO_HOP_WALK.rstrip(), walk.rstrip())
    assert assembled != postgres_backend.MULTI_HOP_COLLABORATORS_SQL, "the mutated walk was not substituted"
    return assembled


async def _sqlpgq_pairs(pool: Any, walk: str, artist_id: str) -> list[tuple[str, str]]:
    """Run a two-hop walk and return its (collaborator, bridge) pairs."""
    async with pool.connection() as conn, conn.cursor() as cursor:
        await cursor.execute(walk, {"artist_id": artist_id})
        rows = await cursor.fetchall()
    return sorted((row[0], row[2]) for row in rows)


async def _cypher_pairs(driver: Any, artist_id: str) -> list[tuple[str, str]]:
    """The same pairs, walked by Neo4j."""
    rows = await run_query(driver, _CYPHER_TWO_HOP_WALK, artist_id=artist_id)
    return sorted((row["collaborator_id"], row["bridge_id"]) for row in rows)


async def _assembled_rows(pool: Any, sql: str, artist_id: str, *, depth: int = 2, limit: int = 50) -> list[dict[str, Any]]:
    """Run an assembled collaborators statement and map it the way the module does."""
    async with pool.connection() as conn, conn.cursor() as cursor:
        await cursor.execute(sql, {"artist_id": artist_id, "depth": depth, "limit": limit})
        rows = await cursor.fetchall()
    return [{"artist_id": row[0], "artist_name": row[1], "distance": row[2], "collaboration_count": row[3]} for row in rows]


async def test_the_fixture_carries_a_release_crediting_three_artists() -> None:
    """The `far <> near` predicate is unobservable without one, so this is load-bearing."""
    assert len(RELEASES[THREE_CREDIT_RELEASE_ID]) == 3
    assert PROBE_ANCHOR_ARTIST_ID in RELEASES[THREE_CREDIT_RELEASE_ID]


@pytest.mark.parametrize("anchor", [ANCHOR_ARTIST_ID, PROBE_ANCHOR_ARTIST_ID])
async def test_the_two_hop_walk_matches_the_cypher_walk_exactly(parity_backends: ParityBackends, anchor: str) -> None:
    """With all four predicates, SQL/PGQ walks what Neo4j walks — pair for pair.

    This is the control every drop below is measured against. The two engines reach the
    same set of (collaborator, bridge) pairs the same number of times, which is what makes
    an extra pair in a mutant attributable to the mutation.
    """
    sqlpgq = await _sqlpgq_pairs(parity_backends.postgres, _TWO_HOP_WALK, anchor)
    cypher = await _cypher_pairs(parity_backends.neo4j, anchor)

    assert sqlpgq == cypher


@pytest.mark.parametrize("predicate_name", sorted(NO_REVISIT_PREDICATES))
async def test_dropping_a_no_revisit_predicate_admits_a_walk_the_cypher_forbids(
    parity_backends: ParityBackends,
    predicate_name: str,
) -> None:
    """Each predicate is the only thing standing between the walk and a revisited edge.

    Drop one and SQL/PGQ reaches a (collaborator, bridge) pair Neo4j never reaches,
    because Neo4j's relationship isomorphism forbids the edge binding that produced it.
    """
    cypher = await _cypher_pairs(parity_backends.neo4j, PROBE_ANCHOR_ARTIST_ID)
    mutant = await _sqlpgq_pairs(parity_backends.postgres, _walk_dropping(predicate_name), PROBE_ANCHOR_ARTIST_ID)

    admitted = set(mutant) - set(cypher)
    assert admitted, f"dropping {predicate_name} changed nothing; the fixture cannot falsify it"
    assert admitted == ADMITTED_BY_DROPPING[predicate_name]


async def test_only_a_three_credit_release_exposes_the_far_near_predicate(parity_backends: ParityBackends) -> None:
    """Why the fixture grew a release crediting three artists.

    Turning around inside a release means leaving by a third credit. Read from the
    ordering component, where every release credits exactly two artists, dropping
    `far <> near` admits nothing at all — the only artists to turn around onto are the
    anchor and the bridge, and two other predicates already exclude them. Read from the
    predicate component it admits a pair immediately.
    """
    walk = _walk_dropping("far<>near")

    two_credit_control = await _sqlpgq_pairs(parity_backends.postgres, _TWO_HOP_WALK, ANCHOR_ARTIST_ID)
    two_credit_mutant = await _sqlpgq_pairs(parity_backends.postgres, walk, ANCHOR_ARTIST_ID)
    three_credit_control = await _sqlpgq_pairs(parity_backends.postgres, _TWO_HOP_WALK, PROBE_ANCHOR_ARTIST_ID)
    three_credit_mutant = await _sqlpgq_pairs(parity_backends.postgres, walk, PROBE_ANCHOR_ARTIST_ID)

    assert set(two_credit_mutant) == set(two_credit_control)
    assert set(three_credit_mutant) - set(three_credit_control) == ADMITTED_BY_DROPPING["far<>near"]


@pytest.mark.parametrize("predicate_name", sorted(NO_REVISIT_PREDICATES))
async def test_negating_a_no_revisit_predicate_makes_the_parity_harness_fail(
    parity_backends: ParityBackends,
    predicate_name: str,
) -> None:
    """Mutate one predicate in the assembled statement and the harness catches it.

    Negation rather than deletion, because deletion is what the next test is about: the
    anti-join absorbs a dropped predicate, so the statement that survives it is still at
    parity. An inverted predicate is not absorbed — it keeps only the walks the predicate
    was excluding, every one of which the anti-join then drops, and the two-hop half of
    the result disappears.
    """
    call = ParityCall("get_multi_hop_collaborators", (ANCHOR_ARTIST_ID,), {"depth": 2, "limit": 50})
    cypher = await neo4j_backend.get_multi_hop_collaborators(parity_backends.neo4j, ANCHOR_ARTIST_ID, depth=2, limit=50)
    mutant = await _assembled_rows(parity_backends.postgres, _assembled_with(_walk_negating(predicate_name)), ANCHOR_ARTIST_ID)

    assert_parity("collaborators", call, neo4j_result=cypher, postgres_result=cypher)
    with pytest.raises(pytest.fail.Exception, match="diverged between the two backends"):
        assert_parity("collaborators", call, neo4j_result=cypher, postgres_result=mutant)


@pytest.mark.parametrize("predicate_name", sorted(NO_REVISIT_PREDICATES))
async def test_the_anti_join_absorbs_a_dropped_predicate_in_the_assembled_statement(
    parity_backends: ParityBackends,
    predicate_name: str,
) -> None:
    """The masking, pinned as a fact rather than left to be rediscovered.

    Every walk the four predicates forbid ends at an artist who shares a release with the
    anchor — that is what "revisiting an edge" means here — and the anti-join drops
    exactly those artists from the two-hop branch. So the assembled statement stays at
    parity with the Cypher with any one of the predicates dropped, which is why the test
    above probes the walk instead of the statement.

    Keeping the predicates is still right: they are what makes the two-hop branch mean
    what it says on its own, and they stop the branch's `count(DISTINCT bridge_id)` from
    depending on the anti-join for its correctness. But a reader who expects deleting one
    to turn the suite red should find this instead of a mystery.
    """
    cypher = await neo4j_backend.get_multi_hop_collaborators(parity_backends.neo4j, PROBE_ANCHOR_ARTIST_ID, depth=2, limit=50)
    mutant = await _assembled_rows(
        parity_backends.postgres,
        _assembled_with(_walk_dropping(predicate_name)),
        PROBE_ANCHOR_ARTIST_ID,
    )

    assert mutant == cypher


# ── The credits family: what its parity calls cannot see (gm-catalog-api-dl8.1) ──
# Two claims the row-for-row harness is structurally unable to make, for two different
# reasons, and both are load-bearing.
#
# The first is a **cap**. `get_person_credits` reproduces `collect(DISTINCT a.name)[..3]`
# and `collect(DISTINCT l.name)[..1]`, and the only release either cap bites on names four
# artists and two labels. It cannot be a parity call: Cypher's `collect` has no defined
# order, so *which* three names survive is undefined on the Neo4j side, and comparing the
# two lists would be comparing two arbitrary choices. How many survive is not arbitrary on
# either side, so that is what is asserted — on both engines, against the same release.
#
# The second is a **guard**. `get_shared_credits` walks two `credited_on` edges into one
# release, and Neo4j's relationship isomorphism keeps them from being the same edge.
# SQL/PGQ's walk semantics do not, so the statement writes the constraint out. Unlike the
# collaborators pilot's four no-revisit predicates, nothing downstream masks this one — the
# harness's self-pair call fails outright without it — but the mutation is still probed
# here, because a reader deserves to see what the guard is holding back rather than only
# that something does.

_CREDITS_WALK_GUARD = "WHERE NOT (credit_one.person_name = credit_two.person_name AND credit_one.role = credit_two.role)"


def _shared_credits_without_the_guard() -> str:
    """The shared-credits statement with its walk-semantics guard removed."""
    assert _CREDITS_WALK_GUARD in credits_postgres_backend.SHARED_CREDITS_SQL, (
        "the guard this mutation removes is no longer spelled the way this test expects"
    )
    return credits_postgres_backend.SHARED_CREDITS_SQL.replace(_CREDITS_WALK_GUARD, "", 1)


async def _shared_release_ids(pool: Any, sql: str, person1: str, person2: str) -> list[str]:
    """Run a shared-credits statement and return the release ids it reports."""
    async with pool.connection() as conn, conn.cursor() as cursor:
        await cursor.execute(sql, {"person1": person1, "person2": person2})
        rows = await cursor.fetchall()
    return sorted(row[0] for row in rows)


async def test_the_fixture_carries_the_two_releases_the_credits_probes_need() -> None:
    """Both claims below are unobservable without them, so this is load-bearing."""
    overflowing = CREDITS_RELEASES[OVERFLOWING_RELEASE_ID]
    assert len(overflowing["artists"]) > 3, "no release credits enough artists for the [..3] cap to bite"
    assert len(overflowing["labels"]) > 1, "no release names enough labels for the [..1] cap to bite"

    credits = CREDITS_RELEASES[DUAL_ROLE_RELEASE_ID]["extraartists"]
    names = [credit["name"] for credit in credits]
    assert len(names) > len(set(names)), "no release credits one person twice, so one edge binding twice is unobservable"
    assert SAME_AS_PERSON in names, "the dual-role release does not also carry the SAME_AS person"


@pytest.mark.parametrize(("column", "cap"), [("artists", 3), ("labels", 1)])
async def test_both_engines_cap_the_collected_lists_at_the_same_length(
    parity_backends: ParityBackends,
    column: str,
    cap: int,
) -> None:
    """The `[..3]` and `[..1]` slices, asserted by length because order is undefined.

    The release this reads credits four artists and names two labels, and the person
    credited on it is deliberately not a `get_person_credits` parity call for exactly the
    reason this test exists: the row the two engines would be compared on differs in which
    names it kept, not in how many.
    """
    person = CREDITS_RELEASES[OVERFLOWING_RELEASE_ID]["extraartists"][0]["name"]
    neo4j_rows = await credits_neo4j_backend.get_person_credits(parity_backends.neo4j, person)
    postgres_rows = await credits_postgres_backend.get_person_credits(parity_backends.postgres, person)

    assert [row["release_id"] for row in neo4j_rows] == [OVERFLOWING_RELEASE_ID]
    assert [row["release_id"] for row in postgres_rows] == [OVERFLOWING_RELEASE_ID]
    assert len(neo4j_rows[0][column]) == cap
    assert len(postgres_rows[0][column]) == cap


async def test_dropping_the_shared_credits_guard_admits_a_walk_the_cypher_forbids(
    parity_backends: ParityBackends,
) -> None:
    """One `credited_on` edge binding to both halves of the pattern, as walk semantics allow.

    Asked for the releases one person shares with themselves, Neo4j answers with nothing:
    `c1` and `c2` are two relationship variables of one `MATCH`, so they may not bind the
    same relationship, and this person holds one role per release. Drop the guard and
    SQL/PGQ reports every release they are credited on.
    """
    cypher = await credits_neo4j_backend.get_shared_credits(parity_backends.neo4j, SAME_AS_PERSON, SAME_AS_PERSON)
    guarded = await _shared_release_ids(parity_backends.postgres, credits_postgres_backend.SHARED_CREDITS_SQL, SAME_AS_PERSON, SAME_AS_PERSON)
    mutant = await _shared_release_ids(parity_backends.postgres, _shared_credits_without_the_guard(), SAME_AS_PERSON, SAME_AS_PERSON)

    assert cypher == []
    assert guarded == []
    assert mutant, "dropping the guard changed nothing; the fixture cannot falsify it"

    credited_on = sorted(
        release_id for release_id, release in CREDITS_RELEASES.items() if any(credit["name"] == SAME_AS_PERSON for credit in release["extraartists"])
    )
    assert mutant == credited_on


async def test_the_shared_credits_guard_keeps_two_different_people_intact(
    parity_backends: ParityBackends,
) -> None:
    """The guard excludes an edge binding twice, not a person credited twice.

    A guard that also dropped the second of two roles one person holds on a shared release
    would pass the test above and quietly lose rows here, so the mutation is measured
    against a pair the guard must not touch at all.
    """
    call = ParityCall("get_shared_credits", (SAME_AS_PERSON, "Marlon Hale"))
    cypher = await credits_neo4j_backend.get_shared_credits(parity_backends.neo4j, SAME_AS_PERSON, "Marlon Hale")
    postgres = await credits_postgres_backend.get_shared_credits(parity_backends.postgres, SAME_AS_PERSON, "Marlon Hale")

    assert cypher, "the fixture stopped giving these two a shared release"
    assert_parity("credits", call, neo4j_result=cypher, postgres_result=postgres)
