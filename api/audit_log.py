"""Admin audit log — records admin actions to the admin_audit_log table."""

from __future__ import annotations

import json
from typing import Any

import structlog
from psycopg.types.json import Jsonb


logger = structlog.get_logger(__name__)


async def record_audit_entry(
    *,
    pool: Any,
    admin_id: str,
    action: str,
    target: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    """Write an audit log entry. Never raises — failures are logged as warnings.

    Best effort, so not for an entry anything else names: see :func:`insert_audit_entry`.
    """
    if pool is None:
        return
    try:
        details_json = json.dumps(details) if details else None
        async with pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO admin_audit_log (admin_id, action, target, details) VALUES (%s::uuid, %s, %s, %s::jsonb)",
                (admin_id, action, target, details_json),
            )
        logger.debug("📋 Audit entry recorded", action=action, admin_id=admin_id)
    except Exception:
        logger.warning("⚠️ Failed to record audit entry", action=action, admin_id=admin_id, exc_info=True)


_INSERT_ENTRY = "INSERT INTO admin_audit_log (id, admin_id, action, target, details) VALUES (%s::uuid, %s::uuid, %s, %s, %s)"
_UPDATE_ENTRY = "UPDATE admin_audit_log SET action = %s, details = %s WHERE id = %s::uuid"


async def insert_audit_entry(cur: Any, *, entry_id: str, admin_id: str, action: str, target: str | None, details: dict[str, Any]) -> None:
    """Write an audit log entry under a fixed id, raising on failure.

    For a caller that must not go on without the entry: a re-attachment run writes its entry
    before any identity write, because every supersession it opens names the entry as its
    ``decision_ref``. On the pool's autocommit connections the row is durable once this returns.
    """
    await cur.execute(_INSERT_ENTRY, (entry_id, admin_id, action, target, Jsonb(details)))


async def update_audit_entry(cur: Any, *, entry_id: str, action: str, details: dict[str, Any]) -> None:
    """Replace the action and details of an entry :func:`insert_audit_entry` wrote, raising on failure."""
    await cur.execute(_UPDATE_ENTRY, (action, Jsonb(details), entry_id))
