"""Unit tests for the automatic identity-maintenance watcher (api/reattach_trigger.py).

The statement shapes and the lock / marker ordering are asserted against the `FakePool`
double; `tests/test_reattach_trigger_integration.py` proves the same flow against a real
server, including two replicas racing for one extraction.
"""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest

from api import reattach_trigger
from api.api import _start_identity_maintenance
from api.config import ApiConfig
from api.reattach_trigger import (
    AUDIT_ACTION,
    DISCOGS_DATA_TYPES,
    FAILED_ACTION,
    STARTED_ACTION,
    SYSTEM_ACTOR_EMAIL,
    SYSTEM_ACTOR_ID,
    extraction_target,
    run_pending,
    run_trigger_loop,
)
from tests.fake_postgres import FakePool
from tests.test_metrics_config import REQUIRED_ENV


VERSION = "20260901"
_PENDING = [[(True,)], [(VERSION,)], [(False,)]]
_ACTOR = [[], [(SYSTEM_ACTOR_EMAIL, False, False)]]
_REPORT = {"apply": True, "census": {}, "outcomes": {"release": {"reattached": 1}}}
_PROJECTION = {"Release": 3}


def _sql(pool: FakePool) -> list[str]:
    return [" ".join(call.sql.split()) for call in pool.calls]


@pytest.fixture
def jobs() -> MagicMock:
    """Both jobs, patched on one parent mock so their call order is observable."""
    parent = MagicMock()
    parent.reattach = AsyncMock(return_value=_REPORT)
    parent.project = AsyncMock(return_value=_PROJECTION)
    with (
        patch.object(reattach_trigger, "run_reattachment", parent.reattach),
        patch.object(reattach_trigger, "run_gm_id_projection", parent.project),
    ):
        yield parent


@pytest.mark.asyncio
async def test_no_latch_relation_does_nothing(jobs: MagicMock) -> None:
    pool = FakePool([[(False,)]])
    assert await run_pending(pool, object()) is None
    assert "to_regclass('public.loader_extraction_latch')" in pool.sql
    jobs.reattach.assert_not_called()


@pytest.mark.asyncio
async def test_no_complete_extraction_does_nothing(jobs: MagicMock) -> None:
    pool = FakePool([[(True,)], []])
    assert await run_pending(pool, object()) is None
    latest = pool.calls[1]
    assert "loader = 'discogs'" in latest.sql
    assert "signals @> %s::text[]" in latest.sql
    assert "ORDER BY created_at DESC" in latest.sql
    assert latest.params == (list(DISCOGS_DATA_TYPES),)
    jobs.reattach.assert_not_called()


@pytest.mark.asyncio
async def test_handled_extraction_takes_no_lock(jobs: MagicMock) -> None:
    pool = FakePool([[(True,)], [(VERSION,)], [(True,)]])
    assert await run_pending(pool, object()) is None
    assert pool.calls[2].params == (str(SYSTEM_ACTOR_ID), AUDIT_ACTION, "discogs:20260901")
    assert not any("advisory" in sql for sql in _sql(pool))
    jobs.reattach.assert_not_called()


@pytest.mark.asyncio
async def test_lock_held_elsewhere_skips_without_unlocking(jobs: MagicMock) -> None:
    pool = FakePool([*_PENDING, [(False,)]])
    assert await run_pending(pool, object()) is None
    assert "pg_try_advisory_lock" in _sql(pool)[-1]
    assert not any("pg_advisory_unlock" in sql for sql in _sql(pool))
    jobs.reattach.assert_not_called()


@pytest.mark.asyncio
async def test_marker_written_by_another_replica_is_rechecked_under_the_lock(jobs: MagicMock) -> None:
    pool = FakePool([*_PENDING, [(True,)], [(True,)], [(VERSION,)], [(True,)], [(True,)]])
    assert await run_pending(pool, object()) is None
    assert "pg_advisory_unlock" in _sql(pool)[-1]
    jobs.reattach.assert_not_called()


# Everything up to and including the system actor check, on the lock's own connection.
_UP_TO_ACTOR = [*_PENDING, [(True,)], *_PENDING, *_ACTOR]


def _audit(pool: FakePool) -> list[tuple[str, tuple[Any, ...]]]:
    """The run's admin_audit_log statements, as (verb, params) with a Jsonb's payload unwrapped."""
    return [
        (call.sql.split()[0], tuple(getattr(param, "obj", param) for param in call.params))
        for call in pool.calls
        if "admin_audit_log (id" in call.sql or call.sql.startswith("UPDATE admin_audit_log")
    ]


