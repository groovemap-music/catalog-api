"""SQL queries for the Explore endpoint's artist-identity lookup — the PostgreSQL backend.

`collaborator_queries.get_artist_identity` (the Cypher this replaces) is a single-vertex
lookup: ``MATCH (a:Artist {id: $artist_id}) RETURN a.id, a.name``. Per the coverage spike
(`gm-database-schema-9c8.2-cypher-coverage.md`, "A single-vertex lookup is SQL-only"), the
PostgreSQL side is a plain ``SELECT`` over the phase 0 ``graph.artist`` view rather than a
one-element ``GRAPH_TABLE`` pattern — a pattern match buys nothing over a plain select for a
single vertex and costs a planner detour. This is why the family this function belongs to
(coverage spike family 1, "vertex lookups and store statistics") needs no new relations and
runs on every integration tier rather than only PostgreSQL 19.
"""

from __future__ import annotations

from typing import Any, cast

from common.query_debug import execute_sql


# `graph.artist` is keyed on `artists.data_id`, so there is never a second row to match.
ARTIST_IDENTITY_SQL = """
SELECT artist_id, name
FROM graph.artist
WHERE artist_id = %(artist_id)s
"""


async def get_artist_identity(pool: Any, artist_id: str) -> dict[str, Any] | None:
    """Return the artist's id and name, or ``None`` when no such artist exists.

    Mirrors :func:`api.queries.collaborator_queries.get_artist_identity` column for column.
    """
    async with pool.connection() as conn, conn.cursor() as cursor_cm:
        cursor = cast("Any", cursor_cm)
        await execute_sql(cursor, ARTIST_IDENTITY_SQL, {"artist_id": artist_id})
        row = await cursor.fetchone()

    if row is None:
        return None
    return {"artist_id": row[0], "artist_name": row[1]}
