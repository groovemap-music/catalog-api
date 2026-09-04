"""PostgreSQL full-text search queries for /api/search.

Entity tables all have schema:
    data_id  VARCHAR PRIMARY KEY
    data     JSONB NOT NULL

Name fields: artists/labels → data->>'name', masters/releases → data->>'title'
Genres field (JSONB array): releases.data->'genres'
Year field (text): masters/releases.data->>'year'

Runs 6 concurrent queries per uncached search:
  1. Paginated results
  2. Total result count (unfiltered by pagination)
  3. Per-type counts for facets
  4. Genre facets (from releases matching query)
  5. Decade facets (from masters + releases matching query)
  6. Media facets (from releases matching query, by family — see ADR 0007)
"""

import asyncio
import contextlib
import hashlib
import json
from functools import lru_cache
from typing import Any

import structlog
from common import AsyncPostgreSQLPool
from common.media import family_ids as _media_family_ids
from common.media import medium_ids as _media_medium_ids
from common.query_debug import execute_sql
from psycopg import sql
from psycopg.rows import dict_row

from api.telemetry import CACHE_SEARCH, cache_get


logger = structlog.get_logger(__name__)

ALL_TYPES: list[str] = ["artist", "label", "master", "release"]

# Search cache TTL (1 hour — longer than the old 5 min to reduce cold cache
# frequency for high-cardinality terms like "Rock" that take ~9s to compute)
_SEARCH_CACHE_TTL = 3600

# Maps entity type → (table, name_field, has_year, has_genres, has_media)
_ENTITY_CONFIG: dict[str, tuple[str, str, bool, bool, bool]] = {
    "artist": ("artists", "name", False, False, False),
    "label": ("labels", "name", False, False, False),
    "master": ("masters", "title", True, False, False),
    "release": ("releases", "title", True, True, True),
}


@lru_cache(maxsize=1)
def _valid_media_ids() -> tuple[frozenset[str], frozenset[str]]:
    """Return (family ids, medium ids) from the vendored ADR 0007 vocabulary, cached per process."""
    return frozenset(_media_family_ids()), frozenset(_media_medium_ids())


def split_media_filter(media: list[str]) -> tuple[list[str], list[str], list[str]]:
    """Split requested ``media`` query values into (family_ids, medium_ids, unknown_ids).

    Each value is checked against the closed vocabulary from ``common.media``
    (ADR 0007): a family id, a medium id, or unknown. Order within each
    returned list follows the order the caller supplied.
    """
    families, mediums = _valid_media_ids()
    family_filter: list[str] = []
    medium_filter: list[str] = []
    unknown: list[str] = []
    for value in media:
        if value in families:
            family_filter.append(value)
        elif value in mediums:
            medium_filter.append(value)
        else:
            unknown.append(value)
    return family_filter, medium_filter, unknown


def cache_key(
    q: str,
    types: list[str],
    genres: list[str],
    year_min: int | None,
    year_max: int | None,
    limit: int,
    offset: int,
    media: list[str] | None = None,
) -> str:
    """Stable Redis cache key for the given search parameters."""
    params = {
        "q": q.lower().strip(),
        "types": sorted(types),
        "genres": sorted(genres),
        "year_min": year_min,
        "year_max": year_max,
        "limit": limit,
        "offset": offset,
        "media": sorted(media) if media else [],
    }
    digest = hashlib.md5(json.dumps(params, sort_keys=True).encode(), usedforsecurity=False).hexdigest()
    return f"search:{digest}"


def _year_filter_clause(year_min: int | None, year_max: int | None, column: sql.Composable | None = None) -> tuple[sql.Composable, list[Any]]:
    """Return (SQL_clause, params) for optional year filtering.

    Rows with NULL year (artists, labels) are always included regardless of
    year filter — only rows with a parseable year are filtered.

    ``column`` lets callers point the clause at a raw column expression (e.g.
    ``(data->>'year')``) instead of the default ``year`` identifier — needed
    to push the filter into a per-table subquery *before* its rank LIMIT.
    """
    col = column if column is not None else sql.SQL("year")
    clauses: list[sql.Composable] = []
    params: list[Any] = []
    if year_min is not None:
        clauses.append(sql.SQL("({c} IS NULL OR {c}::int >= %s)").format(c=col))
        params.append(year_min)
    if year_max is not None:
        clauses.append(sql.SQL("({c} IS NULL OR {c}::int <= %s)").format(c=col))
        params.append(year_max)
    return (sql.SQL(" AND ").join(clauses), params) if clauses else (sql.SQL("TRUE"), [])


