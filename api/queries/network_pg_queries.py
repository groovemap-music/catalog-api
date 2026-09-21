"""SQL/PGQ queries for Collaboration Network endpoints — the PostgreSQL backend.

This is the pilot the rest of the Neo4j -> PostgreSQL read migration (ADR 0012) is
cut from, so the shape below is the template rather than a one-off. Four rules make
it one:

1. **Pattern matching replaces Cypher, not SQL.** Every traversal is a
   ``GRAPH_TABLE`` over ``graph.catalog``; everything that is not a traversal —
   grouping, the anti-join, ordering, the limit — stays ordinary SQL around it.
   ``GRAPH_TABLE`` is a table expression, so it composes with CTEs, ``GROUP BY``,
   and ``NOT EXISTS`` the way any other relation does.
2. **One constant per query.** The SQL is a module-level string, exactly as in
   :mod:`api.queries.insights_pg_queries`, so the statement a reviewer reads is the
   statement the server runs and nothing is assembled from caller input.
3. **Every value is a parameter.** Artist id, depth, and limit are bound through
   ``%(name)s`` placeholders — including inside the graph pattern, where PostgreSQL
    19 accepts a parameter in an element pattern's ``WHERE`` like any other
   expression.
4. **Parity with the Cypher is column-for-column.** Same column names, same types,
   same ordering as :mod:`api.queries.network_queries`, because the router calls
   one or the other through the graph-backend seam and the response schema cannot
   move.

Mapping the Cypher onto the graph
---------------------------------
``graph.catalog`` is declared by the ``groovemap-database-schema`` initializer (the
``PROPERTY_GRAPH_STATEMENT`` in ``src/groovemap_schema/postgres.py``) and is
documented in that repository's ``docs/architecture.md`` under "Graph schema". The
labels are the view names verbatim, which is what the de-reserving rule in ADR 0012
buys: ``:Artist`` is ``graph.artist`` is ``(a IS artist)``, ``:Release`` is
``graph.release`` is ``(r IS release)``, and the overloaded ``[:BY]`` out of a
release is ``graph.by_artist`` is ``-[IS by_artist]->``.

The edge is directed release -> artist, so the Cypher's
``(a)<-[:BY]-(:Release)-[:BY]->(hop1)`` is spelled with the same two arrow
directions here. The DDL for that edge, as the initializer renders it::

    graph.by_artist AS by_artist KEY (release_id, artist_id)
        SOURCE KEY (release_id) REFERENCES release (release_key)
        DESTINATION KEY (artist_id) REFERENCES artist (artist_key)
        LABEL by_artist PROPERTIES ALL COLUMNS

``release_key`` and ``artist_key`` are the appended ``text`` restatements the four
Discogs vertex views carry because PostgreSQL 19 beta 3 refuses a ``character
varying`` vertex key. They are structural: no query names them, and ``artist_id``
remains the property, unified on ``text`` by the vertex declaration
(``artist_id::text AS artist_id``).

Where the two engines differ
----------------------------
Neo4j applies relationship isomorphism to a ``MATCH`` path: no relationship may be
bound twice, which silently forbids the two-hop pattern from walking back down the
release it arrived on. SQL/PGQ's default is walk semantics — the same edge may bind
to two edge patterns — so every constraint Neo4j derives from that rule is written
out in the pattern's ``WHERE``: the bridge and the peer are not the anchor, the peer
is not the bridge, and the far release is not the near one. Those four predicates
are what make the PostgreSQL result set equal to the Cypher one rather than a
superset of it.
"""

from __future__ import annotations

from typing import Any, cast

import structlog
from common.query_debug import execute_sql


logger = structlog.get_logger(__name__)


# The anchor artist, matched as a single vertex. LIMIT 1 mirrors `run_single`, which
# takes the first record; `graph.artist` is keyed on `artists.data_id`, so there is
# never a second row to take.
ARTIST_IDENTITY_SQL = """
SELECT anchor_row.artist_id, anchor_row.artist_name
FROM GRAPH_TABLE (graph.catalog
    MATCH (anchor IS artist WHERE anchor.artist_id = %(artist_id)s)
    COLUMNS (anchor.artist_id AS artist_id, anchor.name AS artist_name)
) AS anchor_row
LIMIT 1
"""

