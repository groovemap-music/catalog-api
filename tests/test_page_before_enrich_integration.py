"""Collection, wantlist and gap pages cut the page first and still return the rows they always did.

Audit H8 (``gm-catalog-api-og3t``): these statements ran every OPTIONAL MATCH over every row, then
sorted and kept 50. The oracles below are the statements they replaced, kept verbatim, run on a real
Neo4j over releases with several artists, labels and genres, with wantlist rows, with equal collection
dates, across page offsets and with the wantlist and media filters. A mock cannot show that the
artist and label picked for a multi-artist release did not move.

Nothing here touches a shared database: ``just test-integration`` starts throwaway containers.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from common import AsyncResilientNeo4jDriver

from api.queries import gap_queries, user_queries
from api.queries.gap_queries import _build_filters
from api.queries.helpers import run_query
from tests.test_real_databases import neo4j_driver


__all__ = ["neo4j_driver"]

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_OLD_COLLECTION = """
MATCH (u:User {id: $user_id})-[c:COLLECTED]->(r:Release)
OPTIONAL MATCH (r)-[:BY]->(a:Artist)
OPTIONAL MATCH (r)-[:ON]->(l:Label)
OPTIONAL MATCH (r)-[:IS]->(g:Genre)
OPTIONAL MATCH (r)-[:IS]->(s:Style)
WITH r, c,
     collect(DISTINCT a.name)[0] AS artist_name,
     collect(DISTINCT l.name)[0] AS label_name,
     collect(DISTINCT g.name) AS genres,
     collect(DISTINCT s.name) AS styles
RETURN r.id AS id, r.title AS title, r.year AS year,
       r.catalog_number AS catalog_number,
       artist_name AS artist, label_name AS label,
       genres, styles,
       c.rating AS rating, c.date_added AS date_added,
       c.folder_id AS folder_id
ORDER BY c.date_added DESC, r.id
SKIP $offset LIMIT $limit
"""

_OLD_WANTLIST = (
    _OLD_COLLECTION.replace("COLLECTED", "WANTS")
    .replace("[c:", "[w:")
    .replace(", c,", ", w,")
    .replace("c.", "w.")
    .replace("w.folder_id AS folder_id", "")
)
_OLD_WANTLIST = _OLD_WANTLIST.replace("w.date_added AS date_added,\n       \n", "w.date_added AS date_added\n")

_OLD_LABEL_GAPS = """
MATCH (u:User {{id: $user_id}})
MATCH (l:Label {{id: $label_id}})<-[:ON]-(r:Release)
WHERE NOT (u)-[:COLLECTED]->(r)
{extra}
OPTIONAL MATCH (r)-[:BY]->(a:Artist)
OPTIONAL MATCH (r)-[:IS]->(g:Genre)
OPTIONAL MATCH (u)-[w:WANTS]->(r)
WITH r,
     collect(DISTINCT a.name)[0] AS artist_name,
     collect(DISTINCT g.name) AS genres,
     w IS NOT NULL AS on_wantlist
RETURN r.id AS id, r.title AS title, r.year AS year,
       r.formats AS formats, artist_name AS artist, genres, on_wantlist
ORDER BY r.year DESC, r.title
SKIP $offset LIMIT $limit
"""

_OLD_ARTIST_GAPS = """
MATCH (u:User {{id: $user_id}})
MATCH (a:Artist {{id: $artist_id}})<-[:BY]-(r:Release)
WHERE NOT (u)-[:COLLECTED]->(r)
{extra}
OPTIONAL MATCH (r)-[:ON]->(l:Label)
OPTIONAL MATCH (r)-[:IS]->(g:Genre)
OPTIONAL MATCH (u)-[w:WANTS]->(r)
WITH r,
     collect(DISTINCT l.name)[0] AS label_name,
     collect(DISTINCT g.name) AS genres,
     w IS NOT NULL AS on_wantlist
RETURN r.id AS id, r.title AS title, r.year AS year,
       r.formats AS formats, label_name AS label, genres, on_wantlist
ORDER BY r.year DESC, r.title
SKIP $offset LIMIT $limit
"""

_OLD_MASTER_GAPS = """
MATCH (u:User {{id: $user_id}})
MATCH (m:Master {{id: $master_id}})<-[:DERIVED_FROM]-(r:Release)
WHERE NOT (u)-[:COLLECTED]->(r)
{extra}
OPTIONAL MATCH (r)-[:BY]->(a:Artist)
OPTIONAL MATCH (r)-[:ON]->(l:Label)
OPTIONAL MATCH (r)-[:IS]->(g:Genre)
OPTIONAL MATCH (u)-[w:WANTS]->(r)
WITH r,
     collect(DISTINCT a.name)[0] AS artist_name,
     collect(DISTINCT l.name)[0] AS label_name,
     collect(DISTINCT g.name) AS genres,
     w IS NOT NULL AS on_wantlist
RETURN r.id AS id, r.title AS title, r.year AS year,
       r.formats AS formats, artist_name AS artist, label_name AS label,
       genres, on_wantlist
ORDER BY r.year DESC, r.title
SKIP $offset LIMIT $limit
"""

_SEED = """
CREATE (u:User {id: 'u1'}), (:Label {id: 'L1', name: 'Label One'}), (:Label {id: 'L2', name: 'Label Two'}),
       (:Master {id: 'M1'}), (:Medium {id: 'vinyl_12'})