def _genre_filter_clause(genres: list[str], column: sql.Composable | None = None) -> tuple[sql.Composable, list[Any]]:
    """Return (SQL_clause, params) for optional genre filtering.

    Rows with NULL genres (artists, labels, masters) are always included
    regardless of genre filter — only rows with genre data are filtered.

    ``column`` lets callers point the clause at a raw column expression (e.g.
    ``(data->'genres')``) instead of the default ``genres`` identifier —
    needed to push the filter into a per-table subquery *before* its rank
    LIMIT.
    """
    if not genres:
        return (sql.SQL("TRUE"), [])
    col = column if column is not None else sql.SQL("genres")
    # ?| checks if JSONB array contains any of the given strings
    return (sql.SQL("({c} IS NULL OR {c} ?| %s::text[])").format(c=col), [genres])


def _media_filter_clause(families: list[str], mediums: list[str], column: sql.Composable | None = None) -> tuple[sql.Composable, list[Any]]:
    """Return (SQL_clause, params) for optional media (family/medium) filtering.

    Rows with NULL media (artists, labels, masters — no media column) are
    always included regardless of the media filter — only the releases
    table carries the ADR 0007 canonical media block.

    Family ids match the GIN-indexed ``media->'families'`` array with ``?|``
    (same indexed pattern as :func:`_genre_filter_clause`). Medium ids match
    any item in ``media->'items'`` via ``jsonb_path_exists``, since a medium
    is nested per-item rather than hoisted to a top-level array. Requested
    family and medium ids are OR-combined (matches any of them), consistent
    with how a single facet's multiple selected values combine elsewhere.

    ``column`` lets callers point the clause at a raw column expression
    instead of the default ``media`` identifier — needed to push the filter
    into a per-table subquery *before* its rank LIMIT, same as
    :func:`_year_filter_clause` and :func:`_genre_filter_clause`.
    """
    if not families and not mediums:
        return (sql.SQL("TRUE"), [])
    col = column if column is not None else sql.SQL("media")
    parts: list[sql.Composable] = []
    params: list[Any] = []
    if families:
        parts.append(sql.SQL("({c}->'families') ?| %s::text[]").format(c=col))
        params.append(families)
    for medium in mediums:
        parts.append(sql.SQL("jsonb_path_exists({c}, '$.items[*] ? (@.medium == $m)', jsonb_build_object('m', %s::text))").format(c=col))
        params.append(medium)
    match_clause = sql.SQL(" OR ").join(parts)
    return (sql.SQL("({c} IS NULL OR ({match}))").format(c=col, match=match_clause), params)


def _entity_select(
    entity_type: str,
    name_field: str,
    has_year: bool,
    has_genres: bool,
    *,
    per_table_limit: int | None = None,
    year_min: int | None = None,
    year_max: int | None = None,
    genres: list[str] | None = None,
    has_media: bool = False,
    media_families: list[str] | None = None,
    media_mediums: list[str] | None = None,
) -> tuple[sql.Composable, list[Any]]:
    """Return a (SELECT fragment, params) for one entity type in the UNION ALL.

    When per_table_limit is set, each entity type returns at most that many
    rows (ordered by ts_rank DESC).  This prevents high-cardinality terms
    like "Rock" from materializing 100K+ rows in the UNION ALL CTE.

    year_min/year_max/genres/media — when given — are applied INSIDE this
    subquery's WHERE, before its ORDER BY rank LIMIT, so the rank cap is
    applied to already-filtered rows. Applying them only in an outer WHERE
    (after the cap) would silently drop/empty filtered results for
    high-cardinality terms, since rank is uncorrelated with year/genre/media.
    """
    year_col = sql.SQL("(data->>'year')") if has_year else sql.SQL("NULL::text")
    genres_col = sql.SQL("(data->'genres')") if has_genres else sql.SQL("NULL::jsonb")
    media_col = sql.SQL("media") if has_media else sql.SQL("NULL::jsonb")
    table = sql.Identifier(_ENTITY_CONFIG[entity_type][0])
    name_lit = sql.Literal(name_field)
    # data_id is a unique tiebreaker so the per-table rank cap selects a
    # deterministic subset among tied ts_rank values across page executions.
    limit_clause = sql.SQL(" ORDER BY rank DESC, data_id LIMIT {n}").format(n=sql.Literal(per_table_limit)) if per_table_limit else sql.SQL("")

    year_clause, year_params = _year_filter_clause(year_min, year_max, column=year_col)
    genre_clause, genre_params = _genre_filter_clause(genres or [], column=genres_col)
    media_clause, media_params = _media_filter_clause(media_families or [], media_mediums or [], column=media_col)
    filter_params = [*year_params, *genre_params, *media_params]
    filter_clause = sql.SQL(" AND {y} AND {g} AND {m}").format(y=year_clause, g=genre_clause, m=media_clause) if filter_params else sql.SQL("")

    select_sql = sql.SQL(
        "(SELECT {entity_type}::text AS type, data_id AS id, data->>{name} AS name,"
        " ts_rank(to_tsvector('english', COALESCE(data->>{name}, '')), q.tsq) AS rank,"
        " ts_headline('english', COALESCE(data->>{name}, ''), q.tsq) AS highlight,"
        " {year_col} AS year, {genres_col} AS genres"
        " FROM {table}, q"
        " WHERE to_tsvector('english', COALESCE(data->>{name}, '')) @@ q.tsq"
        "{filter_clause}"
        "{limit_clause})"
    ).format(
        entity_type=sql.Literal(entity_type),
        name=name_lit,
        year_col=year_col,
        genres_col=genres_col,
        table=table,
        filter_clause=filter_clause,
        limit_clause=limit_clause,
    )
    return select_sql, filter_params