# Depth 1: every artist reachable over one shared release. One row per (collaborator,
# release), so the caller's COUNT(DISTINCT release_id) is the Cypher's
# `count(DISTINCT nodes(path)[1])` — the number of releases the two share.
_DIRECT_COLLABORATORS = """
    SELECT collaborator_id, collaborator_name, release_id
    FROM GRAPH_TABLE (graph.catalog
        MATCH (anchor IS artist WHERE anchor.artist_id = %(artist_id)s)
              <-[IS by_artist]-(credit IS release)-[IS by_artist]->(peer IS artist)
        WHERE peer.artist_id <> anchor.artist_id
        COLUMNS (
            peer.artist_id AS collaborator_id,
            peer.name AS collaborator_name,
            credit.release_id AS release_id
        )
    ) AS hop
"""

# Depth 2: the chained two-hop pattern, written out rather than reached with a
# quantifier, so each of the four edges can be constrained individually. One row per
# (collaborator, bridge), so COUNT(DISTINCT bridge_id) is the Cypher's
# `count(DISTINCT mid)` — how many intermediaries connect the pair.
_INDIRECT_COLLABORATORS = """
    SELECT collaborator_id, collaborator_name, bridge_id
    FROM GRAPH_TABLE (graph.catalog
        MATCH (anchor IS artist WHERE anchor.artist_id = %(artist_id)s)
              <-[IS by_artist]-(near IS release)-[IS by_artist]->(bridge IS artist)
              <-[IS by_artist]-(far IS release)-[IS by_artist]->(peer IS artist)
        WHERE bridge.artist_id <> anchor.artist_id
          AND peer.artist_id <> anchor.artist_id
          AND peer.artist_id <> bridge.artist_id
          AND far.release_id <> near.release_id
        COLUMNS (
            peer.artist_id AS collaborator_id,
            peer.name AS collaborator_name,
            bridge.artist_id AS bridge_id
        )
    ) AS hop
"""

# The anti-join: a second GRAPH_TABLE walking the depth-1 pattern again, so a
# collaborator already reachable in one hop never appears as a two-hop result. It is
# the direct transcription of the Cypher's
# `NOT EXISTS { MATCH (a)<-[:BY]-(:Release)-[:BY]->(hop2) }`, and it is deliberately
# a second pattern rather than a reference back to the depth-1 CTE: the two express
# different things (reachability versus the aggregated result set) and only the
# subquery form stays correct if the depth-1 projection ever grows a filter the
# exclusion must not inherit. Nothing inside it depends on the outer row except the
# final equality, so the planner hashes it once per statement.
_DIRECT_COLLABORATOR_EXISTS = """
        SELECT 1
        FROM GRAPH_TABLE (graph.catalog
            MATCH (anchor IS artist WHERE anchor.artist_id = %(artist_id)s)
                  <-[IS by_artist]-(credit IS release)-[IS by_artist]->(peer IS artist)
            COLUMNS (peer.artist_id AS collaborator_id)
        ) AS one_hop
        WHERE one_hop.collaborator_id = indirect.collaborator_id
"""

# `%(depth)s >= 2` gates the whole two-hop branch as a one-time filter: at depth 1 the
# planner never executes the subplan. Depth 3 is accepted by the endpoint and behaves
# as depth 2 here, exactly as the Cypher does — its second UNION branch is the only
# one that adds hops, and it adds two.
MULTI_HOP_COLLABORATORS_SQL = f"""
WITH direct AS ({_DIRECT_COLLABORATORS.rstrip()}
),
indirect AS ({_INDIRECT_COLLABORATORS.rstrip()}
)
SELECT collaborator_id AS artist_id,
       collaborator_name AS artist_name,
       distance,
       collaboration_count
FROM (
    SELECT collaborator_id,
           collaborator_name,
           1 AS distance,
           count(DISTINCT release_id)::bigint AS collaboration_count
    FROM direct
    GROUP BY collaborator_id, collaborator_name
    UNION ALL
    SELECT collaborator_id,
           collaborator_name,
           2 AS distance,
           count(DISTINCT bridge_id)::bigint AS collaboration_count
    FROM indirect
    WHERE %(depth)s >= 2
      AND NOT EXISTS ({_DIRECT_COLLABORATOR_EXISTS.strip()}
      )
    GROUP BY collaborator_id, collaborator_name
) AS reachable
ORDER BY distance ASC, collaboration_count DESC
LIMIT %(limit)s
"""  # noqa: S608 — every interpolated part is a module-level constant above; no caller input reaches it

