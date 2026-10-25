"""Unit tests for the identity ledger's refresh triggers (api/ledger_refresh.py).

The decision, the lock, and the statement order are asserted against the `FakePool` double;
`tests/test_identity_ledger_integration.py` runs the same statements against the real schema,
including a re-attachment run's supersession reaching the works list with no poll.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api import ledger_refresh
from api.api import _start_ledger_refresh
from api.config import ApiConfig
from api.ledger_refresh import (
    REASON_QUIET,
    REASON_STALE,
    REASON_UNPOPULATED,
    ChangeTracker,
    LedgerState,
    poll_once,
    refresh_ledger,
    refresh_reason,
    run_refresh_loop,
)
from tests.fake_postgres import FakePool


REFRESHED = datetime(2026, 10, 25, 21, 0, tzinfo=UTC)
QUIET, MAX_STALENESS = 600.0, 21600.0


def _sql(pool: FakePool) -> list[str]:
    return [" ".join(call.sql.split()) for call in pool.calls]


class Clock:
    """A monotonic clock the test moves by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def recorded() -> Any:
    with patch.object(ledger_refresh.telemetry, "record_identity_ledger_refresh") as record:
        yield record


# ---------------------------------------------------------------------------------------------
# The decision
# ---------------------------------------------------------------------------------------------


def _state(*, populated: bool = True, stored: int | None = 100, age: float | None = 60.0, current: int = 100) -> LedgerState:
    return LedgerState(populated=populated, stored=stored, age_seconds=age, current=current)


@pytest.mark.parametrize(
    ("state", "still_for", "expected"),
    [
        # Nothing written since the last refresh: never, however long ago it was.
        (_state(), 10_000.0, None),
        (_state(age=10**6), 10_000.0, None),
        # Changed, then still for a quiet interval: the load is done.
        (_state(current=150), QUIET, REASON_QUIET),
        # Changed and still writing, within the cap: wait.
        (_state(current=150), QUIET - 1, None),
        # Still writing, but the last refresh is older than the cap.
        (_state(current=150, age=MAX_STALENESS), 0.0, REASON_STALE),
        # A watermark this module did not write, or none at all, differs from any sum.
        (_state(stored=None, current=0), QUIET, REASON_QUIET),
        # Populated, but never refreshed by the function: as old as can be.
        (_state(stored=None, age=None), 0.0, REASON_STALE),
        # Unpopulated (before the first refresh, or a view recreated): at once, changed or not.
        (_state(populated=False), 0.0, REASON_UNPOPULATED),
        (_state(populated=False, stored=None, age=None), 0.0, REASON_UNPOPULATED),
    ],
    ids=["no-change", "no-change-old", "quiet", "still-writing", "cap", "foreign-watermark", "never-refreshed", "unpopulated", "first"],
)
def test_refresh_reason(state: LedgerState, still_for: float, expected: str | None) -> None:
    assert refresh_reason(state, still_for=still_for, quiet=QUIET, max_staleness=MAX_STALENESS) == expected


def test_tracker_measures_how_long_the_watermark_held_still() -> None:
    clock = Clock()
    tracker = ChangeTracker(clock=clock)

    # A replica that has just started has not watched for a quiet interval yet.
    assert tracker.observe(100) == 0.0
    clock.now += 300
    assert tracker.observe(100) == 300.0
    clock.now += 300
    assert tracker.observe(140) == 0.0  # a load wrote: start over
    clock.now += 600
    assert tracker.observe(140) == 600.0


# ---------------------------------------------------------------------------------------------
# The poll
# ---------------------------------------------------------------------------------------------


def _poll_pool(current: int, *states: tuple[Any, ...], locked: bool = True) -> FakePool:
    """Results for one poll: the watermark, the stored state, then (if it gets that far) the lock,
    the state re-read under it, and the refresh."""
    results: list[list[tuple[Any, ...]]] = [[(current,)], [states[0]]]
    if len(states) > 1:
        results += [[(locked,)], [states[1]], [(REFRESHED,)]]
    return FakePool(results)


async def _poll(pool: FakePool, tracker: ChangeTracker) -> str | None:
    return await poll_once(pool, tracker, quiet=QUIET, max_staleness=MAX_STALENESS)


