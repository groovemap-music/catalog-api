"""Neo4j Cypher queries for taste fingerprint endpoints.

Analyses a user's COLLECTED relationships to build genre x decade heatmaps,
obscurity scores, taste drift timelines, blind spots, and top labels.
"""

import asyncio
from typing import Any

from common import AsyncResilientNeo4jDriver

from api.queries.helpers import run_count, run_query
from api.queries.strata_queries import _recorded
from api.taste_projection import HeldItem, build_taste_projection


async def get_collection_count(driver: AsyncResilientNeo4jDriver, user_id: str) -> int:
    """Return the number of releases in a user's collection."""
    cypher = """
    MATCH (u:User {id: $user_id})-[:COLLECTED]->(r:Release)
    RETURN count(r) AS total
    """
    return await run_count(driver, cypher, timeout=120, user_id=user_id)


async def get_taste_heatmap(
    driver: AsyncResilientNeo4jDriver,
    user_id: str,
) -> tuple[list[dict[str, Any]], int]:
    """Return genre x decade heatmap cells and total collection size.

    Returns (cells, total) where each cell has genre, decade, count.
    """
    cypher = """
    MATCH (u:User {id: $user_id})-[:COLLECTED]->(r:Release)-[:IS]->(g:Genre)
    WHERE r.year IS NOT NULL
    WITH g.name AS genre, (r.year / 10) * 10 AS decade, count(*) AS count
    RETURN genre, decade, count
    ORDER BY count DESC
    """
    count_cypher = """
    MATCH (u:User {id: $user_id})-[:COLLECTED]->(r:Release)
    RETURN count(r) AS total
    """
    cells, total = await asyncio.gather(
        run_query(driver, cypher, timeout=120, user_id=user_id),
        run_count(driver, count_cypher, timeout=120, user_id=user_id),
    )
    return cells, total


async def get_obscurity_score(
    driver: AsyncResilientNeo4jDriver,
    user_id: str,
) -> dict[str, Any]:
    """Calculate how obscure a user's collection is.

    For each release, counts how many *other* users also collected it.
    Zero collectors means maximally obscure (score = 1.0).
    Score = 1 - (median_collectors / max_collectors) clamped to [0, 1].
    """
    cypher = """
    MATCH (u:User {id: $user_id})-[:COLLECTED]->(r:Release)
    WITH DISTINCT u, r
    OPTIONAL MATCH (other:User)-[:COLLECTED]->(r)
    WHERE other <> u
    WITH r, count(DISTINCT other) AS collectors
    RETURN collectors
    ORDER BY collectors
    """
    rows = await run_query(driver, cypher, timeout=120, user_id=user_id)

    if not rows:
        return {"score": 1.0, "median_collectors": 0.0, "total_releases": 0}

    collector_counts = sorted(r["collectors"] for r in rows)
    n = len(collector_counts)
    mid = n // 2
    median = (collector_counts[mid - 1] + collector_counts[mid]) / 2.0 if n % 2 == 0 else float(collector_counts[mid])

    max_collectors = max(collector_counts) if collector_counts else 0
    score = 1.0 if max_collectors == 0 else max(0.0, min(1.0, 1.0 - (median / max_collectors)))

    # total_releases MUST match the distinct-release basis the score/median are
    # computed on. `rows` is already one row per distinct release (the main
    # query dedups via `WITH DISTINCT u, r`), so len(rows) is the correct
    # count. A separate `count(r)` query (no DISTINCT) would count COLLECTED
    # *edges* instead — inflated for any release owned in multiple physical
    # copies (syncer.py keys the edge on instance_id) and internally
    # inconsistent with the distinct-release score/median in the same
    # response.
    return {"score": round(score, 4), "median_collectors": median, "total_releases": n}


async def get_taste_drift(
    driver: AsyncResilientNeo4jDriver,
    user_id: str,
) -> list[dict[str, Any]]:
    """Return top genre per year based on date_added.

    Buckets by year using substring(c.date_added, 0, 4).
    """
    cypher = """
    MATCH (u:User {id: $user_id})-[c:COLLECTED]->(r:Release)-[:IS]->(g:Genre)
    WHERE c.date_added IS NOT NULL
    WITH substring(c.date_added, 0, 4) AS year, g.name AS genre, count(*) AS count
    ORDER BY year, count DESC
    WITH year, collect({genre: genre, count: count})[0] AS top
    RETURN year, top.genre AS top_genre, top.count AS count
    ORDER BY year
    """
    return await run_query(driver, cypher, timeout=120, user_id=user_id)