WITH u
UNWIND range(1, 4) AS i CREATE (:Artist {id: 'A' + i, name: 'Artist ' + i})
WITH DISTINCT u
UNWIND ['Rock', 'Jazz', 'Folk'] AS gname CREATE (:Genre {name: gname})
WITH DISTINCT u
UNWIND ['Punk', 'Bop'] AS sname CREATE (:Style {name: sname})
WITH DISTINCT u
UNWIND range(1, 120) AS i
CREATE (r:Release {id: 'r' + right('000' + toString(i), 3), title: 'Title ' + right('000' + toString(i), 3),
                   year: 1960 + i % 25, catalog_number: 'CAT' + i, formats: ['LP'],
                   media_families: CASE WHEN i % 3 = 0 THEN ['vinyl'] ELSE ['cd'] END})
WITH u, r, i
MATCH (l1:Label {id: 'L1'}) CREATE (r)-[:ON]->(l1)
WITH u, r, i
CALL (r, i) { MATCH (l2:Label {id: 'L2'}) WHERE i % 4 = 0 CREATE (r)-[:ON]->(l2) }
CALL (r, i) { MATCH (a:Artist) WHERE a.id IN CASE i % 3 WHEN 0 THEN ['A1'] WHEN 1 THEN ['A2', 'A1'] ELSE ['A4', 'A3', 'A1'] END CREATE (r)-[:BY]->(a) }
CALL (r, i) { MATCH (g:Genre) WHERE i % 2 = 0 OR g.name = 'Rock' CREATE (r)-[:IS]->(g) }
CALL (r, i) { MATCH (s:Style) WHERE i % 5 <> 0 CREATE (r)-[:IS]->(s) }
CALL (r, i) { MATCH (m:Master {id: 'M1'}) WHERE i % 2 = 1 CREATE (r)-[:DERIVED_FROM]->(m) }
CALL (r, i) { MATCH (med:Medium {id: 'vinyl_12'}) WHERE i % 6 = 0 CREATE (r)-[:ISSUED_ON]->(med) }
CALL (u, r, i) { WITH u, r, i WHERE i % 4 = 1 CREATE (u)-[:COLLECTED {rating: i % 5, date_added: '2026-01-0' + (1 + i % 3), folder_id: i % 2}]->(r) }
CALL (u, r, i) { WITH u, r, i WHERE i % 6 = 2 CREATE (u)-[:WANTS {rating: 0, date_added: '2026-02-0' + (1 + i % 4)}]->(r) }
"""


async def _seed(driver: AsyncResilientNeo4jDriver) -> None:
    await run_query(driver, "MATCH (n) DETACH DELETE n", database="neo4j", timeout=None)
    await run_query(driver, _SEED, database="neo4j", timeout=None)


async def _passthrough(rows: list[dict[str, Any]], **_kwargs: object) -> list[dict[str, Any]]:
    return rows


_PAGES = [(50, 0), (50, 20), (7, 14), (50, 200)]


@pytest.mark.parametrize(("limit", "offset"), _PAGES)
async def test_collection_and_wantlist_pages_are_unchanged(neo4j_driver: AsyncResilientNeo4jDriver, limit: int, offset: int) -> None:
    await _seed(neo4j_driver)
    params = {"user_id": "u1", "limit": limit, "offset": offset}
    with patch.object(user_queries, "attach_release_identity", _passthrough):
        collection, collected_total = await user_queries.get_user_collection(neo4j_driver, "u1", limit=limit, offset=offset)
        wantlist, wanted_total = await user_queries.get_user_wantlist(neo4j_driver, "u1", limit=limit, offset=offset)

    assert collection == await run_query(neo4j_driver, _OLD_COLLECTION, database="neo4j", **params)
    assert wantlist == await run_query(neo4j_driver, _OLD_WANTLIST, database="neo4j", **params)
    assert collected_total == 30
    assert wanted_total == 20
    if offset == 0:
        assert any(len(row["genres"]) > 1 for row in wantlist)  # multi-genre rows are in the page
    if offset == 200:
        assert collection == wantlist == []


@pytest.mark.parametrize(("limit", "offset"), _PAGES)
@pytest.mark.parametrize(
    ("exclude_wantlist", "families", "mediums"),
    [(False, None, None), (True, None, None), (False, ["vinyl"], None), (True, None, ["vinyl_12"]), (False, ["cd"], ["vinyl_12"])],
)
async def test_gap_pages_are_unchanged(
    neo4j_driver: AsyncResilientNeo4jDriver,
    limit: int,
    offset: int,
    exclude_wantlist: bool,
    families: list[str] | None,
    mediums: list[str] | None,
) -> None:
    await _seed(neo4j_driver)
    extra = _build_filters(exclude_wantlist, families, mediums)
    params: dict[str, Any] = {"user_id": "u1", "limit": limit, "offset": offset}
    if families:
        params["families"] = families
    if mediums:
        params["mediums"] = mediums
    cases = [
        (gap_queries.get_label_gaps, "label_id", "L1", _OLD_LABEL_GAPS),
        (gap_queries.get_artist_gaps, "artist_id", "A1", _OLD_ARTIST_GAPS),
        (gap_queries.get_master_gaps, "master_id", "M1", _OLD_MASTER_GAPS),
    ]
    with patch.object(gap_queries, "attach_gap_identity", _passthrough):
        for function, key, value, old in cases:
            page, total = await function(neo4j_driver, "u1", value, limit, offset, exclude_wantlist, families, mediums)
            expected = await run_query(neo4j_driver, old.format(extra=extra), database="neo4j", **{**params, key: value})
            assert page == expected, function.__name__
            assert total >= len(page)
            if offset == 0 and not (exclude_wantlist or families or mediums):
                assert page, function.__name__  # a filter can legitimately empty a page; the unfiltered one cannot