@pytest.mark.asyncio
async def test_change_then_quiet_refreshes_and_stores_the_watermark(recorded: MagicMock) -> None:
    clock = Clock()
    tracker = ChangeTracker(clock=clock)
    assert await _poll(_poll_pool(150, (True, 100, 60.0)), tracker) is None  # just changed: wait

    clock.now += QUIET
    pool = _poll_pool(150, (True, 100, 660.0), (True, 100, 660.0))
    assert await _poll(pool, tracker) == REASON_QUIET

    sql = _sql(pool)
    assert sql[0] == "BEGIN" and sql[-1] == "COMMIT"
    assert "FROM pg_stat_user_tables" in sql[1]
    assert "pg_try_advisory_xact_lock(hashtext(%s))" in sql[3]
    assert pool.calls[3].params == ("groovemap:identity-ledger-refresh",)
    assert sql[5] == "SELECT public.identity_ledger_refresh(%s::jsonb)"
    assert json.loads(pool.calls[5].params[0]) == {"writes": 150}
    recorded.assert_called_once_with("poll", "success")


@pytest.mark.asyncio
async def test_no_change_takes_no_lock_and_refreshes_nothing(recorded: MagicMock) -> None:
    tracker = ChangeTracker(clock=Clock())
    pool = _poll_pool(100, (True, 100, 10**6))

    assert await _poll(pool, tracker) is None
    assert not any("advisory" in sql or "identity_ledger_refresh(" in sql for sql in _sql(pool))
    recorded.assert_not_called()


@pytest.mark.asyncio
async def test_a_load_still_writing_waits_until_the_cap(recorded: MagicMock) -> None:
    clock = Clock()
    tracker = ChangeTracker(clock=clock)
    for current in (110, 120, 130):  # every poll sees new writes
        assert await _poll(_poll_pool(current, (True, 100, 3600.0)), tracker) is None
        clock.now += 300

    pool = _poll_pool(140, (True, 100, MAX_STALENESS), (True, 100, MAX_STALENESS))
    assert await _poll(pool, tracker) == REASON_STALE
    assert json.loads(pool.calls[5].params[0]) == {"writes": 140}
    recorded.assert_called_once_with("poll", "success")


@pytest.mark.asyncio
async def test_unpopulated_views_are_refreshed_at_once(recorded: MagicMock) -> None:
    pool = _poll_pool(0, (False, None, None), (False, None, None))
    assert await _poll(pool, ChangeTracker(clock=Clock())) == REASON_UNPOPULATED
    assert pool.calls[1].params is None
    assert pool.calls[2].params == {"views": list(ledger_refresh.VIEWS)}
    recorded.assert_called_once_with("poll", "success")


@pytest.mark.asyncio
async def test_a_replica_that_cannot_take_the_lock_skips_this_poll(recorded: MagicMock) -> None:
    pool = _poll_pool(0, (False, None, None), (False, None, None), locked=False)

    assert await _poll(pool, ChangeTracker(clock=Clock())) is None
    assert not any("identity_ledger_refresh(" in sql for sql in _sql(pool))
    recorded.assert_not_called()


@pytest.mark.asyncio
async def test_a_refresh_another_replica_just_committed_is_not_repeated(recorded: MagicMock) -> None:
    # Before the lock the views were unpopulated; under it, the other replica's refresh is visible.
    pool = _poll_pool(150, (False, None, None), (True, 150, 0.5))

    assert await _poll(pool, ChangeTracker(clock=Clock())) is None
    assert _sql(pool)[4].startswith("SELECT (SELECT count(*)")
    assert not any("identity_ledger_refresh(" in sql for sql in _sql(pool))
    recorded.assert_not_called()


@pytest.mark.asyncio
async def test_a_failed_poll_refresh_is_logged_counted_and_rolled_back(recorded: MagicMock) -> None:
    pool = FakePool(
        [[(0,)], [(False, None, None)], [(True,)], [(False, None, None)]],
        raise_on={"SELECT public.identity_ledger_refresh": RuntimeError("disk full")},
    )

    with patch.object(ledger_refresh.logger, "error") as error:
        assert await _poll(pool, ChangeTracker(clock=Clock())) is None

    assert _sql(pool)[-1] == "ROLLBACK"  # the transaction-level lock goes with it
    recorded.assert_called_once_with("poll", "failed")
    assert error.call_args.kwargs["alert"] == "identity_ledger_refresh_failed"


# ---------------------------------------------------------------------------------------------
# The inline refresh
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inline_refresh_waits_for_the_lock_and_stores_the_watermark(recorded: MagicMock) -> None:
    pool = FakePool([[], [(4242,)], [(REFRESHED,)]])

    assert await refresh_ledger(pool, trigger="reattach", job_id="j") == REFRESHED

    sql = _sql(pool)
    assert sql[0] == "BEGIN" and sql[-1] == "COMMIT"
    # Blocking, not try: the run must be in the views when it reports done.
    assert sql[1] == "SELECT pg_advisory_xact_lock(hashtext(%s))"
    assert pool.calls[1].params == ("groovemap:identity-ledger-refresh",)
    assert "FROM pg_stat_user_tables" in sql[2]
    assert json.loads(pool.calls[3].params[0]) == {"writes": 4242}
    recorded.assert_called_once_with("reattach", "success")


