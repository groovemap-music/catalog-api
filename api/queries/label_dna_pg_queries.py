"""PostgreSQL implementation of the label-DNA query family.

The relational graph keeps the same ownership boundaries as Neo4j: ``on_label``
connects releases to labels, ``in_genre``/``in_style`` carry tags, and media is
the complete ``issued_on`` -> ``medium`` -> ``in_family`` -> ``media_family``
path. The media statements use SQL/PGQ so a missing endpoint cannot masquerade
as a valid edge. Document properties such as year, formats, and the pre-cutover
media-family fallback remain ordinary SQL over ``graph.release``.
"""

from __future__ import annotations

import asyncio
from typing import Any, cast

from common.query_debug import execute_sql


MIN_RELEASES = 5
_CANDIDATE_BATCH_SIZE = 25


LABEL_IDENTITY_SQL = """
SELECT label_id, name AS label_name, release_count, artist_count
FROM graph.label_vertex
WHERE label_id = %(label_id)s
"""

LABEL_GENRE_PROFILE_SQL = """
SELECT genre.genre_name AS name, count(DISTINCT label.release_id)::bigint AS count
FROM graph.on_label AS label
JOIN graph.in_genre AS genre ON genre.release_id = label.release_id
WHERE label.label_id = %(label_id)s
GROUP BY genre.genre_name
ORDER BY count DESC, name
"""

LABEL_STYLE_PROFILE_SQL = """
SELECT style.style_name AS name, count(DISTINCT label.release_id)::bigint AS count
FROM graph.on_label AS label
JOIN graph.in_style AS style ON style.release_id = label.release_id
WHERE label.label_id = %(label_id)s
GROUP BY style.style_name
ORDER BY count DESC, name
"""

# Neo4j stores ``year`` as an integer and applies ``year > 0``. PostgreSQL
# publishes the normalized JSON value as text, so the CASE protects the cast
# while deliberately leaving plausibility policy with the importer.
_DATED_LABEL_RELEASES = """
SELECT label.release_id,
       CASE
           WHEN btrim(release.year) ~ '^[0-9]{1,9}$'
               THEN btrim(release.year)::integer
       END AS year
FROM graph.on_label AS label
JOIN graph.release AS release ON release.release_id = label.release_id
WHERE label.label_id = %(label_id)s
"""

LABEL_DECADE_PROFILE_SQL = f"""
WITH dated AS ({_DATED_LABEL_RELEASES})
SELECT (year / 10) * 10 AS decade, count(DISTINCT release_id)::bigint AS count
FROM dated
WHERE year > 0
GROUP BY decade
ORDER BY decade
"""  # noqa: S608 - interpolates only the static CTE above

LABEL_ACTIVE_YEARS_SQL = f"""
WITH dated AS ({_DATED_LABEL_RELEASES})
SELECT DISTINCT year
FROM dated
WHERE year > 0
ORDER BY year
"""  # noqa: S608 - interpolates only the static CTE above

LABEL_FORMAT_PROFILE_SQL = """
SELECT format.name AS name, count(DISTINCT label.release_id)::bigint AS count
FROM graph.on_label AS label
JOIN graph.release AS release ON release.release_id = label.release_id
CROSS JOIN LATERAL unnest(release.formats) AS format(name)
WHERE label.label_id = %(label_id)s
GROUP BY format.name
ORDER BY count DESC, name
"""

# This is the acceptance-critical three-hop traversal. Binding the medium and
# family vertices, rather than grouping the edge-table columns directly, makes
# the PostgreSQL answer follow Neo4j when an endpoint is missing.
LABEL_MEDIA_FAMILY_COUNTS_SQL = """
SELECT family_name AS family, count(DISTINCT release_id)::bigint AS count
FROM GRAPH_TABLE (graph.catalog
    MATCH (label IS label WHERE label.label_id = %(label_id)s)
          <-[IS on_label]-(release IS release)
          -[IS issued_on]->(medium IS medium)
          -[IS in_family]->(family IS media_family)
    COLUMNS (release.release_id AS release_id, family.name AS family_name)
) AS media_path
GROUP BY family_name
ORDER BY count DESC, family
"""

LABEL_MEDIUM_COUNTS_SQL = """
SELECT family_name AS family,
       medium_id,
       medium_label,
       count(DISTINCT release_id)::bigint AS count
FROM GRAPH_TABLE (graph.catalog
    MATCH (label IS label WHERE label.label_id = %(label_id)s)
          <-[IS on_label]-(release IS release)
          -[IS issued_on]->(medium IS medium)
          -[IS in_family]->(family IS media_family)
    COLUMNS (
        release.release_id AS release_id,
        family.name AS family_name,
        medium.medium_id AS medium_id,
        medium.label AS medium_label
    )
) AS media_path
GROUP BY family_name, medium_id, medium_label
ORDER BY family, count DESC, medium_id
"""

