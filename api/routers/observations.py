"""Observation endpoints — user-captured evidence about a copy the caller holds.

ADR 0009 makes the physical copy first class: a copy exists because a user says it does,
not because a provider listed it, and the observation record is what lets that user
assert a fact about their own copy — a matrix inscription, a grading, a purchase price —
without the fact having to be true of the edition in general.

Both endpoints are owner-scoped. The copy id is a caller-supplied path parameter, so the
ownership check is a predicate in the statement rather than a separate read, and a copy
that belongs to somebody else is reported exactly the way an unknown one is: 404, with no
hint that the id names a real row.
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

import structlog
from common.query_debug import execute_sql
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse
from psycopg.rows import dict_row

from api.dependencies import UnifiedAuth, require_user, require_user_or_app_token
from api.models import CreateObservationRequest


logger = structlog.get_logger(__name__)

router = APIRouter()

_pool: Any = None


def configure(pool: Any) -> None:
    """Wire the PostgreSQL pool from ``api.api`` startup."""
    global _pool
    _pool = pool


# The ownership predicate travels with the write instead of preceding it: a separate
# SELECT would leave a window in which the copy changes hands between the check and the
# INSERT, and would cost a second round trip on every create.
_INSERT_OBSERVATION = """
INSERT INTO observations (user_id, owned_copy_id, kind, value, source, confidence, observed_at)
SELECT copy.user_id, copy.id, %s, %s, %s, %s, COALESCE(%s, NOW())
FROM owned_copies AS copy
WHERE copy.id = %s::uuid AND copy.user_id = %s::uuid
RETURNING id, owned_copy_id, kind, value, source, confidence, observed_at, created_at
"""

_SELECT_OBSERVATIONS = """
SELECT observation.id, observation.owned_copy_id, observation.kind, observation.value,
       observation.source, observation.confidence, observation.observed_at, observation.created_at
