"""Unit tests for the cache key budget (the real-Valkey proof is ``test_cache_budget_real.py``)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from api import cache_budget
from api.routers import explore


def _valkey(*, live: int, holds_key: bool = False) -> AsyncMock:
    valkey = AsyncMock()
    valkey.zscore = AsyncMock(return_value=1.0 if holds_key else None)
    valkey.zcard = AsyncMock(return_value=live)
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
