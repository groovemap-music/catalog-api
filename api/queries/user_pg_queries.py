"""PostgreSQL implementation of collection and wantlist query families."""

from __future__ import annotations

import math
from itertools import pairwise
from typing import Any, cast

from common.query_debug import execute_sql

from api.queries.user_queries import attach_release_identity


_YEAR = "CASE WHEN btrim(release.year) ~ '^[0-9]{1,9}$' THEN btrim(release.year)::integer END"

USER_COLLECTION_SQL = f"""
SELECT collected.release_id, release.title, {_YEAR} AS year, release.catalog_number,
       (SELECT min(artist.name) FROM graph.by_artist edge JOIN graph.artist artist USING (artist_id)
         WHERE edge.release_id = collected.release_id) AS artist,
       (SELECT min(label.name) FROM graph.on_label edge JOIN graph.label label USING (label_id)
         WHERE edge.release_id = collected.release_id) AS label,
       ARRAY(SELECT DISTINCT edge.genre_name FROM graph.in_genre edge
              WHERE edge.release_id = collected.release_id ORDER BY edge.genre_name) AS genres,
       ARRAY(SELECT DISTINCT edge.style_name FROM graph.in_style edge
              WHERE edge.release_id = collected.release_id ORDER BY edge.style_name) AS styles,
       collected.rating,
       to_char(collected.date_added AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"') AS date_added,
       collected.folder_id
FROM graph.collected collected
JOIN graph.release release ON release.release_id = collected.release_id::text
WHERE collected.user_id = %(user_id)s::uuid
ORDER BY collected.date_added DESC NULLS LAST, collected.release_id
OFFSET %(offset)s LIMIT %(limit)s
"""  # noqa: S608 -- interpolates only the static guarded year expression

USER_COLLECTION_COUNT_SQL = "SELECT count(*)::bigint FROM graph.collected WHERE user_id = %(user_id)s::uuid"

USER_WANTLIST_SQL = f"""
SELECT wants.release_id, release.title, {_YEAR} AS year, release.catalog_number,
       (SELECT min(artist.name) FROM graph.by_artist edge JOIN graph.artist artist USING (artist_id)
         WHERE edge.release_id = wants.release_id) AS artist,
       (SELECT min(label.name) FROM graph.on_label edge JOIN graph.label label USING (label_id)
         WHERE edge.release_id = wants.release_id) AS label,
       ARRAY(SELECT DISTINCT edge.genre_name FROM graph.in_genre edge
              WHERE edge.release_id = wants.release_id ORDER BY edge.genre_name) AS genres,
       ARRAY(SELECT DISTINCT edge.style_name FROM graph.in_style edge
              WHERE edge.release_id = wants.release_id ORDER BY edge.style_name) AS styles,
       wants.rating,
       to_char(wants.date_added AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"') AS date_added
FROM graph.wants wants
JOIN graph.release release ON release.release_id = wants.release_id::text
WHERE wants.user_id = %(user_id)s::uuid
ORDER BY wants.date_added DESC NULLS LAST, wants.release_id
OFFSET %(offset)s LIMIT %(limit)s
"""  # noqa: S608 -- interpolates only the static guarded year expression

USER_WANTLIST_COUNT_SQL = "SELECT count(*)::bigint FROM graph.wants WHERE user_id = %(user_id)s::uuid"

