"""Shared FastAPI dependency functions for API routers."""

import asyncio
import hmac
from dataclasses import dataclass
from enum import StrEnum, auto
from typing import Annotated, Any, Literal

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from psycopg.rows import dict_row

from api.app_tokens import (
    TOKEN_PREFIX as _APP_TOKEN_PREFIX,
)
from api.app_tokens import (
    AppTokenAuth,  # noqa: F401  — re-exported for callers
    _background_tasks,
    _lookup_active_token,
    _touch_last_used_at,
    hash_token,
    require_app_token,  # noqa: F401  — re-exported for callers
)
from api.auth import (
    REASON_CREDENTIALS_CHANGED,
    REASON_REVOKED,
    decode_token,
    token_revocation_reason,
)


_security = HTTPBearer(auto_error=False)
_jwt_secret: str | None = None
_redis: Any = None
_pool: Any = None


class JwtKind(StrEnum):
    """JWT purposes recognized by the shared validation boundary."""

    ACCESS = auto()
    ADMIN = auto()
    TWO_FACTOR_CHALLENGE = auto()


def configure(jwt_secret: str | None, redis: Any = None, pool: Any = None) -> None:
    global _jwt_secret, _redis, _pool
    _jwt_secret = jwt_secret
    _redis = redis
    _pool = pool


async def validate_token(
    token: str,
    jwt_secret: str | None,
    redis: Any = None,
    *,
    kind: JwtKind = JwtKind.ACCESS,
    expose_admin_mismatch: bool = False,
) -> dict[str, Any]:
    """Decode a JWT and apply the complete policy for its intended use.

    This is the single JWT validation boundary. Callers may choose whether an
    access endpoint exposes the admin/user distinction, or adapt an error for
    optional authentication, but must not repeat signature, token-kind,
    subject, or revocation checks locally.
    """
    if jwt_secret is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Authentication not configured")

    try:
        payload = decode_token(token, jwt_secret)
    except ValueError as exc:
        detail = "Invalid or expired challenge token" if kind is JwtKind.TWO_FACTOR_CHALLENGE else "Invalid or expired token"
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=detail,
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    actual_type = payload.get("type")
    if kind is JwtKind.ACCESS:
        # Access tokens are allowlisted by the absence of a type claim. Every
        # typed token is reserved for another purpose and denied by default.
        if actual_type == "admin" and expose_admin_mismatch:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin tokens cannot be used for user endpoints")
        if actual_type is not None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token",
                headers={"WWW-Authenticate": "Bearer"},
            )
    else:
        expected_type = "admin" if kind is JwtKind.ADMIN else "2fa_challenge"
        if actual_type != expected_type:
            detail = "Admin access required" if kind is JwtKind.ADMIN else "Invalid challenge token type"
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN if kind is JwtKind.ADMIN else status.HTTP_401_UNAUTHORIZED,
                detail=detail,
            )

    user_id = payload.get("sub")
    if not user_id:
        detail = "Invalid challenge token" if kind is JwtKind.TWO_FACTOR_CHALLENGE else "Invalid token"
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail, headers={"WWW-Authenticate": "Bearer"})

    reason = await token_revocation_reason(payload, redis)
    if reason == REASON_REVOKED:
        detail = "Challenge invalidated by password change" if kind is JwtKind.TWO_FACTOR_CHALLENGE else "Token has been revoked"
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail, headers={"WWW-Authenticate": "Bearer"})
    if reason == REASON_CREDENTIALS_CHANGED:
        detail = (
            "Challenge invalidated by password change" if kind is JwtKind.TWO_FACTOR_CHALLENGE else "Token invalidated by password change (revoked)"
        )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail, headers={"WWW-Authenticate": "Bearer"})

    return payload


async def get_optional_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_security)],
) -> dict[str, Any] | None:
    if credentials is None or _jwt_secret is None:
        return None
    try:
        return await validate_token(credentials.credentials, _jwt_secret, _redis)
    except HTTPException:
        return None


async def require_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_security)],
) -> dict[str, Any]:
    if _jwt_secret is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Personalized endpoints not enabled")
    if credentials is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required", headers={"WWW-Authenticate": "Bearer"})
    return await validate_token(credentials.credentials, _jwt_secret, _redis, expose_admin_mismatch=True)


