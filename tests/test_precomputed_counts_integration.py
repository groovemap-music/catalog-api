"""Stored counts equal the live counts they replace, and the fallback agrees, on a real Neo4j.

Audit H5 and H7 (``gm-catalog-api-vus1``): /api/node and the /api/expand totals recounted live (Rock: 39.8 s,
112M db hits). They now read the enricher's properties. Each count is computed three ways here: by the
stored property, by the live fallback after the property is removed, and by hand. All three must agree.

Nothing here touches a shared database: ``just test-integration`` starts throwaway containers.
"""

from __future__ import annotations

from typing import Any

import pytest
from common import AsyncResilientNeo4jDriver

from api.queries import neo4j_queries as q
from api.queries.helpers import run_query
from tests.test_real_databases import neo4j_driver


__all__ = ["neo4j_driver"]

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

# Rock: r1 (Punk, label L1, artists A1 A2), r2 (Punk, L1, A1), r3 (no style, L2, A3).
# Jazz: r3, r4 (Bop, L2, A3 A4). So Rock has 3 releases, 3 artists, 2 labels, 1 style.
_GRAPH = """
CREATE (rock:Genre {name: 'Rock'}), (jazz:Genre {name: 'Jazz'}), (punk:Style {name: 'Punk'}), (bop:Style {name: 'Bop'}),
       (l1:Label {id: 'L1', name: 'One'}), (l2:Label {id: 'L2', name: 'Two'}),
       (a1:Artist {id: 'A1', name: 'A1'}), (a2:Artist {id: 'A2', name: 'A2'}), (a3:Artist {id: 'A3', name: 'A3'}), (a4:Artist {id: 'A4', name: 'A4'}),
       (r1:Release {id: 'r1'}), (r2:Release {id: 'r2'}), (r3:Release {id: 'r3'}), (r4:Release {id: 'r4'}),
       (r1)-[:IS]->(rock), (r1)-[:IS]->(punk), (r1)-[:ON]->(l1), (r1)-[:BY]->(a1), (r1)-[:BY]->(a2),
       (r2)-[:IS]->(rock), (r2)-[:IS]->(punk), (r2)-[:ON]->(l1), (r2)-[:BY]->(a1),
       (r3)-[:IS]->(rock), (r3)-[:IS]->(jazz), (r3)-[:ON]->(l2), (r3)-[:BY]->(a3),
       (r4)-[:IS]->(jazz), (r4)-[:IS]->(bop), (r4)-[:ON]->(l2), (r4)-[:BY]->(a3), (r4)-[:BY]->(a4)
"""

# The maintenance step the enricher runs: one live count per node and property.
_MAINTAIN = [
    "MATCH (n:Genre|Style|Label) SET n.release_count = COUNT { (n)<-[:IS|ON]-(:Release) }",
    "MATCH (g:Genre|Style) SET g.artist_count = COUNT { MATCH (r:Release)-[:IS]->(g), (r)-[:BY]->(a:Artist) RETURN DISTINCT a }",
    "MATCH (g:Genre|Style) SET g.label_count = COUNT { MATCH (r:Release)-[:IS]->(g), (r)-[:ON]->(l:Label) RETURN DISTINCT l }",
    "MATCH (g:Genre) SET g.style_count = COUNT { MATCH (r:Release)-[:IS]->(g), (r)-[:IS]->(s:Style) RETURN DISTINCT s }",
    "MATCH (s:Style) SET s.genre_count = COUNT { MATCH (r:Release)-[:IS]->(s), (r)-[:IS]->(g:Genre) RETURN DISTINCT g }",
    "MATCH (l:Label) SET l.artist_count = COUNT { MATCH (r:Release)-[:ON]->(l), (r)-[:BY]->(a:Artist) RETURN DISTINCT a }",
    "MATCH (l:Label) SET l.genre_count = COUNT { MATCH (r:Release)-[:ON]->(l), (r)-[:IS]->(g:Genre) RETURN DISTINCT g }",
]

_CASES: list[tuple[Any, str, int]] = [
    (q.count_genre_releases, "Rock", 3),
    (q.count_genre_artists, "Rock", 3),
    (q.count_genre_labels, "Rock", 2),
    (q.count_genre_styles, "Rock", 1),
    (q.count_style_releases, "Punk", 2),
    (q.count_style_artists, "Punk", 2),
    (q.count_style_labels, "Punk", 1),
    (q.count_style_genres, "Punk", 1),
    (q.count_label_releases, "Two", 2),
    (q.count_label_artists, "Two", 2),
    (q.count_label_genres, "Two", 2),
]


# L2 has two releases and no dated one: no span, zero dated releases, and not a placeholder.
_L2_LIVE: dict[str, Any] = {
    "id": "L2",
    "name": "Two",
    "release_count": 2,
    "is_placeholder": False,
    "first_year": None,
    "last_year": None,
    "years_active": None,
    "total_releases": 0,
    "peak_decade": None,
}
_L2_DATED: dict[str, Any] = {**_L2_LIVE, "first_year": 1980, "last_year": 1990, "years_active": 11, "total_releases": 2, "peak_decade": 1980}
_STORED_LABEL = (
    "MATCH (l:Label {id: 'L2'}) SET l.first_year = 1980, l.last_year = 1990, l.years_active = 11, l.total_releases = 2, l.peak_decade = 1980"
)


async def _seed(driver: AsyncResilientNeo4jDriver, *, maintained: bool) -> None:
    await run_query(driver, "MATCH (n) DETACH DELETE n", database="neo4j", timeout=None)
    await run_query(driver, _GRAPH, database="neo4j", timeout=None)
    if maintained:
        for statement in _MAINTAIN:
            await run_query(driver, statement, database="neo4j", timeout=None)


