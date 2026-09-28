"""PostgreSQL queries for insights computations.

Each function takes an AsyncPostgreSQLPool, executes queries against
the Discogs entity tables, and returns typed results.

The four graph-backed computations mirror :mod:`api.queries.insights_neo4j_queries`
column for column.  They use the materialized graph relations rather than rebuilding
the same aggregates from JSON documents.  In particular, artist centrality reads
``graph.artist_degree``: the loader-owned, descending-indexed counter that contains the
same undirected edge count Neo4j computes with ``size([(a)-[]-() | 1])``.

All SQL queries are pre-built as string constants from a hardcoded allowlist of table
names and JSONB keys — no user input is interpolated.
"""

import asyncio
from typing import Any, cast

import structlog
from common.query_debug import execute_sql


logger = structlog.get_logger(__name__)


# ── The "insights" graph-query family ───────────────────────────────────────

# Artists without an edge have no row in graph.artist_degree, while Neo4j's MATCH over
# every Artist returns them with degree zero.  The UNION keeps that exact result set.  The
# populated branch still reads the indexed counter directly, which is the load-bearing
# replacement for counting every incident edge at request time.
ARTIST_CENTRALITY_SQL = """
WITH ranked AS (
    SELECT degree.artist_id AS artist_id,
           artist.name AS artist_name,
           degree.degree AS edge_count
    FROM graph.artist_degree AS degree
    JOIN graph.artist AS artist ON artist.artist_id = degree.artist_id
    UNION ALL
    SELECT artist.artist_id AS artist_id,
           artist.name AS artist_name,
           0::bigint AS edge_count
    FROM graph.artist AS artist
    WHERE NOT EXISTS (
        SELECT 1
        FROM graph.artist_degree AS degree
        WHERE degree.artist_id = artist.artist_id
    )
)
SELECT artist_id, artist_name, edge_count
FROM ranked
ORDER BY edge_count DESC, artist_id COLLATE "C"
LIMIT %(limit)s
"""

GENRE_TRENDS_SQL = """
SELECT edge.genre_name AS genre,
       ((btrim(release.year)::int / 10) * 10)::bigint AS decade,
       count(*)::bigint AS release_count
FROM graph.in_genre AS edge
JOIN graph.release AS release ON release.release_id = edge.release_id
WHERE btrim(release.year) ~ '^[0-9]{1,9}$'
  AND btrim(release.year)::numeric > 0
GROUP BY edge.genre_name, ((btrim(release.year)::int / 10) * 10)
ORDER BY edge.genre_name COLLATE "C", decade
"""

GENRE_TRENDS_FILTERED_SQL = """
SELECT edge.genre_name AS genre,
       ((btrim(release.year)::int / 10) * 10)::bigint AS decade,
       count(*)::bigint AS release_count
FROM graph.in_genre AS edge
JOIN graph.release AS release ON release.release_id = edge.release_id
WHERE edge.genre_name = %(genre)s
  AND btrim(release.year) ~ '^[0-9]{1,9}$'
  AND btrim(release.year)::numeric > 0
GROUP BY edge.genre_name, ((btrim(release.year)::int / 10) * 10)
ORDER BY decade
"""

