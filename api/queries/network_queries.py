"""Neo4j Cypher queries for Collaboration Network endpoints.

Provides multi-hop collaborator traversal, betweenness centrality
(via Neo4j GDS), and community detection (Louvain/Label Propagation).
"""

from typing import Any

import structlog
from common import AsyncResilientNeo4jDriver

from api.queries.helpers import run_query, run_single


logger = structlog.get_logger(__name__)


async def get_artist_identity(driver: AsyncResilientNeo4jDriver, artist_id: str) -> dict[str, Any] | None:
    """Get basic artist info (id, name) for existence check."""
    cypher = """
    MATCH (a:Artist {id: $artist_id})
    RETURN a.id AS artist_id, a.name AS artist_name
    """
    return await run_single(driver, cypher, artist_id=artist_id)


# Collaborator traversal bounds (audit H1, ``docs/cypher-performance-audit-2026-11-04.md``).
#
# Bridge artists: an artist credited on more than this many releases (or with no name: Discogs
# placeholders such as "Various", 194, and "Unknown Artist", 355) is not used as a stepping stone to a
# second-hop collaborator. Sharing a release with Various says nothing about who works with whom, and
# expanding through it reaches every artist on 1.5M releases. Such an artist still appears as a direct
# collaborator. 5,000 keeps 164 of David Bowie's 187 neighbours as bridges.
BRIDGE_ARTIST_MAX_CREDITS = 5_000
# A root with more credits than this is itself a hub (Various has 1.53M): only its first
# HUB_ROOT_MAX_CREDITS releases are read, it returns direct collaborators only, and the result is
# flagged ``truncated``. This bounds the rows and memory a hub root can make one statement hold.
HUB_ROOT_MAX_CREDITS = 100_000
# Only two hops are traversed. The route accepts depth up to 3 for compatibility and clamps it.
MAX_COLLABORATOR_DEPTH = 2
# Server budget for the collaborator statement, in seconds.
COLLABORATOR_QUERY_TIMEOUT_SECONDS = 30.0


async def get_collaborator_page(
    driver: AsyncResilientNeo4jDriver,
    artist_id: str,
    depth: int = 2,
    limit: int = 50,
) -> tuple[list[dict[str, Any]], int, bool]:
    """Return one page of collaborators within ``depth`` hops, the total and whether it was truncated.

    Distance 1 is a shared release; distance 2 is a collaborator of a bridge collaborator that the
    artist has not shared a release with. ``collaboration_count`` is the shared releases at distance 1
    and the number of distinct bridge artists at distance 2. Ranking is distance, then count, then id.

    ``truncated`` is true only for a hub root, whose releases beyond the first 100,000 were not read, so
    the page and total describe that sample.

    The direct set is collected once and expanded one bridge at a time, so a neighbour reached through
    several releases is walked once and the root is never re-expanded per row.
    """
    effective_depth = max(1, min(depth, MAX_COLLABORATOR_DEPTH))
    cypher = """
    MATCH (a:Artist {id: $artist_id})
    CALL {
        WITH a
        MATCH (a)<-[:BY]-(sampled:Release)
        WITH a, sampled LIMIT $hub_root_max_credits
        MATCH (sampled)-[:BY]->(n:Artist)
        WHERE n <> a
        RETURN n AS collaborator, 1 AS hops, count(DISTINCT sampled) AS shared
    }
    WITH a, collect({collaborator: collaborator, hops: hops, shared: shared}) AS direct,
         COUNT { (a)<-[:BY]-() } > $hub_root_max_credits AS truncated
    CALL {
        WITH a, direct
        UNWIND direct AS d
        WITH a, d.collaborator AS bridge
        WHERE $depth >= 2
              AND COUNT { (a)<-[:BY]-() } <= $hub_root_max_credits
              AND bridge.name IS NOT NULL
              AND COUNT { (bridge)<-[:BY]-() } <= $bridge_max_credits
        CALL {
            WITH a, bridge
            MATCH (bridge)<-[:BY]-(:Release)-[:BY]->(far:Artist)
            WHERE far <> a
            RETURN DISTINCT far
        }
        WITH far, count(*) AS shared
        // A global aggregate always yields one row, so depth 1 (no bridges) still returns the direct set.
        RETURN collect({collaborator: far, hops: 2, shared: shared}) AS indirect
    }
    UNWIND direct + indirect AS row
    WITH truncated, row.collaborator AS collaborator,
         min(row.hops) AS distance,
         sum(CASE row.hops WHEN 1 THEN row.shared ELSE 0 END) AS direct_count,
         sum(CASE row.hops WHEN 2 THEN row.shared ELSE 0 END) AS indirect_count
    WITH truncated, collaborator, distance, CASE distance WHEN 1 THEN direct_count ELSE indirect_count END AS collaboration_count
    ORDER BY distance ASC, collaboration_count DESC, collaborator.id ASC
    WITH truncated, collect([collaborator, distance, collaboration_count]) AS ranked
    RETURN size(ranked) AS total,
           truncated,
           [r IN ranked[0..$limit] | {
               artist_id: (r[0]).id,
               artist_name: (r[0]).name,
               distance: r[1],
               collaboration_count: r[2]
           }] AS page
    """
    row = await run_single(
        driver,
        cypher,
        timeout=COLLABORATOR_QUERY_TIMEOUT_SECONDS,
        artist_id=artist_id,
        depth=effective_depth,
        limit=limit,
        hub_root_max_credits=HUB_ROOT_MAX_CREDITS,
        bridge_max_credits=BRIDGE_ARTIST_MAX_CREDITS,
    )
    if row is None:
        return [], 0, False
    return list(row["page"]), int(row["total"]), bool(row["truncated"])


