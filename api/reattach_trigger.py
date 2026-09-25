"""Automatic post-import identity maintenance, triggered by a completed Discogs extraction.

The deployment runbook's "Post-import identity maintenance" asks an operator to run the
catalog re-attachment (`api/reattach.py`, ADR 0014 section 8) and then the `gm_id` projection
(`api/projection.py`, ADR 0009) after every Discogs import. This module does the same thing on
its own: a background task, started in the app lifespan beside the metrics collector, polls
for a completed Discogs extraction it has not yet handled and runs
``run_reattachment(apply=True)`` followed by ``run_gm_id_projection``. It only calls those two
functions; their internals are theirs.

It is off by default. ``IDENTITY_AUTO_REATTACH_ENABLED=true`` turns it on and
``IDENTITY_AUTO_REATTACH_INTERVAL`` (seconds, default 300) sets the poll.

**What "complete" means.** `discogs-sql-loader` records every `extraction_complete` it
processes in ``public.loader_extraction_latch`` (declared by `database-schema`), one row per
``(loader, version)``, and adds the signalling data type to that row's ``signals`` array
before it acks the delivery (`tableinator/extraction_latch.py`, `tableinator/durable_refresh.py`).
An extraction is complete once its ``loader = 'discogs'`` row carries all four data types —
the same test the loader uses to schedule its own derived-relation refresh. This module only
reads that table; it writes nothing to it and needs no loader change (ADR 0005).

Only the *newest* complete extraction (by the row's ``created_at``) is a candidate. Both jobs
work over the whole catalog, so one run after the latest import also covers any earlier one,
and a straggler signal completing an older, superseded extraction does not trigger a run.
Enabling the task on a deployment that has already imported therefore runs once, for the
latest extraction.

**Where "handled" lives.** An automatic run that finishes records one ``admin_audit_log`` row
with ``action = 'identity.reattach.auto'``, ``target = 'discogs:<version>'`` and the system
actor below as ``admin_id``. That row is both the audit record and the durable handled marker,
so the two cannot disagree and no new table is needed. Its row id is the run's job id, which
every supersession the run opens records as its ``decision_ref`` (ADR 0009's native-id merge);
a run that fails is recorded under the same id as ``identity.reattach.auto.failed``, which is
not the marker. The alternatives do not fit:
``loader_extraction_latch`` belongs to the loader, ``extraction_history`` is keyed on an
admin-*triggered* run, and ``app_config`` holds the encrypted Discogs credentials.

**Exactly once.** Every replica polls, and each takes the session-level advisory lock
``pg_try_advisory_lock(hashtext('groovemap:identity-auto-reattach:discogs'))`` before doing
anything. A replica that cannot take it skips this poll. The holder then re-checks the marker
*under the lock* — so a replica that saw "not handled" just before another one finished cannot
run it again — runs both jobs, writes the marker on the lock's own connection, and unlocks.
A restart is covered by the marker being in PostgreSQL. A failure anywhere (including a
process that dies mid-run, which drops its session and so its lock) writes no marker, so the
next poll by any replica retries it; both jobs are safe to re-run, which is what makes that
retry harmless. "Exactly once" is therefore exactly one *completed* run per extraction.

**The system actor.** `admin_audit_log.admin_id` must reference a ``users`` row. The CLI makes
``--admin-id`` mandatory and the endpoint uses the caller's JWT because there a person decides
to write, and the audit row says who. Here nobody does: the decision is the operator's
configuration and the trigger is the loader's latch, so attributing the run to a human admin
would record something that did not happen. Automatic runs are instead recorded against a
reserved system user, provisioned on first use with a fixed id (:data:`SYSTEM_ACTOR_ID`), an
``.invalid`` email, ``is_active = false``, ``is_admin = false``, and a password hash that
matches no password. It can neither log in nor pass ``require_admin``; it exists only so the
audit row has an actor, and the audit viewer shows its email.
"""

from __future__ import annotations

import asyncio
from typing import Any, Final
from uuid import UUID, uuid4

import structlog
from psycopg.types.json import Jsonb

from api.audit_log import record_audit_entry
from api.projection import run_gm_id_projection
from api.reattach import audit_details, failure_details, run_reattachment


logger = structlog.get_logger(__name__)

AUDIT_ACTION: Final = "identity.reattach.auto"
# A run that ended on an error. It is not the handled marker, so the next poll still retries.
FAILED_ACTION: Final = "identity.reattach.auto.failed"

# The four data types whose `extraction_complete` a Discogs extraction collects.
DISCOGS_DATA_TYPES: Final[tuple[str, ...]] = ("artists", "labels", "masters", "releases")

SYSTEM_ACTOR_ID: Final = UUID("6d3c1f0a-5e1b-4c7a-9a4f-1d0e1d3a7a11")
SYSTEM_ACTOR_EMAIL: Final = "identity-maintenance@system.groovemap.invalid"
# `_verify_password` splits on ":" and fails on anything else, so no password matches this.
_UNUSABLE_PASSWORD_HASH: Final = "!system-actor-no-login"  # noqa: S105 - deliberately not a hash

_LOCK_KEY: Final = "groovemap:identity-auto-reattach:discogs"