# The two aggregates are deliberately separate.  ``summary`` answers the lifetime and
# release count, while ``peak`` ranks decade buckets.  The decade tiebreaker is also added
# to the Cypher implementation so both engines choose the earliest peak deterministically.
LABEL_LONGEVITY_SQL = """
WITH labeled AS (
    SELECT label.label_id,
           label.name AS label_name,
           btrim(release.year)::int AS release_year
    FROM graph.on_label AS edge
    JOIN graph.label AS label ON label.label_id = edge.label_id
    JOIN graph.release AS release ON release.release_id = edge.release_id
    WHERE btrim(release.year) ~ '^[0-9]{1,9}$'
      AND btrim(release.year)::numeric > 0
),
summary AS (
    SELECT label_id,
           label_name,
           min(release_year)::bigint AS first_year,
           max(release_year)::bigint AS last_year,
           count(*)::bigint AS total_releases
    FROM labeled
    GROUP BY label_id, label_name
),
peak AS (
    SELECT DISTINCT ON (label_id)
           label_id,
           ((release_year / 10) * 10)::bigint AS peak_decade
    FROM labeled
    GROUP BY label_id, ((release_year / 10) * 10)
    ORDER BY label_id, count(*) DESC, ((release_year / 10) * 10) ASC
)
SELECT summary.label_id,
       summary.label_name,
       summary.first_year,
       summary.last_year,
       summary.last_year - summary.first_year + 1 AS years_active,
       summary.total_releases,
       peak.peak_decade
FROM summary
JOIN peak USING (label_id)
ORDER BY years_active DESC, summary.label_id COLLATE "C"
LIMIT %(limit)s
"""

MONTHLY_ANNIVERSARIES_SQL = """
WITH target_years AS (
    SELECT unnest(%(target_years)s::int[]) AS target_year
),
anniversaries AS (
    SELECT master.master_id,
           master.title,
           btrim(master.year)::int AS release_year
    FROM target_years
    JOIN graph.master AS master
      ON btrim(master.year) ~ '^[0-9]{1,9}$'
     AND btrim(master.year)::numeric = target_years.target_year
)
SELECT anniversary.master_id,
       anniversary.title,
       min(artist.name COLLATE "C") AS artist_name,
       anniversary.release_year::bigint AS release_year
FROM anniversaries AS anniversary
LEFT JOIN graph.master_by_artist AS credit ON credit.master_id = anniversary.master_id
LEFT JOIN graph.artist AS artist ON artist.artist_id = credit.artist_id
GROUP BY anniversary.master_id, anniversary.title, anniversary.release_year
ORDER BY anniversary.release_year ASC, anniversary.master_id COLLATE "C"
"""

_ARTIST_CENTRALITY_COLUMNS = ("artist_id", "artist_name", "edge_count")
_GENRE_TRENDS_COLUMNS = ("genre", "decade", "release_count")
_LABEL_LONGEVITY_COLUMNS = (
    "label_id",
    "label_name",
    "first_year",
    "last_year",
    "years_active",
    "total_releases",
    "peak_decade",
)
_MONTHLY_ANNIVERSARIES_COLUMNS = ("master_id", "title", "artist_name", "release_year")


async def _fetch_dicts(pool: Any, sql: str, params: dict[str, Any], columns: tuple[str, ...]) -> list[dict[str, Any]]:
    """Execute one insights statement and map its fixed projection to dictionaries."""
    async with pool.connection() as conn, conn.cursor() as cursor_cm:
        cursor = cast("Any", cursor_cm)
        await execute_sql(cursor, sql, params)
        rows = await cursor.fetchall()
    return [dict(zip(columns, row, strict=True)) for row in rows]


async def query_artist_centrality(pool: Any, limit: int = 100) -> list[dict[str, Any]]:
    """Return artists ranked by the loader-owned undirected degree counter."""
    rows = await _fetch_dicts(pool, ARTIST_CENTRALITY_SQL, {"limit": limit}, _ARTIST_CENTRALITY_COLUMNS)
    logger.info("🔍 Artist centrality query complete", count=len(rows))
    return rows


async def query_genre_trends(pool: Any, genre: str | None = None) -> list[dict[str, Any]]:
    """Return release counts by genre and decade, optionally for one genre."""
    sql = GENRE_TRENDS_FILTERED_SQL if genre else GENRE_TRENDS_SQL
    params = {"genre": genre} if genre else {}
    rows = await _fetch_dicts(pool, sql, params, _GENRE_TRENDS_COLUMNS)
    logger.info("🔍 Genre trends query complete", count=len(rows), genre=genre)
    return rows


