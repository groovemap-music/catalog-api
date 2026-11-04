"""Engine-backed proof that the late-materialized search statement returns what it always did.

``/api/search`` ranks every match before it reads anything else (audit H1,
``docs/sql-performance-audit-2026-11-04.md``): the statement used to share one ``q`` CTE across its four
branches, which forced a serial plan, and it detoasted each matching document about five times. The
rewrite ranks first, then reads name, year, genres, country and the headline for the rows that survive
the cap. These tests run it on the real PostgreSQL planner with bound parameters, past psycopg's prepare
threshold, and compare it with the statement it replaced: a mock cannot tell the two apart.

Nothing here touches a shared database: ``just test-integration`` starts throwaway containers.
"""

# ruff: noqa: S608  (the legacy oracle statement interpolates only constants of this module)

from __future__ import annotations

import json
from typing import Any

import pytest
import pytest_asyncio
from common import AsyncPostgreSQLPool
from psycopg import sql
from psycopg.rows import dict_row

from api.queries.search_queries import _build_union, _run_results, _run_total
from tests.test_real_databases import postgres_pool


__all__ = ["postgres_pool"]

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_TYPES = ["artist", "label", "master", "release"]
# psycopg prepares a statement on its sixth execution; the plan must hold past that.
_PAST_PREPARE_THRESHOLD = 8


@pytest_asyncio.fixture
async def pool(postgres_pool: AsyncPostgreSQLPool) -> AsyncPostgreSQLPool:
    async with postgres_pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("TRUNCATE artists, labels, masters, releases")
        await cur.executemany("INSERT INTO artists (data_id, hash, data) VALUES (%s, %s, %s::jsonb)", _artists())
        await cur.executemany("INSERT INTO labels (data_id, hash, data) VALUES (%s, %s, %s::jsonb)", _labels())
        await cur.executemany("INSERT INTO masters (data_id, hash, data) VALUES (%s, %s, %s::jsonb)", _masters())
        await cur.executemany("INSERT INTO releases (data_id, hash, data) VALUES (%s, %s, %s::jsonb)", _releases())
        await cur.execute("ANALYZE artists, labels, masters, releases")
    return postgres_pool


def _artists() -> list[tuple[str, str, str]]:
    return [
        (f"a{i}", f"a{i}", json.dumps({"name": name})) for i, name in enumerate(["Love", "Love Love", "Lovers Rock", "Miles Davis", "Loved One"] * 6)
    ]


def _labels() -> list[tuple[str, str, str]]:
    return [(f"l{i}", f"l{i}", json.dumps({"name": name})) for i, name in enumerate(["Love Records", "Love", "Other Label"] * 5)]


def _masters() -> list[tuple[str, str, str]]:
    return [
        (f"m{i}", f"m{i}", json.dumps({"title": title, "year": str(1960 + i % 40)})) for i, title in enumerate(["Love Supreme", "Love", "Blue"] * 20)
    ]


def _releases() -> list[tuple[str, str, str]]:
    genres = [["Rock"], ["Jazz"], ["Rock", "Pop"], ["Electronic"]]
    countries = ["UK", "US", "Germany", "Japan"]
    titles = ["Love", "Love Love", "Love Will Tear Us Apart", "Crazy Love Songs", "Lovers"]
    rows = []
    for i in range(400):
        doc: dict[str, Any] = {
            "title": titles[i % len(titles)],
            "year": str(1950 + i % 70),
            "genres": genres[i % len(genres)],
            "country": countries[i % len(countries)],
            # Bulk so the document is stored out of line, as 60% of lux's are.
            "notes": "x" * 2500,
        }
        rows.append((f"r{i:04d}", f"r{i:04d}", json.dumps(doc)))
    return rows


# The statement /api/search ran before the rewrite: one shared q CTE, every column computed per match.
_LEGACY_BRANCHES = {
    "artist": ("artists", "name", "NULL::text", "NULL::jsonb", "NULL::text"),
    "label": ("labels", "name", "NULL::text", "NULL::jsonb", "NULL::text"),
    "master": ("masters", "title", "(data->>'year')", "NULL::jsonb", "NULL::text"),
    "release": ("releases", "title", "(data->>'year')", "(data->'genres')", "(data->>'country')"),
}


