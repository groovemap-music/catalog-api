"""Catalog caches and auth state on two REAL Valkey instances (gm-catalog-api-kj2d).

Opt-in like ``test_valkey_noeviction_real.py`` (``just integration-valkey``): throwaway containers from the
production image, a 4 MB cap on each. The auth instance is ``noeviction``; the cache instance is
``allkeys-lru``, as deployment's ``docker-compose.cache-isolation.yml`` runs them. The routing client is
the one the service builds, over real clients.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
import pytest_asyncio
import valkey.asyncio as aiovalkey
from valkey.exceptions import OutOfMemoryError

from api.cache_budget import store_within_budget
from api.import_cache import invalidate_explore_and_trends
from api.valkey_keys import valkey_key
from api.valkey_routing import RoutedValkey
from tests.valkey_noeviction_services import (
    OPT_IN_ENV,
    VALKEY_MAXMEMORY_BYTES,
    Container,
    DockerUnavailableError,
    ValkeyFiller,
    require_docker,
    start_cache_valkey,
    start_valkey,
)


pytestmark = [pytest.mark.integration, pytest.mark.valkey_noeviction, pytest.mark.asyncio(loop_scope="function")]

VALUE = "x" * 2048


@dataclass
class Pair:
    auth: aiovalkey.Valkey
    cache: aiovalkey.Valkey
    routed: aiovalkey.Valkey
    auth_container: Container
    cache_container: Container


async def _keys(client: aiovalkey.Valkey, pattern: str = "*") -> list[str]:
    return sorted([key async for key in client.scan_iter(match=pattern, count=500)])


async def _evicted(client: aiovalkey.Valkey) -> int:
    return int((await client.info("stats"))["evicted_keys"])


@pytest_asyncio.fixture
async def pair(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Pair]:
    if os.environ.get(OPT_IN_ENV) != "1":
        pytest.skip(f"set {OPT_IN_ENV}=1 (just integration-valkey)")
    try:
        require_docker()
    except DockerUnavailableError as exc:
        pytest.skip(str(exc))
    monkeypatch.setenv("VALKEY_KEY_PREFIX", "groovemap:")
    auth_container = start_valkey()
    try:
        cache_container = start_cache_valkey()
    except Exception:
        auth_container.remove()
        raise
    auth = aiovalkey.from_url(f"valkey://{auth_container.host}:{auth_container.port}/0", decode_responses=True)
    cache = aiovalkey.from_url(
        f"valkey://{cache_container.host}:{cache_container.port}/0", decode_responses=True, socket_timeout=2, socket_connect_timeout=2
    )
    try:
        yield Pair(auth, cache, RoutedValkey(auth, cache), auth_container, cache_container)  # type: ignore[arg-type]
    finally:
        await auth.aclose()
        await cache.aclose()
        auth_container.remove()
        cache_container.remove()


async def test_filling_the_cache_instance_leaves_auth_writes_working(pair: Pair) -> None:
    await pair.routed.setex(valkey_key("revoked:jti:before"), 3600, "1")
    stored = 0
    for number in range(4000):  # about 8 MB of values against the 4 MB cap
        await pair.routed.setex(valkey_key(f"explore:artist:name-{number}"), 3600, VALUE)
        stored += 1
    evicted = await _evicted(pair.cache)
    # Auth writes of every family the review named, after the cache overfilled.
    for stem in ("revoked:jti:after", "password_changed:u1", "reset:t1", "2fa_challenge:j1", "sync:lock:u1"):
        await pair.routed.setex(valkey_key(stem), 3600, "1")

    print(
        f"\nPLACEMENT: {stored} cache writes accepted with 0 errors; cache evicted {evicted} keys; auth instance evicted {await _evicted(pair.auth)}"
    )
    assert evicted > 0, "the cache instance really overfilled and evicted"
    assert await _evicted(pair.auth) == 0
    assert await pair.routed.get(valkey_key("revoked:jti:before")) == "1"
    assert await pair.routed.get(valkey_key("password_changed:u1")) == "1"
    assert await _keys(pair.auth, valkey_key("explore:*")) == []
    assert await _keys(pair.cache, valkey_key("revoked:*")) == []
    assert await _keys(pair.cache, valkey_key("password_changed:*")) == []
    assert len(await _keys(pair.auth)) == 6
    assert int((await pair.auth.info("memory"))["used_memory"]) < VALKEY_MAXMEMORY_BYTES


async def test_a_full_auth_instance_still_rejects_auth_writes_but_never_touches_cache_writes(pair: Pair) -> None:
    filler = ValkeyFiller(pair.auth)
    await filler.fill()
    try:
        await pair.routed.setex(valkey_key("explore:artist:ok"), 60, "v")
        assert await pair.routed.get(valkey_key("explore:artist:ok")) == "v"
        with pytest.raises(OutOfMemoryError):
            await pair.routed.setex(valkey_key("password_changed:u"), 60, "1")
    finally:
        await filler.drain()


async def test_a_cache_instance_that_is_down_gives_misses_and_auth_is_unaffected(pair: Pair) -> None:
    await pair.routed.setex(valkey_key("explore:artist:x"), 60, "v")
    pair.cache_container.remove()

    miss = await pair.routed.get(valkey_key("explore:artist:x"))
    write = await pair.routed.setex(valkey_key("explore:artist:y"), 60, "v")
    stored = await store_within_budget(pair.routed, cache="explore", key=valkey_key("explore:artist:z"), value="v", ttl=60, cost=3)
    scan = await pair.routed.scan(cursor=0, match=valkey_key("explore:*"), count=100)
    deleted = await pair.routed.delete(valkey_key("explore:artist:x"))
    await pair.routed.setex(valkey_key("revoked:jti:still"), 60, "1")

    print(
        f"\nDOWN: cache get -> {miss!r}, cache write -> {write!r}, budgeted write -> {stored!r}, scan -> {scan!r}, delete -> {deleted}; auth write OK"
    )
    assert miss is None
    assert write is None
    assert isinstance(stored, bool), "a budgeted write on a down cache answers, it does not raise"
    assert scan == (0, [])
    assert deleted == 0
    assert await pair.routed.get(valkey_key("revoked:jti:still")) == "1"


async def test_import_invalidation_clears_the_cache_instance(pair: Pair) -> None:
    for stem in ("explore:artist:a", "trends:genre:g", "expand:e1"):
        await pair.routed.setex(valkey_key(stem), 3600, "v")
    assert await store_within_budget(pair.routed, cache="explore", key=valkey_key("explore:artist:b"), value="v", ttl=3600, cost=5)
    await pair.routed.set(valkey_key("import_cache:discogs_version"), "v1")
    await pair.routed.setex(valkey_key("revoked:jti:keep"), 3600, "1")
    before = len(await _keys(pair.cache))

    deleted = await invalidate_explore_and_trends(pair.routed)

    print(
        f"\nINVALIDATION: {before} keys on the cache instance, {deleted} deleted, {len(await _keys(pair.cache))} left; auth keys {len(await _keys(pair.auth))}"
    )
    assert deleted >= 4
    assert await _keys(pair.cache) == []
    assert await _keys(pair.auth) == sorted([valkey_key("import_cache:discogs_version"), valkey_key("revoked:jti:keep")])
