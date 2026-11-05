"""/api/network/cluster ranks neighbours first and reads the primary genre after the cut, on a real Neo4j (audit H6, ``gm-catalog-api-575h``).

The old statement built every neighbour's genre histogram before the LIMIT. Its text is the oracle for the genre; the stored
``Artist.primary_genre`` (the enricher's: most releases, a tie to the alphabetically first genre, absent with no genre) and the
live computation must agree with it and with each other. Nothing here touches a shared database.
"""

from __future__ import annotations

import pytest
from common import AsyncResilientNeo4jDriver

from api.queries import network_queries as nq
from api.queries.helpers import run_query
from tests.test_real_databases import neo4j_driver


__all__ = ["neo4j_driver"]

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_OLD = """
MATCH (a:Artist {id: $artist_id})
MATCH (a)<-[:BY]-(r:Release)-[:BY]->(neighbor:Artist)
WHERE neighbor <> a
WITH a, neighbor, count(DISTINCT r) AS shared_releases
OPTIONAL MATCH (neighbor)<-[:BY]-(r2:Release)-[:IS]->(g:Genre)
WITH neighbor, shared_releases, g.name AS genre, count(r2) AS genre_count
ORDER BY genre_count DESC
WITH neighbor, shared_releases, collect(genre)[0] AS primary_genre
ORDER BY shared_releases DESC
LIMIT $limit
RETURN neighbor.id AS artist_id, shared_releases, primary_genre
"""

# Root R shares releases with n1..n6 (n_i shares i). Genres: n1 Jazz x3 + Rock x1 (Jazz); n2 Rock x2 + Blues x2 (a tie: Blues);
# n3 no genre at all; n4 Pop x1 (Pop); n5 Rock x4 + Jazz x1 (Rock); n6 Blues x1 (Blues).
_GRAPH = """
CREATE (root:Artist {id: 'R', name: 'Root'})
WITH root
UNWIND range(1, 6) AS i
CREATE (n:Artist {id: 'n' + toString(i), name: 'N' + toString(i)})
WITH root, n, i
UNWIND range(1, i) AS k
CREATE (s:Release {id: 'shared-' + n.id + '-' + toString(k)}), (s)-[:BY]->(root), (s)-[:BY]->(n)
"""
_GENRES = [
    ("n1", "Jazz", 3),
    ("n1", "Rock", 1),
    ("n2", "Rock", 2),
    ("n2", "Blues", 2),
    ("n4", "Pop", 1),
    ("n5", "Rock", 4),
    ("n5", "Jazz", 1),
    ("n6", "Blues", 1),
]


async def _seed(driver: AsyncResilientNeo4jDriver) -> None:
    await run_query(driver, "MATCH (n) DETACH DELETE n", database="neo4j", timeout=None)
    await run_query(driver, _GRAPH, database="neo4j", timeout=None)
    for who, genre, count in _GENRES:
        await run_query(
            driver,
            "MATCH (n:Artist {id: $who}) MERGE (g:Genre {name: $genre}) WITH n, g UNWIND range(1, $count) AS k "
            "CREATE (r:Release {id: $who + '-' + $genre + '-' + toString(k)}), (r)-[:BY]->(n), (r)-[:IS]->(g)",
            database="neo4j",
            timeout=None,
            who=who,
            genre=genre,
            count=count,
        )


def _by_artist(clusters: list[dict[str, object]]) -> dict[str, str]:
    return {m["artist_id"]: c["cluster_label"] for c in clusters for m in c["members"]}  # type: ignore[index, union-attr]


_EXPECTED = {"n1": "Jazz", "n2": "Blues", "n3": "Unknown", "n4": "Pop", "n5": "Rock", "n6": "Blues"}


async def test_live_primary_genre_matches_the_old_statement_and_the_tie_goes_to_the_first_genre(neo4j_driver: AsyncResilientNeo4jDriver) -> None:
    await _seed(neo4j_driver)
    assert _by_artist(await nq.get_artist_cluster(neo4j_driver, "R", limit=50)) == _EXPECTED
    old = {r["artist_id"]: r["primary_genre"] for r in await run_query(neo4j_driver, _OLD, database="neo4j", artist_id="R", limit=50)}
    for artist, genre in _EXPECTED.items():
        if artist != "n2":  # the old statement left the n2 tie to the planner
            assert (old[artist] or "Unknown") == genre


async def test_the_stored_property_gives_the_same_clusters_and_is_not_recomputed(neo4j_driver: AsyncResilientNeo4jDriver) -> None:
    await _seed(neo4j_driver)
    live = await nq.get_artist_cluster(neo4j_driver, "R", limit=50)
    for artist, genre in _EXPECTED.items():
        if genre != "Unknown":  # absent when the artist has no genre
            await run_query(neo4j_driver, "MATCH (n:Artist {id: $a}) SET n.primary_genre = $g", database="neo4j", timeout=None, a=artist, g=genre)
    # Delete every genre edge: if the property is really used, the answer does not change.
    await run_query(neo4j_driver, "MATCH (:Release)-[e:IS]->(:Genre) DELETE e", database="neo4j", timeout=None)
    stored = await nq.get_artist_cluster(neo4j_driver, "R", limit=50)
    assert _by_artist(stored) == _by_artist(live) == _EXPECTED


async def test_the_limit_cuts_neighbours_by_shared_releases_before_any_genre_is_read(neo4j_driver: AsyncResilientNeo4jDriver) -> None:
    await _seed(neo4j_driver)
    top = _by_artist(await nq.get_artist_cluster(neo4j_driver, "R", limit=3))
    assert set(top) == {"n6", "n5", "n4"}  # the most shared releases: 6, 5, 4
    assert top == {"n6": "Blues", "n5": "Rock", "n4": "Pop"}
