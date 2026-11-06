"""A global budget on how many keys a Valkey cache may hold at once.

The Valkey instance is ``noeviction`` and shared with auth state, and the key of every Explore, trends
and strata entry is chosen by the caller, so the caches grow with whatever distinct names are walked
(gm-catalog-api-57d6; ``/api/expand`` got the same budget in gm-catalog-api-9d8z). Each cache keeps an
index of its live keys, a sorted set scored by expiry, and a second sorted set scored by each key's cost
(the deterministic size of the work behind the value). Once the index is full a write is skipped, unless
its cost is strictly greater than the lightest live entry's: then that lightest entry is removed and the
heavier one takes its slot (gm-catalog-api-yac9), so a caller who filled the budget with the cheapest
results that clear the admission rule cannot keep a heavy entry out for the rest of the TTL.

A skipped write changes nothing a caller can see: the response is the one it would have got, just not
stored. A spent budget is reported as its own state, ``groovemap.api.cache.budget_full``, and an eviction
as ``groovemap.api.cache.budget_evictions``; neither is a Valkey failure, so neither pages anyone or fills
the log.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import structlog

from api.telemetry import record_cache_budget_evicted, record_cache_budget_full
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


def cost_key(cache: str) -> str:
    """The sorted-set key that scores ``cache``'s live keys by cost; it lives and dies with :func:`index_key`'s."""
    return valkey_key(f"budget:{cache}:cost")


# A candidate that turns out to be gone already (another writer took it, or its entry had expired) is skipped; this many are
# tried before the write is refused as if the budget were full.
_EVICTION_ATTEMPTS = 5


async def _evict_lightest_below(valkey: Any, index: str, costs: str, cost: int) -> bool:
    """Remove the lightest live entry if it is strictly cheaper than ``cost``; whether a slot was freed.

    ``ZREM`` on the cost set is the claim: of several writers that picked the same entry, one gets ``1`` and goes on to
    remove it, so a slot is freed once, not once per writer. An entry that was in the cost set but no longer in the index
    freed no slot and is dropped before the next candidate is tried.
    """
    for _ in range(_EVICTION_ATTEMPTS):
        lightest = await valkey.zrange(costs, 0, 0, withscores=True)
        if not lightest:
            # The index is full yet the cost set is empty. Both are written together with one TTL, so the only way
            # here is an evicting cache instance (allkeys-lru) dropping the cost set while the index survived.
            # Nothing can be ranked, so the index is forgotten and the slot is free: what it named is still
            # served until its own TTL, just uncounted. Refusing the write instead would hold the cache shut
            # until the index expired.
            if await valkey.zcard(index) and not await valkey.exists(costs):
                await valkey.delete(index)
                return True
            return False
        victim, victim_cost = lightest[0]
        if cost <= victim_cost:
            return False
        if await valkey.zrem(costs, victim) != 1:
            continue
        if await valkey.zrem(index, victim) == 1:
            await valkey.delete(victim)
            return True
    return False


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

    When the budget is full and ``cost`` is strictly greater than the lightest live entry's, that entry is
    evicted and this write takes its slot (gm-catalog-api-yac9). A cost equal to or below the lightest never
    displaces anything, so a cheaper result cannot push out a heavier one and equal results do not churn. A write
    without a ``cost`` counts as cost ``0``: it never evicts, and it is the first to go.
    """
    size = len(value.encode("utf-8")) if isinstance(value, str) else len(value)
    if len(key) > MAX_KEY_LENGTH or size > max_value_bytes:
        return False
    if cost is not None and cost < min_cost:
        return False
    index = index_key(cache)
    costs = cost_key(cache)
    now = time.time()
    expired = await valkey.zrangebyscore(index, "-inf", now)
    if expired:
        await valkey.zrem(costs, *expired)
        await valkey.zremrangebyscore(index, "-inf", now)
    weight = 0 if cost is None else cost
    if await valkey.zscore(index, key) is None and await valkey.zcard(index) >= max_keys:
        if not await _evict_lightest_below(valkey, index, costs, weight):
            record_cache_budget_full(cache)
            return False
        record_cache_budget_evicted(cache)
    await valkey.zadd(index, {key: now + ttl})
    await valkey.zadd(costs, {key: weight})
    await valkey.expire(index, ttl)
    await valkey.expire(costs, ttl)
    await valkey.setex(key, ttl, value)
    return True
