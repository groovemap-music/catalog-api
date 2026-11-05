"""The Explore, trends and strata key budget holds on a real Valkey (``gm-catalog-api-57d6``).

Opt-in like ``test_valkey_noeviction_real.py``: set ``CATALOG_VALKEY_INTEGRATION=1`` (``just
integration-valkey``) with Docker available. Walks many distinct names against a throwaway Valkey and
checks that the number of keys stays at the budget, that a response body is the same with the cache, a
full cache and no cache, and that a full budget is reported as its own state, not as a Valkey failure.
"""

from __future__ import annotations

import asyncio
import dataclasses
import functools
import os
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import patch

import pytest
import pytest_asyncio
import valkey.asyncio as aiovalkey

import api.routers.explore as explore_router
from api import cache_budget
from tests.valkey_noeviction_services import OPT_IN_ENV, Container, DockerUnavailableError, require_docker, start_valkey


pytestmark = [pytest.mark.integration, pytest.mark.valkey_noeviction, pytest.mark.asyncio]

BUDGET = 20
WALK = 120


@pytest.fixture(scope="module")
def container() -> Iterator[Container]:
    if os.environ.get(OPT_IN_ENV) != "1":
        pytest.skip(f"opt-in: set {OPT_IN_ENV}=1 (use `just integration-valkey`)")
    try:
        require_docker()
    except DockerUnavailableError as exc:
        pytest.skip(str(exc))
    started = start_valkey()
    try:
        yield started
    finally:
        started.remove()


@pytest_asyncio.fixture
async def valkey(container: Container) -> AsyncIterator[Any]:
    client = aiovalkey.Valkey(host=container.host, port=container.port, decode_responses=True)
    await client.flushall()
    try:
        yield client
    finally:
        await client.aclose()


async def _keys(client: Any, prefix: str) -> list[str]:
    return [key async for key in client.scan_iter(f"{prefix}*")]


async def test_a_walk_of_distinct_keys_stops_at_the_budget(valkey: Any) -> None:
    results = [await cache_budget.store_within_budget(valkey, cache="t", key=f"t:{i}", value=f"v{i}", ttl=300, max_keys=BUDGET) for i in range(WALK)]

    assert results.count(True) == BUDGET
    assert results[:BUDGET] == [True] * BUDGET  # the first writes are the ones kept
    assert len(await _keys(valkey, "t:")) == BUDGET
    assert await valkey.zcard(cache_budget.index_key("t")) == BUDGET


async def test_a_key_already_held_is_refreshed_when_the_budget_is_full(valkey: Any) -> None:
    for i in range(BUDGET):
        await cache_budget.store_within_budget(valkey, cache="t", key=f"t:{i}", value="old", ttl=300, max_keys=BUDGET)

    assert await cache_budget.store_within_budget(valkey, cache="t", key="t:3", value="new", ttl=300, max_keys=BUDGET) is True
    assert await valkey.get("t:3") == "new"
    assert await cache_budget.store_within_budget(valkey, cache="t", key="t:extra", value="x", ttl=300, max_keys=BUDGET) is False
    assert await valkey.get("t:extra") is None


async def test_expired_keys_free_budget(valkey: Any) -> None:
    for i in range(BUDGET):
        await cache_budget.store_within_budget(valkey, cache="t", key=f"t:{i}", value="v", ttl=1, max_keys=BUDGET)
    assert await cache_budget.store_within_budget(valkey, cache="t", key="t:late", value="v", ttl=300, max_keys=BUDGET) is False

    await asyncio.sleep(2.2)

    assert await cache_budget.store_within_budget(valkey, cache="t", key="t:late", value="v", ttl=300, max_keys=BUDGET) is True
    assert await _keys(valkey, "t:") == ["t:late"]


async def test_each_cache_has_its_own_budget(valkey: Any) -> None:
    for i in range(BUDGET + 5):
        await cache_budget.store_within_budget(valkey, cache="a", key=f"a:{i}", value="v", ttl=300, max_keys=BUDGET)
    assert await cache_budget.store_within_budget(valkey, cache="b", key="b:0", value="v", ttl=300, max_keys=BUDGET) is True


