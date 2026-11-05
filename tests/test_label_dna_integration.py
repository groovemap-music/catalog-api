"""Label DNA and similar labels read stored counts and HAS_STYLE, with a live fallback that gives the same answer (audit H9).

``gm-catalog-api-j4ml``: identity recounted every release of the label, similar labels scanned every release of the label's top
styles, and decades and active years were two reads of the same years. The enricher now writes ``Label.release_count`` and
``artist_count`` and ``(:Label)-[:HAS_STYLE {release_count}]->(:Style)``. Each stored form is compared with the live one on a
real Neo4j, with no per-style cap. Nothing here touches a shared database.
"""

from __future__ import annotations

import pytest
from common import AsyncResilientNeo4jDriver

from api.queries import label_dna_queries as q
from api.queries.helpers import run_query
from tests.test_real_databases import neo4j_driver


__all__ = ["neo4j_driver"]

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

# Target label T has 8 releases across years; labels L1..L5 share its styles; X shares none. Releases carry 1-2 styles each.
_SEEDS = [
    "CREATE (:Label {id: 'T', name: 'Target'}), (:Label {id: 'X', name: 'Unrelated'}), (:Genre {name: 'G'}), (:Genre {name: 'H'})"
    " WITH 1 AS one UNWIND range(1, 6) AS n CREATE (:Style {name: 'S' + toString(n)})",
    # Target T: 8 releases, 1980..2004, one style each and a second on the even ones; four artists.
    """
    MATCH (t:Label {id: 'T'}), (g:Genre {name: 'G'})
    UNWIND range(1, 8) AS i
    CREATE (r:Release {id: 'tr' + toString(i), year: 1980 + (i * 7) % 25}), (r)-[:ON]->(t), (r)-[:IS]->(g)
    MERGE (a:Artist {id: 'ta' + toString(i % 4)})
    CREATE (r)-[:BY]->(a)
    WITH r, i
    MATCH (s:Style {name: 'S' + toString(1 + i % 6)})
    CREATE (r)-[:IS]->(s)
    """,
    """
    MATCH (r:Release)-[:ON]->(:Label {id: 'T'}) WHERE toInteger(substring(r.id, 2)) % 2 = 0
    MATCH (s:Style {name: 'S' + toString(1 + (toInteger(substring(r.id, 2)) + 1) % 6)})
    CREATE (r)-[:IS]->(s)
    """,
    # Five other labels with 7, 10, 13, 16 and 19 releases, each in one style and the genre G.
    """
    MATCH (g:Genre {name: 'G'})
    UNWIND range(1, 5) AS n
    CREATE (l:Label {id: 'L' + toString(n), name: 'Label ' + toString(n)})
    WITH l, n, g
    UNWIND range(1, 4 + n * 3) AS k
    CREATE (r:Release {id: l.id + '-' + toString(k), year: 1990 + k % 15}), (r)-[:ON]->(l), (r)-[:IS]->(g)
    WITH r, k, n
    MATCH (s:Style {name: 'S' + toString(1 + (k + n) % 6)})
    CREATE (r)-[:IS]->(s)
    """,
]
_STORE = [
    "MATCH (l:Label) SET l.release_count = COUNT { (l)<-[:ON]-(:Release) }",
    "MATCH (l:Label) SET l.artist_count = COUNT { MATCH (l)<-[:ON]-(r:Release)-[:BY]->(a:Artist) RETURN DISTINCT a }",
    "MATCH (l:Label)<-[:ON]-(r:Release)-[:IS]->(s:Style) WITH l, s, count(DISTINCT r) AS c MERGE (l)-[h:HAS_STYLE]->(s) SET h.release_count = c",
]


async def _seed(driver: AsyncResilientNeo4jDriver) -> None:
    await run_query(driver, "MATCH (n) DETACH DELETE n", database="neo4j", timeout=None)
    for statement in _SEEDS:
        await run_query(driver, statement, database="neo4j", timeout=None)


