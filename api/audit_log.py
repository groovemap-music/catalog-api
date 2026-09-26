"""Admin audit log — records admin actions to the admin_audit_log table."""

from __future__ import annotations

import json
from typing import Any

import structlog


logger = structlog.get_logger(__name__)


async def record_audit_entry(
    *,
    pool: Any,
    admin_id: str,
    action: str,
    target: str | None = None,
    details: dict[str, Any] | None = None,
    entry_id: str | None = None,
) -> None:
    """Write an audit log entry. Never raises — failures are logged as warnings.

    ``entry_id`` fixes the row's id instead of letting the database pick one, for a caller that
    has already recorded that id elsewhere (a re-attachment run's supersessions name its entry).
    """
    if pool is None:
        return
    try:
        details_json = json.dumps(details) if details else None
        async with pool.connection() as conn, conn.cursor() as cur:
            if entry_id is None:
                await cur.execute(
                    "INSERT INTO admin_audit_log (admin_id, action, target, details) VALUES (%s::uuid, %s, %s, %s::jsonb)",
                    (admin_id, action, target, details_json),
                )
            else:
                await cur.execute(
                    "INSERT INTO admin_audit_log (id, admin_id, action, target, details) VALUES (%s::uuid, %s::uuid, %s, %s, %s::jsonb)",
                    (entry_id, admin_id, action, target, details_json),
                )
        logger.debug("📋 Audit entry recorded", action=action, admin_id=admin_id)
    except Exception:
        logger.warning("⚠️ Failed to record audit entry", action=action, admin_id=admin_id, exc_info=True)
