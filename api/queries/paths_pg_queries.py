"""PostgreSQL implementation of the variable-length ``paths`` family."""

from __future__ import annotations

from typing import Any, cast

from common.query_debug import execute_sql

from api.queries.neo4j_queries import DEFAULT_PATH_DEPTH, MAX_PATH_DEPTH, MIN_PATH_DEPTH


_KIND_FOR_TYPE = {"artist": "a", "genre": "g", "label": "l", "master": "m", "release": "r", "style": "s"}
_LABEL_FOR_KIND = {"a": "Artist", "g": "Genre", "l": "Label", "m": "Master", "r": "Release", "s": "Style"}

FIND_SHORTEST_PATH_SQL = """
SELECT found, depth, nodes, rels
FROM graph.find_shortest_path(
    %(from_kind)s::"char", %(from_key)s,
    %(to_kind)s::"char", %(to_key)s,
    %(max_depth)s
)
"""

HYDRATE_PATH_NODES_SQL = """
WITH requested AS (
    SELECT kinds.kind, keys.key, kinds.ordinality
    FROM unnest(%(kinds)s::text[]) WITH ORDINALITY AS kinds(kind, ordinality)
    JOIN unnest(%(keys)s::text[]) WITH ORDINALITY AS keys(key, ordinality)
      USING (ordinality)
)
SELECT requested.key,
       coalesce(
           CASE requested.kind
               WHEN 'a' THEN (SELECT artist.name FROM graph.artist AS artist WHERE artist.artist_id = requested.key)
               WHEN 'l' THEN (SELECT label.name FROM graph.label AS label WHERE label.label_id = requested.key)
               WHEN 'r' THEN (SELECT release.title FROM graph.release AS release WHERE release.release_id = requested.key)
               WHEN 'm' THEN (SELECT master.title FROM graph.master AS master WHERE master.master_id = requested.key)
               WHEN 'g' THEN requested.key
               WHEN 's' THEN requested.key
           END,
           ''
       ) AS name,
       requested.kind
FROM requested
ORDER BY requested.ordinality
"""

RESOLVE_PATH_KIND_SQL = """
SELECT candidate.kind
FROM (VALUES
    (1, 'a', EXISTS (SELECT 1 FROM graph.artist WHERE artist_id = %(key)s)),
    (2, 'l', EXISTS (SELECT 1 FROM graph.label WHERE label_id = %(key)s)),
    (3, 'r', EXISTS (SELECT 1 FROM graph.release WHERE release_id = %(key)s)),
    (4, 'm', EXISTS (SELECT 1 FROM graph.master WHERE master_id = %(key)s)),
    (5, 'g', EXISTS (SELECT 1 FROM graph.genre WHERE name = %(key)s)),
    (6, 's', EXISTS (SELECT 1 FROM graph.style WHERE name = %(key)s))
) AS candidate(priority, kind, present)
WHERE candidate.present
ORDER BY candidate.priority
LIMIT 1
"""

EXPLORE_TRAVERSAL_SQL = """
SELECT id, name, type, path_names, rel_types, dist
FROM graph.explore_traversal(
    %(from_kind)s::"char", %(from_key)s, %(hops)s, %(row_limit)s
)
"""


async def _resolve_kind(cursor: Any, entity_type: str, key: str) -> str | None:
    kind = _KIND_FOR_TYPE.get(entity_type)
    if kind is not None:
        return kind
    await execute_sql(cursor, RESOLVE_PATH_KIND_SQL, {"key": key})
    row = await cursor.fetchone()
    return str(row[0]) if row else None


async def find_shortest_path(
    pool: Any,
    from_id: str,
    to_id: str,
    max_depth: int = DEFAULT_PATH_DEPTH,
    from_type: str = "",
    to_type: str = "",
) -> dict[str, Any] | None:
    """Call ``graph.find_shortest_path`` and hydrate its compact node identities."""
    depth = max(MIN_PATH_DEPTH, min(int(max_depth), MAX_PATH_DEPTH))
    async with pool.connection() as conn, conn.cursor() as cursor_cm:
        cursor = cast("Any", cursor_cm)
        from_kind = await _resolve_kind(cursor, from_type, from_id)
        to_kind = await _resolve_kind(cursor, to_type, to_id)
        if from_kind is None or to_kind is None:
            return None
        await execute_sql(
            cursor,
            FIND_SHORTEST_PATH_SQL,
            {"from_kind": from_kind, "from_key": from_id, "to_kind": to_kind, "to_key": to_id, "max_depth": depth},
        )
        row = await cursor.fetchone()
        if not row or not row[0]:
            return None
        node_tokens = list(row[2] or ())
        rels = list(row[3] or ())
        kinds: list[str] = []
        keys: list[str] = []
        for token in node_tokens:
            kind, separator, key = str(token).partition(":")
            if not separator or kind not in _LABEL_FOR_KIND:
                raise ValueError(f"invalid graph path node identity: {token!r}")
            kinds.append(kind)
            keys.append(key)
        await execute_sql(cursor, HYDRATE_PATH_NODES_SQL, {"kinds": kinds, "keys": keys})
        hydrated = await cursor.fetchall()

    nodes = [{"id": str(key), "name": str(name), "labels": [_LABEL_FOR_KIND[str(kind)]]} for key, name, kind in hydrated]
    if len(nodes) != len(rels) + 1 or int(row[1]) != len(rels):
        raise ValueError("graph.find_shortest_path returned inconsistent node, relationship, and depth arrays")
    return {"nodes": nodes, "rels": rels}


async def get_explore_traversal(
    pool: Any,
    entity_type: str,
    entity_id: str,
    hops: int = 2,
    row_limit: int = 100,
) -> list[dict[str, Any]]:
    """Call the bounded traversal with an explicit mandatory row limit."""
    bounded_hops = hops if 1 <= hops <= 3 else 2
    async with pool.connection() as conn, conn.cursor() as cursor_cm:
        cursor = cast("Any", cursor_cm)
        from_kind = await _resolve_kind(cursor, entity_type, entity_id)
        if from_kind is None:
            return []
        await execute_sql(
            cursor,
            EXPLORE_TRAVERSAL_SQL,
            {"from_kind": from_kind, "from_key": entity_id, "hops": bounded_hops, "row_limit": row_limit},
        )
        rows = await cursor.fetchall()
    columns = ("id", "name", "type", "path_names", "rel_types", "dist")
    return [dict(zip(columns, row, strict=True)) for row in rows]