def _build_union(
    types: list[str],
    *,
    per_table_limit: int | None = None,
    year_min: int | None = None,
    year_max: int | None = None,
    genres: list[str] | None = None,
    media_families: list[str] | None = None,
    media_mediums: list[str] | None = None,
) -> tuple[sql.Composable, list[Any]]:
    """Build UNION ALL of SELECT fragments for the requested entity types.

    When per_table_limit is set, each entity type returns at most that many
    rows (pre-sorted by rank), preventing high-cardinality term explosion.
    year_min/year_max/genres/media are pushed into each per-table subquery so
    the rank cap is applied to already-filtered rows (see _entity_select).
    media_families/media_mediums are ignored for entity types with no media
    column (only "release" has one) — see _entity_select's has_media flag.

    Returns (UNION_ALL SQL fragment, flattened params list in emission order).
    """
    if not types:  # would produce invalid SQL
        raise ValueError("types must not be empty")
    parts: list[sql.Composable] = []
    params: list[Any] = []
    for t in types:
        _table, name_field, has_year, has_genres, has_media = _ENTITY_CONFIG[t]
        select_sql, select_params = _entity_select(
            t,
            name_field,
            has_year,
            has_genres,
            per_table_limit=per_table_limit,
            year_min=year_min,
            year_max=year_max,
            genres=genres,
            has_media=has_media,
            media_families=media_families,
            media_mediums=media_mediums,
        )
        parts.append(select_sql)
        params.extend(select_params)
    return sql.SQL(" UNION ALL ").join(parts), params


