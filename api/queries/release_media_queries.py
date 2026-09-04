"""PostgreSQL query for the ADR 0007 canonical media block of a single release.

Reads ``releases.media``, the block the Discogs SQL loader computes at load
time and stores keyed by the same id as the Neo4j ``Release.id`` node
property (``releases.data_id``, the Discogs release id).
"""

from typing import Any

from common.query_debug import execute_sql
from psycopg.rows import dict_row


async def get_release_media(pg_pool: Any, release_id: str) -> dict[str, Any] | None:
    """Return the canonical ``media`` block stored for one release.

    Args:
        pg_pool: The async PostgreSQL pool.
        release_id: The Discogs release id (``releases.data_id`` / Neo4j ``Release.id``).

    Returns:
        The JSON-ready media block, or ``None`` when the release row does not
        exist in PostgreSQL, or exists but its ``media`` column is NULL.
        Callers derive a best-effort fallback for either case (see
        ``common.media.legacy_format_names_to_media``).
    """
    async with pg_pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await execute_sql(cur, "SELECT media FROM releases WHERE data_id = %s", (release_id,))
        row: dict[str, Any] | None = await cur.fetchone()
    if row is None:
        return None
    media: dict[str, Any] | None = row["media"]
    return media
