"""PostgreSQL query for the user's collection media summary.

Reads the ADR 0007 canonical ``media`` block that ``api.syncer`` computes and
stores on ``user_collections`` at sync time, so the families and mediums
returned use canonical taxonomy ids even though the collection itself was
synced from raw Discogs format names.
"""

from typing import Any

from common.media import medium_label
from common.query_debug import execute_sql
from psycopg.rows import dict_row


# Distinct release_id + media pairs first, so a release synced with more than
# one collection instance (a duplicate physical copy) is counted once rather
# than once per instance_id.
_FAMILY_COUNTS_SQL = """
    SELECT family, COUNT(DISTINCT release_id) AS count
    FROM (
        SELECT DISTINCT release_id, media
        FROM user_collections
        WHERE user_id = %s::uuid AND media IS NOT NULL
    ) AS collection_media, jsonb_array_elements_text(collection_media.media->'families') AS family
    GROUP BY family
    ORDER BY count DESC, family
"""

_MEDIUM_COUNTS_SQL = """
    SELECT item->>'family' AS family, item->>'medium' AS medium, COUNT(DISTINCT release_id) AS count
    FROM (
        SELECT DISTINCT release_id, media
        FROM user_collections
        WHERE user_id = %s::uuid AND media IS NOT NULL
    ) AS collection_media, jsonb_array_elements(collection_media.media->'items') AS item
    WHERE item->>'medium' IS NOT NULL
    GROUP BY family, medium
    ORDER BY family, count DESC, medium
"""


async def get_collection_media_summary(pg_pool: Any, user_id: str) -> dict[str, list[dict[str, Any]]]:
    """Return the media families and mediums present in a user's synced collection.

    Args:
        pg_pool: The async PostgreSQL pool.
        user_id: The authenticated user's id.

    Returns:
        ``{"families": [{"id", "count"}, ...], "mediums": [{"id", "label", "family", "count"}, ...]}``,
        counting distinct releases (not physical copies) per family/medium.
    """
    async with pg_pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await execute_sql(cur, _FAMILY_COUNTS_SQL, (user_id,))
        family_rows = await cur.fetchall()
        await execute_sql(cur, _MEDIUM_COUNTS_SQL, (user_id,))
        medium_rows = await cur.fetchall()

    families = [{"id": row["family"], "count": row["count"]} for row in family_rows]
    mediums = []
    for row in medium_rows:
        medium_id = row["medium"]
        try:
            label = medium_label(medium_id)
        except KeyError:
            # A medium id the collection carries but the currently-vendored
            # taxonomy no longer knows (vocabulary rolled forward) — fall
            # back to the raw id rather than failing the whole response.
            label = medium_id
        mediums.append({"id": medium_id, "label": label, "family": row["family"], "count": row["count"]})

    return {"families": families, "mediums": mediums}
