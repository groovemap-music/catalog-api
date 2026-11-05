"""Drop the Explore and trends caches when a Discogs import completes.

Both caches hold for 24 hours, and their counts come from the graph as it was when they were
written. A load that finishes inside that window therefore leaves stale counts on the page
(gm-catalog-api-64bl: Bowie showed 8,850 releases while the graph held 14,698). Nothing else
invalidates them, so this watcher does.

The completion signal is the newest complete Discogs extraction in
``public.loader_extraction_latch``, the same row the automatic re-attachment trigger reads
(`api/reattach_trigger.py`). The loader writes it for every writer that counts, so no loader
change is needed. The watcher keeps the version it last acted on in Valkey. When the newest
complete version differs, it deletes every ``explore:*`` and ``trends:*`` key, which includes
the strata catalog and personnel entries. On the first run, with no marker, it records the
version and deletes nothing, since an empty cache has nothing stale in it.

It is always on and cheap: one indexed lookup per poll, and a SCAN only when a new import lands.
"""

from __future__ import annotations

import asyncio
from typing import Any, Final

import structlog

from api.reattach_trigger import _LATCH_PRESENT, _LATEST_COMPLETE, DISCOGS_DATA_TYPES, _scalar
from api.valkey_keys import valkey_key, valkey_pattern, with_legacy_patterns


logger = structlog.get_logger(__name__)

_MARKER_KEY: Final = valkey_key("import_cache:discogs_version")
_INVALIDATED_FAMILIES: Final = ("explore:*", "trends:*")


async def latest_discogs_version(pool: Any) -> str | None:
    """The newest complete Discogs extraction's version, or ``None`` before any import completes."""
    async with pool.connection() as conn, conn.cursor() as cur:
        if not await _scalar(cur, _LATCH_PRESENT):
            return None
        version = await _scalar(cur, _LATEST_COMPLETE, (list(DISCOGS_DATA_TYPES),))
    return None if version is None else str(version)


async def invalidate_explore_and_trends(valkey: Any) -> int:
    """Delete every Explore and trends key by SCAN, and return how many were deleted."""
    deleted = 0
    patterns = with_legacy_patterns([valkey_pattern(family) for family in _INVALIDATED_FAMILIES])
    for pattern in patterns:
        cursor: str | int = "0"
        while True:
            cursor, keys = await valkey.scan(cursor=int(cursor), match=pattern, count=500)
            if keys:
                deleted += int(await valkey.delete(*keys))
            if str(cursor) == "0":
                break
    return deleted


async def check_import_cache(pool: Any, valkey: Any) -> bool:
    """One poll: invalidate when a new complete import has landed. Returns whether it invalidated."""
    version = await latest_discogs_version(pool)
    if version is None:
        return False
    stored = await valkey.get(_MARKER_KEY)
    stored_text = stored.decode() if isinstance(stored, bytes) else stored
    if stored_text == version:
        return False
    if stored_text is not None:
        deleted = await invalidate_explore_and_trends(valkey)
        logger.info("🧹 Import completed; Explore and trends caches invalidated", version=version, deleted=deleted)
    await valkey.set(_MARKER_KEY, version)
    return stored_text is not None


async def run_import_cache_watch(pool: Any, valkey: Any, interval: int) -> None:
    """Poll every *interval* seconds; a failure is logged and retried next poll. Re-raises ``CancelledError``."""
    while True:
        try:
            await check_import_cache(pool, valkey)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error("❌ Import cache watch failed; retrying next poll", exc_info=True)
        await asyncio.sleep(interval)
