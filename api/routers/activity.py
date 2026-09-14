"""The endpoints ADR 0010 puts in front of the activity record.

One client-facing outcome endpoint, the two consent endpoints, and the erasure and export
endpoints all live here because they are one decision's surface: what is recorded, what
the user permitted, and how the user gets it back or has it removed.

Every write here goes through :mod:`api.activity`, which never raises into a request, so a
degraded behavioural record never costs a caller their response.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

import structlog
from common.events import consent_purposes
from common.query_debug import execute_sql
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from psycopg.rows import dict_row

import api.activity as activity
from api.dependencies import require_user
from api.models import ActivityOutcomeRequest, ConsentUpdateRequest


logger = structlog.get_logger(__name__)

router = APIRouter()

_pool: Any = None
_redis: Any = None
_neo4j_driver: Any = None


def configure(pool: Any, redis: Any = None, neo4j: Any = None) -> None:
    """Wire the three stores the activity endpoints reach, from ``api.api`` startup."""
    global _pool, _redis, _neo4j_driver
    _pool = pool
    _redis = redis
    _neo4j_driver = neo4j


def _caller_id(current_user: dict[str, Any]) -> str:
    user_id: str = current_user.get("sub", "")
    return user_id


@router.post("/api/activity/events", status_code=status.HTTP_202_ACCEPTED)
async def record_outcome(
    body: ActivityOutcomeRequest,
    current_user: Annotated[dict[str, Any], Depends(require_user)],
) -> JSONResponse:
    """Record one client-reported outcome against a recommendation that was shown.

    ADR 0010 keeps outcomes out of the impression row: opened, saved, dismissed, and
    hidden are events carrying the ``impression_id``, which is what lets one impression
    accrue several outcomes over time while the row itself stays immutable.

    The body carries ``item_id`` as well as ``impression_id`` because the published
    ``impression_outcome`` payload requires both and is closed over them. Reading the item
    back from ``activity.impressions`` instead would mean scanning a table partitioned by
    occurrence time for a key that does not carry the partition column.

    Any type outside the four outcomes is rejected by the request model as a 422, so a
    client can never reach the recorder with a type from elsewhere in the vocabulary.

    Returns 202: the row is written before the response, but the caller is being told the
    outcome was accepted, not that an analysis has seen it.
    """
    user_id = _caller_id(current_user)
    impression_id = str(body.impression_id)

    await activity.record_event(
        user_id,
        body.event_type,
        {"impression_id": impression_id, "item_id": str(body.item_id)},
        # One outcome of one kind against one impression is the same fact however many
        # times a client reports it, so the natural key makes a retry a no-op.
        idempotency_key=f"{body.event_type}:{impression_id}",
    )

    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={"recorded": True, "event_type": body.event_type, "impression_id": impression_id},
    )


# ---------------------------------------------------------------------------
# Consent (ADR 0010)
# ---------------------------------------------------------------------------

EVENT_CONSENT_GRANTED = "consent.granted"
EVENT_CONSENT_REVOKED = "consent.revoked"

# The latest state of each purpose. A revocation sets `revoked_at` rather than deleting
# the grant, so the same purpose accumulates rows over time and the newest one is the
# current answer. DISTINCT ON is what picks it without a self-join.
_SELECT_CONSENT = """
SELECT DISTINCT ON (purpose) purpose, granted_at, revoked_at
FROM activity.consent_grants
WHERE user_id = %s::uuid
ORDER BY purpose, granted_at DESC, id DESC
"""

# A grant is idempotent because it writes only when no active grant exists: the
# `WHERE NOT EXISTS` makes the second identical request a no-op rather than a second row,
# and `RETURNING` is what tells the handler whether anything actually changed.
_INSERT_GRANT = """
INSERT INTO activity.consent_grants (user_id, purpose)
SELECT %s::uuid, %s
WHERE NOT EXISTS (
    SELECT 1 FROM activity.consent_grants
    WHERE user_id = %s::uuid AND purpose = %s AND revoked_at IS NULL
)
RETURNING id, granted_at, revoked_at
"""

# A revocation closes every active grant for the purpose, which keeps the state
# single-valued even if two concurrent grants ever raced past the guard above.
_REVOKE_GRANT = """
UPDATE activity.consent_grants
SET revoked_at = NOW()
WHERE user_id = %s::uuid AND purpose = %s AND revoked_at IS NULL
RETURNING id
"""


def _isoformat(value: Any) -> str | None:
    """Best-effort ISO-8601 stringification for timestamps, preserving null."""
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return str(value.isoformat())
    return str(value)


def _require_pool() -> Any:
    if _pool is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Service not ready")
    return _pool


def _validated_purpose(purpose: str) -> str:
    """Reject a purpose outside the two the published vocabulary carries.

    422 rather than 404: the path names a purpose the caller may grant, and an unknown one
    is an unprocessable request rather than a missing resource.
    """
    if purpose not in consent_purposes():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Unknown purpose {purpose!r}; must be one of: {', '.join(consent_purposes())}",
        )
    return purpose


def _consent_state(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Render both purposes, in vocabulary order, whether or not either has a row.

    A purpose nobody has ever acted on is reported as not granted rather than omitted, so
    a client renders the same two controls before and after the first decision.
    """
    latest = {row["purpose"]: row for row in rows}
    state = []
    for purpose in consent_purposes():
        row = latest.get(purpose)
        granted = row is not None and row["revoked_at"] is None
        state.append(
            {
                "purpose": purpose,
                "granted": granted,
                "granted_at": _isoformat(row["granted_at"]) if row else None,
                "revoked_at": _isoformat(row["revoked_at"]) if row else None,
            }
        )
    return state


@router.get("/api/user/consent")
async def get_consent(current_user: Annotated[dict[str, Any], Depends(require_user)]) -> JSONResponse:
    """Return both consent purposes with their current grant and revocation times."""
    user_id = _caller_id(current_user)
    pool = _require_pool()

    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await execute_sql(cur, _SELECT_CONSENT, (user_id,))
        rows = await cur.fetchall()

    return JSONResponse(content={"purposes": _consent_state(list(rows))})


@router.put("/api/user/consent/{purpose}")
async def set_consent(
    purpose: str,
    body: ConsentUpdateRequest,
    current_user: Annotated[dict[str, Any], Depends(require_user)],
) -> JSONResponse:
    """Grant or revoke consent for one purpose.

    Idempotent in both directions: granting what is already granted and revoking what is
    already revoked both succeed and change nothing. The event is emitted only when the
    state actually changed, because ADR 0010 makes each change itself an event and a
    repeated request is not a second decision.
    """
    user_id = _caller_id(current_user)
    purpose = _validated_purpose(purpose)
    pool = _require_pool()

    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        if body.granted:
            await execute_sql(cur, _INSERT_GRANT, (user_id, purpose, user_id, purpose))
        else:
            await execute_sql(cur, _REVOKE_GRANT, (user_id, purpose))
        changed = await cur.fetchone() is not None

    if changed:
        event_type = EVENT_CONSENT_GRANTED if body.granted else EVENT_CONSENT_REVOKED
        await activity.record_event(
            user_id,
            event_type,
            {"purpose": purpose},
            idempotency_key=f"{event_type}:{purpose}:{datetime.now(UTC).isoformat()}",
        )
        logger.info("🔏 Consent updated", purpose=purpose, granted=body.granted)

    return JSONResponse(content={"purpose": purpose, "granted": body.granted, "changed": changed})
