"""One mapping from a rejected Valkey write to an HTTP 503 (gm-catalog-api-cmb6).

Production Valkey runs ``maxmemory-policy noeviction``: once full, every
memory-adding command (SET, SETEX, SET NX, INCR, DECR) is rejected with
``OOM command not allowed`` while reads, GETDEL, DEL and EXPIRE keep working.
A user-facing path whose write is rejected must not surface that as a raw 500:
it answers 503 with a clear, non-leaking message and a ``Retry-After`` hint,
and logs the failure at error level under a stable ``alert`` key so a full
Valkey is visible beyond the memory alarm.

Only *unavailability* maps to 503: the server refusing writes (OOM, read-only
replica) or being unreachable. Any other Valkey error (e.g. ``WRONGTYPE``) is a
bug and keeps propagating as a 500.

Callers stay responsible for ordering their writes so that a rejected write
leaves state consistent; this module only decides what the client is told.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import structlog
from fastapi import HTTPException, status
from valkey.exceptions import ConnectionError as ValkeyConnectionError
from valkey.exceptions import OutOfMemoryError, ReadOnlyError
from valkey.exceptions import TimeoutError as ValkeyTimeoutError


logger = structlog.get_logger(__name__)

VALKEY_UNAVAILABLE_ERRORS: tuple[type[Exception], ...] = (
    OutOfMemoryError,
    ReadOnlyError,
    ValkeyConnectionError,
    ValkeyTimeoutError,
)
"""Valkey errors meaning "writes are not accepted right now" — retryable, not bugs."""

VALKEY_UNAVAILABLE_RETRY_AFTER = "30"
"""``Retry-After`` seconds sent with every Valkey-unavailable 503."""

ALERT_VALKEY_WRITE_REJECTED = "valkey_write_rejected"
"""Default stable log key for alerting on a rejected user-facing Valkey write."""


def valkey_write_unavailable(
    exc: BaseException,
    *,
    operation: str,
    detail: str,
    alert: str = ALERT_VALKEY_WRITE_REJECTED,
    event: str = "❌ Valkey rejected a write on a user-facing path; returning 503",
    **context: Any,
) -> HTTPException:
    """Log *exc* under *alert* and return the 503 to raise for it.

    *detail* is the client-facing message; it must say what did (not) happen
    and never include the Valkey error text. *context* is logged only.
    """
    logger.error(
        event,
        alert=alert,
        operation=operation,
        error_type=type(exc).__name__,
        error=str(exc),
        **context,
    )
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=detail,
        headers={"Retry-After": VALKEY_UNAVAILABLE_RETRY_AFTER},
    )


@contextmanager
def valkey_write_guard(
    *,
    operation: str,
    detail: str,
    alert: str = ALERT_VALKEY_WRITE_REJECTED,
    event: str | None = None,
    **context: Any,
) -> Iterator[None]:
    """Convert a Valkey-unavailable error raised inside the block into a 503.

    Usage::

        with valkey_write_guard(operation="logout", detail="..."):
            await valkey.setex(key, ttl, "1")
    """
    try:
        yield
    except VALKEY_UNAVAILABLE_ERRORS as exc:
        kwargs: dict[str, Any] = {"event": event} if event is not None else {}
        raise valkey_write_unavailable(exc, operation=operation, detail=detail, alert=alert, **kwargs, **context) from exc
