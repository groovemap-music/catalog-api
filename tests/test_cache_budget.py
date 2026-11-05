"""Unit tests for the cache key budget (the real-Valkey proof is ``test_cache_budget_real.py``)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import fakeredis as fake_store
import pytest

from api import cache_budget
from api.routers import explore


def _valkey(*, live: int, holds_key: bool = False) -> AsyncMock:
    valkey = AsyncMock()
    valkey.zscore = AsyncMock(return_value=1.0 if holds_key else None)
    valkey.zcard = AsyncMock(return_value=live)
    valkey.zrangebyscore = AsyncMock(return_value=[])
    valkey.zrange = AsyncMock(return_value=[])  # nothing lighter to evict
    return valkey


@pytest.mark.asyncio
async def test_a_spent_budget_skips_the_write_and_reports_its_own_state() -> None:
    valkey = _valkey(live=5)
    with patch.object(cache_budget, "record_cache_budget_full") as full:
        stored = await cache_budget.store_within_budget(valkey, cache="c", key="k", value="v", ttl=60, max_keys=5)

    assert stored is False
    valkey.setex.assert_not_called()
    valkey.zadd.assert_not_called()
    full.assert_called_once_with("c")


@pytest.mark.asyncio
async def test_a_budget_with_room_writes_and_indexes_the_key() -> None:
    valkey = _valkey(live=4)
    with patch.object(cache_budget, "record_cache_budget_full") as full:
        stored = await cache_budget.store_within_budget(valkey, cache="c", key="k", value="v", ttl=60, max_keys=5)

    assert stored is True
    valkey.setex.assert_awaited_once_with("k", 60, "v")
    assert valkey.zadd.await_args.args[1].keys() == {"k"}
    full.assert_not_called()


@pytest.mark.asyncio
async def test_a_held_key_is_rewritten_even_when_full() -> None:
    valkey = _valkey(live=5, holds_key=True)
    assert await cache_budget.store_within_budget(valkey, cache="c", key="k", value="v", ttl=60, max_keys=5) is True


@pytest.mark.asyncio
async def test_a_valkey_error_propagates_for_the_callers_failure_handler() -> None:
    valkey = _valkey(live=0)
    valkey.setex = AsyncMock(side_effect=ConnectionError("down"))
    with pytest.raises(ConnectionError):
        await cache_budget.store_within_budget(valkey, cache="c", key="k", value="v", ttl=60)


def test_the_budgets() -> None:
    assert cache_budget.DEFAULT_MAX_KEYS == 10_000
    assert explore._STRATA_CACHE_MAX_KEYS == 100


@pytest.mark.asyncio
async def test_an_oversized_value_or_key_is_not_stored_and_costs_no_budget() -> None:
    valkey = _valkey(live=0)
    with patch.object(cache_budget, "record_cache_budget_full") as full:
        assert await cache_budget.store_within_budget(valkey, cache="c", key="k", value="x" * 11_000, ttl=60) is False
        assert await cache_budget.store_within_budget(valkey, cache="c", key="k" * 300, value="v", ttl=60) is False
        assert await cache_budget.store_within_budget(valkey, cache="c", key="k", value="x" * 11_000, ttl=60, max_value_bytes=20_000) is True

    valkey.setex.assert_awaited_once()
    full.assert_not_called()


@pytest.mark.asyncio
async def test_a_cheap_result_is_not_stored_and_leaves_the_budget_alone() -> None:
    valkey = _valkey(live=5)
    with patch.object(cache_budget, "record_cache_budget_full") as full:
        stored = await cache_budget.store_within_budget(valkey, cache="c", key="k", value="v", ttl=60, max_keys=5, cost=3, min_cost=500)

    assert stored is False
    valkey.zadd.assert_not_called()
    valkey.setex.assert_not_called()
    full.assert_not_called()  # a cost skip is not a spent budget


@pytest.mark.asyncio
async def test_an_expensive_result_is_stored() -> None:
    valkey = _valkey(live=0)
    stored = await cache_budget.store_within_budget(valkey, cache="c", key="k", value="v", ttl=60, cost=100_000, min_cost=500)

    assert stored is True
    valkey.setex.assert_called_once()


@pytest.mark.asyncio
async def test_no_measured_cost_keeps_the_old_behaviour() -> None:
    valkey = _valkey(live=0)
    assert await cache_budget.store_within_budget(valkey, cache="c", key="k", value="v", ttl=60, min_cost=500) is True


def _store() -> fake_store.FakeAsyncValkey:
    return fake_store.FakeAsyncValkey(decode_responses=True)


async def _fill(valkey: fake_store.FakeAsyncValkey, count: int, cost: int, *, prefix: str = "k", max_keys: int = 5) -> None:
    for i in range(count):
        assert await cache_budget.store_within_budget(valkey, cache="c", key=f"{prefix}{i}", value="v", ttl=60, max_keys=max_keys, cost=cost)


@pytest.mark.asyncio
async def test_a_heavier_entry_evicts_the_lightest_one_when_the_budget_is_full() -> None:
    valkey = _store()
    for i, cost in enumerate([700, 900, 600, 800, 1000]):
        await cache_budget.store_within_budget(valkey, cache="c", key=f"k{i}", value="v", ttl=60, max_keys=5, cost=cost)

    with patch.object(cache_budget, "record_cache_budget_evicted") as evicted, patch.object(cache_budget, "record_cache_budget_full") as full:
        stored = await cache_budget.store_within_budget(valkey, cache="c", key="heavy", value="h", ttl=60, max_keys=5, cost=50_000)

    assert stored is True
    assert await valkey.get("heavy") == "h"
    assert await valkey.get("k2") is None  # cost 600 was the lightest
    assert {key async for key in valkey.scan_iter("k*")} == {"k0", "k1", "k3", "k4"}
    assert await valkey.zcard(cache_budget.index_key("c")) == 5  # the key budget still holds
    assert await valkey.zcard(cache_budget.cost_key("c")) == 5
    evicted.assert_called_once_with("c")
    full.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("cost", [100, 600, 599])
async def test_a_cheaper_or_equal_entry_cannot_displace_a_heavier_one(cost: int) -> None:
    valkey = _store()
    await _fill(valkey, 5, 600)

    with patch.object(cache_budget, "record_cache_budget_evicted") as evicted, patch.object(cache_budget, "record_cache_budget_full") as full:
        stored = await cache_budget.store_within_budget(valkey, cache="c", key="new", value="n", ttl=60, max_keys=5, cost=cost)

    assert stored is False
    assert await valkey.get("new") is None
    assert {key async for key in valkey.scan_iter("k*")} == {f"k{i}" for i in range(5)}
    evicted.assert_not_called()
    full.assert_called_once_with("c")


@pytest.mark.asyncio
async def test_a_write_without_a_cost_never_evicts_and_is_the_first_to_go() -> None:
    valkey = _store()
    await _fill(valkey, 4, 600)
    assert await cache_budget.store_within_budget(valkey, cache="c", key="uncosted", value="u", ttl=60, max_keys=5)

    assert await cache_budget.store_within_budget(valkey, cache="c", key="another", value="a", ttl=60, max_keys=5) is False
    assert await cache_budget.store_within_budget(valkey, cache="c", key="heavy", value="h", ttl=60, max_keys=5, cost=700) is True
    assert await valkey.get("uncosted") is None  # cost 0 was the lightest
    assert await valkey.get("k0") == "v"


@pytest.mark.asyncio
async def test_each_eviction_removes_the_current_lightest_in_turn() -> None:
    valkey = _store()
    await _fill(valkey, 3, 500, prefix="a", max_keys=3)
    for round_, cost in enumerate([600, 700, 800]):
        await cache_budget.store_within_budget(valkey, cache="c", key=f"b{round_}", value="v", ttl=60, max_keys=3, cost=cost)
        assert await valkey.zcard(cache_budget.index_key("c")) == 3
    assert {key async for key in valkey.scan_iter("*") if not key.startswith("groovemap")} >= {"b0", "b1", "b2"}
    assert await valkey.exists("a0", "a1", "a2") == 0


@pytest.mark.asyncio
async def test_a_refreshed_key_takes_its_new_cost_without_evicting() -> None:
    valkey = _store()
    await _fill(valkey, 5, 600)
    with patch.object(cache_budget, "record_cache_budget_evicted") as evicted:
        assert await cache_budget.store_within_budget(valkey, cache="c", key="k0", value="v2", ttl=60, max_keys=5, cost=5000)
    evicted.assert_not_called()
    assert await valkey.zscore(cache_budget.cost_key("c"), "k0") == 5000
    assert await valkey.zcard(cache_budget.index_key("c")) == 5


@pytest.mark.asyncio
async def test_expired_entries_leave_the_cost_set_with_the_index() -> None:
    valkey = _store()
    await valkey.zadd(cache_budget.index_key("c"), {"gone": 1.0})  # expired long ago
    await valkey.zadd(cache_budget.cost_key("c"), {"gone": 900})
    assert await cache_budget.store_within_budget(valkey, cache="c", key="k", value="v", ttl=60, max_keys=5, cost=700)

    assert await valkey.zscore(cache_budget.cost_key("c"), "gone") is None
    assert await valkey.zcard(cache_budget.index_key("c")) == 1


@pytest.mark.asyncio
async def test_a_ghost_in_the_cost_set_is_dropped_and_the_next_lightest_is_evicted() -> None:
    """A cost entry whose index entry is gone freed no slot; it must not stand in for a real eviction."""
    valkey = _store()
    await _fill(valkey, 5, 600)
    await valkey.zadd(cache_budget.cost_key("c"), {"ghost": 1})

    assert await cache_budget.store_within_budget(valkey, cache="c", key="heavy", value="h", ttl=60, max_keys=5, cost=700)

    assert await valkey.zscore(cache_budget.cost_key("c"), "ghost") is None
    assert await valkey.zcard(cache_budget.index_key("c")) == 5  # one real entry was evicted, so the budget held
    assert await valkey.get("heavy") == "h"


@pytest.mark.asyncio
async def test_concurrent_heavier_writers_stay_within_a_writer_count_of_the_budget() -> None:
    import asyncio

    valkey = _store()
    await _fill(valkey, 5, 600)
    results = await asyncio.gather(
        *[cache_budget.store_within_budget(valkey, cache="c", key=f"w{i}", value="v", ttl=60, max_keys=5, cost=1000 + i) for i in range(8)]
    )

    assert any(results)
    assert await valkey.zcard(cache_budget.index_key("c")) <= 5 + len(results)
    assert await valkey.zcard(cache_budget.cost_key("c")) == await valkey.zcard(cache_budget.index_key("c"))
