"""The Explore and trends caches are dropped once a new Discogs import is complete in both stores.

See api/import_cache.py. The SQL latch and the graph's own latch are both read here through
their functions, so each test patches the one it is not exercising.
"""

from unittest.mock import AsyncMock, patch

import fakeredis as fake_store
import pytest
import structlog
from common.config import valkey_key

import api.import_cache as import_cache
from api.cache_budget import index_key, store_within_budget
from api.telemetry import CACHE_EXPLORE, CACHE_STRATA, CACHE_TRENDS


_REAL_GRAPH_MAINTENANCE = import_cache.graph_maintenance


@pytest.fixture(autouse=True)
def graph_done() -> object:
    with (
        patch.object(import_cache, "graph_extraction_complete", AsyncMock(return_value=True)) as graph,
        patch.object(import_cache, "graph_maintenance", AsyncMock(return_value=(True, None))),
    ):
        yield graph


def _store() -> fake_store.FakeAsyncValkey:
    return fake_store.FakeAsyncValkey()


async def _get(valkey: fake_store.FakeAsyncValkey, key: str) -> str | None:
    value = await valkey.get(key)
    return value.decode() if isinstance(value, bytes) else value


async def _keys(valkey: fake_store.FakeAsyncValkey) -> list[str]:
    return sorted([key.decode() if isinstance(key, bytes) else key async for key in valkey.scan_iter()])


@pytest.mark.asyncio
async def test_a_new_import_drops_every_explore_and_trends_key_and_keeps_the_rest() -> None:
    valkey = _store()
    await valkey.set(valkey_key("explore:artist:David Bowie"), "{}")
    await valkey.set(valkey_key("explore:strata:10263"), "{}")
    await valkey.set(valkey_key("explore:strata:personnel:abc"), "[]")
    await valkey.set(valkey_key("trends:artist:Rock"), "{}")
    await valkey.set(valkey_key("recommend:explore:u1:x"), "{}")
    await valkey.set(import_cache._MARKER_KEY, "1")

    with patch.object(import_cache, "latest_discogs_version", AsyncMock(return_value="2")):
        assert await import_cache.check_import_cache(AsyncMock(), valkey, None) is True

    assert await _keys(valkey) == sorted([import_cache._MARKER_KEY, valkey_key("recommend:explore:u1:x")])
    assert await _get(valkey, import_cache._MARKER_KEY) == "2"


@pytest.mark.asyncio
async def test_expand_pages_and_their_budget_index_are_dropped_with_the_explore_caches() -> None:
    """Pre-import expand totals are the same stale counts as the Bowie page; the budget index counts those pages."""
    valkey = _store()
    await valkey.set(valkey_key("expand:3f2a9c"), "{}")
    await valkey.zadd(valkey_key("expand:index"), {valkey_key("expand:3f2a9c"): 9_999_999_999})
    await valkey.set(valkey_key("expand:unrelated-but-same-family"), "{}")
    await valkey.set(valkey_key("explore:artist:David Bowie"), "{}")
    await valkey.set(import_cache._MARKER_KEY, "1")

    with patch.object(import_cache, "latest_discogs_version", AsyncMock(return_value="2")):
        assert await import_cache.check_import_cache(AsyncMock(), valkey, None) is True

    assert await _keys(valkey) == sorted([import_cache._MARKER_KEY])


@pytest.mark.asyncio
async def test_auth_keys_survive_an_import_invalidation() -> None:
    """Revocations, password-reset tokens and password-change markers are security state, not cache."""
    valkey = _store()
    survivors = [
        valkey_key("revoked:jti:0f1e2d3c-aaaa-bbbb-cccc-ddddeeeeffff"),
        valkey_key("reset:4b2f0c9a7d1e"),
        valkey_key("password_changed:00000000-0000-0000-0000-000000000001"),
    ]
    for key in survivors:
        await valkey.set(key, "1")
    await valkey.set(valkey_key("explore:artist:David Bowie"), "{}")
    await valkey.set(import_cache._MARKER_KEY, "1")

    with patch.object(import_cache, "latest_discogs_version", AsyncMock(return_value="2")):
        assert await import_cache.check_import_cache(AsyncMock(), valkey, None) is True

    for key in survivors:
        assert await _get(valkey, key) == "1", key


@pytest.mark.asyncio
async def test_the_first_run_records_the_version_and_deletes_nothing() -> None:
    valkey = _store()
    await valkey.set(valkey_key("explore:artist:David Bowie"), "{}")

    with patch.object(import_cache, "latest_discogs_version", AsyncMock(return_value="1")):
        assert await import_cache.check_import_cache(AsyncMock(), valkey, None) is False

    assert await _get(valkey, valkey_key("explore:artist:David Bowie")) == "{}"
    assert await _get(valkey, import_cache._MARKER_KEY) == "1"


