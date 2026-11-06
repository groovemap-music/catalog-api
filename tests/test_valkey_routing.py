"""Catalog caches go to the evicting instance and auth state to the noeviction one (gm-catalog-api-kj2d)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import fakeredis
import pytest
from valkey.exceptions import ConnectionError as ValkeyConnectionError
from valkey.exceptions import OutOfMemoryError, ReadOnlyError
from valkey.exceptions import TimeoutError as ValkeyTimeoutError

import api.api as api_module
from api.cache_budget import cost_key, index_key, store_within_budget
from api.config import get_cache_valkey_url
from api.import_cache import invalidate_explore_and_trends
from api.valkey_keys import valkey_key, valkey_key_prefix
from api.valkey_routing import AUTH_FAMILIES, CACHE_FAMILIES, RoutedValkey, is_cache_name


API_ROOT = Path(__file__).resolve().parents[1] / "api"

# The auth keys the review of gm-deployment-t52v named: losing one silently changes who may do what.
NEVER_CACHE = [
    "revoked:jti:abc",
    "password_changed:uid-1",
    "reset:token",
    "2fa_challenge:jti",
    "snapshot:tok",
    "snapshot:usercount:u",
    "discogs:oauth:state:s",
]


@pytest.fixture(autouse=True)
def _namespaced(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run with a key prefix, as a deployment does: without one an unprefixed name is not a legacy key."""
    monkeypatch.setenv("VALKEY_KEY_PREFIX", "groovemap:")


def _pair() -> tuple[Any, Any, RoutedValkey]:
    auth = fakeredis.FakeAsyncRedis(decode_responses=True)
    cache = fakeredis.FakeAsyncRedis(decode_responses=True)
    return auth, cache, RoutedValkey(auth, cache)


class TestWhichInstance:
    @pytest.mark.parametrize("stem", NEVER_CACHE)
    def test_auth_state_can_never_reach_the_cache_client(self, stem: str) -> None:
        assert not is_cache_name(valkey_key(stem))
        assert not is_cache_name(stem), "a legacy, unprefixed copy lives on the auth instance only"

    @pytest.mark.parametrize("stem", ["revoked:jti:", "password_changed:", "reset:", "2fa_challenge:"])
    def test_no_cache_family_can_be_mistaken_for_an_auth_family(self, stem: str) -> None:
        assert not any(family.startswith(stem) or stem.startswith(family) for family in CACHE_FAMILIES)

    @pytest.mark.parametrize(
        "stem",
        ["explore:artist:x", "trends:genre:y", "expand:abc", "search:d", "recommend:explore:u:1", "budget:explore:index", "explore-hub:v1:lock:a:1"],
    )
    def test_catalog_caches_go_to_the_cache_instance(self, stem: str) -> None:
        assert is_cache_name(valkey_key(stem))
        assert not is_cache_name(stem), "an unprefixed name is a legacy key, which only the auth instance can hold"

    def test_a_glob_that_names_no_cache_family_scans_the_auth_instance(self) -> None:
        assert not is_cache_name(valkey_key("*"))
        assert is_cache_name(valkey_key("explore:*"))

    def test_every_key_family_in_the_source_is_classified(self) -> None:
        stems: set[str] = set()
        for source in API_ROOT.rglob("*.py"):
            for match in re.finditer(r'valkey_key\(\s*f?"([^"]*)"', source.read_text()):
                stem = match.group(1).split("{")[0]
                if stem:
                    stems.add(stem)
        assert stems, "the scan found no key families, so it is not looking at the source"
        unclassified = sorted(stem for stem in stems if not stem.startswith((*CACHE_FAMILIES, *AUTH_FAMILIES)))
        assert unclassified == [], f"decide whether {unclassified} are cache or auth state in api/valkey_routing.py"

    def test_the_two_lists_do_not_overlap(self) -> None:
        assert not [c for c in CACHE_FAMILIES for a in AUTH_FAMILIES if c.startswith(a) or a.startswith(c)]


