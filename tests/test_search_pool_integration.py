"""Engine-backed proof that cold searches cannot starve the connection pool (audit H3).

An uncached search opens six connections from a pool of eight. With statements as slow as the audit
measured at full scale, a few cold searches held every connection past the checkout budget, and then
unrelated routes such as login failed too. These tests drive the real ``AsyncPostgreSQLPool`` against a
real server, with ``pg_sleep`` standing in for the slow plans (the duration is the only simulated part),
and require that a login-style probe stays fast, that a statement past its timeout is cancelled with the
connection left usable, and that a search that cannot get a slot says so instead of waiting forever.

Nothing here touches a shared database: ``just test-integration`` starts throwaway containers.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from time import perf_counter

import pytest
import pytest_asyncio
from common import AsyncPostgreSQLPool, parse_postgres_host_port

from api.queries import search_queries
from api.queries.search_queries import SearchBusy, SearchTimedOut, _search_cursor


pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_POOL_SIZE = 8
_SEARCHES = 8
# Two long statements and four short ones per search, the shape the audit measured on lux.
_LONG, _SHORT = 2.0, 0.1


def _env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.fail(f"{name} must be supplied by `just test-integration`")
    return value


@pytest_asyncio.fixture
async def pool() -> AsyncIterator[AsyncPostgreSQLPool]:
    """A pool of the production size (the shared fixture's two connections would hide the problem)."""
    host, port = parse_postgres_host_port(_env("POSTGRES_HOST"))
    pool = AsyncPostgreSQLPool(
        connection_params={
            "host": host,
            "port": port,
            "dbname": _env("POSTGRES_DATABASE"),
            "user": _env("POSTGRES_USERNAME"),
            "password": _env("POSTGRES_PASSWORD"),
        },
        min_connections=1,
        max_connections=_POOL_SIZE,
        max_retries=3,
        health_check_interval=3600,
    )
    await pool.initialize()
    try:
        yield pool
    finally:
        await pool.close()


async def _statement(pool: AsyncPostgreSQLPool, seconds: float) -> None:
    async with _search_cursor(pool) as cur:
        await cur.execute("SELECT pg_sleep(%s)", (seconds,))


async def _search(pool: AsyncPostgreSQLPool) -> str:
    """One cold search: six concurrent statements, like ``execute_search``."""
    try:
        await asyncio.gather(*[_statement(pool, _LONG if i < 2 else _SHORT) for i in range(6)])
    except SearchBusy:
        return "busy"
    return "ok"


async def _probe(pool: AsyncPostgreSQLPool, stop: asyncio.Event) -> list[float]:
    """A login-style probe: check a connection out, run one trivial statement, give it back."""
    waits: list[float] = []
    while not stop.is_set():
        started = perf_counter()
        async with pool.connection() as conn, conn.cursor() as cur:
            await cur.execute("SELECT 1")
        waits.append(perf_counter() - started)
        await asyncio.sleep(0.05)
    return waits


async def _storm(pool: AsyncPostgreSQLPool) -> tuple[list[str], list[float]]:
    stop = asyncio.Event()
    probe = asyncio.create_task(_probe(pool, stop))
    await asyncio.sleep(0.2)
    outcomes = await asyncio.gather(*[_search(pool) for _ in range(_SEARCHES)])
    stop.set()
    return list(outcomes), await probe


async def test_logins_stay_healthy_under_concurrent_cold_searches(pool: AsyncPostgreSQLPool, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(search_queries, "SEARCH_QUEUE_TIMEOUT_SECONDS", 60.0)
    outcomes, waits = await _storm(pool)
    assert outcomes == ["ok"] * _SEARCHES
    assert len(waits) > 5
    # Without the slots the probe queues behind forty-eight statements for the same eight connections.
    assert max(waits) < 1.0, f"login probe waited {max(waits):.2f}s"


async def test_a_statement_past_its_timeout_is_cancelled_and_the_connection_stays_usable(
    pool: AsyncPostgreSQLPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(search_queries, "SEARCH_STATEMENT_TIMEOUT_SECONDS", 0.5)
    started = perf_counter()
    with pytest.raises(SearchTimedOut):
        await _statement(pool, 30.0)
    assert perf_counter() - started < 5.0
    # The setting is scoped to the statement's transaction: no pooled connection keeps it.
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("SHOW statement_timeout")
        assert (await cur.fetchone())[0] == "0"  # type: ignore[index]
        await cur.execute("SELECT 1")
        assert (await cur.fetchone())[0] == 1  # type: ignore[index]


async def test_a_search_that_cannot_get_a_slot_is_busy_not_hung(pool: AsyncPostgreSQLPool, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(search_queries, "SEARCH_QUEUE_TIMEOUT_SECONDS", 0.3)
    holders = [asyncio.create_task(_statement(pool, 2.0)) for _ in range(_POOL_SIZE - search_queries.SEARCH_POOL_RESERVE)]
    await asyncio.sleep(0.3)
    started = perf_counter()
    with pytest.raises(SearchBusy):
        await _statement(pool, 0.1)
    assert perf_counter() - started < 1.5
    await asyncio.gather(*holders)


async def test_searches_never_take_the_reserved_connections(pool: AsyncPostgreSQLPool, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(search_queries, "SEARCH_QUEUE_TIMEOUT_SECONDS", 60.0)
    holders = [asyncio.create_task(_statement(pool, 1.0)) for _ in range(_POOL_SIZE * 2)]
    await asyncio.sleep(0.4)
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("SELECT count(*) FROM pg_stat_activity WHERE query LIKE 'SELECT pg_sleep%%' AND state = 'active'")
        active = (await cur.fetchone())[0]  # type: ignore[index]
    assert active <= _POOL_SIZE - search_queries.SEARCH_POOL_RESERVE
    await asyncio.gather(*holders)


async def test_search_statements_run_without_jit_and_leave_the_connection_at_its_default(pool: AsyncPostgreSQLPool) -> None:
    """gm-catalog-api-0n70: JIT is off for the statement's transaction only, like the statement timeout."""
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("SHOW jit")
        default = (await cur.fetchone())[0]  # type: ignore[index]
    async with _search_cursor(pool) as cur:
        await cur.execute("SHOW jit")
        assert (await cur.fetchone())["jit"] == "off"
    for _ in range(_POOL_SIZE):  # every pooled connection, whichever the search used
        async with pool.connection() as conn, conn.cursor() as cur:
            await cur.execute("SHOW jit")
            assert (await cur.fetchone())[0] == default  # type: ignore[index]


async def test_a_pool_at_the_search_floor_has_one_search_open_before_the_first_request() -> None:
    """gm-catalog-api-0n70: initialize() opens the floor, so the first search checks out open backends."""
    host, port = parse_postgres_host_port(_env("POSTGRES_HOST"))
    floor = search_queries.search_pool_floor(2, _POOL_SIZE)
    pool = AsyncPostgreSQLPool(
        connection_params={
            "host": host,
            "port": port,
            "dbname": _env("POSTGRES_DATABASE"),
            "user": _env("POSTGRES_USERNAME"),
            "password": _env("POSTGRES_PASSWORD"),
        },
        min_connections=floor,
        max_connections=_POOL_SIZE,
        max_retries=3,
        health_check_interval=3600,
    )
    await pool.initialize()
    try:
        assert floor == search_queries.SEARCH_STATEMENTS_PER_SEARCH
        assert pool.connections.qsize() == floor
        # One search's worth of concurrent statements opens no further backend.
        await asyncio.gather(*[_statement(pool, 0.2) for _ in range(floor)])
        assert pool.active_connections == floor
    finally:
        await pool.close()