@pytest.mark.asyncio
async def test_the_same_version_on_a_later_poll_changes_nothing() -> None:
    valkey = _store()
    await valkey.set(import_cache._MARKER_KEY, "1")
    await valkey.set(valkey_key("explore:artist:David Bowie"), "{}")

    with patch.object(import_cache, "latest_discogs_version", AsyncMock(return_value="1")):
        assert await import_cache.check_import_cache(AsyncMock(), valkey, None) is False

    assert await _get(valkey, valkey_key("explore:artist:David Bowie")) == "{}"


@pytest.mark.asyncio
async def test_no_complete_import_yet_does_nothing() -> None:
    valkey = _store()
    await valkey.set(valkey_key("explore:artist:David Bowie"), "{}")

    with patch.object(import_cache, "latest_discogs_version", AsyncMock(return_value=None)):
        assert await import_cache.check_import_cache(AsyncMock(), valkey, None) is False

    assert await _get(valkey, import_cache._MARKER_KEY) is None
    assert await _get(valkey, valkey_key("explore:artist:David Bowie")) == "{}"


@pytest.mark.asyncio
async def test_a_finished_sql_load_waits_for_the_graph_then_invalidates_once() -> None:
    """The SQL latch can complete before the Neo4j counts do. Nothing is dropped until the graph is done too."""
    valkey = _store()
    await valkey.set(import_cache._MARKER_KEY, "1")
    await valkey.set(valkey_key("explore:artist:David Bowie"), '{"release_count": 8850}')

    with (
        patch.object(import_cache, "latest_discogs_version", AsyncMock(return_value="2")),
        patch.object(import_cache, "graph_extraction_complete", AsyncMock(return_value=False)),
    ):
        assert await import_cache.check_import_cache(AsyncMock(), valkey, None) is False
        assert await _get(valkey, valkey_key("explore:artist:David Bowie")) == '{"release_count": 8850}'
        assert await _get(valkey, import_cache._MARKER_KEY) == "1"

    with (
        patch.object(import_cache, "latest_discogs_version", AsyncMock(return_value="2")),
        patch.object(import_cache, "graph_extraction_complete", AsyncMock(return_value=True)),
    ):
        assert await import_cache.check_import_cache(AsyncMock(), valkey, None) is True
        assert await import_cache.check_import_cache(AsyncMock(), valkey, None) is False

    assert await _get(valkey, valkey_key("explore:artist:David Bowie")) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("cache", [CACHE_EXPLORE, CACHE_TRENDS, CACHE_STRATA])
async def test_a_full_budget_accepts_new_writes_after_invalidation(cache: str) -> None:
    """The budget index sits outside the invalidated families: left behind, a full cache stayed full and refused writes."""
    valkey = _store()
    for n in range(3):
        assert await store_within_budget(valkey, cache=cache, key=valkey_key(f"{cache}:k{n}"), value="{}", ttl=86400, max_keys=3)
    assert not await store_within_budget(valkey, cache=cache, key=valkey_key(f"{cache}:new"), value="{}", ttl=86400, max_keys=3)  # full
    assert await valkey.zcard(index_key(cache)) == 3
    await valkey.set(import_cache._MARKER_KEY, "1")

    with patch.object(import_cache, "latest_discogs_version", AsyncMock(return_value="2")):
        assert await import_cache.check_import_cache(AsyncMock(), valkey, None) is True

    assert await valkey.zcard(index_key(cache)) == 0
    assert await store_within_budget(valkey, cache=cache, key=valkey_key(f"{cache}:new"), value="{}", ttl=86400, max_keys=3)  # accepted


@pytest.mark.asyncio
async def test_the_watcher_waits_for_the_enricher_maintenance_before_invalidating() -> None:
    """A cache refilled before maintained_at holds pre-maintenance counts for its whole TTL."""
    valkey = _store()
    await valkey.set(import_cache._MARKER_KEY, "1")
    await valkey.set(valkey_key("explore:artist:David Bowie"), '{"release_count": 8850}')

    with (
        patch.object(import_cache, "latest_discogs_version", AsyncMock(return_value="2")),
        patch.object(import_cache, "graph_maintenance", AsyncMock(return_value=(False, None))),
    ):
        assert await import_cache.check_import_cache(AsyncMock(), valkey, None) is False
        assert await _get(valkey, import_cache._MARKER_KEY) == "1"  # untouched: the next poll tries again
    assert await _get(valkey, valkey_key("explore:artist:David Bowie")) == '{"release_count": 8850}'

    with patch.object(import_cache, "latest_discogs_version", AsyncMock(return_value="2")):
        assert await import_cache.check_import_cache(AsyncMock(), valkey, None) is True  # maintained now
    assert await _get(valkey, valkey_key("explore:artist:David Bowie")) is None