async def get_blind_spots(
    driver: AsyncResilientNeo4jDriver,
    user_id: str,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Find genres the user's favourite artists release in but the user hasn't collected.

    Carries `u` through WITH clauses (no re-lookup). Scores by artist_overlap.
    """
    cypher = """
    MATCH (u:User {id: $user_id})-[:COLLECTED]->(r:Release)-[:BY]->(a:Artist)
    WITH u, a, count(r) AS artist_releases
    ORDER BY artist_releases DESC
    LIMIT 20
    MATCH (a)<-[:BY]-(other:Release)-[:IS]->(g:Genre)
    WHERE NOT (u)-[:COLLECTED]->(other)
    WITH u, g.name AS genre, count(DISTINCT a) AS artist_overlap,
         collect(DISTINCT other.title)[0] AS example_release
    OPTIONAL MATCH (u)-[:COLLECTED]->(cr:Release)-[:IS]->(cg:Genre)
    WHERE cg.name = genre
    WITH genre, artist_overlap, example_release, count(cr) AS already_have
    WHERE already_have = 0
    RETURN genre, artist_overlap, example_release
    ORDER BY artist_overlap DESC
    LIMIT $limit
    """
    return await run_query(driver, cypher, timeout=120, user_id=user_id, limit=limit)


async def get_top_labels(
    driver: AsyncResilientNeo4jDriver,
    user_id: str,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Return the user's most collected labels."""
    cypher = """
    MATCH (u:User {id: $user_id})-[:COLLECTED]->(r:Release)-[:ON]->(l:Label)
    RETURN l.name AS label, count(r) AS count
    ORDER BY count DESC
    LIMIT $limit
    """
    return await run_query(driver, cypher, timeout=120, user_id=user_id, limit=limit)


# The most a taste projection read may run. A collection is bounded by what Discogs holds for
# one account, and the read walks each held release's master, genres, and styles once.
TASTE_PROJECTION_TIMEOUT_SECONDS = 30.0

# One row per held release and edge type, so a release in both lists is two rows that
# get_taste_projection folds into one item. ``recorded`` holds the ``date_added`` of every edge
# that has one (a release owned in several copies has an edge per copy). The master read takes
# the lowest id when a release derives from more than one, as the strata ladder does, and no
# query here filters the Discogs ``"0"`` master sentinel: it is dropped at its source.
_TASTE_PROJECTION = """
MATCH (:User {id: $user_id})-[edge:COLLECTED|WANTS]->(r:Release)
WITH r, type(edge) AS kind, count(edge) AS edges, collect(edge.date_added) AS recorded
CALL (r) {
    OPTIONAL MATCH (r)-[:DERIVED_FROM]->(m:Master)
    RETURN m.id AS master_id, CASE WHEN m.year > 0 THEN m.year END AS master_year
    ORDER BY master_id
    LIMIT 1
}
CALL (r) {
    OPTIONAL MATCH (r)-[:IS]->(g:Genre)
    RETURN collect(DISTINCT g.name) AS genres
}
CALL (r) {
    OPTIONAL MATCH (r)-[:IS]->(s:Style)
    RETURN collect(DISTINCT s.name) AS styles
}
RETURN r.id AS id, kind, edges, recorded, master_id, master_year, genres, styles
"""


async def get_taste_projection(driver: AsyncResilientNeo4jDriver, user_id: str) -> dict[str, Any]:
    """The caller's collection and wantlist folded to genre, style, and master aggregates.

    Reads only the caller's own ``COLLECTED`` and ``WANTS`` edges. See :mod:`api.taste_projection`
    for what is counted and how.
    """
    rows = await run_query(driver, _TASTE_PROJECTION, timeout=TASTE_PROJECTION_TIMEOUT_SECONDS, user_id=user_id)
    held: dict[str, dict[str, Any]] = {}
    for row in rows:
        entry = held.setdefault(
            str(row["id"]),
            {
                "master_id": None if row["master_id"] is None else str(row["master_id"]),
                "master_year": row["master_year"],
                "genres": row["genres"],
                "styles": row["styles"],
                "owned_edges": 0,
                "wanted_edges": 0,
                "owned_at": [],
                "wanted_at": [],
            },
        )
        state = "owned" if row["kind"] == "COLLECTED" else "wanted"
        entry[f"{state}_edges"] += row["edges"]
        entry[f"{state}_at"].extend(text for text in map(_recorded, row["recorded"]) if text is not None)
    return build_taste_projection(HeldItem(release_id, **fields) for release_id, fields in held.items())