@pytest.mark.parametrize(
    ("route", "prefix", "entity_type", "dispatch"),
    [("explore", "explore:", "artist", "EXPLORE_DISPATCH"), ("get_trends", "trends:", "genre", "TRENDS_DISPATCH")],
)
async def test_the_routes_stay_bounded_and_answer_the_same(valkey: Any, route: str, prefix: str, entity_type: str, dispatch: str) -> None:
    async def fake_query(_driver: Any, name: str) -> Any:
        if route == "explore":
            return {"id": name, "name": name, "release_count": 5, "label_count": 2, "alias_count": 1}
        return [{"year": 2000, "count": 5}]  # one cost for every name, so none is heavy enough to displace another

    handler = getattr(explore_router, route)
    names = [f"Name {i}" for i in range(WALK)]

    async def walk(client: Any) -> list[bytes]:
        with (
            patch.object(explore_router, "_neo4j_driver", object()),
            patch.object(explore_router, "_valkey", client),
            patch.dict(getattr(explore_router, dispatch), {entity_type: fake_query}),
            patch.object(explore_router, "_MIN_CACHE_RELEASES", 0),  # the fakes have no release counts; the cost rule has its own tests
            patch.object(explore_router, "store_within_budget", functools.partial(cache_budget.store_within_budget, max_keys=BUDGET)),
            patch.object(cache_budget, "record_cache_budget_full") as budget_full,
            patch("api.telemetry.record_cache_write_failure") as write_failure,
        ):
            bodies = [(await handler(name=name, type=entity_type)).body for name in names]
        if client is not None:
            assert budget_full.call_count == WALK - BUDGET  # reported as its own state
            write_failure.assert_not_called()  # and never as a Valkey failure
        return bodies

    with_cache = await walk(valkey)
    keys_after_walk = len(await _keys(valkey, prefix))
    again = await walk(valkey)  # budget full, and the first BUDGET names now answer from the cache
    without_cache = await walk(None)

    assert keys_after_walk == BUDGET
    assert len(await _keys(valkey, prefix)) == BUDGET
    assert with_cache == again == without_cache  # bodies identical with a cache, a full cache and no cache


async def test_a_noeviction_valkey_never_sees_more_than_the_budget(valkey: Any) -> None:
    with patch.object(cache_budget, "record_cache_budget_full"):
        for i in range(2000):
            await cache_budget.store_within_budget(valkey, cache="t", key=f"t:{i}", value="x" * 200, ttl=300, max_keys=BUDGET)
    assert await valkey.dbsize() == BUDGET + 2  # the budgeted keys, their index and their cost set


HEAVY = 100_000  # releases behind a heavy root
CHEAP = 3  # releases behind a stub name


async def test_a_walk_of_cheap_roots_leaves_the_slots_to_heavy_ones(valkey: Any) -> None:
    """A client walking distinct cheap names (60/min, so ~100 in two minutes) must not fill strata's budget."""
    admit = functools.partial(cache_budget.store_within_budget, valkey, cache="strata", ttl=300, max_keys=BUDGET, min_cost=500)

    cheap = [await admit(key=f"strata:cheap{i}", value="c", cost=CHEAP) for i in range(WALK)]
    heavy = [await admit(key=f"strata:heavy{i}", value="h", cost=HEAVY) for i in range(BUDGET)]

    assert not any(cheap)  # a cheap root is recomputed cheaper than a slot is worth
    assert all(heavy)  # and every heavy root is still cacheable afterwards
    assert len(await _keys(valkey, "strata:cheap")) == 0
    assert len(await _keys(valkey, "strata:heavy")) == BUDGET
    assert await valkey.zcard(cache_budget.index_key("strata")) == BUDGET  # bounds from 57d6 unchanged


async def test_cheap_roots_never_register_as_a_spent_budget(valkey: Any) -> None:
    """A skip for cost is not ``budget_full``, so only real exhaustion of the budget raises the alert."""
    with patch.object(cache_budget, "record_cache_budget_full") as full:
        for i in range(WALK):
            await cache_budget.store_within_budget(valkey, cache="t", key=f"t:{i}", value="v", ttl=300, max_keys=BUDGET, cost=CHEAP, min_cost=500)
        full.assert_not_called()
        for i in range(BUDGET + 3):
            await cache_budget.store_within_budget(valkey, cache="t", key=f"t:h{i}", value="v", ttl=300, max_keys=BUDGET, cost=HEAVY, min_cost=500)
        assert full.call_count == 3  # three heavy writes met a genuinely full budget