# Both anti-joins compare the candidate release endpoint against the user's edge
# endpoints. No edge alias from the positive traversal is reused in either exclusion.
USER_RECOMMENDATIONS_SQL = f"""
WITH favorite_artists AS (
    SELECT by_artist.artist_id, count(*)::bigint AS collected_count
    FROM graph.collected collected
    JOIN graph.by_artist by_artist ON by_artist.release_id = collected.release_id::text
    WHERE collected.user_id = %(user_id)s::uuid
    GROUP BY by_artist.artist_id
    ORDER BY collected_count DESC, by_artist.artist_id
    LIMIT 10
), candidates AS (
    SELECT by_artist.release_id, sum(favorite.collected_count)::bigint AS score
    FROM favorite_artists favorite
    JOIN graph.by_artist by_artist ON by_artist.artist_id = favorite.artist_id
    WHERE NOT EXISTS (
              SELECT 1 FROM graph.collected owned
              WHERE owned.user_id = %(user_id)s::uuid
                AND owned.release_id::text = by_artist.release_id
          )
      AND NOT EXISTS (
              SELECT 1 FROM graph.wants wanted
              WHERE wanted.user_id = %(user_id)s::uuid
                AND wanted.release_id::text = by_artist.release_id
          )
    GROUP BY by_artist.release_id
)
SELECT candidate.release_id, release.title, {_YEAR} AS year,
       (SELECT min(artist.name) FROM graph.by_artist edge JOIN graph.artist artist USING (artist_id)
         WHERE edge.release_id = candidate.release_id) AS artist,
       (SELECT min(label.name) FROM graph.on_label edge JOIN graph.label label USING (label_id)
         WHERE edge.release_id = candidate.release_id) AS label,
       ARRAY(SELECT DISTINCT edge.genre_name FROM graph.in_genre edge
              WHERE edge.release_id = candidate.release_id ORDER BY edge.genre_name) AS genres,
       candidate.score
FROM candidates candidate
JOIN graph.release release ON release.release_id = candidate.release_id
ORDER BY candidate.score DESC, candidate.release_id
LIMIT %(limit)s
"""  # noqa: S608 -- interpolates only the static guarded year expression

USER_COLLECTION_STATS_SQL = """
WITH owned AS (
    SELECT collected.release_id::text AS release_id, collected.rating
    FROM graph.collected collected
    WHERE collected.user_id = %(user_id)s::uuid
), genres AS (
    SELECT edge.genre_name AS name, count(*)::bigint AS count
    FROM owned JOIN graph.in_genre edge USING (release_id)
    GROUP BY edge.genre_name ORDER BY count DESC, name LIMIT 20
), decades AS (
    SELECT (year / 10) * 10 AS decade, count(*)::bigint AS count
    FROM (SELECT CASE WHEN btrim(release.year) ~ '^[0-9]{1,9}$' THEN btrim(release.year)::integer END AS year
          FROM owned JOIN graph.release release USING (release_id)) dated
    WHERE year IS NOT NULL GROUP BY decade ORDER BY decade
), labels AS (
    SELECT label.name, count(*)::bigint AS count
    FROM owned JOIN graph.on_label edge USING (release_id) JOIN graph.label label USING (label_id)
    GROUP BY label.name ORDER BY count DESC, label.name LIMIT 20
)
SELECT (SELECT count(*)::bigint FROM owned) AS total,
       (SELECT count(DISTINCT edge.artist_id)::bigint FROM owned JOIN graph.by_artist edge USING (release_id)) AS unique_artists,
       (SELECT count(DISTINCT edge.label_id)::bigint FROM owned JOIN graph.on_label edge USING (release_id)) AS unique_labels,
       (SELECT avg(rating) FROM owned WHERE rating > 0) AS average_rating,
       COALESCE((SELECT jsonb_agg(jsonb_build_object('name', name, 'count', count) ORDER BY count DESC, name) FROM genres), '[]') AS genres,
       COALESCE((SELECT jsonb_agg(jsonb_build_object('decade', decade, 'count', count) ORDER BY decade) FROM decades), '[]') AS decades,
       COALESCE((SELECT jsonb_agg(jsonb_build_object('name', name, 'count', count) ORDER BY count DESC, name) FROM labels), '[]') AS labels
"""

USER_COLLECTION_TIMELINE_SQL = """
WITH owned AS (
    SELECT DISTINCT collected.release_id::text AS release_id
    FROM graph.collected collected
    WHERE collected.user_id = %(user_id)s::uuid
), releases AS (
    SELECT owned.release_id,
           CASE WHEN btrim(release.year) ~ '^[0-9]{1,9}$' THEN btrim(release.year)::integer END AS year
    FROM owned JOIN graph.release release USING (release_id)
), enriched AS (
    SELECT releases.release_id, (year / %(multiplier)s) * %(multiplier)s AS bucket,
           ARRAY(SELECT DISTINCT genre_name FROM graph.in_genre WHERE release_id = releases.release_id ORDER BY genre_name) AS genres,
           ARRAY(SELECT DISTINCT style_name FROM graph.in_style WHERE release_id = releases.release_id ORDER BY style_name) AS styles,
           ARRAY(SELECT DISTINCT label.name FROM graph.on_label edge JOIN graph.label label USING (label_id)
                 WHERE edge.release_id = releases.release_id ORDER BY label.name) AS labels
    FROM releases WHERE year > 0
)
SELECT bucket, count(*)::bigint,
       COALESCE(jsonb_agg(to_jsonb(genres) ORDER BY release_id), '[]') AS genres,
       COALESCE(jsonb_agg(to_jsonb(styles) ORDER BY release_id), '[]') AS styles,
       COALESCE(jsonb_agg(to_jsonb(labels) ORDER BY release_id), '[]') AS labels
FROM enriched GROUP BY bucket ORDER BY bucket
"""