async def query_label_longevity(pool: Any, limit: int = 50) -> list[dict[str, Any]]:
    """Return labels ranked by the span between their first and last releases."""
    rows = await _fetch_dicts(pool, LABEL_LONGEVITY_SQL, {"limit": limit}, _LABEL_LONGEVITY_COLUMNS)
    logger.info("🔍 Label longevity query complete", count=len(rows))
    return rows


async def query_monthly_anniversaries(
    pool: Any,
    current_year: int,
    current_month: int,
    milestone_years: list[int] | None = None,
) -> list[dict[str, Any]]:
    """Return masters whose release year reaches one of the requested milestones."""
    if milestone_years is None:
        milestone_years = [25, 30, 40, 50, 75, 100]
    target_years = [current_year - milestone for milestone in milestone_years]
    rows = await _fetch_dicts(pool, MONTHLY_ANNIVERSARIES_SQL, {"target_years": target_years}, _MONTHLY_ANNIVERSARIES_COLUMNS)
    logger.info("🔍 Monthly anniversaries query complete", count=len(rows), month=current_month, year=current_year)
    return rows


# ── PostgreSQL-only data-completeness computation ───────────────────────────

# Fields to check for completeness, per entity type.
_COMPLETENESS_FIELDS: dict[str, list[tuple[str, str]]] = {
    "artists": [("with_image", "images")],
    "labels": [("with_image", "images")],
    "masters": [("with_year", "year"), ("with_genre", "genres"), ("with_image", "images")],
    "releases": [
        ("with_year", "year"),
        ("with_country", "country"),
        ("with_genre", "genres"),
        ("with_image", "images"),
    ],
}

# Pre-built combined queries per entity type — single table scan each.
# Uses count(*) FILTER to compute all field counts in one pass.
_COMBINED_QUERIES: dict[str, str] = {}
for _table, _fields in _COMPLETENESS_FIELDS.items():
    _filter_parts = []
    _aliases = []
    for _field_name, _jsonb_key in _fields:
        _filter_parts.append(
            f"count(*) FILTER (WHERE data->>'{_jsonb_key}' IS NOT NULL"
            f" AND data->>'{_jsonb_key}' != ''"
            f" AND data->>'{_jsonb_key}' != '[]') AS {_field_name}"
        )
        _aliases.append(_field_name)
    _filters_sql = ", ".join(_filter_parts)
    _COMBINED_QUERIES[_table] = f"SELECT count(*) AS total_count, {_filters_sql} FROM {_table}"  # noqa: S608


async def _query_single_entity(pool: Any, entity_type: str, fields: list[tuple[str, str]]) -> dict[str, Any]:
    """Run a single entity completeness query and return the result dict."""
    async with pool.connection() as conn, conn.cursor() as cursor:
        cursor = cast("Any", cursor)
        await execute_sql(cursor, _COMBINED_QUERIES[entity_type])
        row = await cursor.fetchone()

    total_count = row[0] if row else 0
    item: dict[str, Any] = {
        "entity_type": entity_type,
        "total_count": total_count,
        "with_image": 0,
        "with_year": 0,
        "with_country": 0,
        "with_genre": 0,
    }

    if total_count > 0 and row:
        for i, (field_name, _) in enumerate(fields):
            item[field_name] = row[i + 1]

        field_pcts = [item[field_name] / total_count * 100 for field_name, _ in fields]
        item["completeness_pct"] = round(sum(field_pcts) / len(field_pcts), 2) if field_pcts else 0.0
    else:
        item["completeness_pct"] = 0.0

    return item


async def query_data_completeness(pool: Any) -> list[dict[str, Any]]:
    """Compute data completeness scores for each entity type.

    For each entity table, counts total records and how many have
    non-null/non-empty values for key metadata fields in a single
    table scan using FILTER clauses. Queries run concurrently for speed.
    """
    tasks = [_query_single_entity(pool, entity_type, fields) for entity_type, fields in _COMPLETENESS_FIELDS.items()]
    results = await asyncio.gather(*tasks)

    logger.info("🔍 Data completeness query complete", entity_count=len(results))
    return list(results)