async def get_artist_centrality(
    driver: AsyncResilientNeo4jDriver,
    artist_id: str,
) -> dict[str, Any] | None:
    """Compute centrality scores for a single artist.

    Returns degree centrality (total relationship count) and
    collaboration-based betweenness approximation via Neo4j GDS
    (falls back to degree-only if GDS is unavailable).
    """
    # First try GDS betweenness centrality
    gds_cypher = """
    MATCH (a:Artist {id: $artist_id})
    WITH a, size([(a)-[]-() | 1]) AS degree
    OPTIONAL MATCH (a)<-[:BY]-(r:Release)-[:BY]->(other:Artist)
    WHERE other <> a
    WITH a, degree,
         count(DISTINCT other) AS collaborator_count,
         count(DISTINCT r) AS collaboration_releases
    OPTIONAL MATCH (a)-[:MEMBER_OF]->(g)
    WITH a, degree, collaborator_count, collaboration_releases,
         count(DISTINCT g) AS group_count
    OPTIONAL MATCH (a)-[:ALIAS_OF]->(al)
    WITH a, degree, collaborator_count, collaboration_releases,
         group_count, count(DISTINCT al) AS alias_count
    RETURN a.id AS artist_id, a.name AS artist_name,
           degree,
           collaborator_count,
           collaboration_releases,
           group_count,
           alias_count
    """
    return await run_single(driver, gds_cypher, artist_id=artist_id)


_CLUSTER_NEIGHBOURS = """
MATCH (a:Artist {id: $artist_id})<-[:BY]-(r:Release)-[:BY]->(neighbor:Artist)
WHERE neighbor <> a
WITH neighbor, count(DISTINCT r) AS shared_releases
ORDER BY shared_releases DESC, neighbor.id
LIMIT $limit
RETURN neighbor.id AS artist_id,
       neighbor.name AS artist_name,
       shared_releases,
       neighbor.primary_genre AS primary_genre
"""

# The live equivalent of the enricher's Artist.primary_genre: the genre on most of an artist's releases, a tie going to the
# alphabetically first genre. Run only for the neighbours that were already ranked and cut, and only where the stored
# property is absent (not computed yet, or the artist has no genre).
_PRIMARY_GENRE_LIVE = """
UNWIND $ids AS nid
MATCH (n:Artist {id: nid})<-[:BY]-(r:Release)-[:IS]->(g:Genre)
WITH n, g.name AS genre, count(r) AS genre_count
ORDER BY n.id, genre_count DESC, genre
WITH n, collect(genre)[0] AS primary_genre
RETURN n.id AS artist_id, primary_genre
"""


async def get_artist_cluster(
    driver: AsyncResilientNeo4jDriver,
    artist_id: str,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Detect the community/cluster around an artist.

    Finds the artists who share the most releases with the artist, cuts to ``limit``, and only then reads each one's
    primary genre (the genre on most of their releases, a tie to the alphabetically first). The old statement built
    the genre histogram of every neighbour's whole discography before the cut: one Various co-credit cost 1.53M
    releases, and Bowie took 29.6 s and 37.5M hits (audit H6). The neighbours are the same; ties in shared releases
    are now broken by artist id.

    The primary genre is read from ``Artist.primary_genre`` when the enricher has written it, and computed live for a
    ranked neighbour where it is absent, so the answer is identical before and after the first maintenance pass.
    """
    results = await run_query(driver, _CLUSTER_NEIGHBOURS, artist_id=artist_id, limit=limit)

    missing = [row["artist_id"] for row in results if row["primary_genre"] is None]
    if missing:
        live = {r["artist_id"]: r["primary_genre"] for r in await run_query(driver, _PRIMARY_GENRE_LIVE, ids=missing)}
        for row in results:
            if row["primary_genre"] is None:
                row["primary_genre"] = live.get(row["artist_id"])

    # Group results by primary_genre to form clusters
    clusters: dict[str, list[dict[str, Any]]] = {}
    for row in results:
        genre = row.get("primary_genre") or "Unknown"
        if genre not in clusters:
            clusters[genre] = []
        clusters[genre].append(
            {
                "artist_id": row["artist_id"],
                "artist_name": row["artist_name"],
                "shared_releases": row["shared_releases"],
            }
        )

    return [
        {"cluster_label": genre, "members": members, "size": len(members)}
        for genre, members in sorted(clusters.items(), key=lambda x: len(x[1]), reverse=True)
    ]