_LATCH_PRESENT: Final = "SELECT to_regclass('public.loader_extraction_latch') IS NOT NULL"

_LATEST_COMPLETE: Final = """
    SELECT version
    FROM public.loader_extraction_latch
    WHERE loader = 'discogs' AND signals @> %s::text[]
    ORDER BY created_at DESC, version DESC
    LIMIT 1
"""

_HANDLED: Final = "SELECT EXISTS (SELECT 1 FROM admin_audit_log WHERE admin_id = %s::uuid AND action = %s AND target = %s)"

_TRY_LOCK: Final = "SELECT pg_try_advisory_lock(hashtext(%s))"
_UNLOCK: Final = "SELECT pg_advisory_unlock(hashtext(%s))"

_ENSURE_ACTOR: Final = """
    INSERT INTO users (id, email, hashed_password, is_active, is_admin)
    VALUES (%s::uuid, %s, %s, false, false)
    ON CONFLICT (id) DO NOTHING
"""
_SELECT_ACTOR: Final = "SELECT email, is_active, is_admin FROM users WHERE id = %s::uuid"

_RECORD: Final = "INSERT INTO admin_audit_log (id, admin_id, action, target, details) VALUES (%s::uuid, %s::uuid, %s, %s, %s)"


def extraction_target(version: str) -> str:
    """The `admin_audit_log.target` that marks one Discogs extraction as handled."""
    return f"discogs:{version}"


async def _scalar(cur: Any, sql: str, params: Any = None) -> Any:
    await cur.execute(sql, params)
    row = await cur.fetchone()
    return row[0] if row else None


async def _ensure_system_actor(cur: Any) -> None:
    """Provision the reserved system user if absent, and refuse a row that is not it."""
    await cur.execute(_ENSURE_ACTOR, (str(SYSTEM_ACTOR_ID), SYSTEM_ACTOR_EMAIL, _UNUSABLE_PASSWORD_HASH))
    await cur.execute(_SELECT_ACTOR, (str(SYSTEM_ACTOR_ID),))
    row = await cur.fetchone()
    if row is None or tuple(row) != (SYSTEM_ACTOR_EMAIL, False, False):
        raise RuntimeError(f"users row {SYSTEM_ACTOR_ID} is not the inactive, non-admin identity-maintenance system actor")


async def pending_extraction(cur: Any) -> str | None:
    """The newest complete Discogs extraction, when it has not yet been handled."""
    if not await _scalar(cur, _LATCH_PRESENT):
        logger.debug("⏳ No loader_extraction_latch relation yet; nothing to watch")
        return None
    version = await _scalar(cur, _LATEST_COMPLETE, (list(DISCOGS_DATA_TYPES),))
    if version is None:
        return None
    if await _scalar(cur, _HANDLED, (str(SYSTEM_ACTOR_ID), AUDIT_ACTION, extraction_target(version))):
        return None
    return str(version)


async def run_pending(pool: Any, driver: Any) -> str | None:
    """Handle the newest complete Discogs extraction if no replica has yet.

    Returns:
        The extraction version this call handled, or None when there was nothing to do,
        another replica holds the lock, or it was already handled.

    Raises:
        Exception: Whatever either job or PostgreSQL raised. No marker has been written, so
            the next poll retries.
    """
    async with pool.connection() as conn, conn.cursor() as cur:
        if await pending_extraction(cur) is None:
            return None
        if not await _scalar(cur, _TRY_LOCK, (_LOCK_KEY,)):
            logger.debug("⏳ Another replica holds the identity-maintenance lock")
            return None
        try:
            # Re-read under the lock: the pre-check above can predate another replica's marker.
            version = await pending_extraction(cur)
            if version is None:
                return None
            await _ensure_system_actor(cur)
            job_id = str(uuid4())
            logger.info("🚀 Automatic identity maintenance started", extraction=version, job_id=job_id)
            try:
                report = await run_reattachment(pool, apply=True, decision_ref=UUID(job_id))
                projection = await run_gm_id_projection(pool, driver)
            except Exception as exc:
                # On its own connection: this one's transaction is about to roll back.
                await record_audit_entry(
                    pool=pool,
                    admin_id=str(SYSTEM_ACTOR_ID),
                    action=FAILED_ACTION,
                    target=extraction_target(version),
                    details={**failure_details(job_id, exc), "extraction": version},
                    entry_id=job_id,
                )
                raise
            details = {**audit_details(report, job_id), "extraction": version, "projection": projection}
            await cur.execute(_RECORD, (job_id, str(SYSTEM_ACTOR_ID), AUDIT_ACTION, extraction_target(version), Jsonb(details)))
            logger.info(
                "✅ Automatic identity maintenance finished",
                extraction=version,
                job_id=job_id,
                outcomes=report.get("outcomes"),
                projection=projection,
            )
            return version
        finally:
            await cur.execute(_UNLOCK, (_LOCK_KEY,))


async def run_trigger_loop(pool: Any, driver: Any, interval: int) -> None:
    """Poll every *interval* seconds; a failure is logged and retried next poll. Re-raises ``CancelledError``."""
    while True:
        try:
            await run_pending(pool, driver)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error("❌ Automatic identity maintenance failed; retrying next poll", exc_info=True)
        await asyncio.sleep(interval)
