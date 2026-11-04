"""Engine-backed proof that the sort-free search total counts what the ranked one did.

``_run_total`` used to build the full ranked results statement and count its rows, sorting up to
10,000 documents per table only to throw the order away (audit H2,
``docs/sql-performance-audit-2026-11-04.md``). It now stops each branch at the cap without ranking.
The cap keeps *some* 10,000 matches and the count is how many it kept, so which ones is irrelevant;
these tests run both statements on the real PostgreSQL planner and require the same totals, with
filters applied before the cap and with the cap itself biting.

Nothing here touches a shared database: ``just test-integration`` starts throwaway containers.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
import pytest_asyncio
from common import AsyncPostgreSQLPool
from psycopg import sql
from psycopg.rows import dict_row

from api.queries import search_queries
from api.queries.search_queries import _build_count_union, _build_union, _run_total
from tests.test_real_databases import postgres_pool


__all__ = ["postgres_pool"]

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_TYPES = ["artist", "label", "master", "release"]


@pytest_asyncio.fixture
async def pool(postgres_pool: AsyncPostgreSQLPool) -> AsyncPostgreSQLPool:
    genres = [["Rock"], ["Jazz"], ["Rock", "Pop"], ["Electronic"]]
    countries = ["UK", "US", "Germany", "Japan"]
    releases = [
        (
            f"r{i:04d}",
            f"r{i:04d}",
            json.dumps(
                {
                    "title": ["Love", "Love Love", "Crazy Love Songs", "Lovers", "Blue"][i % 5],
                    "year": str(1950 + i % 70),
                    "genres": genres[i % 4],
                    "country": countries[i % 4],
                }
            ),
        )
        for i in range(400)
    ]
    masters = [(f"m{i}", f"m{i}", json.dumps({"title": ["Love Supreme", "Love", "Blue"][i % 3], "year": str(1960 + i % 40)})) for i in range(60)]
    artists = [(f"a{i}", f"a{i}", json.dumps({"name": ["Love", "Miles Davis", "Lovers Rock"][i % 3]})) for i in range(30)]
    labels = [(f"l{i}", f"l{i}", json.dumps({"name": ["Love Records", "Other"][i % 2]})) for i in range(15)]
    async with postgres_pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("TRUNCATE artists, labels, masters, releases")
        for table, rows in (("artists", artists), ("labels", labels), ("masters", masters), ("releases", releases)):
            await cur.executemany(f"INSERT INTO {table} (data_id, hash, data) VALUES (%s, %s, %s::jsonb)", rows)  # noqa: S608
        await cur.execute("ANALYZE artists, labels, masters, releases")
    return postgres_pool


async def _ranked_total(pool: AsyncPostgreSQLPool, q: str, **filters: Any) -> int:
    """The statement `_run_total` ran before: rank and sort every branch, then count."""
    union_sql, union_params = _build_union(_TYPES, per_table_limit=search_queries._TOTAL_COUNT_CAP, **filters)
    query = sql.SQL("WITH q AS (SELECT plainto_tsquery('english', %s) AS tsq), results AS ({u}) SELECT COUNT(*) AS total FROM results").format(
        u=union_sql
    )
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(query, [q, *union_params])
        row = await cur.fetchone()
    return int(row["total"]) if row else 0


@pytest.mark.parametrize("cap", [10000, 25, 3])
async def test_total_matches_the_ranked_count_with_and_without_the_cap(pool: AsyncPostgreSQLPool, monkeypatch: pytest.MonkeyPatch, cap: int) -> None:
    monkeypatch.setattr(search_queries, "_TOTAL_COUNT_CAP", cap)
    expected = await _ranked_total(pool, "love")
    assert expected > 0
    assert await _run_total(pool, "love", _TYPES, [], None, None) == expected


async def test_total_matches_the_ranked_count_under_every_filter(pool: AsyncPostgreSQLPool, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(search_queries, "_TOTAL_COUNT_CAP", 40)
    cases: list[dict[str, Any]] = [
        {"year_min": 1980},
        {"year_max": 1970},
        {"year_min": 1960, "year_max": 1999},
        {"genres": ["Jazz"]},
        {"countries": ["UK", "US"]},
        {"genres": ["Rock"], "year_min": 1975, "countries": ["Germany"]},
    ]
    for case in cases:
        expected = await _ranked_total(pool, "love", **case)
        actual = await _run_total(
            pool,
            "love",
            _TYPES,
            case.get("genres", []),
            case.get("year_min"),
            case.get("year_max"),
            countries=case.get("countries"),
        )
        assert actual == expected, case
        assert expected > 0, case


async def test_total_is_stable_past_the_prepare_threshold_and_for_a_term_with_no_match(pool: AsyncPostgreSQLPool) -> None:
    first = await _run_total(pool, "love", _TYPES, [], None, None)
    for _ in range(8):
        assert await _run_total(pool, "love", _TYPES, [], None, None) == first
    assert await _run_total(pool, "zzzznomatch", _TYPES, [], None, None) == 0


async def test_count_statement_has_no_sort_and_no_materialized_cte(pool: AsyncPostgreSQLPool) -> None:
    union_sql, union_params = _build_count_union(_TYPES, genres=["Rock"], year_min=1960)
    query = sql.SQL(
        "EXPLAIN (FORMAT JSON) WITH q AS NOT MATERIALIZED (SELECT plainto_tsquery('english', %s) AS tsq) SELECT COUNT(*) FROM ({u}) matched"
    ).format(u=union_sql)
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(query, ["love", *union_params])
        plan = json.dumps((await cur.fetchone())[0])  # type: ignore[index]
    assert "CTE Scan" not in plan
    assert '"Sort"' not in plan
    assert "Top-N" not in plan
