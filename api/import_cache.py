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
the strata catalog and personnel entries, and the expand pages with their budget index. On the first run, with no marker, it records the
version and deletes nothing, since an empty cache has nothing stale in it.

It invalidates only when the enricher has finished maintaining the stored counts (`maintained_at` on the version's
``ExtractionCompletion`` node): a cache refilled earlier would hold pre-maintenance counts for its whole TTL. The arrival of
``stats_version`` (the artist and label sweeps) is a new state and invalidates once more. The budget indexes of the budgeted caches
(`budget:<cache>:index`, api/cache_budget.py) are reset with the pages they count.

It is always on and cheap: one indexed lookup per poll, and a SCAN only when a new import lands.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Final

import structlog

from api.cache_budget import index_key
from api.queries.helpers import run_single
from api.reattach_trigger import _LATCH_PRESENT, _LATEST_COMPLETE, DISCOGS_DATA_TYPES, _scalar
from api.telemetry import CACHE_EXPAND, CACHE_EXPLORE, CACHE_STRATA, CACHE_TRENDS
from api.valkey_keys import valkey_key, valkey_pattern, with_legacy_keys, with_legacy_patterns


logger = structlog.get_logger(__name__)

_MARKER_KEY: Final = valkey_key("import_cache:discogs_version")
# When the watcher first found a version loaded in both stores but not maintained: "<version>|<epoch seconds>". Kept in Valkey so
# a restart does not reset the clock, and cleared once the version is maintained.
_WAIT_KEY: Final = valkey_key("import_cache:maintenance_wait_since")
# A maintenance pass that has not finished this long after the load is stuck, or the enricher is not running. The watcher
# logs an error with an alert for it on every poll after the threshold, so a pass that stalls does not sit unnoticed.
MAINTENANCE_WAIT_ALERT_SECONDS: Final = 2 * 3600
# expand:* holds the paged /api/explore expansions and their budget index (expand:index, a sorted set),
# which the same pattern covers, so the index is reset with the pages it counts.
# enrichment:status is the cached /api/enrichment/status coverage (api/routers/musicbrainz.py): its Postgres and Neo4j counts
# move with an import too, so it is dropped with the rest rather than served stale for its TTL.
_INVALIDATED_FAMILIES: Final = ("explore:*", "trends:*", "expand:*", "enrichment:status")
# Each budgeted cache keeps its live-key index at <prefix>budget:<cache>:index (api/cache_budget.py). It lives outside the
# families above, so it is deleted by name: left behind, a full budget stays full for up to 24 h after the pages it counts
# are gone, and every new write is refused.
_BUDGETED_CACHES: Final = (CACHE_EXPLORE, CACHE_TRENDS, CACHE_STRATA, CACHE_EXPAND)


async def latest_discogs_version(pool: Any) -> str | None:
    """The newest complete Discogs extraction's version, or ``None`` before any import completes."""
    async with pool.connection() as conn, conn.cursor() as cur:
        if not await _scalar(cur, _LATCH_PRESENT):
            return None
        version = await _scalar(cur, _LATEST_COMPLETE, (list(DISCOGS_DATA_TYPES),))
    return None if version is None else str(version)


_GRAPH_LATCH: Final = """
MATCH (m:ExtractionCompletion {version: $version})
RETURN coalesce(m.signals, []) AS signals,
       m.maintained_at IS NOT NULL AS maintained,
       m.stats_version AS stats_version
"""


async def _graph_latch(driver: Any, version: str) -> dict[str, Any] | None:
    if driver is None:
        return None
    return await run_single(driver, _GRAPH_LATCH, version=version)


async def graph_extraction_complete(driver: Any, version: str) -> bool:
    """Whether the graph has every data type's extraction_complete for this version.

    The SQL latch and the Neo4j counts are written by different services. graphinator records
    each data type's signal in Neo4j after that type's queue has reached the signal, so all four
    means the graph's writes for this extraction have landed. Until then the counts can still
    move, so the watcher waits.
    """
    record = await _graph_latch(driver, version)
    signals = set(record["signals"]) if record else set()
    return set(DISCOGS_DATA_TYPES) <= signals


