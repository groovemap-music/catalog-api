"""A global budget on how many keys a Valkey cache may hold at once.

The Valkey instance is ``noeviction`` and shared with auth state, and the key of every Explore, trends
and strata entry is chosen by the caller, so the caches grow with whatever distinct names are walked
(gm-catalog-api-57d6; ``/api/expand`` got the same budget in gm-catalog-api-9d8z). Each cache keeps an
index of its live keys, a sorted set scored by expiry, and a write is skipped once that index is full.

A skipped write changes nothing a caller can see: the response is the one it would have got, just not
stored. A spent budget is reported as its own state, ``groovemap.api.cache.budget_full``, and never
as a Valkey failure, so it does not page anyone or fill the log.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import structlog

from api.telemetry import record_cache_budget_full
from api.valkey_keys import valkey_key


if TYPE_CHECKING:
    from typing import Any


logger = structlog.get_logger(__name__)

# A key longer than this is never stored: every part of it is chosen by the caller.
MAX_KEY_LENGTH = 256

# Live keys one cache may hold. At about 10 KB an entry this is on the order of 100 MB.
DEFAULT_MAX_KEYS = 10_000

# The largest value one entry may hold. Together with the key budget this bounds a cache in bytes, not
# only in keys: 10,000 entries of at most 10 KB is 100 MB.
DEFAULT_MAX_VALUE_BYTES = 10_240


def index_key(cache: str) -> str:
    """The sorted-set key that indexes ``cache``'s live keys."""
    return valkey_key(f"budget:{cache}:index")


async def store_within_budget(
    valkey: Any,
    *,
    cache: str,
    key: str,
    value: str | bytes,
    ttl: int,
    max_keys: int = DEFAULT_MAX_KEYS,
    max_value_bytes: int = DEFAULT_MAX_VALUE_BYTES,
    cost: int | None = None,
    min_cost: int = 0,
) -> bool:
    """Write ``value`` under ``key`` for ``ttl`` seconds unless ``cache`` already holds ``max_keys`` live keys.

    An oversized value, or a key longer than ``MAX_KEY_LENGTH``, is not stored either: it returns ``False``
    without touching the budget. Expired index entries are dropped first, and a key already in the index is refreshed rather than
    counted again. Returns ``False`` when the budget is spent and nothing was written. A Valkey error
    propagates, for the caller's existing best-effort handler to count as a write failure.

    A key is chosen by the caller, so a budget with no admission rule is spent by whoever walks the most
    names, however cheap each is to recompute (gm-catalog-api-dcwo). A caller passes ``cost``, a
    deterministic size of the work behind the value (the release count of the entity it describes), never a
    measured latency, which a client could inflate with load; below ``min_cost`` the write is skipped and the
    budget is left alone, because recomputing it costs less than the slot is worth. The slots then go to
    results that are expensive to recompute, which is what a cache is for.

    The check and the write are separate commands, so concurrent writers can overshoot the budget by about
    their own number, which the budget tolerates; an index entry whose ``setex`` then fails expires with
    its TTL. A Lua script would close that gap but cannot run on the fake Valkey the unit tests use.
    """
    size = len(value.encode("utf-8")) if isinstance(value, str) else len(value)
    if len(key) > MAX_KEY_LENGTH or size > max_value_bytes:
        return False
    if cost is not None and cost < min_cost:
        return False
    index = index_key(cache)
    now = time.time()
    await valkey.zremrangebyscore(index, "-inf", now)
    if await valkey.zscore(index, key) is None and await valkey.zcard(index) >= max_keys:
        record_cache_budget_full(cache)
        return False
    await valkey.zadd(index, {key: now + ttl})
    await valkey.expire(index, ttl)
    await valkey.setex(key, ttl, value)
    return True
