"""PostgreSQL full-text search queries for /api/search.

Entity tables all have schema:
    data_id  VARCHAR PRIMARY KEY
    data     JSONB NOT NULL

Name fields: artists/labels → data->>'name', masters/releases → data->>'title'
Genres field (JSONB array): releases.data->'genres'
Year field (text): masters/releases.data->>'year'
Country field (text): releases.data->>'country' (ADR 0011)

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
import os
import weakref
from collections.abc import AsyncIterator
from functools import lru_cache
from typing import Any

import structlog
from common import AsyncPostgreSQLPool
from common.media import family_ids as _media_family_ids
from common.media import medium_ids as _media_medium_ids
from common.query_debug import execute_sql
from psycopg import sql
from psycopg.errors import QueryCanceled
from psycopg.rows import dict_row

from api.identity import native_ids_for_pairs
from api.telemetry import CACHE_SEARCH, cache_get, record_cache_write_failure
from api.valkey_keys import valkey_key


logger = structlog.get_logger(__name__)

ALL_TYPES: list[str] = ["artist", "label", "master", "release"]

# Search cache TTL (1 hour — longer than the old 5 min to reduce cold cache
# frequency for high-cardinality terms like "Rock" that take ~9s to compute)
_SEARCH_CACHE_TTL = 3600

# Maps entity type → (table, name_field, has_year, has_genres, has_media, has_country)
_ENTITY_CONFIG: dict[str, tuple[str, str, bool, bool, bool, bool]] = {
    "artist": ("artists", "name", False, False, False, False),
    "label": ("labels", "name", False, False, False, False),
    "master": ("masters", "title", True, False, False, False),
    "release": ("releases", "title", True, True, True, True),
}


class SearchBusy(Exception):
    """Every search slot stayed taken for the whole wait: the caller should retry shortly (HTTP 503)."""


class SearchTimedOut(Exception):
    """A search statement ran past its statement timeout and was cancelled (HTTP 504)."""


def _env_seconds(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except ValueError:
        return default
    return value if value > 0 else default


# Audit H3. An uncached search opens six connections from a pool of eight, so a handful of cold
# searches can hold every connection past the checkout budget and fail unrelated routes such as
# login. Searches therefore share a bounded set of slots (one per connection, less a reserve that
# searches never take), and every search statement carries a timeout so one slow plan cannot hold
# its slot indefinitely. Both are a backstop: the statement rewrites are what make searches fast.
SEARCH_STATEMENT_TIMEOUT_SECONDS = _env_seconds("SEARCH_STATEMENT_TIMEOUT_SECONDS", 20.0)
SEARCH_QUEUE_TIMEOUT_SECONDS = _env_seconds("SEARCH_QUEUE_TIMEOUT_SECONDS", 5.0)
# Connections left for everything that is not a search. Two keeps login and token routes alive.
SEARCH_POOL_RESERVE = 2

# Keyed weakly by pool: the slots belong to one pool (and its event loop) and die with it.
_slots: weakref.WeakKeyDictionary[Any, asyncio.Semaphore] = weakref.WeakKeyDictionary()


def _search_slots(pool: AsyncPostgreSQLPool) -> asyncio.Semaphore:
    """The slot semaphore for ``pool``: its connections minus the reserve, never fewer than one."""
    slots = _slots.get(pool)
    if slots is None:
        size = max(1, int(getattr(pool, "max_connections", 8)) - SEARCH_POOL_RESERVE)
        slots = _slots[pool] = asyncio.Semaphore(size)
    return slots


@contextlib.asynccontextmanager
async def _search_cursor(pool: AsyncPostgreSQLPool) -> AsyncIterator[Any]:
    """A dict-row cursor for one search statement, inside a slot and under a statement timeout.

    Waiting for a slot is bounded by ``SEARCH_QUEUE_TIMEOUT_SECONDS`` (``SearchBusy``); the
    statement is bounded by ``SEARCH_STATEMENT_TIMEOUT_SECONDS``, scoped to this transaction so the
    pooled connection keeps no setting (``SearchTimedOut``). Neither is ever an empty success.
    """
    slots = _search_slots(pool)
    try:
        await asyncio.wait_for(slots.acquire(), timeout=SEARCH_QUEUE_TIMEOUT_SECONDS)
    except TimeoutError:
        raise SearchBusy from None
    try:
        async with pool.connection() as conn, conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT set_config('statement_timeout', %s, true)", (f"{int(SEARCH_STATEMENT_TIMEOUT_SECONDS * 1000)}ms",))
            yield cur
    except QueryCanceled:
        raise SearchTimedOut from None
    finally:
        slots.release()


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
    countries: list[str] | None = None,
) -> str:
    """Stable Valkey cache key for the given search parameters."""
    params = {
        "q": q.lower().strip(),
        "types": sorted(types),
        "genres": sorted(genres),
        "year_min": year_min,
        "year_max": year_max,
        "limit": limit,
        "offset": offset,
        "media": sorted(media) if media else [],
        "countries": sorted(countries) if countries else [],
    }
    digest = hashlib.md5(json.dumps(params, sort_keys=True).encode(), usedforsecurity=False).hexdigest()
    return valkey_key(f"search:{digest}")


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


def _country_filter_clause(countries: list[str], column: sql.Composable | None = None) -> tuple[sql.Composable, list[Any]]:
    """Return (SQL_clause, params) for optional release-country filtering (ADR 0011).

    Only the releases table carries a country, so the clause is applied on the releases
    branch of the union and the other branches pass it on ``NULL IS NULL`` — the same
    shape :func:`_genre_filter_clause` and :func:`_media_filter_clause` use, and the reason
    a country filter narrows release hits without silently emptying the artist and label
    hits beside them.

    Requested countries are OR-combined through ``= ANY``, which is how one facet's
    multiple selected values combine everywhere else on this surface, and which the
    existing ``releases.data->>'country'`` index answers. The value is matched exactly as
    the catalog stores it: Discogs writes country names rather than ISO codes, and folding
    them here would invent an equivalence the catalog never asserted.

    ``column`` lets callers point the clause at a raw column expression instead of the
    default ``country`` identifier — needed to push the filter into a per-table subquery
    *before* its rank LIMIT, same as the other three filters.
    """
    if not countries:
        return (sql.SQL("TRUE"), [])
    col = column if column is not None else sql.SQL("country")
    return (sql.SQL("({c} IS NULL OR {c} = ANY(%s::text[]))").format(c=col), [countries])


def _uses_release_search(entity_type: str, filter_params: list[Any]) -> bool:
    """True when a branch can match on ``public.release_search.title_tsv`` instead of the document.

    Only an unfiltered releases branch can: the year, genre, media and country filters read
    ``releases.data`` for every match anyway, so the stored vector would save nothing there and
    those branches keep ``to_tsvector(...)`` over the document (``idx_releases_fts``).
    """
    return entity_type == "release" and not filter_params


def _uses_master_search(entity_type: str) -> bool:
    """True when a masters branch can match on ``public.master_search.title_tsv``.

    Every masters branch can, filtered or not: masters carry no genres, media or country (those
    filters are TRUE for them), and the year filter reads ``master_search.year``, which is
    ``masters.data->>'year'``, so no ``masters.data`` is read per match.
    """
    return entity_type == "master"


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
    has_country: bool = False,
    countries: list[str] | None = None,
) -> tuple[sql.Composable, list[Any]]:
    """Return a (SELECT fragment, params) for one entity type in the UNION ALL.

    When per_table_limit is set, each entity type returns at most that many
    rows (ordered by ts_rank DESC).  This prevents high-cardinality terms
    like "Rock" from materializing 100K+ rows in the UNION ALL CTE.

    year_min/year_max/genres/media/countries — when given — are applied INSIDE
    this subquery's WHERE, before its ORDER BY rank LIMIT, so the rank cap is
    applied to already-filtered rows. Applying them only in an outer WHERE
    (after the cap) would silently drop/empty filtered results for
    high-cardinality terms, since rank is uncorrelated with those facets.

    The fragment always projects a ``country`` column so every branch of the
    UNION ALL has the same shape; only releases carry one, and the rest
    project NULL.
    """
    year_col = sql.SQL("(data->>'year')") if has_year else sql.SQL("NULL::text")
    genres_col = sql.SQL("(data->'genres')") if has_genres else sql.SQL("NULL::jsonb")
    media_col = sql.SQL("media") if has_media else sql.SQL("NULL::jsonb")
    country_col = sql.SQL("(data->>'country')") if has_country else sql.SQL("NULL::text")
    table = sql.Identifier(_ENTITY_CONFIG[entity_type][0])
    name_lit = sql.Literal(name_field)
    # data_id is a unique tiebreaker so the per-table rank cap selects a
    # deterministic subset among tied ts_rank values across page executions.
    limit_clause = sql.SQL(" ORDER BY rank DESC, data_id LIMIT {n}").format(n=sql.Literal(per_table_limit)) if per_table_limit else sql.SQL("")

    year_clause, year_params = _year_filter_clause(year_min, year_max, column=year_col)
    genre_clause, genre_params = _genre_filter_clause(genres or [], column=genres_col)
    media_clause, media_params = _media_filter_clause(media_families or [], media_mediums or [], column=media_col)
    country_clause, country_params = _country_filter_clause(countries or [], column=country_col)
    filter_params = [*year_params, *genre_params, *media_params, *country_params]
    filter_clause = (
        sql.SQL(" AND {y} AND {g} AND {m} AND {c}").format(y=year_clause, g=genre_clause, m=media_clause, c=country_clause)
        if filter_params
        else sql.SQL("")
    )

    # Rank first, read documents after the LIMIT (audit H1). The inner query touches
    # each match's document once, for the rank; name, year, genres, country and the
    # headline are read only for the rows that survive the cap, via a primary-key join.
    # The tsquery comes from the caller's ``q`` CTE, which must be NOT MATERIALIZED: a CTE
    # referenced by every branch otherwise materializes, and a CTE scan is
    # parallel-restricted, which forces a serial plan.
    if _uses_release_search(entity_type, filter_params):
        # Rank the stored title vector (public.release_search, database-schema v0.11.0): no
        # releases.data is read, and so detoasted, per match. Same expression, same index
        # order as below, so the ranks and the top rows are identical.
        ranked_sql: sql.Composable = sql.SQL(
            "SELECT release_id AS data_id, ts_rank(title_tsv, q.tsq) AS rank FROM public.release_search, q WHERE title_tsv @@ q.tsq"
        )
    elif _uses_master_search(entity_type):
        # Rank the stored title vector (public.master_search, database-schema v0.12.0); the year
        # filter, when given, reads its own year column. Same expression as below.
        m_year_clause, m_year_params = _year_filter_clause(year_min, year_max, column=sql.SQL("year"))
        filter_params = m_year_params  # the genre, media and country filters are TRUE for masters
        ranked_sql = sql.SQL(
            "SELECT master_id AS data_id, ts_rank(title_tsv, q.tsq) AS rank FROM public.master_search, q WHERE title_tsv @@ q.tsq AND {y}"
        ).format(y=m_year_clause)
    else:
        ranked_sql = sql.SQL(
            "SELECT data_id, ts_rank(to_tsvector('english', COALESCE(data->>{name}, '')), q.tsq) AS rank"
            " FROM {table}, q"
            " WHERE to_tsvector('english', COALESCE(data->>{name}, '')) @@ q.tsq"
            "{filter_clause}"
        ).format(name=name_lit, table=table, filter_clause=filter_clause)
    select_sql = sql.SQL(
        "(SELECT {entity_type}::text AS type, d.data_id AS id, d.data->>{name} AS name,"
        " top.rank AS rank,"
        " ts_headline('english', COALESCE(d.data->>{name}, ''), q.tsq) AS highlight,"
        " {year_col} AS year, {genres_col} AS genres, {country_col} AS country"
        " FROM q, ({ranked}"
        "{limit_clause}) top"
        " JOIN {table} d ON d.data_id = top.data_id)"
    ).format(
        ranked=ranked_sql,
        entity_type=sql.Literal(entity_type),
        name=name_lit,
        year_col=year_col,
        genres_col=genres_col,
        country_col=country_col,
        table=table,
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
    countries: list[str] | None = None,
) -> tuple[sql.Composable, list[Any]]:
    """Build UNION ALL of SELECT fragments for the requested entity types.

    When per_table_limit is set, each entity type returns at most that many
    rows (pre-sorted by rank), preventing high-cardinality term explosion.
    year_min/year_max/genres/media are pushed into each per-table subquery so
    the rank cap is applied to already-filtered rows (see _entity_select).
    media_families/media_mediums are ignored for entity types with no media
    column (only "release" has one) — see _entity_select's has_media flag.
    countries is likewise ignored for entity types with no country column
    (again only "release") — see _entity_select's has_country flag.

    Returns (UNION_ALL SQL fragment, flattened params list in emission order).
    """
    if not types:  # would produce invalid SQL
        raise ValueError("types must not be empty")
    parts: list[sql.Composable] = []
    params: list[Any] = []
    for t in types:
        _table, name_field, has_year, has_genres, has_media, has_country = _ENTITY_CONFIG[t]
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
            has_country=has_country,
            countries=countries,
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
    countries: list[str] | None = None,
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
        countries=countries,
    )

    query = sql.SQL(
        "WITH q AS NOT MATERIALIZED (SELECT plainto_tsquery('english', %s) AS tsq),"
        " results AS ({union_sql})"
        " SELECT type, id, name, rank, highlight, year, genres, country"
        " FROM results"
        " ORDER BY rank DESC, id"
        " LIMIT %s OFFSET %s"
    ).format(union_sql=union_sql)
    params = [q, *union_params, limit, offset]
    async with _search_cursor(pool) as cur:
        await execute_sql(cur, query, params)  # nosemgrep
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


_TOTAL_COUNT_CAP = 10000


def _build_count_union(
    types: list[str],
    *,
    year_min: int | None = None,
    year_max: int | None = None,
    genres: list[str] | None = None,
    media_families: list[str] | None = None,
    media_mediums: list[str] | None = None,
    countries: list[str] | None = None,
) -> tuple[sql.Composable, list[Any]]:
    """Build the UNION ALL that :func:`_run_total` counts, without ranking or sorting.

    A capped count does not depend on which rows the cap keeps, so each branch just
    stops after ``_TOTAL_COUNT_CAP`` matches. Ranking them first (as the results
    statement must) reads every matching document and sorts it, only to throw the order
    away (audit H2). The filters are applied in each branch before the cap, with the same
    clauses and column expressions as :func:`_entity_select`, so the total is unchanged.
    """
    if not types:  # would produce invalid SQL
        raise ValueError("types must not be empty")
    parts: list[sql.Composable] = []
    params: list[Any] = []
    for t in types:
        table, name_field, has_year, has_genres, has_media, has_country = _ENTITY_CONFIG[t]
        year_clause, year_params = _year_filter_clause(year_min, year_max, column=sql.SQL("(data->>'year')") if has_year else sql.SQL("NULL::text"))
        genre_clause, genre_params = _genre_filter_clause(genres or [], column=sql.SQL("(data->'genres')") if has_genres else sql.SQL("NULL::jsonb"))
        media_clause, media_params = _media_filter_clause(
            media_families or [], media_mediums or [], column=sql.SQL("media") if has_media else sql.SQL("NULL::jsonb")
        )
        country_clause, country_params = _country_filter_clause(
            countries or [], column=sql.SQL("(data->>'country')") if has_country else sql.SQL("NULL::text")
        )
        branch_params = [*year_params, *genre_params, *media_params, *country_params]
        filter_clause = (
            sql.SQL(" AND {y} AND {g} AND {m} AND {c}").format(y=year_clause, g=genre_clause, m=media_clause, c=country_clause)
            if branch_params
            else sql.SQL("")
        )
        if _uses_release_search(t, branch_params):
            parts.append(
                sql.SQL("(SELECT 1 FROM public.release_search, q WHERE title_tsv @@ q.tsq LIMIT {cap})").format(cap=sql.Literal(_TOTAL_COUNT_CAP))
            )
        elif _uses_master_search(t):
            m_year_clause, m_year_params = _year_filter_clause(year_min, year_max, column=sql.SQL("year"))
            branch_params = m_year_params
            parts.append(
                sql.SQL("(SELECT 1 FROM public.master_search, q WHERE title_tsv @@ q.tsq AND {y} LIMIT {cap})").format(
                    y=m_year_clause, cap=sql.Literal(_TOTAL_COUNT_CAP)
                )
            )
        else:
            parts.append(
                sql.SQL(
                    "(SELECT 1 FROM {table}, q WHERE to_tsvector('english', COALESCE(data->>{name}, '')) @@ q.tsq{filter_clause} LIMIT {cap})"
                ).format(table=sql.Identifier(table), name=sql.Literal(name_field), filter_clause=filter_clause, cap=sql.Literal(_TOTAL_COUNT_CAP))
            )
        params.extend(branch_params)
    return sql.SQL(" UNION ALL ").join(parts), params


async def _run_total(
    pool: AsyncPostgreSQLPool,
    q: str,
    types: list[str],
    genres: list[str],
    year_min: int | None,
    year_max: int | None,
    media_families: list[str] | None = None,
    media_mediums: list[str] | None = None,
    countries: list[str] | None = None,
) -> int:
    """Count total matching results (ignoring pagination).

    Each table is capped at _TOTAL_COUNT_CAP rows to prevent full scans
    on high-cardinality terms like "Rock" (18.9M releases).  The reported
    total is an approximate lower bound when capped.

    year/genre/media filters are pushed INTO each per-table subquery (before
    its rank LIMIT) for the same reason as _run_results — see its docstring.
    """
    union_sql, union_params = _build_count_union(
        types,
        year_min=year_min,
        year_max=year_max,
        genres=genres,
        media_families=media_families,
        media_mediums=media_mediums,
        countries=countries,
    )

    query = sql.SQL(
        "WITH q AS NOT MATERIALIZED (SELECT plainto_tsquery('english', %s) AS tsq) SELECT COUNT(*) AS total FROM ({union_sql}) matched"
    ).format(union_sql=union_sql)
    params = [q, *union_params]
    async with _search_cursor(pool) as cur:
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
        table, name_field, _, _, _, _ = _ENTITY_CONFIG[t]
        if _uses_release_search(t, []):
            matched: sql.Composable = sql.SQL("SELECT 1 FROM public.release_search, q WHERE title_tsv @@ q.tsq")
        elif _uses_master_search(t):
            matched = sql.SQL("SELECT 1 FROM public.master_search, q WHERE title_tsv @@ q.tsq")
        else:
            matched = sql.SQL("SELECT 1 FROM {table}, q WHERE to_tsvector('english', COALESCE(data->>{name}, '')) @@ q.tsq").format(
                table=sql.Identifier(table), name=sql.Literal(name_field)
            )
        union_parts.append(
            sql.SQL("SELECT {type}::text AS type, (SELECT COUNT(*) FROM ({matched} LIMIT {cap}) sub) AS cnt").format(
                type=sql.Literal(t), matched=matched, cap=sql.Literal(_TOTAL_COUNT_CAP)
            )
        )
    union_sql = sql.SQL(" UNION ALL ").join(union_parts)
    query = sql.SQL("WITH q AS (SELECT plainto_tsquery('english', %s) AS tsq) {union_sql}").format(union_sql=union_sql)
    async with _search_cursor(pool) as cur:
        await execute_sql(cur, query, [q])  # nosemgrep
        rows = await cur.fetchall()
    return {row["type"]: int(row["cnt"]) for row in rows}


# The genre, decade and media facets keep ``to_tsvector(...)`` over the document on purpose.
# Each reads the first _TOTAL_COUNT_CAP matches in no defined order and then reads every one's
# ``releases.data`` anyway, so public.release_search saves little there; measured on lux
# (2026-11-08) it is 2-3x faster on 'love' and 'rock' but slower on a rare term, and which
# 10,000 rows it samples differs from idx_releases_fts, so the facet counts of a term over the
# cap would change.
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
    async with _search_cursor(pool) as cur:
        await execute_sql(cur, query, [q])
        rows = await cur.fetchall()
    return {row["genre"]: int(row["cnt"]) for row in rows}


# The decade facet counts masters and releases. 2jka owns the masters fragment, 4asm the releases one.
_DECADE_MASTERS_MATCHES = "(SELECT year FROM public.master_search, q WHERE title_tsv @@ q.tsq AND year IS NOT NULL LIMIT {cap})"
_DECADE_RELEASES_MATCHES = (
    "(SELECT data->>'year' FROM releases, q"
    " WHERE to_tsvector('english', COALESCE(data->>'title', '')) @@ q.tsq"
    " AND data->>'year' IS NOT NULL"
    " LIMIT {cap})"
)


async def _run_decade_facets(pool: AsyncPostgreSQLPool, q: str) -> dict[str, int]:
    """Count matching masters+releases per decade (for decade facet).

    Caps each table scan to prevent full traversal on common terms.
    """
    query = sql.SQL(
        "WITH q AS (SELECT plainto_tsquery('english', %s) AS tsq),"
        " matches AS (" + _DECADE_MASTERS_MATCHES + " UNION ALL " + _DECADE_RELEASES_MATCHES + ")"
        " SELECT (year::int / 10 * 10)::text || 's' AS decade, COUNT(*) AS cnt"
        " FROM matches"
        " WHERE year ~ '^[0-9]{{4}}$'"
        " GROUP BY decade"
        " ORDER BY decade"
    ).format(cap=sql.Literal(_TOTAL_COUNT_CAP))
    async with _search_cursor(pool) as cur:
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
    async with _search_cursor(pool) as cur:
        await execute_sql(cur, query, [q])
        rows = await cur.fetchall()
    return {row["family"]: int(row["cnt"]) for row in rows}


def _format_result(row: dict[str, Any], gm_ids: dict[tuple[str, str], str] | None = None) -> dict[str, Any]:
    """Convert a DB row into the API result shape.

    `gm_ids` maps (type, id) onto the native id ADR 0009 mints for the entity. A hit whose
    provider id has no valid alias yet carries `gm_id: None` — the provider `id` above it
    is unchanged either way, so a consumer that has not adopted native identity reads the
    same response it always did.
    """
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
        "gm_id": (gm_ids or {}).get((row["type"], str(row["id"]))),
        # ADR 0011, top-level beside `gm_id` rather than inside `metadata`: the country
        # facet filters on it, so a hit has to be able to show what it was filtered by.
        # None for every entity type but a release, and for a release the catalog never
        # gave a country.
        "country": row.get("country"),
        "name": row["name"] or "",
        "highlight": row["highlight"] or row["name"] or "",
        "relevance": round(float(row["rank"]), 4) if row.get("rank") else 0.0,
        "metadata": metadata,
    }


async def execute_search(
    pool: AsyncPostgreSQLPool,
    valkey: Any | None,
    q: str,
    types: list[str],
    genres: list[str],
    year_min: int | None,
    year_max: int | None,
    limit: int,
    offset: int,
    media_families: list[str] | None = None,
    media_mediums: list[str] | None = None,
    countries: list[str] | None = None,
) -> dict[str, Any]:
    """Run full search and return structured response dict.

    Checks Valkey cache first (TTL=300s). On miss, runs 6 DB queries
    concurrently, formats response, stores in Valkey, and returns.

    media_families/media_mediums (ADR 0007 family/medium ids, already
    validated by the caller via :func:`split_media_filter`) filter release
    results and are OR-combined with each other, AND-combined with genres.

    countries (ADR 0011, matched against ``releases.data->>'country'`` exactly
    as stored) filter release results the same way: OR-combined with each
    other, AND-combined with every other facet. They are part of the cache key,
    so a filtered page never serves an unfiltered body.
    """
    if not types:
        raise ValueError("types must not be empty")

    media_families = media_families or []
    media_mediums = media_mediums or []
    countries = countries or []
    key = cache_key(q, types, genres, year_min, year_max, limit, offset, media=[*media_families, *media_mediums], countries=countries)

    # Cache-aside read — Valkey is a pure optimization. A Valkey outage (or a
    # corrupt cache entry) must degrade to a fresh PostgreSQL query, never 500.
    if valkey is not None:
        try:
            cached = await cache_get(valkey, key, cache=CACHE_SEARCH)
            if cached:
                return json.loads(cached)  # type: ignore[no-any-return]
        except Exception:
            logger.debug("⚠️ Search cache read failed, falling through to DB", key=key)

    logger.debug("🔍 Search cache miss, querying DB", q=q, types=types)

    settled = await asyncio.gather(
        _run_results(pool, q, types, genres, year_min, year_max, limit, offset, media_families, media_mediums, countries),
        _run_total(pool, q, types, genres, year_min, year_max, media_families, media_mediums, countries),
        _run_type_counts(pool, q, types),
        _run_genre_facets(pool, q),
        _run_decade_facets(pool, q),
        _run_media_facets(pool, q),
        return_exceptions=True,
    )
    # Let every sibling statement finish (each is bounded by its timeout) before raising, so none
    # is left running unobserved while holding a slot and a connection.
    for outcome in settled:
        if isinstance(outcome, BaseException):
            raise outcome
    results_rows, total, type_counts, genre_facets, decade_facets, media_facets = settled

    # One alias lookup for the whole page, after the six searches settle: the hits carry
    # mixed kinds, so the batch is keyed by (type, id) rather than by id alone.
    gm_ids = await native_ids_for_pairs(((row["type"], row["id"]) for row in results_rows), pool=pool)

    response: dict[str, Any] = {
        "query": q,
        "total": total,
        "facets": {
            "type": type_counts,
            "genre": genre_facets,
            "decade": decade_facets,
            "media": media_facets,
        },
        "results": [_format_result(r, gm_ids) for r in results_rows],
        "pagination": {
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(results_rows) < total,
        },
    }

    # Best-effort cache write — a Valkey outage must not fail an otherwise
    # successful, fully PostgreSQL-backed search response.
    if valkey is not None:
        try:
            await valkey.setex(key, _SEARCH_CACHE_TTL, json.dumps(response))
        except Exception as exc:
            record_cache_write_failure(CACHE_SEARCH, exc)

    return response