async def graph_maintenance(driver: Any, version: str) -> tuple[bool, int | None]:
    """Whether the enricher has finished maintaining the stored counts for this version, and its ``stats_version``.

    ``maintained_at`` is written after the enricher's maintenance pass, which is what makes ``Genre.release_count`` and the other
    stored counts final. A cache refilled before that holds pre-maintenance counts for its whole 24 h TTL, so the watcher waits for
    it. ``stats_version`` is set later, once the enricher's artist and label sweeps (``primary_genre``, ``HAS_STYLE``) finish; it is
    part of the marker so that its arrival invalidates once more.
    """
    record = await _graph_latch(driver, version)
    if record is None:
        return False, None
    stats_version = record.get("stats_version")
    return bool(record.get("maintained")), None if stats_version is None else int(stats_version)


async def invalidate_explore_and_trends(valkey: Any) -> int:
    """Delete every Explore and trends key by SCAN, and return how many were deleted."""
    deleted = 0
    # Built per call: the key prefix is configuration, not a constant of the module.
    for index in with_legacy_keys([index_key(cache) for cache in _BUDGETED_CACHES]):
        deleted += int(await valkey.delete(index))
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


async def _note_maintenance_wait(valkey: Any, version: str) -> None:
    """Remember when this version started waiting on the enricher, and raise an alert once it has waited for hours."""
    raw = await valkey.get(_WAIT_KEY)
    text = raw.decode() if isinstance(raw, bytes) else raw
    since: float | None = None
    if text and text.split("|", 1)[0] == version:
        try:
            since = float(text.split("|", 1)[1])
        except IndexError, ValueError:
            since = None
    now = time.time()
    if since is None:
        await valkey.set(_WAIT_KEY, f"{version}|{now}")
        return
    waited = now - since
    if waited >= MAINTENANCE_WAIT_ALERT_SECONDS:
        logger.error(
            "❌ Import cache is waiting on the enricher's maintenance pass; Explore and trends counts stay stale until it finishes",
            version=version,
            waited_hours=round(waited / 3600, 1),
            alert="import_cache_maintenance_stuck",
        )


async def check_import_cache(pool: Any, valkey: Any, driver: Any) -> bool:
    """One poll: invalidate once a new import is complete in both stores. Returns whether it invalidated.

    Both gates must pass for the same version: the SQL latch (the loader is done) and the graph's
    own latch (the Neo4j counts are done). A poll that finds the SQL side done but the graph still
    loading does nothing and leaves the marker alone, so the next poll after the graph finishes
    invalidates. That re-invalidation is what keeps caches from refilling from a half-loaded graph.
    """
    version = await latest_discogs_version(pool)
    if version is None:
        return False
    if not await graph_extraction_complete(driver, version):
        return False
    # The counts a cache would be refilled with must be final: wait for the enricher's maintenance pass. A poll that finds the
    # graph loaded but not yet maintained leaves the marker alone, so the poll after maintenance invalidates.
    maintained, stats_version = await graph_maintenance(driver, version)
    if not maintained:
        await _note_maintenance_wait(valkey, version)
        return False
    await valkey.delete(_WAIT_KEY)
    # stats_version (the artist and label sweeps) arrives after maintained_at on the same node; its arrival is a new state.
    state = version if stats_version is None else f"{version}:{stats_version}"
    stored = await valkey.get(_MARKER_KEY)
    stored_text = stored.decode() if isinstance(stored, bytes) else stored
    if stored_text == state:
        return False
    if stored_text is not None:
        deleted = await invalidate_explore_and_trends(valkey)
        logger.info("🧹 Import completed; Explore and trends caches invalidated", version=version, state=state, deleted=deleted)
    await valkey.set(_MARKER_KEY, state)
    return stored_text is not None


async def run_import_cache_watch(pool: Any, valkey: Any, driver: Any, interval: int) -> None:
    """Poll every *interval* seconds; a failure is logged and retried next poll. Re-raises ``CancelledError``."""
    while True:
        try:
            await check_import_cache(pool, valkey, driver)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error("❌ Import cache watch failed; retrying next poll", exc_info=True)
        await asyncio.sleep(interval)