@pytest.mark.asyncio
async def test_runs_apply_then_projection_then_marks_and_unlocks(jobs: MagicMock) -> None:
    pool = FakePool([*_UP_TO_ACTOR, [], [], [(True,)]])
    driver = object()
    audited_before_run: list[Any] = []
    jobs.reattach.side_effect = lambda *_args, **_kwargs: audited_before_run.append(_audit(pool)) or _REPORT
    assert await run_pending(pool, driver) == VERSION

    assert [name for name, _args, _kwargs in jobs.mock_calls] == ["reattach", "project"]
    jobs.reattach.assert_awaited_once()
    assert jobs.reattach.await_args.args == (pool,)
    assert jobs.reattach.await_args.kwargs["apply"] is True
    decision_ref = jobs.reattach.await_args.kwargs["decision_ref"]
    jobs.project.assert_awaited_once_with(pool, driver)

    sql = _sql(pool)
    assert "pg_try_advisory_lock(hashtext(%s))" in sql[3]
    assert sql[7].startswith("INSERT INTO users") and "ON CONFLICT (id) DO NOTHING" in sql[7]
    assert pool.calls[7].params[:2] == (str(SYSTEM_ACTOR_ID), SYSTEM_ACTOR_EMAIL)
    assert sql[9].startswith("INSERT INTO admin_audit_log")
    assert sql[10].startswith("UPDATE admin_audit_log")
    assert "pg_advisory_unlock(hashtext(%s))" in sql[11]
    assert pool.calls[3].params == pool.calls[11].params

    # The started row existed before the re-attachment wrote anything, under the run's decision_ref.
    [[(verb, (entry_id, actor, action, target, started))]] = audited_before_run
    assert verb == "INSERT"
    assert UUID(entry_id) == decision_ref
    assert (actor, action, target) == (str(SYSTEM_ACTOR_ID), STARTED_ACTION, extraction_target(VERSION))
    assert started == {"job_id": entry_id, "apply": True, "extraction": VERSION}

    # It then becomes the marker: same row, the handled action and the outcomes.
    [_insert, (verb, (marker_action, details, marker_id))] = _audit(pool)
    assert (verb, marker_action, marker_id) == ("UPDATE", AUDIT_ACTION, entry_id)
    assert details["extraction"] == VERSION
    assert details["apply"] is True
    assert details["outcomes"] == _REPORT["outcomes"]
    assert details["projection"] == _PROJECTION
    assert details["job_id"] == entry_id


@pytest.mark.asyncio
@pytest.mark.parametrize("failing", ["reattach", "project"])
async def test_a_failed_job_writes_no_marker_and_still_unlocks(jobs: MagicMock, failing: str) -> None:
    getattr(jobs, failing).side_effect = RuntimeError("boom")
    pool = FakePool([*_UP_TO_ACTOR, [], [], [(True,)]])
    with pytest.raises(RuntimeError, match="boom"):
        await run_pending(pool, object())
    sql = _sql(pool)
    # The started row is updated to the failed action, under the run's decision_ref: not the marker.
    [(inserted, (entry_id, *_rest)), (updated, (action, details, updated_id))] = _audit(pool)
    assert UUID(entry_id) == jobs.reattach.await_args.kwargs["decision_ref"]
    assert (inserted, updated, action, updated_id) == ("INSERT", "UPDATE", FAILED_ACTION, entry_id)
    assert details == {"job_id": entry_id, "apply": True, "error": "RuntimeError", "extraction": VERSION}
    assert "pg_advisory_unlock" in sql[-1]


@pytest.mark.asyncio
async def test_a_started_row_that_cannot_be_written_stops_the_run_and_unlocks(jobs: MagicMock) -> None:
    pool = FakePool([*_UP_TO_ACTOR, [(True,)]], raise_on={"INSERT INTO admin_audit_log": RuntimeError("audit table unavailable")})
    with pytest.raises(RuntimeError, match="audit table unavailable"):
        await run_pending(pool, object())
    jobs.reattach.assert_not_called()
    jobs.project.assert_not_called()
    assert [verb for verb, _params in _audit(pool)] == ["INSERT"]
    assert "pg_advisory_unlock" in _sql(pool)[-1]


@pytest.mark.asyncio
async def test_a_marker_that_cannot_be_written_leaves_the_started_row_and_unlocks(jobs: MagicMock) -> None:
    pool = FakePool([*_UP_TO_ACTOR, [], [(True,)]], raise_on={"UPDATE admin_audit_log": RuntimeError("audit table unavailable")})
    with pytest.raises(RuntimeError, match="audit table unavailable"):
        await run_pending(pool, object())
    jobs.project.assert_awaited_once()
    # Not handled, so the next poll retries: the row stays at the started action, which is not the marker.
    assert [(verb, params[2] if verb == "INSERT" else params[0]) for verb, params in _audit(pool)] == [
        ("INSERT", STARTED_ACTION),
        ("UPDATE", AUDIT_ACTION),
    ]
    assert "pg_advisory_unlock" in _sql(pool)[-1]