class TestPlacement:
    @pytest.mark.asyncio
    async def test_each_key_lands_only_on_its_own_instance(self) -> None:
        auth, cache, routed = _pair()

        await routed.setex(valkey_key("explore:artist:x"), 60, "v")
        await routed.set(valkey_key("insights:data-completeness"), "v")
        await routed.setex(valkey_key("revoked:jti:abc"), 60, "1")
        await routed.setex(valkey_key("password_changed:u"), 60, "1")
        await routed.set(valkey_key("sync:lock:u"), "1", nx=True, ex=30)

        assert sorted(await cache.keys("*")) == sorted([valkey_key("explore:artist:x"), valkey_key("insights:data-completeness")])
        assert sorted(await auth.keys("*")) == sorted([valkey_key("revoked:jti:abc"), valkey_key("password_changed:u"), valkey_key("sync:lock:u")])
        assert await routed.get(valkey_key("explore:artist:x")) == "v"
        assert await routed.get(valkey_key("revoked:jti:abc")) == "1"

    @pytest.mark.asyncio
    async def test_a_legacy_unprefixed_key_stays_on_the_auth_instance(self) -> None:
        auth, cache, routed = _pair()
        await auth.set("recommend:enhanced:u", "old")

        assert await routed.get("recommend:enhanced:u") == "old"
        assert await routed.delete("recommend:enhanced:u") == 1
        assert await cache.keys("*") == []

    @pytest.mark.asyncio
    async def test_delete_and_scan_split_by_key(self) -> None:
        auth, cache, routed = _pair()
        await cache.set(valkey_key("explore:a"), "1")
        await auth.set(valkey_key("sync:cooldown:u"), "1")

        assert await routed.delete(valkey_key("explore:a"), valkey_key("sync:cooldown:u")) == 2
        await cache.set(valkey_key("explore:b"), "1")
        cursor, keys = await routed.scan(cursor=0, match=valkey_pattern_explore(), count=100)
        assert sorted(keys) == [valkey_key("explore:b")]
        assert cursor == 0

    @pytest.mark.asyncio
    async def test_a_script_runs_where_its_first_key_lives(self) -> None:
        auth, cache, routed = _pair()
        auth.eval = AsyncMock(return_value="auth")  # type: ignore[method-assign]
        cache.eval = AsyncMock(return_value="cache")  # type: ignore[method-assign]

        assert await routed.eval("return 1", 1, valkey_key("explore-hub:v1:lock:a:1"), "t") == "cache"
        assert await routed.eval("return 1", 1, valkey_key("sync:lock:u"), "t") == "auth"
        assert await routed.eval("return 1", 0) == "auth"

    @pytest.mark.asyncio
    async def test_other_attributes_and_closing_reach_both_instances(self) -> None:
        auth, cache, routed = _pair()
        assert await routed.ping() is True
        cache.aclose = AsyncMock()  # type: ignore[method-assign]
        auth.aclose = AsyncMock()  # type: ignore[method-assign]
        await routed.aclose()
        cache.aclose.assert_awaited_once()
        auth.aclose.assert_awaited_once()


def valkey_pattern_explore() -> str:
    return f"{valkey_key_prefix()}explore:*"


FAILURES = [ValkeyConnectionError("down"), ValkeyTimeoutError("slow"), OutOfMemoryError("OOM command not allowed"), ReadOnlyError("read only")]


class TestACacheThatFailsIsAMiss:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("failure", FAILURES, ids=lambda f: type(f).__name__)
    async def test_cache_commands_become_misses_and_never_raise(self, failure: Exception) -> None:
        _auth, cache, routed = _pair()
        for command in (
            "get",
            "set",
            "setex",
            "delete",
            "scan",
            "zadd",
            "zrem",
            "zscore",
            "zcard",
            "zrange",
            "zrangebyscore",
            "zremrangebyscore",
            "expire",
            "exists",
            "pttl",
            "getdel",
            "eval",
        ):
            setattr(cache, command, AsyncMock(side_effect=failure))
        name = valkey_key("explore:x")

        assert await routed.get(name) is None
        assert await routed.setex(name, 60, "v") is None
        assert await routed.set(name, "v") is None
        assert await routed.delete(name) == 0
        assert await routed.scan(cursor=0, match=valkey_pattern_explore()) == (0, [])
        assert await routed.zcard(name) == 0
        assert await routed.zrange(name, 0, 0) == []
        assert await routed.zscore(name, "m") is None
        assert await routed.eval("return 1", 1, name) == 0
        assert await routed.pttl(name) == -2

    @pytest.mark.asyncio
    async def test_auth_commands_still_raise_so_auth_failures_stay_visible(self) -> None:
        auth, _cache, routed = _pair()
        auth.setex = AsyncMock(side_effect=OutOfMemoryError("OOM command not allowed"))  # type: ignore[method-assign]

        with pytest.raises(OutOfMemoryError):
            await routed.setex(valkey_key("revoked:jti:abc"), 60, "1")

    @pytest.mark.asyncio
    async def test_a_down_cache_does_not_touch_auth_writes(self) -> None:
        auth, cache, routed = _pair()
        cache.setex = AsyncMock(side_effect=ValkeyConnectionError("down"))  # type: ignore[method-assign]

        await routed.setex(valkey_key("explore:x"), 60, "v")
        await routed.setex(valkey_key("password_changed:u"), 60, "1")

        assert await auth.get(valkey_key("password_changed:u")) == "1"

    @pytest.mark.asyncio
    async def test_a_programming_error_is_not_swallowed(self) -> None:
        _auth, cache, routed = _pair()
        cache.get = AsyncMock(side_effect=TypeError("bug"))  # type: ignore[method-assign]
        with pytest.raises(TypeError):
            await routed.get(valkey_key("explore:x"))