# The Cypher groups by collaborator and then counts the groups, which is a count of
# distinct collaborators. The two branches are already disjoint — the anti-join is
# what makes them so — but UNION rather than UNION ALL states the intent and costs
# nothing the DISTINCT would not.
COUNT_MULTI_HOP_COLLABORATORS_SQL = f"""
WITH direct AS ({_DIRECT_COLLABORATORS.rstrip()}
),
indirect AS ({_INDIRECT_COLLABORATORS.rstrip()}
)
SELECT count(*)::bigint AS total
FROM (
    SELECT collaborator_id FROM direct
    UNION
    SELECT collaborator_id
    FROM indirect
    WHERE %(depth)s >= 2
      AND NOT EXISTS ({_DIRECT_COLLABORATOR_EXISTS.strip()}
      )
) AS reachable
"""  # noqa: S608 — as above: the composition is of module constants only


# Degree comes from the loader-maintained counter bound into artist_vertex, not a
# request-time traversal. Other centrality fields retain the Cypher's DISTINCT edge
# semantics, including counting only releases shared with another artist.
ARTIST_CENTRALITY_SQL = """
SELECT a.artist_id, a.name AS artist_name, a.degree,
       (SELECT count(DISTINCT peer.artist_id)::bigint
        FROM graph.by_artist own JOIN graph.by_artist peer USING (release_id)
        WHERE own.artist_id = a.artist_id AND peer.artist_id <> a.artist_id) AS collaborator_count,
       (SELECT count(DISTINCT own.release_id)::bigint
        FROM graph.by_artist own
        WHERE own.artist_id = a.artist_id
          AND EXISTS (SELECT 1 FROM graph.by_artist peer
                      WHERE peer.release_id = own.release_id AND peer.artist_id <> a.artist_id)) AS collaboration_releases,
       (SELECT count(DISTINCT group_artist_id)::bigint FROM graph.member_of
        WHERE member_artist_id = a.artist_id) AS group_count,
       (SELECT count(DISTINCT alias_artist_id)::bigint FROM graph.alias_of
        WHERE artist_id = a.artist_id) AS alias_count
FROM graph.artist_vertex a
WHERE a.artist_id = %(artist_id)s
LIMIT 1
"""


async def get_artist_identity(pool: Any, artist_id: str) -> dict[str, Any] | None:
    """Return the anchor artist's id and name, or ``None`` when no such artist exists.

    Mirrors :func:`api.queries.network_queries.get_artist_identity` column for column.
    """
    async with pool.connection() as conn, conn.cursor() as cursor_cm:
        cursor = cast("Any", cursor_cm)
        await execute_sql(cursor, ARTIST_IDENTITY_SQL, {"artist_id": artist_id})
        row = await cursor.fetchone()

    if row is None:
        return None
    return {"artist_id": row[0], "artist_name": row[1]}


async def get_multi_hop_collaborators(
    pool: Any,
    artist_id: str,
    depth: int = 2,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Return collaborators within *depth* hops, ordered as the Cypher orders them.

    Depth 1 counts the releases the pair share; depth 2 counts the intermediaries
    that connect them, and excludes anyone already reachable in one hop.
    """
    async with pool.connection() as conn, conn.cursor() as cursor_cm:
        cursor = cast("Any", cursor_cm)
        await execute_sql(
            cursor,
            MULTI_HOP_COLLABORATORS_SQL,
            {"artist_id": artist_id, "depth": depth, "limit": limit},
        )
        rows = await cursor.fetchall()

    collaborators = [
        {
            "artist_id": row[0],
            "artist_name": row[1],
            "distance": row[2],
            "collaboration_count": row[3],
        }
        for row in rows
    ]
    logger.debug("🔍 Multi-hop collaborators resolved", artist_id=artist_id, depth=depth, count=len(collaborators))
    return collaborators


async def count_multi_hop_collaborators(pool: Any, artist_id: str, depth: int = 2) -> int:
    """Return how many distinct collaborators lie within *depth* hops."""
    async with pool.connection() as conn, conn.cursor() as cursor_cm:
        cursor = cast("Any", cursor_cm)
        await execute_sql(cursor, COUNT_MULTI_HOP_COLLABORATORS_SQL, {"artist_id": artist_id, "depth": depth})
        row = await cursor.fetchone()

    return int(row[0]) if row else 0


async def get_artist_centrality(pool: Any, artist_id: str) -> dict[str, Any] | None:
    """Return centrality with the precomputed artist degree counter."""
    async with pool.connection() as conn, conn.cursor() as cursor_cm:
        cursor = cast("Any", cursor_cm)
        await execute_sql(cursor, ARTIST_CENTRALITY_SQL, {"artist_id": artist_id})
        row = await cursor.fetchone()
    if row is None:
        return None
    return dict(
        zip(("artist_id", "artist_name", "degree", "collaborator_count", "collaboration_releases", "group_count", "alias_count"), row, strict=True)
    )
