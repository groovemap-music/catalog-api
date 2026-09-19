"""Query-shape coverage for the catalog-overview lookups (PostgreSQL backend).

Row-level agreement with the Cypher is `tests/test_real_databases.py`'s `catalog_overview`
family, which runs both engines. This module checks what each statement is made of, how the
module maps rows, and the empty-result edge case a fake pool can show that a live server's
own aggregate semantics would otherwise mask.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from api.queries import neo4j_pg_queries as pg
from tests.fake_postgres import FakePool


pytestmark = pytest.mark.asyncio


class TestYearRangeStatementShape:
    async def test_is_a_plain_select_not_a_graph_table_pattern(self) -> None:
        assert "GRAPH_TABLE" not in pg.YEAR_RANGE_SQL

    async def test_selects_from_the_phase_0_release_view(self) -> None:
        assert "FROM graph.release" in pg.YEAR_RANGE_SQL

    async def test_guards_the_empty_string_year_before_casting_to_int(self) -> None:
        assert "NULLIF(year, '')::int" in pg.YEAR_RANGE_SQL

    async def test_keeps_the_same_zero_sentinel_guard_as_the_cypher(self) -> None:
        assert "year_value > 0" in pg.YEAR_RANGE_SQL

    async def test_returns_no_row_rather_than_a_row_of_nulls_when_nothing_matches(self) -> None:
        # `min()`/`max()` over an empty set still return one row of NULLs; the outer
        # `WHERE matched > 0` is what makes an empty catalog behave like the Cypher's
        # `CALL { ... LIMIT 1 }`, which returns zero rows rather than one null row.
        assert "WHERE matched > 0" in pg.YEAR_RANGE_SQL


class TestGetYearRange:
    async def test_returns_min_and_max_year(self) -> None:
        pool = FakePool([[(1959, 2001)]])
        assert await pg.get_year_range(pool) == {"min_year": 1959, "max_year": 2001}

    async def test_runs_with_no_parameters(self) -> None:
        pool = FakePool([[(1959, 2001)]])
        await pg.get_year_range(pool)
        assert pool.sql == pg.YEAR_RANGE_SQL
        assert pool.params is None

    async def test_returns_none_when_no_release_has_a_year(self) -> None:
        assert await pg.get_year_range(FakePool([[]])) is None


class TestGraphStatsStatementShape:
    async def test_is_a_plain_select_not_a_graph_table_pattern(self) -> None:
        assert "GRAPH_TABLE" not in pg.GRAPH_STATS_SQL

    async def test_counts_every_phase_0_vertex_view_the_cypher_counts(self) -> None:
        for view in ("graph.artist", "graph.label", "graph.release", "graph.master", "graph.genre", "graph.style"):
            assert f"FROM {view}" in pg.GRAPH_STATS_SQL

    async def test_column_order_matches_the_cypher_dict_key_order(self) -> None:
        columns = [line.strip().split(" AS ")[-1].rstrip(",") for line in pg.GRAPH_STATS_SQL.strip().splitlines()[1:]]
        assert columns == ["artists", "labels", "releases", "masters", "genres", "styles"]


class TestGetGraphStats:
    async def test_returns_the_six_counts_in_cypher_order(self) -> None:
        pool = FakePool([[(11, 1, 15, 1, 0, 0)]])
        assert await pg.get_graph_stats(pool) == {
            "artists": 11,
            "labels": 1,
            "releases": 15,
            "masters": 1,
            "genres": 0,
            "styles": 0,
        }
        assert list((await pg.get_graph_stats(FakePool([[(11, 1, 15, 1, 0, 0)]]))).keys()) == [
            "artists",
            "labels",
            "releases",
            "masters",
            "genres",
            "styles",
        ]

    async def test_runs_with_no_parameters(self) -> None:
        pool = FakePool([[(0, 0, 0, 0, 0, 0)]])
        await pg.get_graph_stats(pool)
        assert pool.sql == pg.GRAPH_STATS_SQL
        assert pool.params is None

    async def test_count_star_is_already_an_int_not_a_decimal(self) -> None:
        # Unlike sum()/avg() over numeric, count(*) yields bigint, which psycopg already
        # hands back as int — a Decimal here would mean a cast was needed and missed.
        pool = FakePool([[(11, 1, 15, 1, 0, 0)]])
        stats = await pg.get_graph_stats(pool)
        assert all(not isinstance(value, Decimal) for value in stats.values())