_EVOLUTION_SQL = {
    "genre": """SELECT year, edge.genre_name AS value, count(*)::bigint AS count FROM graph.collected collected JOIN graph.release release ON release.release_id = collected.release_id::text JOIN graph.in_genre edge ON edge.release_id = release.release_id WHERE collected.user_id = %(user_id)s::uuid AND btrim(release.year) ~ '^[1-9][0-9]*$' GROUP BY year, value ORDER BY year::integer, count DESC, value""",
    "style": """SELECT year, edge.style_name AS value, count(*)::bigint AS count FROM graph.collected collected JOIN graph.release release ON release.release_id = collected.release_id::text JOIN graph.in_style edge ON edge.release_id = release.release_id WHERE collected.user_id = %(user_id)s::uuid AND btrim(release.year) ~ '^[1-9][0-9]*$' GROUP BY year, value ORDER BY year::integer, count DESC, value""",
    "label": """SELECT year, label.name AS value, count(*)::bigint AS count FROM graph.collected collected JOIN graph.release release ON release.release_id = collected.release_id::text JOIN graph.on_label edge ON edge.release_id = release.release_id JOIN graph.label label USING (label_id) WHERE collected.user_id = %(user_id)s::uuid AND btrim(release.year) ~ '^[1-9][0-9]*$' GROUP BY year, value ORDER BY year::integer, count DESC, value""",
}

USER_STATUS_SQL = """
SELECT requested.release_id,
       EXISTS (SELECT 1 FROM graph.collected c WHERE c.user_id = %(user_id)s::uuid AND c.release_id::text = requested.release_id),
       EXISTS (SELECT 1 FROM graph.wants w WHERE w.user_id = %(user_id)s::uuid AND w.release_id::text = requested.release_id)
FROM unnest(%(release_ids)s::text[]) WITH ORDINALITY AS requested(release_id, position)
ORDER BY requested.position
"""


async def _rows(pool: Any, sql: str, params: dict[str, Any]) -> list[tuple[Any, ...]]:
    async with pool.connection() as conn, conn.cursor() as cursor_cm:
        cursor = cast("Any", cursor_cm)
        await execute_sql(cursor, sql, params)
        return cast("list[tuple[Any, ...]]", await cursor.fetchall())


async def _scalar(pool: Any, sql: str, params: dict[str, Any]) -> Any:
    rows = await _rows(pool, sql, params)
    return rows[0][0] if rows else 0


async def get_user_collection(pool: Any, user_id: str, limit: int = 50, offset: int = 0) -> tuple[list[dict[str, Any]], int]:
    rows = await _rows(pool, USER_COLLECTION_SQL, {"user_id": user_id, "limit": limit, "offset": offset})
    total = await _scalar(pool, USER_COLLECTION_COUNT_SQL, {"user_id": user_id})
    results = [
        {
            "id": r[0],
            "title": r[1],
            "year": r[2],
            "catalog_number": r[3],
            "artist": r[4],
            "label": r[5],
            "genres": r[6],
            "styles": r[7],
            "rating": r[8],
            "date_added": r[9],
            "folder_id": r[10],
        }
        for r in rows
    ]
    return await attach_release_identity(results, user_id=user_id, include_owned_copy=True), total


async def get_user_wantlist(pool: Any, user_id: str, limit: int = 50, offset: int = 0) -> tuple[list[dict[str, Any]], int]:
    rows = await _rows(pool, USER_WANTLIST_SQL, {"user_id": user_id, "limit": limit, "offset": offset})
    total = await _scalar(pool, USER_WANTLIST_COUNT_SQL, {"user_id": user_id})
    results = [
        {
            "id": r[0],
            "title": r[1],
            "year": r[2],
            "catalog_number": r[3],
            "artist": r[4],
            "label": r[5],
            "genres": r[6],
            "styles": r[7],
            "rating": r[8],
            "date_added": r[9],
        }
        for r in rows
    ]
    return await attach_release_identity(results), total


