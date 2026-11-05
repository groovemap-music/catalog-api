"""Neo4j Cypher queries that build the Genre Tree, off the request path.

Each genre contains the styles that appear on releases tagged with that genre. The tree used to be one
statement that walked every ``IS`` edge twice (59M and 131M db hits on lux, 78 s estimated against a
30 s budget). Nothing here runs per request any more: :mod:`api.genre_tree_store` calls these from a
background refresh and serves the stored result (audit H3, ``gm-catalog-api-somq``).

The release count of a genre is read from ``Genre.release_count`` (the graph enricher maintains it; on
lux it equals the edge count for all 15 genres). Only the genre/style co-occurrence needs a scan, and
it runs one genre at a time so no transaction holds more than one genre's styles.
"""

from typing import Any

from common import AsyncResilientNeo4jDriver

from api.queries.helpers import run_query, run_single


# Budget for one genre's co-occurrence scan. Rock, the largest genre, took 16 s on lux. This is a
# background read, not a request: a genre that outgrows it fails the refresh, which keeps the old tree.
GENRE_STYLES_TIMEOUT_SECONDS = 120.0
_META_TIMEOUT_SECONDS = 30.0

_SIGNATURE_CYPHER = """
MATCH (g:Genre)
RETURN count(g) AS genres,
       sum(coalesce(g.release_count, 0)) AS release_total,
       sum(coalesce(g.style_count, 0)) AS style_total
"""

_GENRES_CYPHER = """
MATCH (g:Genre)
RETURN g.name AS name, g.release_count AS release_count
ORDER BY coalesce(g.release_count, 0) DESC, g.name
"""

_GENRE_COUNT_CYPHER = """
MATCH (g:Genre {name: $genre})<-[:IS]-(r:Release)
RETURN count(DISTINCT r) AS release_count
"""

_GENRE_STYLES_CYPHER = """
MATCH (g:Genre {name: $genre})<-[:IS]-(r:Release)-[:IS]->(s:Style)
WITH s.name AS name, count(DISTINCT r) AS release_count
RETURN name, release_count
ORDER BY release_count DESC, name
"""


async def get_genre_signature(driver: AsyncResilientNeo4jDriver) -> str:
    """A cheap fingerprint of the genre counts: it changes when an import changes them."""
    row = await run_single(driver, _SIGNATURE_CYPHER, timeout=_META_TIMEOUT_SECONDS)
    if row is None:
        return "0:0:0"
    return f"{row['genres']}:{row['release_total']}:{row['style_total']}"


async def build_genre_tree(driver: AsyncResilientNeo4jDriver) -> list[dict[str, Any]]:
    """Scan the graph and return the full tree: ``name``, ``release_count`` and nested ``styles``.

    This is the expensive path (about a minute on the full graph). Never call it per request.
    """
    genres = await run_query(driver, _GENRES_CYPHER, timeout=_META_TIMEOUT_SECONDS)
    tree: list[dict[str, Any]] = []
    for genre in genres:
        name = genre["name"]
        release_count = genre["release_count"]
        if release_count is None:
            counted = await run_single(driver, _GENRE_COUNT_CYPHER, timeout=GENRE_STYLES_TIMEOUT_SECONDS, genre=name)
            release_count = counted["release_count"] if counted else 0
        styles = await run_query(driver, _GENRE_STYLES_CYPHER, timeout=GENRE_STYLES_TIMEOUT_SECONDS, genre=name)
        tree.append({"name": name, "release_count": release_count, "styles": styles})
    tree.sort(key=lambda g: (-g["release_count"], g["name"]))
    return tree
