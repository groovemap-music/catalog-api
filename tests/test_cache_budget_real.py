"""The Explore, trends and strata key budget holds on a real Valkey (``gm-catalog-api-57d6``).

Opt-in like ``test_valkey_noeviction_real.py``: set ``CATALOG_VALKEY_INTEGRATION=1`` (``just
integration-valkey``) with Docker available. Walks many distinct names against a throwaway Valkey and
checks that the number of keys stays at the budget, that a response body is the same with the cache, a
full cache and no cache, and that a full budget is reported as its own state, not as a Valkey failure.
"""

from __future__ import annotations

import asyncio
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
        return [{"year": 2000, "count": len(name)}]

    handler = getattr(explore_router, route)
    names = [f"Name {i}" for i in range(WALK)]

    async def walk(client: Any) -> list[bytes]:
        with (
            patch.object(explore_router, "_neo4j_driver", object()),
            patch.object(explore_router, "_valkey", client),
            patch.dict(getattr(explore_router, dispatch), {entity_type: fake_query}),
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
    assert await valkey.dbsize() == BUDGET + 1  # the budgeted keys and their index