class TestBudgetAndInvalidationOnTheCacheInstance:
    @pytest.mark.asyncio
    async def test_a_full_index_whose_cost_set_was_evicted_does_not_shut_the_cache(self) -> None:
        _auth, cache, routed = _pair()
        for number in range(3):
            assert await store_within_budget(routed, cache="explore", key=valkey_key(f"explore:k{number}"), value="v", ttl=300, max_keys=3, cost=5)
        await cache.delete(cost_key("explore"))  # allkeys-lru dropped it; the index survived

        stored = await store_within_budget(routed, cache="explore", key=valkey_key("explore:new"), value="v", ttl=300, max_keys=3, cost=5)

        assert stored is True
        assert await cache.get(valkey_key("explore:new")) == "v"

    @pytest.mark.asyncio
    async def test_a_missing_index_is_an_empty_one(self) -> None:
        _auth, cache, routed = _pair()
        assert await store_within_budget(routed, cache="explore", key=valkey_key("explore:a"), value="v", ttl=300, max_keys=2, cost=1)
        await cache.delete(index_key("explore"), cost_key("explore"))

        assert await store_within_budget(routed, cache="explore", key=valkey_key("explore:b"), value="v", ttl=300, max_keys=2, cost=1)

    @pytest.mark.asyncio
    async def test_import_invalidation_clears_the_cache_instance_and_leaves_auth_alone(self) -> None:
        auth, cache, routed = _pair()
        for stem in ("explore:a", "trends:b", "expand:c", "enrichment:status"):
            await cache.set(valkey_key(stem), "v")
        await cache.zadd(index_key("explore"), {valkey_key("explore:a"): 1})
        await auth.set(valkey_key("revoked:jti:abc"), "1")
        await auth.set(valkey_key("import_cache:discogs_version"), "v")

        deleted = await invalidate_explore_and_trends(routed)

        assert deleted >= 5
        assert await cache.keys("*") == []
        assert sorted(await auth.keys("*")) == sorted([valkey_key("revoked:jti:abc"), valkey_key("import_cache:discogs_version")])


