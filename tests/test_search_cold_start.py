"""The first search after a start or a quiet spell must not pay for backends opened while it waits.

gm-catalog-api-0n70, measured on lux: the pool idled at its floor of two connections, and the first
uncached search opened four more on the request path; on each new backend the genre and media facets
then spent ~400 ms of a ~480 ms statement in JIT compilation. These tests pin the two changes: the
pool's floor covers one full search and is opened before the service reports healthy, and every search
statement runs with JIT off, scoped to its own transaction.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.config import ApiConfig
from api.queries import search_queries
from api.queries.search_queries import SEARCH_STATEMENTS_PER_SEARCH, _search_cursor, search_pool_floor


def _config(min_size: int = 2, max_size: int = 8) -> ApiConfig:
    return ApiConfig(
        postgres_host="localhost:5432",
        postgres_username="u",
        postgres_password="p",  # nosec B106  # noqa: S106  # test fixture value, not a real credential
        postgres_database="db",
        jwt_secret_key="x" * 32,
        neo4j_host="bolt://localhost:7687",
        neo4j_username="neo4j",
        neo4j_password="pw",  # nosec B106  # noqa: S106  # test fixture value, not a real credential
        postgres_pool_min_size=min_size,
        postgres_pool_max_size=max_size,
    )


class TestSearchPoolFloor:
    def test_the_default_floor_covers_one_search(self) -> None:
        assert SEARCH_STATEMENTS_PER_SEARCH == 6
        assert search_pool_floor(2, 8) == 6

    def test_a_larger_operator_minimum_is_kept(self) -> None:
        assert search_pool_floor(7, 8) == 7

    def test_the_maximum_still_caps_the_floor(self) -> None:
        # max_connections is the shared backend budget: the floor never exceeds it.
        assert search_pool_floor(2, 4) == 4
        assert search_pool_floor(1, 1) == 1

    def test_the_floor_is_never_below_one(self) -> None:
        assert search_pool_floor(0, 0) == 1

    def test_one_search_runs_no_more_statements_than_the_floor(self) -> None:
        """The floor is only right while it matches the gather in execute_search: count the cursors one search opens."""
        import asyncio
        import contextlib

        opened = 0

        @contextlib.asynccontextmanager
        async def counting_cursor(pool: Any) -> Any:  # noqa: ARG001
            nonlocal opened
            opened += 1
            cursor = AsyncMock()
            cursor.fetchall = AsyncMock(return_value=[])
            cursor.fetchone = AsyncMock(return_value={"total": 0})
            yield cursor

        with (
            patch.object(search_queries, "_search_cursor", counting_cursor),
            patch.object(search_queries, "execute_sql", new_callable=AsyncMock),
            patch.object(search_queries, "native_ids_for_pairs", new_callable=AsyncMock, return_value={}),
        ):
            asyncio.run(search_queries.execute_search(MagicMock(), None, "love", list(search_queries.ALL_TYPES), [], None, None, 20, 0))

        assert opened == SEARCH_STATEMENTS_PER_SEARCH


class TestPoolOpenedBeforeHealthy:
    @pytest.mark.asyncio
    async def test_create_pool_opens_one_search_worth_of_connections(self) -> None:
        from api.api import _create_pool

        pool = MagicMock()
        pool.initialize = AsyncMock()
        with (
            patch("api.api.AsyncPostgreSQLPool", return_value=pool) as constructor,
            patch("api.api.reconcile_stale_sync_history", new_callable=AsyncMock),
        ):
            assert await _create_pool(_config()) is pool

        kwargs = constructor.call_args.kwargs
        assert kwargs["min_connections"] == SEARCH_STATEMENTS_PER_SEARCH
        assert kwargs["max_connections"] == 8
        pool.initialize.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_the_service_reports_starting_until_the_floor_is_open(self) -> None:
        """initialize() opens min_connections; the health endpoint must not say healthy before it returns."""
        import api.api as api_module

        statuses: list[str] = []
        pool = MagicMock()

        async def initialize() -> None:
            statuses.append(api_module.get_health_data()["status"])

        pool.initialize = initialize
        api_module._pool = None
        try:
            with (
                patch("api.api.AsyncPostgreSQLPool", return_value=pool),
                patch("api.api.reconcile_stale_sync_history", new_callable=AsyncMock),
                patch("api.api._create_valkey", new_callable=AsyncMock, return_value=AsyncMock()),
                patch("api.api._create_neo4j", return_value=None),
                patch("api.api._configure_routers", return_value=None),
                patch("api.api.ApiConfig.from_env", return_value=_config()),
                patch("api.api.HealthServer", return_value=MagicMock()),
                patch("api.api.start_event_loop_monitor", return_value=None),
                patch("api.api._activity.ensure_startup_partitions", new_callable=AsyncMock),
                patch("api.api._prewarm_search_cache", new_callable=AsyncMock),
                patch("api.api.run_rarity_membership_refresh", new_callable=AsyncMock),
                patch("api.api._start_identity_maintenance"),
                patch("api.api._start_ledger_refresh"),
                patch("api.api._start_import_cache_watch"),
                patch("api.api._start_explore_hubs"),
                patch("api.api._start_genre_tree_refresh"),
            ):
                app = MagicMock()
                await api_module._start_service(app)
                assert api_module.get_health_data()["status"] == "healthy"
                for name in ("prewarm_task", "rarity_membership_task"):
                    await getattr(app.state, name)
        finally:
            api_module._pool = None
            api_module._config = None
            api_module._valkey = None
            api_module._neo4j = None

        assert statuses == ["starting"]


def _fake_pool() -> tuple[MagicMock, AsyncMock]:
    cursor = AsyncMock()
    cursor.__aenter__ = AsyncMock(return_value=cursor)
    cursor.__aexit__ = AsyncMock(return_value=False)
    transaction = MagicMock()
    transaction.__aenter__ = AsyncMock(return_value=None)
    transaction.__aexit__ = AsyncMock(return_value=False)
    conn = AsyncMock()
    conn.cursor = MagicMock(return_value=cursor)
    conn.transaction = MagicMock(return_value=transaction)
    conn.__aenter__ = AsyncMock(return_value=conn)
    conn.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.max_connections = 8
    pool.connection = MagicMock(return_value=conn)
    return pool, cursor


class TestSearchStatementsRunWithoutJit:
    @pytest.mark.asyncio
    async def test_the_cursor_turns_jit_off_with_the_statement_timeout(self) -> None:
        pool, cursor = _fake_pool()
        async with _search_cursor(pool):
            pass

        statement, params = cursor.execute.await_args.args
        # Transaction-local (third argument true): the pooled connection keeps neither setting.
        assert "set_config('jit', 'off', true)" in statement
        assert "set_config('statement_timeout', %s, true)" in statement
        assert params == (f"{int(search_queries.SEARCH_STATEMENT_TIMEOUT_SECONDS * 1000)}ms",)
        pool.connection.return_value.transaction.assert_called_once_with()