async def _run_results(
    pool: AsyncPostgreSQLPool,
    q: str,
    types: list[str],
    genres: list[str],
    year_min: int | None,
    year_max: int | None,
    limit: int,
    offset: int,
    media_families: list[str] | None = None,
    media_mediums: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Fetch paginated search results.

    Uses per-table LIMIT in the UNION ALL to prevent high-cardinality terms
    like "Rock" from materializing 100K+ rows before outer LIMIT/OFFSET.
    Each table returns at most (limit + offset) rows pre-sorted by rank.

    year/genre/media filters are pushed INTO each per-table subquery (before
    its rank LIMIT) — applying them only in an outer WHERE after the
    per-table cap would silently drop/empty filtered results for
    high-cardinality terms, since rank is uncorrelated with year/genre/media.
    """
    # Each entity table returns up to per_table_limit rows pre-sorted by rank.
    # Use 2x multiplier to reduce result loss at higher page offsets while
    # keeping the materialisation bounded.
    per_table_limit = (limit + offset) * 2
    union_sql, union_params = _build_union(
        types,
        per_table_limit=per_table_limit,
        year_min=year_min,
        year_max=year_max,
        genres=genres,
        media_families=media_families,
        media_mediums=media_mediums,
    )

    query = sql.SQL(
        "WITH q AS (SELECT plainto_tsquery('english', %s) AS tsq),"
        " results AS ({union_sql})"
        " SELECT type, id, name, rank, highlight, year, genres"
        " FROM results"
        " ORDER BY rank DESC, id"
        " LIMIT %s OFFSET %s"
    ).format(union_sql=union_sql)
    params = [q, *union_params, limit, offset]
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await execute_sql(cur, query, params)  # nosemgrep
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


_TOTAL_COUNT_CAP = 10000


async def _run_total(
    pool: AsyncPostgreSQLPool,
    q: str,
    types: list[str],
    genres: list[str],
    year_min: int | None,
    year_max: int | None,
    media_families: list[str] | None = None,
    media_mediums: list[str] | None = None,
) -> int:
    """Count total matching results (ignoring pagination).

    Each table is capped at _TOTAL_COUNT_CAP rows to prevent full scans
    on high-cardinality terms like "Rock" (18.9M releases).  The reported
    total is an approximate lower bound when capped.

    year/genre/media filters are pushed INTO each per-table subquery (before
    its rank LIMIT) for the same reason as _run_results — see its docstring.
    """
    union_sql, union_params = _build_union(
        types,
        per_table_limit=_TOTAL_COUNT_CAP,
        year_min=year_min,
        year_max=year_max,
        genres=genres,
        media_families=media_families,
        media_mediums=media_mediums,
    )

    query = sql.SQL(
        "WITH q AS (SELECT plainto_tsquery('english', %s) AS tsq), results AS ({union_sql}) SELECT COUNT(*) AS total FROM results"
    ).format(union_sql=union_sql)
    params = [q, *union_params]
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await execute_sql(cur, query, params)  # nosemgrep
        row = await cur.fetchone()
    return int(row["total"]) if row else 0


async def _run_type_counts(pool: AsyncPostgreSQLPool, q: str, types: list[str]) -> dict[str, int]:
    """Count matching records per entity type (for type facet).

    Each table count is capped at _TOTAL_COUNT_CAP to prevent full scans
    on common terms.  Reported counts are approximate when capped.
    """
    union_parts = []
    for t in types:
        table, name_field, _, _, _ = _ENTITY_CONFIG[t]
        union_parts.append(
            sql.SQL(
                "SELECT {type}::text AS type,"
                " (SELECT COUNT(*) FROM (SELECT 1 FROM {table}, q"
                " WHERE to_tsvector('english', COALESCE(data->>{name}, '')) @@ q.tsq"
                " LIMIT {cap}) sub) AS cnt"
            ).format(
                type=sql.Literal(t),
                table=sql.Identifier(table),
                name=sql.Literal(name_field),
                cap=sql.Literal(_TOTAL_COUNT_CAP),
            )
        )
    union_sql = sql.SQL(" UNION ALL ").join(union_parts)
    query = sql.SQL("WITH q AS (SELECT plainto_tsquery('english', %s) AS tsq) {union_sql}").format(union_sql=union_sql)
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await execute_sql(cur, query, [q])  # nosemgrep
        rows = await cur.fetchall()
    return {row["type"]: int(row["cnt"]) for row in rows}


async def _run_genre_facets(pool: AsyncPostgreSQLPool, q: str) -> dict[str, int]:
    """Count matching releases per genre (for genre facet).

    Caps the release scan to prevent full table traversal on common terms.
    """
    query = sql.SQL(
        "WITH q AS (SELECT plainto_tsquery('english', %s) AS tsq),"
        " matched AS ("
        " SELECT data->'genres' AS genres FROM releases, q"
        " WHERE to_tsvector('english', COALESCE(data->>'title', '')) @@ q.tsq"
        " LIMIT {cap})"
        " SELECT genre, COUNT(*) AS cnt"
        " FROM matched,"
        " jsonb_array_elements_text(genres) AS genre"
        " GROUP BY genre"
        " ORDER BY cnt DESC"
        " LIMIT 20"
    ).format(cap=sql.Literal(_TOTAL_COUNT_CAP))
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await execute_sql(cur, query, [q])
        rows = await cur.fetchall()
    return {row["genre"]: int(row["cnt"]) for row in rows}


async def _run_decade_facets(pool: AsyncPostgreSQLPool, q: str) -> dict[str, int]:
    """Count matching masters+releases per decade (for decade facet).

    Caps each table scan to prevent full traversal on common terms.
    """
    query = sql.SQL(
        "WITH q AS (SELECT plainto_tsquery('english', %s) AS tsq),"
        " matches AS ("
        " (SELECT data->>'year' AS year FROM masters, q"
        " WHERE to_tsvector('english', COALESCE(data->>'title', '')) @@ q.tsq"
        " AND data->>'year' IS NOT NULL"
        " LIMIT {cap})"
        " UNION ALL"
        " (SELECT data->>'year' FROM releases, q"
        " WHERE to_tsvector('english', COALESCE(data->>'title', '')) @@ q.tsq"
        " AND data->>'year' IS NOT NULL"
        " LIMIT {cap}))"
        " SELECT (year::int / 10 * 10)::text || 's' AS decade, COUNT(*) AS cnt"
        " FROM matches"
        " WHERE year ~ '^[0-9]{{4}}$'"
        " GROUP BY decade"
        " ORDER BY decade"
    ).format(cap=sql.Literal(_TOTAL_COUNT_CAP))
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await execute_sql(cur, query, [q])
        rows = await cur.fetchall()
    return {row["decade"]: int(row["cnt"]) for row in rows}


async def _run_media_facets(pool: AsyncPostgreSQLPool, q: str) -> dict[str, int]:
    """Count matching releases per media family (for the media facet, ADR 0007).

    Caps the release scan to prevent full table traversal on common terms,
    mirroring :func:`_run_genre_facets`. A release with no media block (or an
    empty ``families`` list) contributes to no bucket. Shaped the same way as
    the other facets (``{family_id: count}``) for a generic facet renderer.
    """
    query = sql.SQL(
        "WITH q AS (SELECT plainto_tsquery('english', %s) AS tsq),"
        " matched AS ("
        " SELECT media FROM releases, q"
        " WHERE to_tsvector('english', COALESCE(data->>'title', '')) @@ q.tsq"
        " LIMIT {cap})"
        " SELECT family, COUNT(*) AS cnt"
        " FROM matched,"
        " jsonb_array_elements_text(media->'families') AS family"
        " GROUP BY family"
        " ORDER BY cnt DESC"
    ).format(cap=sql.Literal(_TOTAL_COUNT_CAP))
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await execute_sql(cur, query, [q])
        rows = await cur.fetchall()
    return {row["family"]: int(row["cnt"]) for row in rows}


def _format_result(row: dict[str, Any]) -> dict[str, Any]:
    """Convert a DB row into the API result shape."""
    metadata: dict[str, Any] = {}
    if row.get("year"):
        with contextlib.suppress(ValueError, TypeError):
            metadata["year"] = int(row["year"])
    if row.get("genres"):
        genres = row["genres"]
        if isinstance(genres, list):
            metadata["genres"] = genres
    return {
        "type": row["type"],
        "id": row["id"],
        "name": row["name"] or "",
        "highlight": row["highlight"] or row["name"] or "",
        "relevance": round(float(row["rank"]), 4) if row.get("rank") else 0.0,
        "metadata": metadata,
    }


async def execute_search(
    pool: AsyncPostgreSQLPool,
    redis: Any | None,
    q: str,
    types: list[str],
    genres: list[str],
    year_min: int | None,
    year_max: int | None,
    limit: int,
    offset: int,
    media_families: list[str] | None = None,
    media_mediums: list[str] | None = None,
) -> dict[str, Any]:
    """Run full search and return structured response dict.

    Checks Redis cache first (TTL=300s). On miss, runs 6 DB queries
    concurrently, formats response, stores in Redis, and returns.

    media_families/media_mediums (ADR 0007 family/medium ids, already
    validated by the caller via :func:`split_media_filter`) filter release
    results and are OR-combined with each other, AND-combined with genres.
    """
    if not types:
        raise ValueError("types must not be empty")

    media_families = media_families or []
    media_mediums = media_mediums or []
    key = cache_key(q, types, genres, year_min, year_max, limit, offset, media=[*media_families, *media_mediums])

    # Cache-aside read — Redis is a pure optimization. A Redis outage (or a
    # corrupt cache entry) must degrade to a fresh PostgreSQL query, never 500.
    if redis is not None:
        try:
            cached = await cache_get(redis, key, cache=CACHE_SEARCH)
            if cached:
                return json.loads(cached)  # type: ignore[no-any-return]
        except Exception:
            logger.debug("⚠️ Search cache read failed, falling through to DB", key=key)

    logger.debug("🔍 Search cache miss, querying DB", q=q, types=types)

    results_rows, total, type_counts, genre_facets, decade_facets, media_facets = await asyncio.gather(
        _run_results(pool, q, types, genres, year_min, year_max, limit, offset, media_families, media_mediums),
        _run_total(pool, q, types, genres, year_min, year_max, media_families, media_mediums),
        _run_type_counts(pool, q, types),
        _run_genre_facets(pool, q),
        _run_decade_facets(pool, q),
        _run_media_facets(pool, q),
    )

    response: dict[str, Any] = {
        "query": q,
        "total": total,
        "facets": {
            "type": type_counts,
            "genre": genre_facets,
            "decade": decade_facets,
            "media": media_facets,
        },
        "results": [_format_result(r) for r in results_rows],
        "pagination": {
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(results_rows) < total,
        },
    }

    # Best-effort cache write — a Redis outage must not fail an otherwise
    # successful, fully PostgreSQL-backed search response.
    if redis is not None:
        try:
            await redis.setex(key, _SEARCH_CACHE_TTL, json.dumps(response))
        except Exception:
            logger.debug("⚠️ Search cache write failed", key=key)

    return response
