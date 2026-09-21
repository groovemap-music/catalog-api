"""PostgreSQL implementation of the taste-fingerprint query family."""

from __future__ import annotations

from typing import Any, cast

from common.query_debug import execute_sql


COLLECTION_COUNT_SQL = "SELECT count(*)::bigint FROM graph.collected WHERE user_id = %(user_id)s::uuid"

TASTE_HEATMAP_SQL = """
SELECT genre.genre_name, (btrim(release.year)::integer / 10) * 10 AS decade, count(*)::bigint
FROM graph.collected collected
JOIN graph.release release ON release.release_id = collected.release_id::text
JOIN graph.in_genre genre ON genre.release_id = release.release_id
WHERE collected.user_id = %(user_id)s::uuid AND btrim(release.year) ~ '^[0-9]{1,9}$'
GROUP BY genre.genre_name, decade
ORDER BY count DESC, genre.genre_name, decade
"""

OBSCURITY_SQL = """
WITH owned AS (
    SELECT DISTINCT release_id::text AS release_id
    FROM graph.collected WHERE user_id = %(user_id)s::uuid
)
SELECT count(DISTINCT other.user_id)::bigint AS collectors
FROM owned
LEFT JOIN graph.collected other
  ON other.release_id::text = owned.release_id
 AND other.user_id <> %(user_id)s::uuid
GROUP BY owned.release_id
ORDER BY collectors
"""

TASTE_DRIFT_SQL = """
WITH ranked AS (
    SELECT extract(year FROM collected.date_added)::integer::text AS year,
           genre.genre_name AS genre, count(*)::bigint AS count,
           row_number() OVER (PARTITION BY extract(year FROM collected.date_added)
                              ORDER BY count(*) DESC, genre.genre_name) AS position
    FROM graph.collected collected
    JOIN graph.in_genre genre ON genre.release_id = collected.release_id::text
    WHERE collected.user_id = %(user_id)s::uuid AND collected.date_added IS NOT NULL
    GROUP BY extract(year FROM collected.date_added), genre.genre_name
)
SELECT year, genre, count FROM ranked WHERE position = 1 ORDER BY year
"""

# The candidate anti-join binds only the endpoint id. The positive path's owned
# release and edge aliases cannot be rebound, preventing every walk revisit the
# pilot template guards against while keeping this relation-only query SQL-18-safe.
BLIND_SPOTS_SQL = """
WITH favorite_artists AS (
    SELECT by_artist.artist_id, count(*)::bigint AS artist_releases
    FROM graph.collected collected
    JOIN graph.by_artist by_artist ON by_artist.release_id = collected.release_id::text
    WHERE collected.user_id = %(user_id)s::uuid
    GROUP BY by_artist.artist_id
    ORDER BY artist_releases DESC, by_artist.artist_id LIMIT 20
), candidates AS (
    SELECT genre.genre_name AS genre,
           count(DISTINCT favorite.artist_id)::bigint AS artist_overlap,
           min(release.title) AS example_release
    FROM favorite_artists favorite
    JOIN graph.by_artist by_artist ON by_artist.artist_id = favorite.artist_id
    JOIN graph.release release ON release.release_id = by_artist.release_id
    JOIN graph.in_genre genre ON genre.release_id = release.release_id
    WHERE NOT EXISTS (
        SELECT 1 FROM graph.collected owned
        WHERE owned.user_id = %(user_id)s::uuid
          AND owned.release_id::text = release.release_id
    )
    GROUP BY genre.genre_name
)
SELECT candidate.genre, candidate.artist_overlap, candidate.example_release
FROM candidates candidate
WHERE NOT EXISTS (
    SELECT 1
    FROM graph.collected owned
    JOIN graph.in_genre owned_genre ON owned_genre.release_id = owned.release_id::text
    WHERE owned.user_id = %(user_id)s::uuid
      AND owned_genre.genre_name = candidate.genre
)
ORDER BY candidate.artist_overlap DESC, candidate.genre
LIMIT %(limit)s
"""

TOP_LABELS_SQL = """
SELECT label.name, count(*)::bigint
FROM graph.collected collected
JOIN graph.on_label edge ON edge.release_id = collected.release_id::text
JOIN graph.label label USING (label_id)
WHERE collected.user_id = %(user_id)s::uuid
GROUP BY label.name ORDER BY count DESC, label.name LIMIT %(limit)s
"""


async def _rows(pool: Any, sql: str, params: dict[str, Any]) -> list[tuple[Any, ...]]:
    async with pool.connection() as conn, conn.cursor() as cursor_cm:
        cursor = cast("Any", cursor_cm)
        await execute_sql(cursor, sql, params)
        return cast("list[tuple[Any, ...]]", await cursor.fetchall())


async def get_collection_count(pool: Any, user_id: str) -> int:
    rows = await _rows(pool, COLLECTION_COUNT_SQL, {"user_id": user_id})
    return rows[0][0] if rows else 0


async def get_taste_heatmap(pool: Any, user_id: str) -> tuple[list[dict[str, Any]], int]:
    cells = await _rows(pool, TASTE_HEATMAP_SQL, {"user_id": user_id})
    return ([{"genre": row[0], "decade": row[1], "count": row[2]} for row in cells], await get_collection_count(pool, user_id))


async def get_obscurity_score(pool: Any, user_id: str) -> dict[str, Any]:
    rows = await _rows(pool, OBSCURITY_SQL, {"user_id": user_id})
    if not rows:
        return {"score": 1.0, "median_collectors": 0.0, "total_releases": 0}
    counts = sorted(row[0] for row in rows)
    middle = len(counts) // 2
    median = (counts[middle - 1] + counts[middle]) / 2.0 if len(counts) % 2 == 0 else float(counts[middle])
    maximum = max(counts)
    score = 1.0 if maximum == 0 else max(0.0, min(1.0, 1.0 - median / maximum))
    return {"score": round(score, 4), "median_collectors": median, "total_releases": len(counts)}


async def get_taste_drift(pool: Any, user_id: str) -> list[dict[str, Any]]:
    rows = await _rows(pool, TASTE_DRIFT_SQL, {"user_id": user_id})
    return [{"year": row[0], "top_genre": row[1], "count": row[2]} for row in rows]


async def get_blind_spots(pool: Any, user_id: str, limit: int = 5) -> list[dict[str, Any]]:
    rows = await _rows(pool, BLIND_SPOTS_SQL, {"user_id": user_id, "limit": limit})
    return [{"genre": row[0], "artist_overlap": row[1], "example_release": row[2]} for row in rows]


async def get_top_labels(pool: Any, user_id: str, limit: int = 10) -> list[dict[str, Any]]:
    rows = await _rows(pool, TOP_LABELS_SQL, {"user_id": user_id, "limit": limit})
    return [{"label": row[0], "count": row[1]} for row in rows]
