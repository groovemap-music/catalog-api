"""Namespaced Valkey keys, plus the upgrade path for keys written before namespacing.

Every key catalog-api reads, writes, deletes or scans is built with :func:`valkey_key` and
every SCAN glob with :func:`valkey_pattern` (both from ``common``; the prefix defaults to
``groovemap:`` and ``VALKEY_KEY_PREFIX`` overrides it), so the service can share a Valkey
with other applications. ``tests/test_valkey_key_guard.py`` fails on any key literal handed
to the client without going through those helpers.

Upgrade path (``VALKEY_LEGACY_KEYS``)
-------------------------------------
A dedicated stack that ran before namespacing holds *unprefixed* long-lived keys, and on a
shared Valkey an unprefixed key may belong to another application (Discogsography uses the same
key names). So legacy access is split by what a foreign key could do:

* **Deny-direction checks** may fall back to the legacy key by default: ``revoked:jti:{jti}``
  (the jti comes from a signed JWT) and ``password_changed:{user uuid from the token}``. A hit
  can only make access stricter (force a re-login), never grant it, and a *missing* key fails
  open, so dropping them would un-revoke tokens on an upgraded dedicated stack.
* **Positive-authorization lookups** keyed by a requester-supplied value (``reset:{token}``,
  ``snapshot:{token}``, ``2fa_challenge:{jti}``, ``discogs:oauth:state:{state}``) are NEVER read
  from a legacy key: a foreign app's key must not be accepted as ours. They are short-lived, so
  after an upgrade the user simply requests a new one.

Modes:

``off``
    Never touch an unprefixed key.
``read`` (default)
    Deny-direction point lookups of the exact legacy key (the two families above) for a bounded
    window (``VALKEY_LEGACY_WINDOW_SECONDS``, default 24h, longer than their TTLs) that opens
    when the first upgraded process starts. Reads only; nothing unprefixed is written, scanned
    or deleted.
``migrate``
    Explicit opt-in for a **dedicated** Valkey: ``read``, plus a startup migration that copies
    each legacy key of the known families (``LEGACY_KEY_PATTERNS``) to its prefixed name with its
    remaining TTL (``SET NX PX``, idempotent, never overwrites a newer prefixed value) and then
    deletes the legacy copy, so nothing is left on a ``noeviction`` instance. Account erasure
    also removes the legacy copies of the per-user keys, and checks they are gone. Migrated
    positive-authorization keys are then read from their prefixed name like any other.

Every legacy name is validated against the id-shaped patterns below before it is read, copied
or deleted.
"""

from __future__ import annotations

import os
import re
import time
from typing import Any

import structlog
from common.config import valkey_key, valkey_key_prefix, valkey_pattern


logger = structlog.get_logger(__name__)

__all__ = [
    "LEGACY_KEY_PATTERNS",
    "MODE_MIGRATE",
    "MODE_OFF",
    "MODE_READ",
    "get_deny_with_legacy",
    "legacy_mode",
    "legacy_name",
    "migrate_legacy_keys",
    "prepare_legacy_keys",
    "reset_state",
    "valkey_key",
    "valkey_key_prefix",
    "valkey_pattern",
    "with_legacy_keys",
    "with_legacy_patterns",
]

MODE_OFF = "off"
MODE_READ = "read"
MODE_MIGRATE = "migrate"
_MODES = frozenset({MODE_OFF, MODE_READ, MODE_MIGRATE})

DEFAULT_LEGACY_WINDOW_SECONDS = 86_400
_MARKER_NAME = "legacy-keys:cutover"

# The exact legacy families. Each is paired with the shape the remainder must have, so a
# foreign key such as ``reset:foo:bar`` that merely shares a leading word is never touched.
_ID = r"[A-Za-z0-9_-]+"
LEGACY_KEY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("revoked:jti:*", re.compile(rf"^revoked:jti:{_ID}$")),
    ("password_changed:*", re.compile(rf"^password_changed:{_ID}$")),
    ("2fa_challenge:*", re.compile(rf"^2fa_challenge:{_ID}$")),
    ("discogs:oauth:state:*", re.compile(rf"^discogs:oauth:state:{_ID}$")),
    ("reset:*", re.compile(rf"^reset:{_ID}$")),
    ("snapshot:*", re.compile(rf"^snapshot:(usercount:)?{_ID}$")),
)

# The deny-direction subset that ``read`` may consult (see module docstring).
DENY_LEGACY_SHAPES: tuple[re.Pattern[str], ...] = (
    re.compile(rf"^revoked:jti:{_ID}$"),
    re.compile(rf"^password_changed:{_ID}$"),
)

# Per-user legacy keys erasure may remove, in ``migrate`` mode only.
ERASURE_LEGACY_SHAPES: tuple[re.Pattern[str], ...] = (
    re.compile(rf"^snapshot:usercount:{_ID}$"),
    re.compile(rf"^sync:(lock|cooldown):{_ID}$"),
    re.compile(rf"^recommend:enhanced:{_ID}$"),
    re.compile(rf"^recommend:explore:{_ID}:[A-Za-z0-9_:*.-]+$"),
)

_cutover: float | None = None
_window_closed = False


def legacy_mode() -> str:
    """Return the validated ``VALKEY_LEGACY_KEYS`` mode (default ``read``)."""
    raw = (os.environ.get("VALKEY_LEGACY_KEYS") or MODE_READ).strip().lower()
    if raw not in _MODES:
        raise ValueError(f"VALKEY_LEGACY_KEYS must be one of {sorted(_MODES)}, got {raw!r}")
    return raw