FROM observations AS observation
JOIN owned_copies AS copy ON copy.id = observation.owned_copy_id
WHERE copy.id = %s::uuid AND copy.user_id = %s::uuid
ORDER BY observation.observed_at DESC, observation.id DESC
"""

_SELECT_COPY = """
SELECT id FROM owned_copies WHERE id = %s::uuid AND user_id = %s::uuid
"""

_SELECT_OWNED_COPIES_FOR_ITEM = """
SELECT id, item_id, collection_row_id, acquired_at, created_at, updated_at
FROM owned_copies
WHERE item_id = %s::uuid AND user_id = %s::uuid
ORDER BY acquired_at ASC NULLS LAST, id ASC
LIMIT %s
"""

_COPY_NOT_FOUND = "Copy not found"


def _validated_copy_id(copy_id: str) -> str:
    """Reject a malformed copy id the way an unknown one is rejected.

    The path parameter reaches a ``::uuid`` cast, so a non-UUID string would otherwise
    surface as a 500. A malformed id is indistinguishable from an id that names no copy
    the caller owns, and both are the same 404.
    """
    try:
        UUID(copy_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_COPY_NOT_FOUND) from None
    return copy_id


def _shape(row: dict[str, Any]) -> dict[str, Any]:
    """Render one observation row as JSON."""
    confidence = row.get("confidence")
    return {
        "id": str(row["id"]),
        "owned_copy_id": str(row["owned_copy_id"]),
        "kind": row["kind"],
        "value": row["value"],
        "source": row["source"],
        "confidence": float(confidence) if confidence is not None else None,
        "observed_at": _isoformat(row.get("observed_at")),
        "created_at": _isoformat(row.get("created_at")),
    }


def _isoformat(value: Any) -> str | None:
    """Best-effort ISO-8601 stringification for timestamps, preserving null."""
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return str(value.isoformat())
    return str(value)


def _shape_copy(row: dict[str, Any]) -> dict[str, Any]:
    """Render one owner-scoped physical-copy row without merging duplicates."""
    return {
        "id": str(row["id"]),
        "item_id": str(row["item_id"]),
        "collection_row_id": str(row["collection_row_id"]) if row.get("collection_row_id") is not None else None,
        "acquired_at": _isoformat(row.get("acquired_at")),
        "created_at": _isoformat(row.get("created_at")),
        "updated_at": _isoformat(row.get("updated_at")),
    }


def _require_pool() -> Any:
    if _pool is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Service not ready")
    return _pool


async def list_owned_copies(item_id: str, user_id: str, *, limit: int = 50, pool: Any = None) -> dict[str, Any]:
    """List at most ``limit`` physical copies of one native item owned by ``user_id``.

    ``item_id`` is the native catalog UUID; copy IDs are separate UUIDs. The extra row is
    fetched only to expose truncation, so the database result remains bounded at 101 rows.
    ``user_id`` must come from trusted authentication state, never a request parameter.
    """
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    try:
        UUID(item_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Item not found") from None
    active_pool = pool if pool is not None else _require_pool()
    async with active_pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await execute_sql(cur, _SELECT_OWNED_COPIES_FOR_ITEM, (item_id, user_id, limit + 1))
        rows = await cur.fetchall()
    return {
        "item_id": item_id,
        "copies": [_shape_copy(row) for row in rows[:limit]],
        "truncated": len(rows) > limit,
    }


async def _read_copy_timeline(copy_id: str, user_id: str, pool: Any) -> list[dict[str, Any]]:
    """Read the chronological observations for one explicitly selected owned copy."""
    copy_id = _validated_copy_id(copy_id)
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await execute_sql(cur, _SELECT_COPY, (copy_id, user_id))
        if await cur.fetchone() is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_COPY_NOT_FOUND)
        await execute_sql(cur, _SELECT_OBSERVATIONS, (copy_id, user_id))
        rows = await cur.fetchall()
    return [_shape(row) for row in rows]


@router.get("/api/user/items/{native_item_id}/copies")
async def get_owned_copies(
    native_item_id: str,
    current_user: Annotated[dict[str, Any], Depends(require_user)],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> JSONResponse:
    """List the authenticated user's physical copies of a native catalog item.

    This is a first-party-only selection route. App tokens remain limited to their
    existing per-copy observation scopes and cannot enumerate a user's collection.
    """
    result = await list_owned_copies(native_item_id, current_user.get("sub", ""), limit=limit)
    return JSONResponse(content=result)


@router.post("/api/user/copies/{copy_id}/observations", status_code=status.HTTP_201_CREATED)
async def create_observation(
    copy_id: str,
    body: CreateObservationRequest,
    auth: Annotated[UnifiedAuth, Depends(require_user_or_app_token(["observations:write"]))],
) -> JSONResponse:
    """Record one observation about a copy the caller owns.

    Reachable with an ``observations:write`` app token, which is how a kiosk or an agent
    records what it read off a sleeve. The owner id comes from the token's owner, so the
    ownership predicate below is the same predicate either way and a delegate can no more
    write against somebody else's copy than a session can.
    """
    user_id = auth.user_id
    copy_id = _validated_copy_id(copy_id)
    pool = _require_pool()

    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await execute_sql(
            cur,
            _INSERT_OBSERVATION,
            (body.kind, body.value, body.source, body.confidence, body.observed_at, copy_id, user_id),
        )
        row = await cur.fetchone()

    if row is None:
        # The INSERT ... SELECT wrote nothing, which means the ownership predicate matched
        # no row. Unknown copy and someone else's copy are the same answer on purpose.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_COPY_NOT_FOUND)

    # `via` records which auth path wrote the row — never the token itself, whose
    # plaintext is not in the process and whose id says nothing an operator needs.
    logger.info("🔍 Observation recorded", user_id=user_id, copy_id=copy_id, kind=body.kind, source=body.source, via=auth.via)
    return JSONResponse(status_code=status.HTTP_201_CREATED, content=_shape(row))


@router.get("/api/user/copies/{copy_id}/observations")
async def list_observations(
    copy_id: str,
    auth: Annotated[UnifiedAuth, Depends(require_user_or_app_token(["observations:read"]))],
) -> JSONResponse:
    """Return every observation recorded against a copy the caller owns, newest first.

    Reachable with an ``observations:read`` app token; the listing is owner-scoped to the
    token's owner exactly as it is to a session's user.
    """
    user_id = auth.user_id
    copy_id = _validated_copy_id(copy_id)
    pool = _require_pool()
    observations = await _read_copy_timeline(copy_id, user_id, pool)
    return JSONResponse(content={"copy_id": copy_id, "observations": observations})
