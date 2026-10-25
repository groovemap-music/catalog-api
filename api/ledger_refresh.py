"""Keep the precomputed Identity Ledger fresh: inline after identity runs, and by change detection.

The works list and density read four materialized views that `database-schema` declares and
``public.identity_ledger_refresh(source_watermark jsonb)`` refreshes (epic
``gm-database-schema-k8e9``; `api/queries/identity_ledger_queries.py`). That repository never
calls the function, so catalog-api owns every trigger, and no loader changes (epic
``gm-catalog-api-6bxr``, "What triggers a refresh"):

1. **Inline, after every applying re-attachment run**: the CLI and the admin endpoint
   (`api/reattach.py`, ``apply_with_audit``) and the automatic trigger
   (`api/reattach_trigger.py`, ``run_pending``). The run's supersessions are in the works list
   when the run reports done.
2. **Inline, after every applying ledger backfill run** (`api/ledger_backfill.py`).
3. **After loads, by a background poll** (:func:`run_refresh_loop`), off by default. Every
   ``IDENTITY_LEDGER_REFRESH_INTERVAL`` seconds it sums ``n_tup_ins + n_tup_upd + n_tup_del``
   from ``pg_stat_user_tables`` over the ledger's sources (:data:`SOURCE_TABLES`). That sum
   is the *source watermark*, and each refresh stores the one it read in
   ``identity_ledger_refresh_state.source_watermark`` as ``{"writes": <sum>}``. The poll
   refreshes when the sum differs from the stored one and either it has not moved for
   ``IDENTITY_LEDGER_REFRESH_QUIET`` seconds (the load has finished) or the last refresh is
   older than ``IDENTITY_LEDGER_MAX_STALENESS`` (a long load is still writing). It also
   refreshes, at once, any view that is unpopulated: before the first refresh, or after a
   definition change recreated it.

Why not the loaders' ``extraction_complete``: only `discogs-sql-loader` writes
``loader_extraction_latch``, catalog-api consumes neither loader's AMQP signals, and the
backfill and manual repairs write the sources with no extraction at all. Change detection on
the sources covers every writer. Cumulative statistics reset on a crash or ``pg_stat_reset``,
and a write is counted a moment after it commits. Either way the sum then differs from the
stored one and the result is one extra refresh, the safe direction. ``TRUNCATE`` moves no
counter, but no ledger source is truncated outside tests.

**One refresh at a time.** Every refresh holds the transaction-level advisory lock
``hashtext('groovemap:identity-ledger-refresh')``, released when its transaction ends, so a
failed refresh cannot leak it. An inline refresh waits for the lock, because its run must be
in the views when it reports done. A poll only tries it: a replica that cannot take it skips
this poll, and the holder re-reads the stored watermark under the lock, so a refresh another
replica has just committed is not repeated.

**Never fails a run.** A refresh that fails is logged at error level under
``alert = 'identity_ledger_refresh_failed'`` and counted by
``groovemap.api.identity_ledger.refreshes`` with ``outcome = failed``. The run it follows has
already written and recorded its audit entry, and it still reports its own result. The views
keep the previous refresh's rows, and ``refreshed_at`` says how old they are.

**How stale the ledger can be.** After the last write of a load, at most the quiet interval
plus one poll interval plus the refresh. While a load is still writing, at most the maximum
staleness plus one poll interval plus the refresh. After a re-attachment or backfill run,
nothing beyond the refresh itself.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

import structlog

from api import telemetry


logger = structlog.get_logger(__name__)

# What started a refresh: the closed vocabulary of the counter's `trigger` attribute.
TRIGGER_REATTACH: Final = "reattach"
TRIGGER_BACKFILL: Final = "backfill"
TRIGGER_POLL: Final = "poll"

# Why the poll refreshed.
REASON_UNPOPULATED: Final = "unpopulated"
REASON_QUIET: Final = "quiet"
REASON_STALE: Final = "stale"

ALERT_REFRESH_FAILED: Final = "identity_ledger_refresh_failed"

# Every relation the four views read (database-schema's architecture notes, "The precomputed
# Identity Ledger"). A write to any of them can change what the ledger shows.
SOURCE_TABLES: Final[tuple[tuple[str, str], ...]] = (
    ("public", "provider_aliases"),
    ("public", "catalog_items"),
    ("public", "catalog_item_supersessions"),
    ("public", "identity_link_events"),
    ("graph", "derived_from"),
    ("musicbrainz", "releases"),
)

VIEWS: Final[tuple[str, ...]] = (
    "identity_ledger_edition_events",
    "identity_ledger_edition_parents",
    "identity_ledger_work_summary",
    "identity_ledger_density",
)

_LOCK_KEY: Final = "groovemap:identity-ledger-refresh"
_LOCK: Final = "SELECT pg_advisory_xact_lock(hashtext(%s))"
_TRY_LOCK: Final = "SELECT pg_try_advisory_xact_lock(hashtext(%s))"

_WATERMARK: Final = f"""
SELECT COALESCE(sum(n_tup_ins + n_tup_upd + n_tup_del), 0)::bigint
FROM pg_stat_user_tables
WHERE (schemaname, relname) IN ({", ".join(f"('{schema}', '{table}')" for schema, table in SOURCE_TABLES)})
"""  # noqa: S608 — SOURCE_TABLES is a module constant, not caller input

# Whether every view is populated, the watermark the last refresh stored (NULL when there was
# none, or it is not one this module wrote), and that refresh's age in seconds by the server's
# clock (NULL before the first refresh).
_STORED: Final = """
SELECT (SELECT count(*) FILTER (WHERE relispopulated) = cardinality(%(views)s::text[])
        FROM pg_class
        WHERE relnamespace = 'public'::regnamespace AND relkind = 'm' AND relname = ANY(%(views)s::text[])),
       (SELECT CASE WHEN jsonb_typeof(source_watermark -> 'writes') = 'number' THEN (source_watermark ->> 'writes')::bigint END
        FROM identity_ledger_refresh_state),
       (SELECT extract(epoch FROM now() - refreshed_at)::float8 FROM identity_ledger_refresh_state)
