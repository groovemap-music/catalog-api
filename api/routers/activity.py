"""The endpoints ADR 0010 puts in front of the activity record.

One client-facing outcome endpoint, the two consent endpoints, and the erasure and export
endpoints all live here because they are one decision's surface: what is recorded, what
the user permitted, and how the user gets it back or has it removed.

Every write here goes through :mod:`api.activity`, which never raises into a request, so a
degraded behavioural record never costs a caller their response.
"""

from __future__ import annotations

from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse

import api.activity as activity
from api.dependencies import require_user
from api.models import ActivityOutcomeRequest


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
