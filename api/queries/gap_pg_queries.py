"""SQL queries for the three gap-analysis metadata lookups — the PostgreSQL backend.

`gap_queries.get_label_metadata`, `get_artist_metadata`, and `get_master_metadata` (the
Cypher these replace) are single-vertex lookups: `MATCH (x {id: $id}) RETURN x.id, x.name`
(`x.title` for a master). Per the coverage spike's single-vertex-lookup rule, each is a plain
``SELECT`` over its phase 0 vertex view rather than a ``GRAPH_TABLE`` pattern — these
functions, along with the rest of coverage spike family 1 ("vertex lookups and store
statistics"), touch no edge and need no new relations, so they run on every integration
tier rather than only PostgreSQL 19.
"""

from __future__ import annotations

from typing import Any, cast

from common.query_debug import execute_sql


LABEL_METADATA_SQL = """
SELECT label_id, name
FROM graph.label
WHERE label_id = %(label_id)s
"""

ARTIST_METADATA_SQL = """
SELECT artist_id, name
FROM graph.artist
WHERE artist_id = %(artist_id)s
"""

# `graph.master` names the title column `title`, not `name`; the Cypher projects it as
# `m.title AS name`, and the response dict below keeps that same key.
MASTER_METADATA_SQL = """
SELECT master_id, title
FROM graph.master
WHERE master_id = %(master_id)s
"""


async def get_label_metadata(pool: Any, label_id: str) -> dict[str, Any] | None:
    """Return the label's id and name, or ``None`` when no such label exists.

    Mirrors :func:`api.queries.gap_queries.get_label_metadata` column for column.
    """
    async with pool.connection() as conn, conn.cursor() as cursor_cm:
        cursor = cast("Any", cursor_cm)
        await execute_sql(cursor, LABEL_METADATA_SQL, {"label_id": label_id})
        row = await cursor.fetchone()

    if row is None:
        return None
    return {"id": row[0], "name": row[1]}


async def get_artist_metadata(pool: Any, artist_id: str) -> dict[str, Any] | None:
    """Return the artist's id and name, or ``None`` when no such artist exists.

    Mirrors :func:`api.queries.gap_queries.get_artist_metadata` column for column.
    """
    async with pool.connection() as conn, conn.cursor() as cursor_cm:
        cursor = cast("Any", cursor_cm)
        await execute_sql(cursor, ARTIST_METADATA_SQL, {"artist_id": artist_id})
        row = await cursor.fetchone()

    if row is None:
        return None
    return {"id": row[0], "name": row[1]}


async def get_master_metadata(pool: Any, master_id: str) -> dict[str, Any] | None:
    """Return the master's id and title (as ``name``), or ``None`` when it does not exist.

    Mirrors :func:`api.queries.gap_queries.get_master_metadata` column for column.
    """
    async with pool.connection() as conn, conn.cursor() as cursor_cm:
        cursor = cast("Any", cursor_cm)
        await execute_sql(cursor, MASTER_METADATA_SQL, {"master_id": master_id})
        row = await cursor.fetchone()

    if row is None:
        return None
    return {"id": row[0], "name": row[1]}