@pytest.mark.asyncio
async def test_the_arrival_of_stats_version_invalidates_once_more() -> None:
    """The artist and label sweeps (primary_genre, HAS_STYLE) finish after maintained_at; caches filled in between are stale."""
    valkey = _store()
    await valkey.set(import_cache._MARKER_KEY, "1")
    with patch.object(import_cache, "latest_discogs_version", AsyncMock(return_value="2")):
        assert await import_cache.check_import_cache(AsyncMock(), valkey, None) is True  # maintained, no stats_version yet
        assert await _get(valkey, import_cache._MARKER_KEY) == "2"
        await valkey.set(valkey_key("explore:artist:Filled in between"), "{}")
        assert await import_cache.check_import_cache(AsyncMock(), valkey, None) is False  # nothing new
        with patch.object(import_cache, "graph_maintenance", AsyncMock(return_value=(True, 1))):
            assert await import_cache.check_import_cache(AsyncMock(), valkey, None) is True
            assert await _get(valkey, import_cache._MARKER_KEY) == "2:1"
            assert await import_cache.check_import_cache(AsyncMock(), valkey, None) is False  # same state again
        assert await _get(valkey, valkey_key("explore:artist:Filled in between")) is None


@pytest.mark.asyncio
async def test_graph_maintenance_reads_the_marker_from_the_graph_node() -> None:
    record = {"signals": [], "maintained": True, "stats_version": 3}
    with patch.object(import_cache, "run_single", AsyncMock(return_value=record)):
        assert await _REAL_GRAPH_MAINTENANCE(object(), "2") == (True, 3)
    with patch.object(import_cache, "run_single", AsyncMock(return_value={"signals": [], "maintained": False, "stats_version": None})):
        assert await _REAL_GRAPH_MAINTENANCE(object(), "2") == (False, None)
    with patch.object(import_cache, "run_single", AsyncMock(return_value=None)):
        assert await _REAL_GRAPH_MAINTENANCE(object(), "2") == (False, None)
    assert await _REAL_GRAPH_MAINTENANCE(None, "2") == (False, None)


@pytest.mark.parametrize(("valkey", "neo4j", "reason"), [(None, object(), "no_valkey"), (object(), None, "no_neo4j")])
def test_a_watcher_that_cannot_start_is_reported_as_an_error_with_an_alert(valkey: object, neo4j: object, reason: str) -> None:
    from types import SimpleNamespace

    from api.api import _start_import_cache_watch

    app = SimpleNamespace(state=SimpleNamespace())
    with structlog.testing.capture_logs() as logs:
        _start_import_cache_watch(app, SimpleNamespace(import_cache_watch_interval=60), object(), valkey, neo4j)  # type: ignore[arg-type]
    assert app.state.import_cache_task is None
    assert [(entry["log_level"], entry["alert"], entry["reason"]) for entry in logs] == [("error", "import_cache_watch_not_started", reason)]


@pytest.mark.asyncio
async def test_a_version_waiting_hours_on_maintenance_raises_an_alert_and_a_short_wait_does_not() -> None:
    valkey = _store()
    await valkey.set(import_cache._MARKER_KEY, "1")
    with (
        patch.object(import_cache, "latest_discogs_version", AsyncMock(return_value="2")),
        patch.object(import_cache, "graph_maintenance", AsyncMock(return_value=(False, None))),
    ):
        with structlog.testing.capture_logs() as first:
            assert await import_cache.check_import_cache(AsyncMock(), valkey, None) is False  # the clock starts
        assert not [entry for entry in first if entry.get("alert")]
        with structlog.testing.capture_logs() as soon, patch.object(import_cache.time, "time", return_value=__import__("time").time() + 600):
            await import_cache.check_import_cache(AsyncMock(), valkey, None)
        assert not [entry for entry in soon if entry.get("alert")]  # ten minutes is a normal pass
        with structlog.testing.capture_logs() as late, patch.object(import_cache.time, "time", return_value=__import__("time").time() + 3 * 3600):
            await import_cache.check_import_cache(AsyncMock(), valkey, None)
        alerts = [entry for entry in late if entry.get("alert")]
        assert [(entry["log_level"], entry["alert"], entry["version"]) for entry in alerts] == [("error", "import_cache_maintenance_stuck", "2")]
        assert alerts[0]["waited_hours"] >= 3


@pytest.mark.asyncio
async def test_the_wait_clock_is_cleared_once_the_version_is_maintained_and_restarts_for_a_new_version() -> None:
    valkey = _store()
    await valkey.set(import_cache._MARKER_KEY, "1")
    with patch.object(import_cache, "latest_discogs_version", AsyncMock(return_value="2")):
        with patch.object(import_cache, "graph_maintenance", AsyncMock(return_value=(False, None))):
            await import_cache.check_import_cache(AsyncMock(), valkey, None)
        assert await _get(valkey, import_cache._WAIT_KEY) is not None
        await import_cache.check_import_cache(AsyncMock(), valkey, None)  # maintained now
        assert await _get(valkey, import_cache._WAIT_KEY) is None
    await valkey.set(import_cache._WAIT_KEY, "1|0.0")  # a stale clock from an older version
    with (
        patch.object(import_cache, "latest_discogs_version", AsyncMock(return_value="3")),
        patch.object(import_cache, "graph_maintenance", AsyncMock(return_value=(False, None))),
        structlog.testing.capture_logs() as logs,
    ):
        await import_cache.check_import_cache(AsyncMock(), valkey, None)
    assert not [entry for entry in logs if entry.get("alert")]  # version 3 starts its own clock
    assert (await _get(valkey, import_cache._WAIT_KEY) or "").startswith("3|")