@pytest.mark.asyncio
async def test_inline_refresh_failure_is_logged_and_counted_never_raised(recorded: MagicMock) -> None:
    pool = FakePool([[], [(1,)]], raise_on={"SELECT public.identity_ledger_refresh": RuntimeError("canceling statement due to lock timeout")})

    with patch.object(ledger_refresh.logger, "error") as error:
        assert await refresh_ledger(pool, trigger="backfill", job_id="j") is None

    assert _sql(pool)[-1] == "ROLLBACK"
    recorded.assert_called_once_with("backfill", "failed")
    assert error.call_args.kwargs == {"alert": "identity_ledger_refresh_failed", "trigger": "backfill", "exc_info": True, "job_id": "j"}


def test_the_watermark_sums_every_source_the_views_read() -> None:
    for schema, table in (
        ("public", "provider_aliases"),
        ("public", "catalog_items"),
        ("public", "catalog_item_supersessions"),
        ("public", "identity_link_events"),
        ("graph", "derived_from"),
        ("musicbrainz", "releases"),
    ):
        assert f"('{schema}', '{table}')" in ledger_refresh._WATERMARK
    assert "n_tup_ins + n_tup_upd + n_tup_del" in ledger_refresh._WATERMARK


# ---------------------------------------------------------------------------------------------
# The loop and its configuration
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loop_polls_then_sleeps_the_interval_with_one_tracker() -> None:
    poll = AsyncMock(return_value=None)
    sleep = AsyncMock(side_effect=[None, asyncio.CancelledError()])
    with patch.object(ledger_refresh, "poll_once", poll), patch.object(ledger_refresh.asyncio, "sleep", sleep), pytest.raises(asyncio.CancelledError):
        await run_refresh_loop("pool", interval=42, quiet=600, max_staleness=3600)

    assert poll.await_count == 2
    assert poll.await_args_list[0].args[1] is poll.await_args_list[1].args[1]
    assert poll.await_args.kwargs == {"quiet": 600, "max_staleness": 3600}
    sleep.assert_awaited_with(42)


REQUIRED_ENV = {
    "POSTGRES_HOST": "localhost",
    "POSTGRES_USERNAME": "test",
    "POSTGRES_PASSWORD": "test",
    "POSTGRES_DATABASE": "test",
    "JWT_SECRET_KEY": "secret",
    "NEO4J_HOST": "localhost",
    "NEO4J_USERNAME": "neo4j",
    "NEO4J_PASSWORD": "pass",
}


def _config(**overrides: str) -> ApiConfig:
    with patch.dict(os.environ, {**REQUIRED_ENV, **overrides}, clear=True):
        return ApiConfig.from_env()


def test_poll_is_off_by_default_with_the_designed_intervals() -> None:
    config = _config()
    assert config.identity_ledger_refresh_enabled is False
    assert (config.identity_ledger_refresh_interval, config.identity_ledger_refresh_quiet, config.identity_ledger_max_staleness) == (300, 600, 21600)


def test_poll_settings_are_read_and_clamped() -> None:
    config = _config(
        IDENTITY_LEDGER_REFRESH_ENABLED="true",
        IDENTITY_LEDGER_REFRESH_INTERVAL="0",
        IDENTITY_LEDGER_REFRESH_QUIET="120",
        IDENTITY_LEDGER_MAX_STALENESS="invalid",
    )
    assert config.identity_ledger_refresh_enabled is True
    assert (config.identity_ledger_refresh_interval, config.identity_ledger_refresh_quiet, config.identity_ledger_max_staleness) == (1, 120, 21600)


@pytest.mark.asyncio
async def test_lifespan_starts_the_poll_only_when_enabled() -> None:
    app = SimpleNamespace(state=SimpleNamespace())
    _start_ledger_refresh(app, _config(), "pool")  # type: ignore[arg-type]
    assert app.state.ledger_refresh_task is None

    loop = AsyncMock()
    with patch("api.api.run_refresh_loop", loop):
        _start_ledger_refresh(app, _config(IDENTITY_LEDGER_REFRESH_ENABLED="true", IDENTITY_LEDGER_REFRESH_INTERVAL="30"), "pool")  # type: ignore[arg-type]
        await app.state.ledger_refresh_task
    loop.assert_awaited_once_with("pool", interval=30, quiet=600, max_staleness=21600)