@dataclass(frozen=True, slots=True)
class UnifiedAuth:
    """Resolved authentication context — populated by either JWT or app-token path.

    Endpoints that accept both first-party and third-party tokens consume this
    so they don't need to branch on auth path in the handler body.
    """

    user_id: str
    via: Literal["jwt", "app_token"]
    token_id: str | None  # set only when via == "app_token"
    scopes: list[str]  # empty for JWT (no scope vocabulary on first-party auth)


def require_user_or_app_token(scopes: list[str]) -> Any:
    """Dependency factory that accepts EITHER a first-party JWT OR an app token.

    Used by endpoints exposed to third-party apps (GRUVAX, MCP) where the same
    behavior should be reachable via the user's own login session OR via a
    delegated, scoped app token.

    Routing rule: if the Bearer credential starts with the app-token prefix
    (`dscg_`), it goes through app-token auth + scope check. Otherwise it goes
    through the existing `require_user` flow. This keeps the JWT path
    1:1 with the existing behavior — no surprise drift for existing clients.

    Returns a `UnifiedAuth` so the handler reads `auth.user_id` regardless of path.
    """
    required_scopes = list(scopes)

    async def dependency(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_security)],
    ) -> UnifiedAuth:
        # ─── App-token path ─────────────────────────────────────────────────
        # Only routes here when the caller explicitly presents an app token.
        # No credentials, JWT, or anything else falls through to require_user
        # so the JWT path's 503/401 ordering and revocation checks are preserved
        # byte-for-byte for existing clients.
        if credentials is not None and credentials.credentials.startswith(_APP_TOKEN_PREFIX):
            token_hash = hash_token(credentials.credentials)
            row = await _lookup_active_token(token_hash)
            if row is None:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid or revoked app token",
                    headers={"WWW-Authenticate": "Bearer"},
                )
            # Defense in depth: mirrors require_app_token's check against the
            # row's own persisted token_hash, so the two app-token entry
            # points stay in lockstep instead of silently diverging
            # (groovemap-osoc).
            if not hmac.compare_digest(row["token_hash"], token_hash):
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid app token",
                    headers={"WWW-Authenticate": "Bearer"},
                )
            granted = list(row.get("scope") or [])
            missing = [s for s in required_scopes if s not in granted]
            if missing:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"App token missing required scope(s): {', '.join(missing)}",
                )
            # Fire-and-forget last_used_at bookkeeping — mirrors require_app_token
            # so tokens authenticated through the unified path are also audited.
            # A slow PG round trip never blocks the request; the reference is held
            # in _background_tasks so the loop does not GC the coroutine mid-flight.
            task = asyncio.create_task(_touch_last_used_at(str(row["id"])))
            _background_tasks.add(task)
            task.add_done_callback(_background_tasks.discard)

            return UnifiedAuth(
                user_id=str(row["user_id"]),
                via="app_token",
                token_id=str(row["id"]),
                scopes=granted,
            )

        # ─── JWT path ───────────────────────────────────────────────────────
        # Delegated entirely to require_user so behavior is identical to before:
        # _jwt_secret is None → 503; missing credentials → 401; invalid → 401;
        # admin token → 403; jti / password-changed revocation → 401.
        payload = await require_user(credentials)
        user_id_value = payload.get("sub")
        if not user_id_value:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return UnifiedAuth(
            user_id=str(user_id_value),
            via="jwt",
            token_id=None,
            scopes=[],
        )

    return dependency


async def require_admin(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_security)],
) -> dict[str, Any]:
    """Require a valid admin JWT token. Rejects non-admin tokens with 403."""
    if _jwt_secret is None:
        raise HTTPException(status_code=503, detail="Admin endpoints not configured")
    if credentials is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    payload = await validate_token(credentials.credentials, _jwt_secret, _redis, kind=JwtKind.ADMIN)
    # DB verification: confirm user exists and is_admin=True
    if _pool is not None:
        async with _pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT is_admin FROM users WHERE id = %s AND is_active = true",
                (payload["sub"],),
            )
            row = await cur.fetchone()
        if row is None or not row["is_admin"]:
            raise HTTPException(status_code=403, detail="Admin access required")
    return payload