async def _store(driver: AsyncResilientNeo4jDriver) -> None:
    for statement in _STORE:
        await run_query(driver, statement, database="neo4j", timeout=None)


async def test_identity_from_stored_counts_equals_the_live_count(neo4j_driver: AsyncResilientNeo4jDriver) -> None:
    await _seed(neo4j_driver)
    live = await q.get_label_identity(neo4j_driver, "T")
    await _store(neo4j_driver)
    stored = await q.get_label_identity(neo4j_driver, "T")
    assert stored == live
    assert live["release_count"] == 8
    assert await q.get_label_identity(neo4j_driver, "nope") is None


async def test_identity_does_not_scan_when_the_counts_are_stored(neo4j_driver: AsyncResilientNeo4jDriver) -> None:
    await _seed(neo4j_driver)
    await _store(neo4j_driver)
    await run_query(neo4j_driver, "MATCH (:Release)-[e:ON]->(:Label {id: 'T'}) DELETE e", database="neo4j", timeout=None)
    assert (await q.get_label_identity(neo4j_driver, "T"))["release_count"] == 8  # the property, not the edges


async def test_stored_candidates_equal_the_live_candidates_with_no_per_style_cap(neo4j_driver: AsyncResilientNeo4jDriver) -> None:
    await _seed(neo4j_driver)
    live = await q.get_candidate_labels_genre_vectors(neo4j_driver, "T")
    await _store(neo4j_driver)
    stored = await q.get_candidate_labels_genre_vectors(neo4j_driver, "T")
    assert live, "the seed must produce candidates"
    key = lambda rows: sorted((r["label_id"], r["release_count"], sorted((g["name"], g["count"]) for g in r["genres"])) for r in rows)  # noqa: E731
    assert key(stored) == key(live)


async def test_stored_candidate_scores_equal_the_live_scores(neo4j_driver: AsyncResilientNeo4jDriver) -> None:
    await _seed(neo4j_driver)
    live = await run_query(neo4j_driver, q._CANDIDATES_LIVE, database="neo4j", label_id="T", min_releases=1)
    await _store(neo4j_driver)
    stored = await run_query(neo4j_driver, q._CANDIDATES_STORED, database="neo4j", label_id="T", min_releases=1)
    assert live and [(r["label_id"], r["total_shared"]) for r in stored] == [(r["label_id"], r["total_shared"]) for r in live]


async def test_year_reads_merge_to_the_old_decades_and_active_years(neo4j_driver: AsyncResilientNeo4jDriver) -> None:
    await _seed(neo4j_driver)
    old_decades = await run_query(
        neo4j_driver,
        "MATCH (r:Release)-[:ON]->(l:Label {id: 'T'}) WHERE r.year > 0 WITH (r.year / 10) * 10 AS decade, count(DISTINCT r) AS count RETURN decade, count ORDER BY decade",
        database="neo4j",
    )
    old_years = [
        r["year"]
        for r in await run_query(
            neo4j_driver,
            "MATCH (r:Release)-[:ON]->(l:Label {id: 'T'}) WHERE r.year > 0 RETURN DISTINCT r.year AS year ORDER BY year",
            database="neo4j",
        )
    ]
    assert await q.get_label_decade_profile(neo4j_driver, "T") == old_decades
    assert await q.get_label_active_years(neo4j_driver, "T") == old_years
    profile = await q.get_label_full_profile(neo4j_driver, "T")
    assert profile["decades"] == old_decades and profile["years"] == old_years


async def test_the_merged_genre_and_style_read_equals_the_two_separate_reads(neo4j_driver: AsyncResilientNeo4jDriver) -> None:
    await _seed(neo4j_driver)
    genres, styles = await q.get_label_genre_style_profile(neo4j_driver, "T")
    assert genres == await q.get_label_genre_profile(neo4j_driver, "T")
    assert sorted(map(str, styles)) == sorted(map(str, await q.get_label_style_profile(neo4j_driver, "T")))  # ties in count may reorder
    assert genres and styles
    profile = await q.get_label_full_profile(neo4j_driver, "T")
    assert profile["genres"] == genres
