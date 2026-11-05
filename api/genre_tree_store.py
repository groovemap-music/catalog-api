"""Serve ``/api/genre-tree`` from a stored tree, rebuilt in the background.

The tree needs a scan of every ``IS`` edge (59M + 131M db hits on lux, about a minute), far past the 30 s
request budget, so it is never built per request (audit H3, ``gm-catalog-api-somq``). Instead:

* A snapshot lives in process memory and, as one JSON value with no expiry, in Valkey, so a restart or a
  second replica serves it at once.
* :func:`run_refresh_loop` rebuilds it when the genre counts change (a one-statement fingerprint over the
  15 ``Genre`` nodes) or when it is older than ``max_age``, one replica at a time (Valkey ``SET NX``).
* A rebuild that fails keeps the previous tree, so the route stays available during a refresh and after
  an import. Only a cold start with no stored tree answers 503 until the first build finishes.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any, Final

import structlog

from api.queries import genre_tree_queries
from api.valkey_keys import valkey_key


logger = structlog.get_logger(__name__)

SNAPSHOT_KEY: Final = valkey_key("genre-tree:v1")
LOCK_KEY: Final = valkey_key("genre-tree:v1:lock")
# Longer than a full build (about a minute on lux), so a crashed builder's lock expires on its own.
LOCK_SECONDS: Final = 900
DEFAULT_MAX_AGE_SECONDS: Final = 86_400


class GenreTreeNotReadyError(RuntimeError):
    """No tree has been built or stored yet (a cold start); the route answers 503."""


@dataclass(frozen=True, slots=True)
class Snapshot:
    genres: list[dict[str, Any]]
    signature: str
    built_at: float  # epoch seconds


_snapshot: Snapshot | None = None
_valkey: Any = None


def configure(valkey: Any = None) -> None:
    global _valkey, _snapshot
    _valkey = valkey
    _snapshot = None


def current() -> Snapshot | None:
    return _snapshot


def _encode(snapshot: Snapshot) -> str:
    return json.dumps({"genres": snapshot.genres, "signature": snapshot.signature, "built_at": snapshot.built_at})


def _decode(raw: str | bytes) -> Snapshot | None:
    try:
        data = json.loads(raw)
        return Snapshot(genres=list(data["genres"]), signature=str(data["signature"]), built_at=float(data["built_at"]))
    except ValueError, KeyError, TypeError:
        return None


async def _load_from_valkey() -> Snapshot | None:
    if _valkey is None:
        return None
    try:
        raw = await _valkey.get(SNAPSHOT_KEY)
    except Exception as exc:
        logger.warning("⚠️ Genre tree snapshot could not be read from Valkey", error_type=type(exc).__name__)
        return None
    return _decode(raw) if raw else None


async def get_genre_tree(_driver: Any = None) -> list[dict[str, Any]]:
    """The stored tree, for the route and the NLQ tool. Never touches Neo4j.

    Raises:
        GenreTreeNotReadyError: nothing is stored yet.
    """
    global _snapshot
    if _snapshot is None:
        stored = await _load_from_valkey()
        if stored is not None:
            _snapshot = stored
    if _snapshot is None:
        raise GenreTreeNotReadyError("the genre tree has not been built yet")
    return _snapshot.genres


async def _store(snapshot: Snapshot) -> None:
    global _snapshot
    _snapshot = snapshot
    if _valkey is None:
        return
    try:
        await _valkey.set(SNAPSHOT_KEY, _encode(snapshot))
    except Exception as exc:
        logger.warning("⚠️ Genre tree snapshot could not be stored in Valkey", error_type=type(exc).__name__)


async def _take_lock() -> bool:
    if _valkey is None:
        return True
    try:
        return bool(await _valkey.set(LOCK_KEY, "1", nx=True, ex=LOCK_SECONDS))
    except Exception:
        return True  # Valkey down: build locally rather than serve nothing


async def _release_lock() -> None:
    if _valkey is None:
        return
    try:
        await _valkey.delete(LOCK_KEY)
    except Exception as exc:
        logger.debug("genre tree lock release failed", error_type=type(exc).__name__)


async def refresh_once(driver: Any, *, max_age: float = DEFAULT_MAX_AGE_SECONDS, force: bool = False) -> bool:
    """Rebuild the tree if it is missing, stale or its counts changed. Returns whether it rebuilt.

    A failure is logged and swallowed: the previous tree keeps serving.
    """
    global _snapshot
    try:
        stored = await _load_from_valkey()
        if stored is not None and (_snapshot is None or stored.built_at > _snapshot.built_at):
            _snapshot = stored  # another replica built a newer one
        signature = await genre_tree_queries.get_genre_signature(driver)
        held = _snapshot
        if not force and held is not None and held.signature == signature and (time.time() - held.built_at) < max_age:
            return False
        if not await _take_lock():
            return False
        try:
            started = time.monotonic()
            genres = await genre_tree_queries.build_genre_tree(driver)
            await _store(Snapshot(genres=genres, signature=signature, built_at=time.time()))
            logger.info("🌳 Genre tree rebuilt", genres=len(genres), seconds=round(time.monotonic() - started, 1))
            return True
        finally:
            await _release_lock()
    except Exception as exc:
        logger.error(
            "❌ Genre tree rebuild failed; the previous tree keeps serving", error_type=type(exc).__name__, alert="genre_tree_refresh_failed"
        )
        return False


RETRY_INITIAL_SECONDS: Final = 30.0
RETRY_MAX_SECONDS: Final = 300.0


async def run_refresh_loop(
    driver: Any,
    *,
    interval: float,
    max_age: float = DEFAULT_MAX_AGE_SECONDS,
    retry_initial: float = RETRY_INITIAL_SECONDS,
    retry_max: float = RETRY_MAX_SECONDS,
) -> None:
    """Check at once, then every ``interval`` seconds, until cancelled.

    While no tree exists anywhere (the first build failed, or another replica holds the lock and has
    stored nothing yet) the route answers 503, so it retries on a short backoff, doubling from
    ``retry_initial`` to ``retry_max``, instead of waiting a full ``interval``. Once a tree is stored it
    returns to ``interval``.
    """
    backoff = retry_initial
    while True:
        await refresh_once(driver, max_age=max_age)
        if _snapshot is None:
            delay = min(backoff, interval)
            backoff = min(backoff * 2, retry_max)
        else:
            delay = interval
            backoff = retry_initial
        await asyncio.sleep(delay)