LABEL_MEDIA_FAMILIES_FALLBACK_SQL = """
SELECT family, count(DISTINCT release.release_id)::bigint AS count
FROM graph.on_label AS label
JOIN graph.release AS release ON release.release_id = label.release_id
CROSS JOIN LATERAL unnest(release.media_families) AS family
WHERE label.label_id = %(label_id)s
GROUP BY family
ORDER BY count DESC, family
"""

# Phase one mirrors the Cypher exactly: take the target's five most common
# styles, score every other label by releases shared inside each style, sum the
# per-style scores, require five, and cap at one hundred candidates.
CANDIDATE_LABELS_SQL = """
WITH target_styles AS (
    SELECT style.style_name, count(DISTINCT label.release_id)::bigint AS style_count
    FROM graph.on_label AS label
    JOIN graph.in_style AS style ON style.release_id = label.release_id
    WHERE label.label_id = %(label_id)s
    GROUP BY style.style_name
    ORDER BY style_count DESC, style.style_name
    LIMIT 5
), per_style AS (
    SELECT candidate.label_id,
           candidate_label.name AS label_name,
           target.style_name,
           count(DISTINCT candidate.release_id)::bigint AS shared_in_style
    FROM target_styles AS target
    JOIN graph.in_style AS style ON style.style_name = target.style_name
    JOIN graph.on_label AS candidate ON candidate.release_id = style.release_id
    JOIN graph.label AS candidate_label ON candidate_label.label_id = candidate.label_id
    WHERE candidate.label_id <> %(label_id)s
    GROUP BY candidate.label_id, candidate_label.name, target.style_name
), ranked AS (
    SELECT label_id, label_name, sum(shared_in_style)::bigint AS total_shared
    FROM per_style
    GROUP BY label_id, label_name
    HAVING sum(shared_in_style) >= %(min_releases)s
    ORDER BY total_shared DESC, label_id
    LIMIT 100
)
SELECT label_id, label_name, total_shared
FROM ranked
ORDER BY total_shared DESC, label_id
"""

# Phase two keeps the candidate order from phase one. A correlated aggregate
# returns ``[]`` for labels without genre tags, matching the Neo4j assembly's
# ``genre_map.get(label_id, [])``.
CANDIDATE_PROFILES_SQL = """
WITH selected(label_id, position) AS (
    SELECT label_id, position
    FROM unnest(%(label_ids)s::text[]) WITH ORDINALITY AS candidate(label_id, position)
)
SELECT label.label_id,
       label.name AS label_name,
       (SELECT count(DISTINCT on_label.release_id)::bigint
          FROM graph.on_label AS on_label
         WHERE on_label.label_id = label.label_id) AS release_count,
       COALESCE(
           (SELECT json_agg(
                       json_build_object('name', genre.name, 'count', genre.count)
                       ORDER BY genre.count DESC, genre.name
                   )
              FROM (
                  SELECT in_genre.genre_name AS name,
                         count(DISTINCT on_label.release_id)::bigint AS count
                  FROM graph.on_label AS on_label
                  JOIN graph.in_genre AS in_genre ON in_genre.release_id = on_label.release_id
                  WHERE on_label.label_id = label.label_id
                  GROUP BY in_genre.genre_name
              ) AS genre),
           '[]'::json
       ) AS genres
FROM selected
JOIN graph.label AS label ON label.label_id = selected.label_id
ORDER BY selected.position
"""


async def _rows(pool: Any, sql: str, params: dict[str, Any]) -> list[tuple[Any, ...]]:
    """Run one statement and return its positional rows."""
    async with pool.connection() as conn, conn.cursor() as cursor_cm:
        cursor = cast("Any", cursor_cm)
        await execute_sql(cursor, sql, params)
        rows = await cursor.fetchall()
    return cast("list[tuple[Any, ...]]", rows)


async def get_label_identity(pool: Any, label_id: str) -> dict[str, Any] | None:
    rows = await _rows(pool, LABEL_IDENTITY_SQL, {"label_id": label_id})
    if not rows:
        return None
    row = rows[0]
    return {"label_id": row[0], "label_name": row[1], "release_count": row[2], "artist_count": row[3]}


