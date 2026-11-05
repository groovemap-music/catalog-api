"""The Explore and trends caches are dropped when a new Discogs import completes (api/import_cache.py)."""

from unittest.mock import AsyncMock, patch

import fakeredis as fake_store
import pytest
from common.config import valkey_key

import api.import_cache as import_cache


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
        assert await import_cache.check_import_cache(AsyncMock(), valkey) is True

    remaining = await _keys(valkey)
    assert remaining == sorted([import_cache._MARKER_KEY, valkey_key("recommend:explore:u1:x")])
    assert await _get(valkey, import_cache._MARKER_KEY) == "2"


@pytest.mark.asyncio
async def test_the_first_run_records_the_version_and_deletes_nothing() -> None:
    valkey = _store()
    await valkey.set(valkey_key("explore:artist:David Bowie"), "{}")

    with patch.object(import_cache, "latest_discogs_version", AsyncMock(return_value="1")):
        assert await import_cache.check_import_cache(AsyncMock(), valkey) is False

    assert await _get(valkey, valkey_key("explore:artist:David Bowie")) == "{}"
    assert await _get(valkey, import_cache._MARKER_KEY) == "1"


@pytest.mark.asyncio
async def test_the_same_version_on_a_later_poll_changes_nothing() -> None:
    valkey = _store()
    await valkey.set(import_cache._MARKER_KEY, "1")
    await valkey.set(valkey_key("explore:artist:David Bowie"), "{}")

    with patch.object(import_cache, "latest_discogs_version", AsyncMock(return_value="1")):
        assert await import_cache.check_import_cache(AsyncMock(), valkey) is False

    assert await _get(valkey, valkey_key("explore:artist:David Bowie")) == "{}"


@pytest.mark.asyncio
async def test_no_complete_import_yet_does_nothing() -> None:
    valkey = _store()
    await valkey.set(valkey_key("explore:artist:David Bowie"), "{}")

    with patch.object(import_cache, "latest_discogs_version", AsyncMock(return_value=None)):
        assert await import_cache.check_import_cache(AsyncMock(), valkey) is False

    assert await _get(valkey, import_cache._MARKER_KEY) is None
    assert await _get(valkey, valkey_key("explore:artist:David Bowie")) == "{}"