def _window_seconds() -> int:
    raw = (os.environ.get("VALKEY_LEGACY_WINDOW_SECONDS") or "").strip()
    if not raw:
        return DEFAULT_LEGACY_WINDOW_SECONDS
    try:
        seconds = int(raw)
    except ValueError:
        raise ValueError(f"VALKEY_LEGACY_WINDOW_SECONDS must be an integer, got {raw!r}") from None
    if seconds < 0:
        raise ValueError("VALKEY_LEGACY_WINDOW_SECONDS must not be negative")
    return seconds


def reset_state() -> None:
    """Forget the cached cutover (tests, and re-configuration)."""
    global _cutover, _window_closed
    _cutover = None
    _window_closed = False


def legacy_name(key: str, shapes: tuple[re.Pattern[str], ...] | None = None) -> str | None:
    """Return the pre-namespace spelling of a prefixed ``key``, or None when it has none.

    With ``shapes`` the result must match one of them, so a legacy name is only ever built for
    a family this service wrote.
    """
    prefix = valkey_key_prefix()
    if not prefix or not key.startswith(prefix):
        return None
    name = key[len(prefix) :]
    if shapes is not None and not any(shape.match(name) for shape in shapes):
        return None
    return name


def _marker_key() -> str:
    return valkey_key(_MARKER_NAME)


async def prepare_legacy_keys(valkey: Any) -> None:
    """Open the legacy-read window and, in ``migrate`` mode, run the migration.

    Called once at startup. A window marker is set with NX so the first upgraded process
    fixes the cutover for the whole fleet. Failures are logged and never block startup:
    an unreadable marker leaves the window open, which is the secure direction.
    """
    mode = legacy_mode()
    if mode == MODE_OFF or not valkey_key_prefix():
        return
    try:
        await valkey.set(_marker_key(), str(int(time.time())), nx=True)
    except Exception as exc:
        logger.warning("⚠️ Could not record the legacy-key cutover; legacy reads stay open", error_type=type(exc).__name__)
    if mode == MODE_MIGRATE:
        await migrate_legacy_keys(valkey)


async def _legacy_active(valkey: Any) -> bool:
    """True while an unprefixed fallback read is allowed."""
    global _cutover, _window_closed
    if _window_closed or legacy_mode() == MODE_OFF or not valkey_key_prefix():
        return False
    if _cutover is None:
        try:
            raw = await valkey.get(_marker_key())
            _cutover = float(raw) if raw else time.time()
        except Exception:
            return True
    if time.time() > _cutover + _window_seconds():
        _window_closed = True
        return False
    return True


async def get_deny_with_legacy(valkey: Any, key: str) -> Any:
    """GET a deny-direction ``key``; on a miss inside the window, GET its legacy spelling.

    Only ``revoked:jti:*`` and ``password_changed:*`` have a legacy spelling here. A hit on a
    foreign application's identical key can only deny, never authorize.
    """
    value = await valkey.get(key)
    if value is not None:
        return value
    legacy = legacy_name(key, DENY_LEGACY_SHAPES)
    if legacy is not None and await _legacy_active(valkey):
        return await valkey.get(legacy)
    return None


def with_legacy_keys(keys: list[str]) -> list[str]:
    """Add the legacy spelling of each per-user key, in ``migrate`` mode only (dedicated stack erasure)."""
    if legacy_mode() != MODE_MIGRATE:
        return keys
    return keys + [name for key in keys if (name := legacy_name(key, ERASURE_LEGACY_SHAPES)) is not None]


def with_legacy_patterns(patterns: list[str]) -> list[str]:
    """Add the unprefixed spelling of each per-user SCAN glob, in ``migrate`` mode only."""
    if legacy_mode() != MODE_MIGRATE:
        return patterns
    return patterns + [name for pattern in patterns if (name := legacy_name(pattern, ERASURE_LEGACY_SHAPES)) is not None]


async def migrate_legacy_keys(valkey: Any) -> dict[str, int]:
    """Copy known legacy keys to their prefixed names (TTL preserved), then delete the legacy copy.

    Idempotent and restartable: ``SET NX`` keeps any newer prefixed value, and a legacy key
    is only deleted once its prefixed twin exists. Keys with no TTL, keys whose shape does
    not match the family, and keys whose copy was rejected are left alone and counted.
    """
    prefix = valkey_key_prefix()
    stats = {"migrated": 0, "already_present": 0, "skipped": 0, "failed": 0}
    if not prefix:
        return stats
    for glob, shape in LEGACY_KEY_PATTERNS:
        cursor: int | str = 0
        while True:
            cursor, keys = await valkey.scan(cursor=int(cursor), match=glob, count=200)
            for key in keys:
                name = key if isinstance(key, str) else key.decode("utf-8", errors="replace")
                if name.startswith(prefix) or not shape.match(name):
                    stats["skipped"] += 1
                    continue
                await _migrate_one(valkey, name, prefix + name, stats)
            if str(cursor) == "0":
                break
    logger.info("🧹 Legacy Valkey keys migrated", **stats)
    return stats


async def _migrate_one(valkey: Any, legacy: str, target: str, stats: dict[str, int]) -> None:
    try:
        ttl_ms = await valkey.pttl(legacy)
        if ttl_ms == -2:  # expired between SCAN and now
            return
        if ttl_ms == -1:  # no expiry: not a key this service writes, leave it
            stats["skipped"] += 1
            return
        value = await valkey.get(legacy)
        if value is None:
            return
        stored = await valkey.set(target, value, px=max(int(ttl_ms), 1), nx=True)
        stats["migrated" if stored else "already_present"] += 1
        await valkey.delete(legacy)
    except Exception as exc:
        stats["failed"] += 1
        logger.warning("⚠️ Legacy Valkey key could not be migrated", key_family=legacy.split(":", maxsplit=1)[0], error_type=type(exc).__name__)