async def get_label_genre_profile(pool: Any, label_id: str) -> list[dict[str, Any]]:
    rows = await _rows(pool, LABEL_GENRE_PROFILE_SQL, {"label_id": label_id})
    return [{"name": row[0], "count": row[1]} for row in rows]


async def get_label_style_profile(pool: Any, label_id: str) -> list[dict[str, Any]]:
    rows = await _rows(pool, LABEL_STYLE_PROFILE_SQL, {"label_id": label_id})
    return [{"name": row[0], "count": row[1]} for row in rows]


async def get_label_decade_profile(pool: Any, label_id: str) -> list[dict[str, Any]]:
    rows = await _rows(pool, LABEL_DECADE_PROFILE_SQL, {"label_id": label_id})
    return [{"decade": row[0], "count": row[1]} for row in rows]


async def get_label_active_years(pool: Any, label_id: str) -> list[int]:
    rows = await _rows(pool, LABEL_ACTIVE_YEARS_SQL, {"label_id": label_id})
    return [row[0] for row in rows]


async def get_label_format_profile(pool: Any, label_id: str) -> list[dict[str, Any]]:
    rows = await _rows(pool, LABEL_FORMAT_PROFILE_SQL, {"label_id": label_id})
    return [{"name": row[0], "count": row[1]} for row in rows]


async def get_label_media_family_counts(pool: Any, label_id: str) -> list[dict[str, Any]]:
    rows = await _rows(pool, LABEL_MEDIA_FAMILY_COUNTS_SQL, {"label_id": label_id})
    return [{"family": row[0], "count": row[1]} for row in rows]


async def get_label_medium_counts(pool: Any, label_id: str) -> list[dict[str, Any]]:
    rows = await _rows(pool, LABEL_MEDIUM_COUNTS_SQL, {"label_id": label_id})
    return [{"family": row[0], "medium_id": row[1], "medium_label": row[2], "count": row[3]} for row in rows]


async def get_label_media_families_fallback(pool: Any, label_id: str) -> list[dict[str, Any]]:
    rows = await _rows(pool, LABEL_MEDIA_FAMILIES_FALLBACK_SQL, {"label_id": label_id})
    return [{"family": row[0], "count": row[1]} for row in rows]


async def get_label_media_profile(pool: Any, label_id: str) -> list[dict[str, Any]]:
    families, mediums = await asyncio.gather(
        get_label_media_family_counts(pool, label_id),
        get_label_medium_counts(pool, label_id),
    )
    if not families:
        fallback = await get_label_media_families_fallback(pool, label_id)
        return [{"family": row["family"], "count": row["count"], "mediums": []} for row in fallback]

    mediums_by_family: dict[str, list[dict[str, Any]]] = {}
    for row in mediums:
        mediums_by_family.setdefault(row["family"], []).append({"id": row["medium_id"], "label": row["medium_label"], "count": row["count"]})
    return [
        {
            "family": row["family"],
            "count": row["count"],
            "mediums": mediums_by_family.get(row["family"], []),
        }
        for row in families
    ]


async def get_label_full_profile(pool: Any, label_id: str) -> dict[str, Any] | None:
    identity = await get_label_identity(pool, label_id)
    if not identity:
        return None
    if identity["release_count"] < MIN_RELEASES:
        return {**identity, "genres": [], "styles": [], "decades": []}

    genres, styles, decades = await asyncio.gather(
        get_label_genre_profile(pool, label_id),
        get_label_style_profile(pool, label_id),
        get_label_decade_profile(pool, label_id),
    )
    return {**identity, "genres": genres, "styles": styles, "decades": decades}


async def get_candidate_labels_genre_vectors(pool: Any, label_id: str) -> list[dict[str, Any]]:
    candidates = await _rows(
        pool,
        CANDIDATE_LABELS_SQL,
        {"label_id": label_id, "min_releases": MIN_RELEASES},
    )
    if not candidates:
        return []

    candidate_ids = [str(row[0]) for row in candidates]
    batches = [candidate_ids[index : index + _CANDIDATE_BATCH_SIZE] for index in range(0, len(candidate_ids), _CANDIDATE_BATCH_SIZE)]
    batch_rows = await asyncio.gather(*[_rows(pool, CANDIDATE_PROFILES_SQL, {"label_ids": batch}) for batch in batches])
    return [
        {
            "label_id": row[0],
            "label_name": row[1],
            "release_count": row[2],
            "genres": row[3],
        }
        for batch in batch_rows
        for row in batch
    ]
