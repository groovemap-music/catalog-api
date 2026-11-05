"""The Explore and trends caches are dropped once a new Discogs import is complete in both stores.

See api/import_cache.py. The SQL latch and the graph's own latch are both read here through
their functions, so each test patches the one it is not exercising.
"""

from unittest.mock import AsyncMock, patch

import fakeredis as fake_store
import pytest
from common.config import valkey_key

import api.import_cache as import_cache


@pytest.fixture(autouse=True)
def graph_done() -> object:
    with patch.object(import_cache, "graph_extraction_complete", AsyncMock(return_value=True)) as graph:
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
