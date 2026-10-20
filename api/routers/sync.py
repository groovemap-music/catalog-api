"""Sync endpoints — migrated from curator service."""

import asyncio
import contextlib
from typing import Annotated, Any
from uuid import UUID

import structlog
from common import AsyncPostgreSQLPool, AsyncResilientNeo4jDriver
from common.query_debug import execute_sql
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from psycopg.rows import dict_row

from api.auth import get_oauth_encryption_key
from api.dependencies import validate_token
from api.limiter import limiter
from api.syncer import run_full_sync
from api.valkey_errors import valkey_write_guard


logger = structlog.get_logger(__name__)

router = APIRouter()
_security = HTTPBearer()

_pool: AsyncPostgreSQLPool | None = None
_neo4j: AsyncResilientNeo4jDriver | None = None
_config: Any = None
_valkey: Any = None
_running_syncs: dict[str, asyncio.Task[Any]] = {}

_SYNC_UNAVAILABLE_DETAIL = "Sync could not be started right now; nothing was started, please try again shortly"


def configure(
    pool: AsyncPostgreSQLPool,
    neo4j: AsyncResilientNeo4jDriver | None,
    config: Any,
    running_syncs: dict[str, asyncio.Task[Any]],
    valkey: Any = None,
) -> None:
    global _pool, _neo4j, _config, _running_syncs, _valkey
    _pool = pool
    _neo4j = neo4j
    _config = config
    _running_syncs = running_syncs
    _valkey = valkey


async def _get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(_security)],
) -> dict[str, Any]:
    if _config is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Service not ready")
    return await validate_token(credentials.credentials, _config.jwt_secret_key, _valkey)


@router.post("/api/sync", status_code=status.HTTP_202_ACCEPTED)
@limiter.limit("10/minute")
async def trigger_sync(
    request: Request,  # noqa: ARG001 — required by slowapi rate limiter
    current_user: Annotated[dict[str, Any], Depends(_get_current_user)],
) -> JSONResponse:
    user_id = current_user.get("sub")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    if _pool is None or _neo4j is None or _config is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Service not ready")

    if not _valkey:
        response, _ = await _start_sync(user_id, _pool, _neo4j)
        return response

    # Valkey-based per-user sync cooldown (prevents rapid re-triggers)
    cooldown_key = f"sync:cooldown:{user_id}"
    in_cooldown = await _valkey.get(cooldown_key)
    if in_cooldown:
        return JSONResponse(
            content={"status": "cooldown", "message": "Sync rate limited. Please wait before triggering again."},
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        )

    # Atomic lock to prevent duplicate sync tasks from concurrent requests. A
    # rejected lock write (Valkey full) means nothing was started: 503.
    lock_key = f"sync:lock:{user_id}"
    with valkey_write_guard(operation="sync_lock", detail=_SYNC_UNAVAILABLE_DETAIL, user_id=user_id):
        acquired = await _valkey.set(lock_key, "1", nx=True, ex=30)
    if not acquired:
        return JSONResponse(
            content={"status": "already_running"},
            status_code=status.HTTP_202_ACCEPTED,
        )

    try:
        response, started = await _start_sync(user_id, _pool, _neo4j)
        if started:
            await _set_cooldown(cooldown_key, user_id)
        return response
    finally:
        # Always release the lock — success, already-running, or failure — so
        # the user is never locked out. On success the cooldown (if it could be
        # written) is already in place, so there is no TOCTOU window. DEL is
        # never rejected for memory.
        with contextlib.suppress(Exception):
            await _valkey.delete(lock_key)


async def _set_cooldown(cooldown_key: str, user_id: str) -> None:
    """Best-effort cooldown write AFTER the sync task has started.

    The sync is already running and recorded, so a rejected write must not turn
    it into an error for the caller (gm-catalog-api-cmb6). Without the cooldown
    the in-process ``_running_syncs`` guard still prevents a duplicate sync;
    only the post-sync re-trigger rate limit is lost.
    """
    try:
        await _valkey.setex(cooldown_key, 60, "1")
    except Exception as exc:
        logger.warning(
            "⚠️ Sync started but its cooldown could not be written; re-trigger rate limit skipped",
            alert="valkey_write_rejected",
            operation="sync_cooldown",
            user_id=user_id,
            error_type=type(exc).__name__,
            error=str(exc),
        )


async def _start_sync(user_id: str, pool: AsyncPostgreSQLPool, neo4j: AsyncResilientNeo4jDriver) -> tuple[JSONResponse, bool]:
    """Start a full sync unless one is already running in this process.

    Returns the response and whether a new sync was started.
    """
    if user_id in _running_syncs and not _running_syncs[user_id].done():
        async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await execute_sql(
                cur,
                "SELECT id, status FROM sync_history WHERE user_id = %s::uuid AND status = 'running' ORDER BY started_at DESC LIMIT 1",
                (user_id,),
            )
            existing = await cur.fetchone()
        content = {"sync_id": str(existing["id"]), "status": "already_running"} if existing else {"status": "already_running"}
        return JSONResponse(content=content, status_code=status.HTTP_202_ACCEPTED), False
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await execute_sql(
            cur,
            "INSERT INTO sync_history (user_id, sync_type, status) VALUES (%s::uuid, 'full', 'running') RETURNING id",
            (user_id,),
        )
        sync_row = await cur.fetchone()
    if not sync_row:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to create sync record")
    sync_id = str(sync_row["id"])
    task = asyncio.create_task(
        run_full_sync(
            user_uuid=UUID(user_id),
            sync_id=sync_id,
            pg_pool=pool,
            neo4j_driver=neo4j,
            discogs_user_agent=_config.discogs_user_agent,
            oauth_encryption_key=get_oauth_encryption_key(_config.encryption_master_key),
            valkey_client=_valkey,
        )
    )
    # Clean up completed task to prevent unbounded memory growth and
    # pinned decrypted OAuth credentials in the coroutine closure.
    _uid = user_id  # capture for callback
    task.add_done_callback(lambda _t: _running_syncs.pop(_uid, None))
    _running_syncs[user_id] = task

    logger.info("🔄 Sync triggered", user_id=user_id, sync_id=sync_id)
    return JSONResponse(content={"sync_id": sync_id, "status": "started"}, status_code=status.HTTP_202_ACCEPTED), True


@router.get("/api/sync/status")
async def sync_status(
    current_user: Annotated[dict[str, Any], Depends(_get_current_user)],
) -> JSONResponse:
    if _pool is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Service not ready")
    user_id = current_user.get("sub")
    async with _pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await execute_sql(
            cur,
            """SELECT id, sync_type, status, items_synced, error_message, started_at, completed_at
               FROM sync_history WHERE user_id = %s::uuid ORDER BY started_at DESC LIMIT 10""",
            (user_id,),
        )
        rows = await cur.fetchall()
    history = [
        {
            "sync_id": str(row["id"]),
            "sync_type": row["sync_type"],
            "status": row["status"],
            "items_synced": row["items_synced"],
            "error": row["error_message"],
            "started_at": row["started_at"].isoformat(),
            "completed_at": row["completed_at"].isoformat() if row["completed_at"] else None,
        }
        for row in rows
    ]
    return JSONResponse(content={"syncs": history})