async def get_user_recommendations(pool: Any, user_id: str, limit: int = 20) -> list[dict[str, Any]]:
    rows = await _rows(pool, USER_RECOMMENDATIONS_SQL, {"user_id": user_id, "limit": limit})
    return [{"id": r[0], "title": r[1], "year": r[2], "artist": r[3], "label": r[4], "genres": r[5], "score": r[6]} for r in rows]


async def get_user_collection_stats(pool: Any, user_id: str) -> dict[str, Any]:
    rows = await _rows(pool, USER_COLLECTION_STATS_SQL, {"user_id": user_id})
    row = rows[0]
    return {
        "total": row[0],
        "unique_artists": row[1],
        "unique_labels": row[2],
        "average_rating": float(row[3]) if row[3] is not None else None,
        "by_genre": row[4],
        "by_decade": row[5],
        "by_label": row[6],
    }


async def get_user_collection_timeline(pool: Any, user_id: str, bucket: str = "year") -> dict[str, Any]:
    multiplier = 10 if bucket == "decade" else 1
    rows = await _rows(pool, USER_COLLECTION_TIMELINE_SQL, {"user_id": user_id, "multiplier": multiplier})
    timeline: list[dict[str, Any]] = []
    genre_totals: dict[str, int] = {}
    styles_by_bucket: list[set[str]] = []
    for year, count, genre_lists, style_lists, label_lists in rows:
        genre_counts: dict[str, int] = {}
        for genres in genre_lists:
            for genre in genres:
                genre_counts[genre] = genre_counts.get(genre, 0) + 1
                genre_totals[genre] = genre_totals.get(genre, 0) + 1
        style_set = {style for styles in style_lists for style in styles}
        label_counts: dict[str, int] = {}
        for labels in label_lists:
            for label in labels:
                label_counts[label] = label_counts.get(label, 0) + 1
        styles_by_bucket.append(style_set)
        timeline.append(
            {
                "year": year,
                "count": count,
                "genres": genre_counts,
                "top_labels": sorted(label_counts, key=lambda name: (-label_counts[name], name))[:5],
                "top_styles": sorted(style_set)[:10],
            }
        )
    peak_year = max(timeline, key=lambda item: item["count"])["year"] if timeline else None
    dominant_genre = max(genre_totals, key=lambda name: (genre_totals[name], name)) if genre_totals else None
    total_genres = sum(genre_totals.values())
    diversity = 0.0
    if total_genres:
        for count in genre_totals.values():
            probability = count / total_genres
            diversity -= probability * math.log2(probability)
        max_entropy = math.log2(len(genre_totals)) if len(genre_totals) > 1 else 1.0
        diversity = round(diversity / max_entropy, 2) if max_entropy else 0.0
    distances = []
    for previous, current in pairwise(styles_by_bucket):
        union = previous | current
        if union:
            distances.append(1.0 - len(previous & current) / len(union))
    return {
        "timeline": timeline,
        "insights": {
            "peak_year": peak_year,
            "dominant_genre": dominant_genre,
            "genre_diversity_score": diversity,
            "style_drift_rate": round(sum(distances) / len(distances), 2) if distances else 0.0,
        },
    }


async def get_user_collection_evolution(pool: Any, user_id: str, metric: str = "genre") -> dict[str, Any]:
    if metric not in _EVOLUTION_SQL:
        raise ValueError(f"Invalid metric: {metric!r}. Must be one of {set(_EVOLUTION_SQL)}")
    rows = await _rows(pool, _EVOLUTION_SQL[metric], {"user_id": user_id})
    years: dict[int, dict[str, int]] = {}
    values: set[str] = set()
    for raw_year, value, count in rows:
        year = int(raw_year)
        years.setdefault(year, {})[value] = count
        values.add(value)
    return {
        "metric": metric,
        "data": [{"year": year, "values": counts} for year, counts in sorted(years.items())],
        "summary": {"total_years": len(years), "unique_values": len(values)},
    }


async def check_releases_user_status(pool: Any, user_id: str, release_ids: list[str]) -> dict[str, dict[str, bool]]:
    if not release_ids:
        return {}
    rows = await _rows(pool, USER_STATUS_SQL, {"user_id": user_id, "release_ids": release_ids})
    return {row[0]: {"in_collection": row[1], "in_wantlist": row[2]} for row in rows}
