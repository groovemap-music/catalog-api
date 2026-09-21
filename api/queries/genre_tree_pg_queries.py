"""PostgreSQL genre/style co-occurrence tree."""

from __future__ import annotations

from typing import Any, cast

from common.query_debug import execute_sql


GENRE_TREE_SQL = """
WITH genre_counts AS (
    SELECT g.name, count(DISTINCT e.release_id) AS release_count
    FROM graph.genre g JOIN graph.in_genre e ON e.genre_name = g.name
    GROUP BY g.name
), style_counts AS (
    SELECT g.genre_name, s.style_name, count(DISTINCT g.release_id) AS release_count
    FROM graph.in_genre g JOIN graph.in_style s USING (release_id)
    GROUP BY g.genre_name, s.style_name
)
SELECT genres.name, genres.release_count,
       COALESCE(jsonb_agg(jsonb_build_object('name', styles.style_name,
                                             'release_count', styles.release_count)
                          ORDER BY styles.release_count DESC, styles.style_name)
                FILTER (WHERE styles.style_name IS NOT NULL), '[]'::jsonb) AS styles
FROM genre_counts genres LEFT JOIN style_counts styles ON styles.genre_name = genres.name
GROUP BY genres.name, genres.release_count
ORDER BY genres.release_count DESC, genres.name
"""


async def get_genre_tree(pool: Any) -> list[dict[str, Any]]:
    async with pool.connection() as conn, conn.cursor() as cursor_cm:
        cursor = cast("Any", cursor_cm)
        await execute_sql(cursor, GENRE_TREE_SQL)
        return [{"name": name, "release_count": count, "styles": styles} for name, count, styles in await cursor.fetchall()]