async def test_the_strata_route_stores_only_a_catalog_that_was_slow_to_build(valkey: Any) -> None:
    from api.routers.explore import _store_strata_catalog, _strata_cache_key
    from tests.test_explore_strata import CATALOG  # a small real Catalog

    with patch.object(explore_router, "_valkey", valkey):
        await _store_strata_catalog(dataclasses.replace(CATALOG, root_id="cheap"), CHEAP)
        await _store_strata_catalog(dataclasses.replace(CATALOG, root_id="heavy"), HEAVY)

    assert await valkey.exists(_strata_cache_key("cheap")) == 0
    assert await valkey.exists(_strata_cache_key("heavy")) == 1


async def test_without_the_cost_rule_the_same_walk_locks_heavy_roots_out(valkey: Any) -> None:
    """The failure the rule fixes (the 57d6 behaviour): the same walk, admitting everything."""
    admit = functools.partial(cache_budget.store_within_budget, valkey, cache="strata", ttl=300, max_keys=BUDGET)
    with patch.object(cache_budget, "record_cache_budget_full"):
        for i in range(WALK):
            await admit(key=f"strata:cheap{i}", value="c")
        heavy = [await admit(key=f"strata:heavy{i}", value="h") for i in range(BUDGET)]

    assert not any(heavy)  # every heavy root refused, recomputed on each request for the whole TTL


async def test_latency_plays_no_part_in_admission(valkey: Any) -> None:
    """A client cannot buy a slot by slowing a cheap name down: the cost is the data's, not the clock's."""
    import time

    admit = functools.partial(cache_budget.store_within_budget, valkey, cache="strata", ttl=300, max_keys=BUDGET, min_cost=500)
    started = time.monotonic()
    await asyncio.sleep(1.5)  # a cheap root made slow by load
    assert await admit(key="strata:slow-but-cheap", value="c", cost=CHEAP) is False
    assert time.monotonic() - started >= 1.5


@pytest.mark.parametrize(
    ("route", "prefix", "entity_type", "dispatch"),
    [("explore", "explore:", "artist", "EXPLORE_DISPATCH"), ("get_trends", "trends:", "genre", "TRENDS_DISPATCH")],
)
async def test_the_routes_walk_of_cheap_names_leaves_heavy_names_cacheable(
    valkey: Any, route: str, prefix: str, entity_type: str, dispatch: str
) -> None:
    """The real routes, the real admission rule (500 releases), a real Valkey: no patching of the threshold."""

    async def fake_query(_driver: Any, name: str) -> Any:
        releases = 50_000 if name.startswith("Heavy") else 3
        if route == "explore":
            return {"id": name, "name": name, "release_count": releases, "label_count": 2, "alias_count": 1}
        return [{"year": 2000, "count": releases}]

    handler = getattr(explore_router, route)
    with (
        patch.object(explore_router, "_neo4j_driver", object()),
        patch.object(explore_router, "_valkey", valkey),
        patch.dict(getattr(explore_router, dispatch), {entity_type: fake_query}),
        patch.object(explore_router, "store_within_budget", functools.partial(cache_budget.store_within_budget, max_keys=BUDGET)),
        patch.object(cache_budget, "record_cache_budget_full") as budget_full,
    ):
        cheap = [(await handler(name=f"Cheap {i}", type=entity_type)).body for i in range(WALK)]
        assert len(await _keys(valkey, prefix)) == 0  # the walk took no slot
        heavy = [(await handler(name=f"Heavy {i}", type=entity_type)).body for i in range(BUDGET)]
        assert len(await _keys(valkey, prefix)) == BUDGET  # and every heavy name was cached after it
        budget_full.assert_not_called()
        again = [(await handler(name=f"Heavy {i}", type=entity_type)).body for i in range(BUDGET)]

    assert heavy == again  # served from the cache, byte for byte
    assert len(cheap) == WALK