@pytest.mark.asyncio
async def test_a_failed_finalize_after_a_failed_job_reraises_the_job_error(jobs: MagicMock) -> None:
    jobs.reattach.side_effect = RuntimeError("boom")
    pool = FakePool([*_UP_TO_ACTOR, [], [(True,)]], raise_on={"UPDATE admin_audit_log": RuntimeError("audit table unavailable")})
    with pytest.raises(RuntimeError, match="boom"):
        await run_pending(pool, object())
    assert "pg_advisory_unlock" in _sql(pool)[-1]


def test_neither_the_started_nor_the_failed_action_is_the_marker() -> None:
    assert len({AUDIT_ACTION, STARTED_ACTION, FAILED_ACTION}) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "actor", [[], [(SYSTEM_ACTOR_EMAIL, True, False)], [(SYSTEM_ACTOR_EMAIL, False, True)], [("someone@example.com", False, False)]]
)
async def test_a_users_row_that_is_not_the_system_actor_is_refused(jobs: MagicMock, actor: list[tuple[Any, ...]]) -> None:
    pool = FakePool([*_PENDING, [(True,)], *_PENDING, [], actor, [(True,)]])
    with pytest.raises(RuntimeError, match="system actor"):
        await run_pending(pool, object())
    jobs.reattach.assert_not_called()
    assert "pg_advisory_unlock" in _sql(pool)[-1]


@pytest.mark.asyncio
async def test_loop_logs_a_failure_and_polls_again() -> None:
    run = AsyncMock(side_effect=[RuntimeError("down"), "20260901"])
    sleep = AsyncMock(side_effect=[None, asyncio.CancelledError()])
    with (
        patch.object(reattach_trigger, "run_pending", run),
        patch.object(reattach_trigger.asyncio, "sleep", sleep),
        pytest.raises(asyncio.CancelledError),
    ):
        await run_trigger_loop("pool", "driver", 42)
    assert run.await_count == 2
    sleep.assert_awaited_with(42)


@pytest.mark.asyncio
async def test_loop_reraises_cancellation_from_a_run() -> None:
    run = AsyncMock(side_effect=asyncio.CancelledError())
    with patch.object(reattach_trigger, "run_pending", run), pytest.raises(asyncio.CancelledError):
        await run_trigger_loop("pool", "driver", 1)


def _config(**overrides: str) -> ApiConfig:
    with patch.dict(os.environ, {**REQUIRED_ENV, **overrides}, clear=True):
        return ApiConfig.from_env()


def test_off_by_default() -> None:
    config = _config()
    assert config.identity_auto_reattach_enabled is False
    assert config.identity_auto_reattach_interval == 300


@pytest.mark.parametrize(("value", "expected"), [("true", True), ("1", True), ("ON", True), ("false", False), ("0", False), ("", False)])
def test_enabled_flag(value: str, expected: bool) -> None:
    assert _config(IDENTITY_AUTO_REATTACH_ENABLED=value).identity_auto_reattach_enabled is expected


@pytest.mark.parametrize(("value", "expected"), [("60", 60), ("invalid", 300), ("0", 1)])
def test_interval(value: str, expected: int) -> None:
    assert _config(IDENTITY_AUTO_REATTACH_INTERVAL=value).identity_auto_reattach_interval == expected


@pytest.mark.asyncio
async def test_lifespan_does_not_start_the_watcher_when_disabled() -> None:
    app = SimpleNamespace(state=SimpleNamespace())
    _start_identity_maintenance(app, _config(), "pool", "neo4j")  # type: ignore[arg-type]
    assert app.state.identity_maintenance_task is None


@pytest.mark.asyncio
async def test_lifespan_does_not_start_the_watcher_without_neo4j() -> None:
    app = SimpleNamespace(state=SimpleNamespace())
    _start_identity_maintenance(app, _config(IDENTITY_AUTO_REATTACH_ENABLED="true"), "pool", None)  # type: ignore[arg-type]
    assert app.state.identity_maintenance_task is None


@pytest.mark.asyncio
async def test_lifespan_starts_the_watcher_when_enabled() -> None:
    app = SimpleNamespace(state=SimpleNamespace())
    loop = AsyncMock()
    with patch("api.api.run_trigger_loop", loop):
        _start_identity_maintenance(app, _config(IDENTITY_AUTO_REATTACH_ENABLED="true", IDENTITY_AUTO_REATTACH_INTERVAL="30"), "pool", "neo4j")  # type: ignore[arg-type]
        await app.state.identity_maintenance_task
    loop.assert_awaited_once_with("pool", "neo4j", 30)