async def test_stored_totals_equal_the_hand_count(neo4j_driver: AsyncResilientNeo4jDriver) -> None:
    await _seed(neo4j_driver, maintained=True)
    for fn, name, expected in _CASES:
        assert await fn(neo4j_driver, name) == expected, fn.__name__


async def test_live_fallback_agrees_when_the_properties_are_missing(neo4j_driver: AsyncResilientNeo4jDriver) -> None:
    await _seed(neo4j_driver, maintained=False)
    for fn, name, expected in _CASES:
        assert await fn(neo4j_driver, name) == expected, fn.__name__


async def test_before_year_is_still_counted_live(neo4j_driver: AsyncResilientNeo4jDriver) -> None:
    await _seed(neo4j_driver, maintained=True)
    await run_query(neo4j_driver, "MATCH (r:Release) SET r.year = CASE r.id WHEN 'r1' THEN 1980 ELSE 2000 END", database="neo4j", timeout=None)
    assert await q.count_genre_releases(neo4j_driver, "Rock") == 3
    assert await q.count_genre_releases(neo4j_driver, "Rock", before_year=1990) == 1  # a stored total cannot answer this


async def test_details_equal_live_either_way(neo4j_driver: AsyncResilientNeo4jDriver) -> None:
    await _seed(neo4j_driver, maintained=False)
    live = (
        await q.get_genre_details(neo4j_driver, "Rock"),
        await q.get_style_details(neo4j_driver, "Punk"),
        await q.get_label_details(neo4j_driver, "L2"),
    )
    assert live == (
        {"id": "Rock", "name": "Rock", "artist_count": 3},
        {"id": "Punk", "name": "Punk", "artist_count": 2},
        _L2_LIVE,
    )
    await _seed(neo4j_driver, maintained=True)
    stored = (
        await q.get_genre_details(neo4j_driver, "Rock"),
        await q.get_style_details(neo4j_driver, "Punk"),
        await q.get_label_details(neo4j_driver, "L2"),
    )
    assert stored == live
    assert await q.get_genre_details(neo4j_driver, "Nope") is None


async def _dated(driver: AsyncResilientNeo4jDriver) -> None:
    await _seed(driver, maintained=True)
    await run_query(
        driver,
        "MATCH (r:Release) WHERE r.id IN ['r3', 'r4'] SET r.year = CASE r.id WHEN 'r3' THEN 1980 ELSE 1990 END",
        database="neo4j",
        timeout=None,
    )


async def _completion(driver: AsyncResilientNeo4jDriver, properties: str) -> None:
    await run_query(driver, f"CREATE (:ExtractionCompletion {{version: '20260801', {properties}}})", database="neo4j", timeout=None)


async def test_label_details_always_return_the_label_row_in_every_gate_state(neo4j_driver: AsyncResilientNeo4jDriver) -> None:
    """The label row survives every state of the ExtractionCompletion gate; only the longevity source changes."""
    # No ExtractionCompletion at all, and no dated release: the row is there, longevity empty.
    await _seed(neo4j_driver, maintained=True)
    assert await q.get_label_details(neo4j_driver, "L2") == _L2_LIVE

    # No ExtractionCompletion, dated releases: computed live.
    await _dated(neo4j_driver)
    assert await q.get_label_details(neo4j_driver, "L2") == _L2_DATED

    # stats_version < 3, even with stored values on the label: the stored values are ignored, live wins.
    await _dated(neo4j_driver)
    await run_query(neo4j_driver, _STORED_LABEL.replace("1980, l.last_year = 1990", "1900, l.last_year = 1901"), database="neo4j", timeout=None)
    await _completion(neo4j_driver, "maintained_at: datetime(), stats_version: 2")
    assert await q.get_label_details(neo4j_driver, "L2") == _L2_DATED

    # Not maintained (no maintained_at) at stats_version 3: live.
    await _dated(neo4j_driver)
    await _completion(neo4j_driver, "stats_version: 3")
    assert await q.get_label_details(neo4j_driver, "L2") == _L2_DATED

    # Maintained at stats_version 3 but this label was not swept (no total_releases): live.
    await _dated(neo4j_driver)
    await _completion(neo4j_driver, "maintained_at: datetime(), stats_version: 3")
    assert await q.get_label_details(neo4j_driver, "L2") == _L2_DATED

    # Maintained at stats_version 3 and swept: the stored values (distinguishable from live) are returned.
    await _dated(neo4j_driver)
    await _completion(neo4j_driver, "maintained_at: datetime(), stats_version: 3")
    await run_query(
        neo4j_driver, _STORED_LABEL.replace("1990", "2001").replace("years_active = 11", "years_active = 22"), database="neo4j", timeout=None
    )
    assert await q.get_label_details(neo4j_driver, "L2") == {**_L2_DATED, "last_year": 2001, "years_active": 22}

    # An absent label is still None.
    assert await q.get_label_details(neo4j_driver, "NOPE") is None


async def test_stored_series_gate_yields_a_row_without_an_extraction_completion(neo4j_driver: AsyncResilientNeo4jDriver) -> None:
    """The trends gate subquery also yields exactly one row when no ExtractionCompletion exists (read live, not dropped)."""
    await _seed(neo4j_driver, maintained=True)
    record = await q.run_single(neo4j_driver, q._STORED_SERIES % {"label": "Genre"}, name="Rock")
    assert record is not None
    assert record["maintained"] is False
    assert record["stats_version"] is None