class TestConfigurationAndStartup:
    def test_unset_means_no_cache_instance(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CATALOG_CACHE_VALKEY_HOST", raising=False)
        assert get_cache_valkey_url() is None

    def test_host_and_port_are_all_an_operator_sets(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CATALOG_CACHE_VALKEY_HOST", "valkey-cache")
        monkeypatch.setenv("CATALOG_CACHE_VALKEY_PORT", "6380")
        monkeypatch.setenv("VALKEY_PASSWORD", "p@ss/word")
        monkeypatch.delenv("VALKEY_PASSWORD_FILE", raising=False)
        monkeypatch.delenv("VALKEY_TLS", raising=False)
        monkeypatch.setenv("VALKEY_DB", "0")

        assert get_cache_valkey_url() == "valkey://:p%40ss%2Fword@valkey-cache:6380/0"

    @pytest.mark.asyncio
    async def test_unset_the_service_keeps_its_one_client_exactly_as_before(self) -> None:
        config = MagicMock(valkey_url="valkey://valkey:6379/0", cache_valkey_url=None, jwt_expire_minutes=30)
        client = MagicMock()
        with (
            patch.object(api_module.aiovalkey, "from_url", AsyncMock(return_value=client)) as from_url,
            patch.object(api_module, "prepare_legacy_keys", AsyncMock()),
        ):
            created = await api_module._create_valkey(config)

        assert not isinstance(created, RoutedValkey)
        from_url.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_set_the_service_routes_between_two_clients(self) -> None:
        config = MagicMock(valkey_url="valkey://valkey:6379/0", cache_valkey_url="valkey://valkey-cache:6379/0", jwt_expire_minutes=30)
        with (
            patch.object(api_module.aiovalkey, "from_url", AsyncMock(side_effect=[MagicMock(), MagicMock()])) as from_url,
            patch.object(api_module, "prepare_legacy_keys", AsyncMock()),
        ):
            created = await api_module._create_valkey(config)

        assert from_url.await_count == 2
        assert [call.args[0] for call in from_url.await_args_list] == ["valkey://valkey:6379/0", "valkey://valkey-cache:6379/0"]
        assert created.__class__.__name__ == "RoutedValkey"


class TestStrictMode:
    """A caller that must not mistake an unreachable cache for an empty one gets the error."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("failure", FAILURES, ids=lambda f: type(f).__name__)
    async def test_the_strict_view_raises_where_the_default_serves_a_miss(self, failure: Exception) -> None:
        _auth, cache, routed = _pair()
        for command in ("get", "delete", "scan", "eval"):
            setattr(cache, command, AsyncMock(side_effect=failure))
        name = valkey_key("explore:x")
        strict = routed.strict()

        assert await routed.get(name) is None
        assert await routed.delete(name) == 0
        assert await routed.scan(cursor=0, match=valkey_pattern_explore()) == (0, [])
        for call in (
            strict.get(name),
            strict.delete(name),
            strict.scan(cursor=0, match=valkey_pattern_explore()),
            strict.eval("return 1", 1, name),
        ):
            with pytest.raises(type(failure)):
                await call

    @pytest.mark.asyncio
    async def test_the_strict_view_still_routes_and_reaches_the_same_instances(self) -> None:
        auth, cache, routed = _pair()
        strict = routed.strict()

        await strict.setex(valkey_key("explore:a"), 60, "v")
        await strict.setex(valkey_key("revoked:jti:j"), 60, "1")

        assert await cache.get(valkey_key("explore:a")) == "v"
        assert await auth.get(valkey_key("revoked:jti:j")) == "1"
        assert strict.auth is routed.auth
        assert strict.cache is routed.cache

    def test_with_no_cache_instance_the_client_is_returned_unchanged(self) -> None:
        from api.valkey_routing import strict_valkey

        plain = object()
        assert strict_valkey(plain) is plain
        _auth, _cache, routed = _pair()
        assert strict_valkey(routed)._strict is True


class TestErasureAndInvalidationFailWithTheCacheDown:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("failure", FAILURES, ids=lambda f: type(f).__name__)
    async def test_an_erasure_does_not_report_complete_while_user_data_sits_on_the_cache_instance(self, failure: Exception) -> None:
        from api.routers import activity as activity_router

        _auth, cache, routed = _pair()
        await cache.set(valkey_key("recommend:enhanced:u1"), "recommendations")
        for command in ("scan", "delete", "get"):
            setattr(cache, command, AsyncMock(side_effect=failure))
        with patch.object(activity_router, "_valkey", routed):
            result = await activity_router._erase_valkey("u1")

        assert result is not None, "the erasure reported complete without being able to look at the cache instance"
        assert "Valkey deletion failed" in result

    @pytest.mark.asyncio
    async def test_the_survivor_count_raises_rather_than_counting_zero(self) -> None:
        from api.routers import activity as activity_router

        _auth, cache, routed = _pair()
        cache.scan = AsyncMock(side_effect=ValkeyConnectionError("down"))  # type: ignore[method-assign]

        with pytest.raises(ValkeyConnectionError):
            await activity_router._surviving_recommendation_keys(routed, "u1")

    @pytest.mark.asyncio
    async def test_an_erasure_with_the_cache_up_still_completes_and_removes_the_keys(self) -> None:
        from api.routers import activity as activity_router

        auth, cache, routed = _pair()
        await cache.set(valkey_key("recommend:enhanced:u1"), "recommendations")
        await cache.set(valkey_key("recommend:explore:u1:artist:1:2"), "recommendations")
        await auth.set(valkey_key("sync:lock:u1"), "1")
        with patch.object(activity_router, "_valkey", routed):
            result = await activity_router._erase_valkey("u1")

        assert result is None
        assert await cache.keys("*") == []
        assert await auth.keys("*") == []

    @pytest.mark.asyncio
    async def test_the_import_invalidation_raises_so_the_marker_is_not_written(self) -> None:
        from api import import_cache

        auth, cache, routed = _pair()
        await cache.set(valkey_key("explore:a"), "stale")
        await auth.set(import_cache._MARKER_KEY, "1")
        cache.scan = AsyncMock(side_effect=ValkeyConnectionError("down"))  # type: ignore[method-assign]
        with (
            patch.object(import_cache, "latest_discogs_version", AsyncMock(return_value="2")),
            patch.object(import_cache, "graph_extraction_complete", AsyncMock(return_value=True)),
            patch.object(import_cache, "graph_maintenance", AsyncMock(return_value=(True, None))),
            pytest.raises(ValkeyConnectionError),
        ):
            await import_cache.check_import_cache(AsyncMock(), routed, None)

        assert await auth.get(import_cache._MARKER_KEY) == "1", "the import was recorded as handled while stale entries remain"

    @pytest.mark.asyncio
    async def test_the_next_poll_invalidates_once_the_cache_is_back(self) -> None:
        from api import import_cache

        auth, cache, routed = _pair()
        await cache.set(valkey_key("explore:a"), "stale")
        await auth.set(import_cache._MARKER_KEY, "1")
        patches = (
            patch.object(import_cache, "latest_discogs_version", AsyncMock(return_value="2")),
            patch.object(import_cache, "graph_extraction_complete", AsyncMock(return_value=True)),
            patch.object(import_cache, "graph_maintenance", AsyncMock(return_value=(True, None))),
        )
        with patches[0], patches[1], patches[2]:
            real_scan = cache.scan
            cache.scan = AsyncMock(side_effect=ValkeyConnectionError("down"))  # type: ignore[method-assign]
            with pytest.raises(ValkeyConnectionError):
                await import_cache.check_import_cache(AsyncMock(), routed, None)
            cache.scan = real_scan  # type: ignore[method-assign]
            assert await import_cache.check_import_cache(AsyncMock(), routed, None) is True

        assert await cache.keys("*") == []
        assert await auth.get(import_cache._MARKER_KEY) == "2"


def _with_info(client: Any, *, policy: str, keys: int, evicted: int = 0) -> None:
    """fakeredis has no INFO; answer the three sections the storage query reads."""
    sections = {
        "memory": {"used_memory_human": "1M", "used_memory_peak_human": "2M", "maxmemory_policy": policy},
        "keyspace": {"db0": {"keys": keys}},
        "stats": {"evicted_keys": evicted},
    }
    client.info = AsyncMock(side_effect=lambda section: sections[section])


class TestAdminStorageIncludesTheCacheInstance:
    @pytest.mark.asyncio
    async def test_the_cache_instance_has_its_own_numbers_beside_the_auth_ones(self) -> None:
        from api.queries.admin_queries import get_valkey_storage

        auth, cache, routed = _pair()
        await auth.set(valkey_key("revoked:jti:a"), "1")
        for number in range(3):
            await cache.set(valkey_key(f"explore:{number}"), "v")
        _with_info(auth, policy="noeviction", keys=1)
        _with_info(cache, policy="allkeys-lru", keys=3, evicted=7)

        storage = await get_valkey_storage(routed)

        assert storage["status"] == "ok"
        assert storage["total_keys"] == 1
        assert storage["keys_by_prefix"] == {"revoked:": 1}
        assert storage["cache_instance"]["status"] == "ok"
        assert storage["cache_instance"]["total_keys"] == 3
        assert storage["cache_instance"]["keys_by_prefix"] == {"explore:": 3}
        assert storage["maxmemory_policy"] == "noeviction"
        assert storage["cache_instance"]["maxmemory_policy"] == "allkeys-lru"
        assert storage["cache_instance"]["evicted_keys"] == 7
        assert storage["evicted_keys"] == 0

    @pytest.mark.asyncio
    async def test_an_unreachable_cache_instance_is_reported_and_does_not_fail_the_page(self) -> None:
        from api.queries.admin_queries import get_valkey_storage

        auth, cache, routed = _pair()
        await auth.set(valkey_key("revoked:jti:a"), "1")
        _with_info(auth, policy="noeviction", keys=1)
        cache.info = AsyncMock(side_effect=ValkeyConnectionError("down"))  # type: ignore[method-assign]

        storage = await get_valkey_storage(routed)

        assert storage["status"] == "ok"
        assert storage["total_keys"] == 1
        assert storage["cache_instance"] == {"status": "unavailable", "error": "ConnectionError"}

    @pytest.mark.asyncio
    async def test_with_one_instance_the_response_has_no_cache_section(self) -> None:
        from api.queries.admin_queries import get_valkey_storage

        auth, _cache, _routed = _pair()
        await auth.set(valkey_key("revoked:jti:a"), "1")
        _with_info(auth, policy="noeviction", keys=1)

        storage = await get_valkey_storage(auth)

        assert "cache_instance" not in storage
        assert storage["total_keys"] == 1
