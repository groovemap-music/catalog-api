"""PostgreSQL graph-view reads for CrateFit's collection and release context."""

from __future__ import annotations

from typing import Any, cast

from common.query_debug import execute_sql

from api.queries.fit_queries import COLLECTION_CACHE_TTL, collection_cache_key, fold_collection


_YEAR = "CASE WHEN btrim(release.year) ~ '^[0-9]{1,9}$' THEN btrim(release.year)::integer END"

COLLECTION_SQL = """
WITH held AS (
    SELECT DISTINCT collected.release_id::text AS release_id
    FROM graph.collected collected WHERE collected.user_id = %(user_id)s::uuid
)
SELECT release.release_id, release.title,
       ARRAY(SELECT DISTINCT edge.artist_id FROM graph.by_artist edge
             WHERE edge.release_id = release.release_id ORDER BY edge.artist_id),
       ARRAY(SELECT DISTINCT edge.label_id FROM graph.on_label edge
             WHERE edge.release_id = release.release_id ORDER BY edge.label_id),
       ARRAY(SELECT DISTINCT edge.genre_name FROM graph.in_genre edge
             WHERE edge.release_id = release.release_id ORDER BY edge.genre_name),
       ARRAY(SELECT DISTINCT edge.style_name FROM graph.in_style edge
             WHERE edge.release_id = release.release_id ORDER BY edge.style_name),
       ARRAY(SELECT DISTINCT edge.master_id FROM graph.derived_from edge
             WHERE edge.release_id = release.release_id ORDER BY edge.master_id)
FROM held JOIN graph.release release USING (release_id)
ORDER BY release.release_id
"""

RELEASE_CONTEXT_SQL = f"""
SELECT release.release_id, release.title, {_YEAR} AS year,
       COALESCE((SELECT jsonb_agg(jsonb_build_object('id', artist.artist_id, 'name', artist.name)
                                  ORDER BY artist.artist_id)
                 FROM graph.by_artist edge JOIN graph.artist artist USING (artist_id)
                 WHERE edge.release_id = release.release_id), '[]'::jsonb),
       COALESCE((SELECT jsonb_agg(jsonb_build_object('id', label.label_id, 'name', label.name)
                                  ORDER BY label.label_id)
                 FROM graph.on_label edge JOIN graph.label label USING (label_id)
                 WHERE edge.release_id = release.release_id), '[]'::jsonb),
       ARRAY(SELECT DISTINCT edge.genre_name FROM graph.in_genre edge
             WHERE edge.release_id = release.release_id ORDER BY edge.genre_name),
       ARRAY(SELECT DISTINCT edge.style_name FROM graph.in_style edge
             WHERE edge.release_id = release.release_id ORDER BY edge.style_name),
       release.media_families,
       derived.master_id, master.title,
       COALESCE((SELECT jsonb_agg(jsonb_build_object(
                      'id', sibling.release_id, 'title', sibling.title, 'year',
                      CASE WHEN btrim(sibling.year) ~ '^[0-9]{{1,9}}$' THEN btrim(sibling.year)::integer END,
                      'media_families', sibling.media_families)
                    ORDER BY sibling.release_id)
                 FROM graph.derived_from sibling_edge
                 JOIN graph.release sibling ON sibling.release_id = sibling_edge.release_id
                 WHERE sibling_edge.master_id = derived.master_id
                   AND sibling.release_id <> release.release_id), '[]'::jsonb)
FROM graph.release release
LEFT JOIN graph.derived_from derived ON derived.release_id = release.release_id
LEFT JOIN graph.master master ON master.master_id = derived.master_id
WHERE release.release_id = %(release_id)s
"""  # noqa: S608 -- interpolates only a static guarded year expression


async def _rows(pool: Any, sql: str, params: dict[str, Any]) -> list[tuple[Any, ...]]:
    async with pool.connection() as conn, conn.cursor() as cursor_cm:
        cursor = cast("Any", cursor_cm)
        await execute_sql(cursor, sql, params)
        return cast("list[tuple[Any, ...]]", await cursor.fetchall())


async def get_collection_ids(pool: Any, user_id: str, *, cache: Any = None) -> dict[str, Any]:
    key = collection_cache_key(user_id)
    if cache is not None:
        cached = await cache.get(key)
        if cached is not None:
            return cast("dict[str, Any]", cached)
    rows = await _rows(pool, COLLECTION_SQL, {"user_id": user_id})
    folded = fold_collection(
        [
            {"release_id": rid, "title": title, "artist_ids": artists, "label_ids": labels, "genres": genres, "styles": styles, "master_ids": masters}
            for rid, title, artists, labels, genres, styles, masters in rows
        ]
    )
    if cache is not None:
        await cache.set(key, folded, ttl=COLLECTION_CACHE_TTL)
    return folded


async def get_release_context(pool: Any, release_id: str) -> dict[str, Any] | None:
    rows = await _rows(pool, RELEASE_CONTEXT_SQL, {"release_id": release_id})
    if not rows:
        return None
    rid, title, year, artists, labels, genres, styles, families, master_id, master_title, siblings = rows[0]
    return {
        "id": str(rid),
        "title": title,
        "year": year,
        "artists": list(artists or []),
        "labels": list(labels or []),
        "genres": list(genres or []),
        "styles": list(styles or []),
        "media_families": list(families or []),
        "master_id": str(master_id) if master_id else None,
        "master_title": master_title,
        "siblings": list(siblings or []),
    }