"""

_REFRESH: Final = "SELECT public.identity_ledger_refresh(%s::jsonb)"


@dataclass(frozen=True, slots=True)
class LedgerState:
    """What the poll decides on."""

    populated: bool
    stored: int | None
    age_seconds: float | None
    current: int


async def _scalar(cur: Any, sql: str, params: Any = None) -> Any:
    await cur.execute(sql, params)
    row = await cur.fetchone()
    return row[0] if row else None


async def _read_state(cur: Any, current: int) -> LedgerState:
    await cur.execute(_STORED, {"views": list(VIEWS)})
    row = await cur.fetchone()
    populated, stored, age = row if row else (False, None, None)
    return LedgerState(populated=bool(populated), stored=stored, age_seconds=age, current=current)


async def _refresh(cur: Any, watermark: int) -> datetime:
    refreshed_at: datetime = await _scalar(cur, _REFRESH, (json.dumps({"writes": watermark}),))
    return refreshed_at


def _record(trigger: str, outcome: str) -> None:
    telemetry.record_identity_ledger_refresh(trigger, outcome)


def _failed(trigger: str, **context: Any) -> None:
    _record(trigger, telemetry.LEDGER_REFRESH_FAILED)
    logger.error(
        "❌ Identity ledger refresh failed; the views keep their previous rows", alert=ALERT_REFRESH_FAILED, trigger=trigger, exc_info=True, **context
    )


async def refresh_ledger(pool: Any, *, trigger: str, **context: Any) -> datetime | None:
    """Refresh the views now, after any refresh another replica is running. Never raises.

    Returns:
        The new ``refreshed_at``, or None when the refresh failed (logged and counted).
    """
    started = time.perf_counter()
    try:
        async with pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            await cur.execute(_LOCK, (_LOCK_KEY,))
            watermark = int(await _scalar(cur, _WATERMARK))
            refreshed_at = await _refresh(cur, watermark)
    except Exception:
        _failed(trigger, **context)
        return None
    _record(trigger, telemetry.LEDGER_REFRESH_SUCCESS)
    logger.info("🔄 Identity ledger refreshed", trigger=trigger, seconds=round(time.perf_counter() - started, 1), **context)
    return refreshed_at


@dataclass(slots=True)
class ChangeTracker:
    """When this replica last saw the source watermark move (monotonic seconds).

    Starts as "just moved": a replica that has not watched the sources for a quiet interval
    cannot know a load has finished.
    """

    clock: Callable[[], float] = time.monotonic
    seen: int | None = None
    moved_at: float = 0.0

    def observe(self, current: int) -> float:
        """Record the current watermark; return how long it has held still."""
        now = self.clock()
        if current != self.seen:
            self.seen = current
            self.moved_at = now
        return now - self.moved_at


def refresh_reason(state: LedgerState, *, still_for: float, quiet: float, max_staleness: float) -> str | None:
    """Why the poll should refresh now, or None when it should not.

    An unpopulated view is refreshed at once. Otherwise the sources must have changed since
    the stored watermark, and then either held still for ``quiet`` seconds or the last refresh
    be older than ``max_staleness``.
    """
    if not state.populated:
        return REASON_UNPOPULATED
    if state.stored == state.current:
        return None
    if still_for >= quiet:
        return REASON_QUIET
    if state.age_seconds is None or state.age_seconds >= max_staleness:
        return REASON_STALE
    return None


async def poll_once(pool: Any, tracker: ChangeTracker, *, quiet: float, max_staleness: float) -> str | None:
    """Refresh if the sources call for it and no other replica is refreshing.

    Returns:
        The reason it refreshed, or None when it did not: nothing changed, a load is still
        writing, another replica holds the lock, or the refresh failed (logged and counted).
    """
    try:
        async with pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            current = int(await _scalar(cur, _WATERMARK))
            still_for = tracker.observe(current)
            reason = refresh_reason(await _read_state(cur, current), still_for=still_for, quiet=quiet, max_staleness=max_staleness)
            if reason is None:
                return None
            if not await _scalar(cur, _TRY_LOCK, (_LOCK_KEY,)):
                logger.debug("⏳ Another replica is refreshing the identity ledger")
                return None
            # Under the lock: a refresh another replica committed since the read above stored
            # this watermark, and repeating it would only cost minutes of I/O.
            reason = refresh_reason(await _read_state(cur, current), still_for=still_for, quiet=quiet, max_staleness=max_staleness)
            if reason is None:
                return None
            started = time.perf_counter()
            await _refresh(cur, current)
    except Exception:
        _failed(TRIGGER_POLL)
        return None
    _record(TRIGGER_POLL, telemetry.LEDGER_REFRESH_SUCCESS)
    logger.info("🔄 Identity ledger refreshed", trigger=TRIGGER_POLL, reason=reason, seconds=round(time.perf_counter() - started, 1))
    return reason


async def run_refresh_loop(pool: Any, *, interval: int, quiet: int, max_staleness: int) -> None:
    """Poll every *interval* seconds until cancelled. Re-raises ``CancelledError``."""
    tracker = ChangeTracker()
    while True:
        await poll_once(pool, tracker, quiet=quiet, max_staleness=max_staleness)
        await asyncio.sleep(interval)