def _legacy_statement(per_table_limit: int, year_filter: str = "TRUE", genre_filter: str = "TRUE", country_filter: str = "TRUE") -> str:
    branches = []
    for kind, (table, field, year, genres, country) in _LEGACY_BRANCHES.items():
        filters = "TRUE" if kind != "release" else f"{year_filter} AND {genre_filter} AND {country_filter}"
        if kind == "master":
            filters = year_filter
        branches.append(
            f"(SELECT '{kind}'::text AS type, data_id AS id, data->>'{field}' AS name,"
            f" ts_rank(to_tsvector('english', COALESCE(data->>'{field}', '')), q.tsq) AS rank,"
            f" ts_headline('english', COALESCE(data->>'{field}', ''), q.tsq) AS highlight,"
            f" {year} AS year, {genres} AS genres, {country} AS country"
            f" FROM {table}, q WHERE to_tsvector('english', COALESCE(data->>'{field}', '')) @@ q.tsq AND {filters}"
            f" ORDER BY rank DESC, data_id LIMIT {per_table_limit})"
        )
    return (
        "WITH q AS (SELECT plainto_tsquery('english', %s) AS tsq), results AS (" + " UNION ALL ".join(branches) + ")"
        " SELECT type, id, name, rank, highlight, year, genres, country FROM results ORDER BY rank DESC, id LIMIT %s OFFSET %s"
    )


async def _legacy(pool: AsyncPostgreSQLPool, q: str, limit: int, offset: int, **filters: str) -> list[dict[str, Any]]:
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(_legacy_statement((limit + offset) * 2, **filters), [q, limit, offset])
        return [dict(r) for r in await cur.fetchall()]


async def test_results_are_identical_to_the_statement_they_replaced(pool: AsyncPostgreSQLPool) -> None:
    for limit, offset in [(20, 0), (10, 10), (50, 0), (5, 30)]:
        expected = await _legacy(pool, "love", limit, offset)
        actual = await _run_results(pool, "love", _TYPES, [], None, None, limit, offset)
        assert expected, "the fixture must produce matches"
        assert actual == expected


async def test_filtered_results_are_identical_and_filters_apply_before_the_rank_cap(pool: AsyncPostgreSQLPool) -> None:
    expected = await _legacy(
        pool,
        "love",
        20,
        0,
        year_filter="((data->>'year') IS NULL OR (data->>'year')::int >= 1980)",
        genre_filter="((data->'genres') IS NULL OR (data->'genres') ?| ARRAY['Jazz']::text[])",
        country_filter="((data->>'country') IS NULL OR (data->>'country') = ANY(ARRAY['UK','US']::text[]))",
    )
    actual = await _run_results(pool, "love", _TYPES, ["Jazz"], 1980, None, 20, 0, countries=["UK", "US"])
    assert expected
    assert actual == expected


async def test_headlines_and_row_shape_survive(pool: AsyncPostgreSQLPool) -> None:
    rows = await _run_results(pool, "love", ["release"], [], None, None, 5, 0)
    assert len(rows) == 5
    assert all("<b>" in r["highlight"] for r in rows)
    assert set(rows[0]) == {"type", "id", "name", "rank", "highlight", "year", "genres", "country"}
    assert isinstance(rows[0]["genres"], list)
    assert rows[0]["country"] in {"UK", "US", "Germany", "Japan"}


async def test_plan_holds_with_bound_parameters_and_past_the_prepare_threshold(pool: AsyncPostgreSQLPool) -> None:
    baseline = await _run_results(pool, "love", _TYPES, [], None, None, 20, 0)
    for _ in range(_PAST_PREPARE_THRESHOLD):
        assert await _run_results(pool, "love", _TYPES, [], None, None, 20, 0) == baseline
    assert await _run_total(pool, "love", _TYPES, [], None, None) > 0


async def test_statement_no_longer_funnels_every_branch_through_a_materialized_cte(pool: AsyncPostgreSQLPool) -> None:
    union_sql, union_params = _build_union(_TYPES, per_table_limit=40)
    query = sql.SQL(
        "EXPLAIN (FORMAT JSON) WITH q AS NOT MATERIALIZED (SELECT plainto_tsquery('english', %s) AS tsq),"
        " results AS ({u}) SELECT type, id, name, rank, highlight, year, genres, country FROM results ORDER BY rank DESC, id LIMIT %s OFFSET %s"
    ).format(u=union_sql)
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(query, ["love", *union_params, 20, 0])
        plan = json.dumps((await cur.fetchone())[0])  # type: ignore[index]
    assert "CTE Scan" not in plan
