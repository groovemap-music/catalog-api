"""Rate limiter singleton for the API service."""

import hashlib
import inspect
from collections.abc import Callable

import slowapi.extension as slowapi_extension
from fastapi import Request
from slowapi import Limiter
from slowapi.util import get_remote_address

from api import dependencies, request_budgets


# SlowAPI 0.1.10 still delegates its coroutine checks to the deprecated
# asyncio.iscoroutinefunction. Point its private module binding at inspect until
# https://github.com/laurentS/slowapi/pull/264 is released. Both modules expose
# the only attribute SlowAPI reads through that binding, so this stays local to
# SlowAPI instead of monkey-patching asyncio process-wide.
if hasattr(slowapi_extension, "asyncio"):
    slowapi_extension.asyncio = inspect


# headers_enabled=True so 429 responses carry `Retry-After` (in seconds) — required
# by the GRUVAX contract and good practice for any throttled API.
def caller_key_func(request: Request) -> str:
    """Rate-limit key: the signed-in user when the request carries a valid access JWT, else the client IP.

    The JWT signature and expiry are checked (``dependencies.rate_limit_subject``), so a made-up ``Authorization`` header cannot
    mint a fresh bucket per request; it falls back to the IP like any anonymous caller. Revocation is not
    consulted: a revoked but correctly signed token only shares its real owner's bucket.

    The IP is ``request.client.host``, which Uvicorn rewrites from ``X-Forwarded-For`` only for peers in
    ``FORWARDED_ALLOW_IPS`` (api/api.py). A client-supplied header is never read here.
    """
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        subject = dependencies.rate_limit_subject(auth[len("bearer ") :].strip())
        if subject:
            return "user:" + subject
    return "ip:" + get_remote_address(request)


# default_limits are the limit for every route without its own decorator, applied by `default_rate_limit` below
# (SlowAPI's own middleware cannot see routes that FastAPI includes lazily). A callable reads the environment per
# request, so the limit is configurable (api/request_budgets.py).
limiter = Limiter(
    key_func=caller_key_func,
    headers_enabled=True,
    default_limits=[lambda: request_budgets.rate_limit("default")],
)


def limit_for(name: str) -> Callable[[], str]:
    """A dynamic limit provider for ``@limiter.limit(limit_for("trends"))``."""
    return lambda: request_budgets.rate_limit(name)


def bearer_token_key_func(request: Request) -> str:
    """Per-endpoint key_func for endpoints that want one rate-limit bucket per caller token.

    The Authorization header is hashed (not stored raw) so the rate-limit key never
    contains the plaintext token in slowapi telemetry, internal data structures, or
    error messages. If no Bearer token is present, falls back to the client IP so
    unauthenticated probes still get throttled.

    Used by `/api/user/collection` and friends, where both first-party JWTs and
    app tokens authenticate the same endpoint and need distinct buckets.
    """
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        token = auth[len("bearer ") :].strip()
        if token:
            # 16 hex chars (64 bits) is plenty of entropy to keep buckets distinct
            # without filling slowapi's in-memory keyspace with full SHA-256 hashes.
            return "tok:" + hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]
    return "ip:" + get_remote_address(request)


async def default_rate_limit(request: Request) -> None:
    """Router dependency: apply the default limit to a route that has no ``@limiter.limit`` of its own.

    A decorated route already checks its own limits together with the defaults inside the decorator, so it is
    skipped here to avoid counting one request twice. ``@limiter.exempt`` routes are skipped by the limiter.
    """
    endpoint = request.scope.get("endpoint")
    if endpoint is None:
        return
    name = f"{endpoint.__module__}.{endpoint.__name__}"
    if name in limiter._route_limits or name in limiter._dynamic_route_limits:
        return
    limiter._check_request_limit(request, endpoint, False)
