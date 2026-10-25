"""Engine-backed proof of the full-export Identity Ledger backfill (api/ledger_backfill.py).

Runs the `catalog-identity-ledger-backfill` command end to end against the real schema the
`postgres_pool` fixture applies (`public.identity_link_events`, its CHECKs and idempotence key,
and the `users` foreign key on `admin_audit_log`), building its own pool from the same
environment. The export is the invented one in `tests/synthetic_fullexport.py`, served by
`httpx.MockTransport`: no network is used. Nothing here touches a shared database:
`just test-integration` starts throwaway containers.
"""

from __future__ import annotations

import asyncio
import hashlib
import sys
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from common import AsyncPostgreSQLPool

import api.ledger_backfill as backfill
from api.ledger_backfill import APPLY_ACTION, FAILED_ACTION, write_events
from api.reattach_trigger import SYSTEM_ACTOR_ID
from tests.synthetic_fullexport import BASE_URL, EXPORT, GID, TINY_MEMBERS, dump, sha256sums, transport
from tests.test_real_databases import postgres_pool
from tests.test_reattach_integration import _execute


__all__ = ["postgres_pool"]

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_EVENTS = (
    "SELECT provider, entity_kind, external_id, event, discogs_id_before, discogs_id_after, event_at, dated_from, source_ref, decision_ref "
    "FROM identity_link_events ORDER BY external_id"
)
_AUDIT = "SELECT id, admin_id, action, target, details FROM admin_audit_log ORDER BY created_at"


@pytest_asyncio.fixture
async def pool(postgres_pool: AsyncPostgreSQLPool) -> AsyncPostgreSQLPool:
    # `users` is truncated with CASCADE by the fixture, which empties admin_audit_log too.
    await _execute(postgres_pool, "TRUNCATE identity_link_events")
    return postgres_pool


@pytest.fixture
def served(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Serve the tiny export to the command and record the URLs it asks for."""
    state: dict[str, Any] = {"data": dump(TINY_MEMBERS), "sums": None, "seen": []}

    def client() -> httpx.Client:
        return httpx.Client(transport=transport(state["data"], sums=state["sums"], seen=state["seen"]))

    monkeypatch.setattr(backfill, "_http_client", client)
    return state


async def _run(monkeypatch: pytest.MonkeyPatch, *argv: str) -> None:
    """The CLI, on a worker thread: it runs its own event loop and its own pool."""
    monkeypatch.setattr(sys, "argv", ["catalog-identity-ledger-backfill", "--base-url", BASE_URL, *argv])
    await asyncio.to_thread(backfill.main)


def _at(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


async def test_apply_writes_labelled_events_under_one_audit_entry_and_a_rerun_adds_nothing(
    pool: AsyncPostgreSQLPool, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], served: dict[str, Any]
) -> None:
    [(started,)] = await _execute(pool, "SELECT clock_timestamp()")
    await _run(monkeypatch, "--apply", "--batch-size", "2")
    assert "Done. 3 events inserted, 0 already present" in capsys.readouterr().out
    # The precomputed Identity Ledger was refreshed inline, after the run began.
    [(refreshed_at,)] = await _execute(pool, "SELECT refreshed_at FROM identity_ledger_refresh_state")
    assert refreshed_at > started

    [(entry_id, admin_id, action, target, details)] = await _execute(pool, _AUDIT)
    assert (admin_id, action, target) == (SYSTEM_ACTOR_ID, APPLY_ACTION, f"musicbrainz-fullexport:{EXPORT}")
    assert details["inserted"] == 3
    assert details["dated_from"] == "musicbrainz_fullexport"
    assert details["undated"] == 1
    events = await _execute(pool, _EVENTS)
    common = ("musicbrainz", "release")
    assert events == [
        (*common, GID.format(7), "link_added", None, 900001, _at("2026-03-10 12:00:00"), "musicbrainz_fullexport", EXPORT, entry_id),
        (*common, GID.format(9), "link_added", None, 900004, _at("2026-04-01 00:00:00"), "musicbrainz_fullexport", EXPORT, entry_id),
        (*common, GID.format(11), "link_added", None, 900006, _at("2026-05-01 00:00:00"), "musicbrainz_fullexport", EXPORT, entry_id),
    ]

    # A second --apply of the same export finds the marker, downloads nothing, and adds no row.
    served["seen"].clear()
    await _run(monkeypatch, "--apply")
    assert "already backfilled; nothing was written" in capsys.readouterr().out
    assert served["seen"] == [f"{BASE_URL}/LATEST"]
    assert await _execute(pool, _EVENTS) == events
    assert len(await _execute(pool, _AUDIT)) == 1

    # Even past the marker, the idempotence key makes rewriting the same export a no-op.
    data = served["data"]
    plan = backfill.read_export([data], hashlib.sha256(data).hexdigest())
    assert await write_events(pool, plan, source_ref=EXPORT, decision_ref=uuid4()) == 0
    assert await _execute(pool, _EVENTS) == events


@pytest.mark.usefixtures("served")
async def test_a_run_after_a_failed_one_keeps_the_rows_it_wrote(
    pool: AsyncPostgreSQLPool, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    await _run(monkeypatch, "--apply")
    [(first_entry, *_)] = await _execute(pool, _AUDIT)
    # As if it had failed: its entry is not the handled marker.
    await _execute(pool, "UPDATE admin_audit_log SET action = %s WHERE id = %s", (FAILED_ACTION, first_entry))
    capsys.readouterr()

    await _run(monkeypatch, "--apply")
    assert "Done. 0 events inserted, 3 already present" in capsys.readouterr().out
    entries = await _execute(pool, _AUDIT)
    assert [action for _, _, action, _, _ in entries] == [FAILED_ACTION, APPLY_ACTION]
    assert {decision for *_, decision in await _execute(pool, _EVENTS)} == {first_entry}


async def test_the_dry_run_writes_nothing(
    pool: AsyncPostgreSQLPool, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], served: dict[str, Any]
) -> None:
    await _run(monkeypatch)
    out = capsys.readouterr().out
    assert "3 link_added events dated" in out
    assert "Dry run only; nothing was written" in out
    assert served["seen"][-1].endswith("/mbdump.tar.bz2")
    assert await _execute(pool, _EVENTS) == []
    assert await _execute(pool, _AUDIT) == []


async def test_a_checksum_mismatch_aborts_before_any_write(
    pool: AsyncPostgreSQLPool, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], served: dict[str, Any]
) -> None:
    served["sums"] = sha256sums(b"another file")
    with pytest.raises(SystemExit) as exited:
        await _run(monkeypatch, "--apply")
    assert exited.value.code == 1
    assert "does not match SHA256SUMS" in capsys.readouterr().err
    assert await _execute(pool, _EVENTS) == []
    assert await _execute(pool, _AUDIT) == []
    assert await _execute(pool, "SELECT count(*) FROM users WHERE id = %s", (SYSTEM_ACTOR_ID,)) == [(0,)]


async def test_every_event_satisfies_the_tables_checks(pool: AsyncPostgreSQLPool, served: dict[str, Any]) -> None:
    data = served["data"]
    plan = backfill.read_export([data], hashlib.sha256(data).hexdigest())
    decision = UUID(int=1)
    assert await write_events(pool, plan, source_ref="it-direct", decision_ref=decision, batch_size=1) == 3
    rows = await _execute(
        pool, "SELECT count(*), count(DISTINCT external_id), min(recorded_at) <= now() FROM identity_link_events WHERE source_ref = 'it-direct'"
    )
    assert rows == [(3, 3, True)]