STRATA_SLOTS = 100  # the strata budget (api/routers/explore.py: _STRATA_CACHE_MAX_KEYS)


async def test_a_heavy_root_is_cached_after_100_lighter_500_plus_roots_fill_the_budget(valkey: Any) -> None:
    """gm-catalog-api-yac9, on a real Valkey: the 57d6 design refused Bowie for the whole TTL here."""
    admit = functools.partial(cache_budget.store_within_budget, valkey, cache="strata", ttl=300, max_keys=STRATA_SLOTS, min_cost=500)
    lighter = [await admit(key=f"strata:root{i}", value="r", cost=500 + i) for i in range(STRATA_SLOTS)]
    assert all(lighter)  # 100 real roots of 500+ releases fill every slot
    assert await admit(key="strata:extra-light", value="x", cost=500) is False  # a root no heavier than the lightest still waits

    bowie = await admit(key="strata:bowie", value="b", cost=39_000)

    assert bowie is True
    assert await valkey.get("strata:bowie") == "b"
    assert await valkey.exists("strata:root0") == 0  # cost 500 was the lightest, and is the one removed
    assert len(await _keys(valkey, "strata:root")) == STRATA_SLOTS - 1
    assert await valkey.zcard(cache_budget.index_key("strata")) == STRATA_SLOTS  # the 57d6 key bound holds
    assert await valkey.zcard(cache_budget.cost_key("strata")) == STRATA_SLOTS


async def test_a_cheaper_root_cannot_displace_a_heavier_one_on_real_valkey(valkey: Any) -> None:
    admit = functools.partial(cache_budget.store_within_budget, valkey, cache="strata", ttl=300, max_keys=BUDGET, min_cost=500)
    for i in range(BUDGET):
        assert await admit(key=f"strata:heavy{i}", value="h", cost=10_000 + i)

    with patch.object(cache_budget, "record_cache_budget_evicted") as evicted:
        refused = [await admit(key=f"strata:light{i}", value="l", cost=cost) for i, cost in enumerate([500, 9_999, 10_000])]

    assert refused == [False, False, False]  # cheaper, and equal to the lightest: neither displaces anything
    evicted.assert_not_called()
    assert len(await _keys(valkey, "strata:heavy")) == BUDGET


async def test_a_walk_of_ever_heavier_roots_keeps_the_heaviest_within_the_bounds(valkey: Any) -> None:
    admit = functools.partial(cache_budget.store_within_budget, valkey, cache="strata", ttl=300, max_keys=BUDGET, min_cost=500)
    for i in range(WALK):
        await admit(key=f"strata:r{i}", value="x" * 200, cost=1_000 + i)

    kept = sorted(int(key.removeprefix("strata:r")) for key in await _keys(valkey, "strata:r"))
    assert kept == list(range(WALK - BUDGET, WALK))  # the BUDGET heaviest of the walk
    assert await valkey.dbsize() == BUDGET + 2  # keys, index, cost set: never more


async def test_concurrent_writers_keep_the_key_and_byte_bounds(valkey: Any) -> None:
    """Many heavier writers at once: eviction is claimed with ZREM, so a slot is freed once, not once per writer."""
    admit = functools.partial(cache_budget.store_within_budget, valkey, cache="strata", ttl=300, max_keys=BUDGET, min_cost=500)
    for i in range(BUDGET):
        await admit(key=f"strata:base{i}", value="b" * 100, cost=1_000 + i)

    writers = 40
    results = await asyncio.gather(*[admit(key=f"strata:w{i}", value="w" * 100, cost=5_000 + i) for i in range(writers)])

    live = len(await _keys(valkey, "strata:"))
    assert any(results)
    assert live <= BUDGET + writers  # the 57d6 tolerance: about the writer count over, never unbounded
    assert await valkey.zcard(cache_budget.index_key("strata")) >= BUDGET
    # ... and the next uncontended write settles back to the budget instead of drifting higher
    for i in range(3):
        await admit(key=f"strata:settle{i}", value="s" * 100, cost=50_000 + i)
    assert len(await _keys(valkey, "strata:")) <= live + 3
    for key in await _keys(valkey, "strata:"):
        assert len(await valkey.get(key)) <= 100  # no value above the byte bound
