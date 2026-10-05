"""Unit tests for RecommendCache."""

import json
from unittest.mock import AsyncMock

import pytest

from api.cache import RecommendCache


class TestRecommendCache:
    """Tests for the RecommendCache class."""

    @pytest.fixture
    def mock_valkey(self) -> AsyncMock:
        valkey = AsyncMock()
        valkey.get = AsyncMock(return_value=None)
        valkey.set = AsyncMock()
        valkey.scan = AsyncMock(return_value=(0, []))
        valkey.delete = AsyncMock()
        return valkey

    @pytest.fixture
    def cache(self, mock_valkey: AsyncMock) -> RecommendCache:
        return RecommendCache(valkey=mock_valkey, default_ttl=3600)

    @pytest.mark.asyncio
    async def test_get_miss(self, cache: RecommendCache) -> None:
        result = await cache.get("recommend:missing")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_hit(self, cache: RecommendCache, mock_valkey: AsyncMock) -> None:
        mock_valkey.get = AsyncMock(return_value=json.dumps({"key": "value"}))
        result = await cache.get("recommend:hit")
        assert result == {"key": "value"}

    @pytest.mark.asyncio
    async def test_get_valkey_error(self, cache: RecommendCache, mock_valkey: AsyncMock) -> None:
        mock_valkey.get = AsyncMock(side_effect=ConnectionError("down"))
        result = await cache.get("recommend:fail")
        assert result is None

    @pytest.mark.asyncio
    async def test_set_stores_with_ttl(self, cache: RecommendCache, mock_valkey: AsyncMock) -> None:
        await cache.set("recommend:key", {"data": 1}, ttl=7200)
        mock_valkey.set.assert_called_once()
        call_kwargs = mock_valkey.set.call_args
        assert call_kwargs[1]["ex"] == 7200

    @pytest.mark.asyncio
    async def test_set_uses_default_ttl(self, cache: RecommendCache, mock_valkey: AsyncMock) -> None:
        await cache.set("recommend:key", {"data": 1})
        call_kwargs = mock_valkey.set.call_args
        assert call_kwargs[1]["ex"] == 3600

    @pytest.mark.asyncio
    async def test_set_valkey_error(self, cache: RecommendCache, mock_valkey: AsyncMock) -> None:
        mock_valkey.set = AsyncMock(side_effect=ConnectionError("down"))
        await cache.set("recommend:key", {"data": 1})  # should not raise

        mock_valkey.set.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_invalidate_user(self, cache: RecommendCache, mock_valkey: AsyncMock) -> None:
        # Two SCAN responses: one per pattern (explore:*, enhanced:{user_id})
        mock_valkey.scan = AsyncMock(
            side_effect=[
                (0, ["recommend:explore:user1:artist:a1"]),
                (0, ["recommend:enhanced:user1"]),
            ]
        )
        await cache.invalidate_user("user1")
        assert mock_valkey.delete.call_count == 2

    @pytest.mark.asyncio
    async def test_invalidate_user_no_keys(self, cache: RecommendCache, mock_valkey: AsyncMock) -> None:
        mock_valkey.scan = AsyncMock(return_value=(0, []))
        await cache.invalidate_user("user1")
        mock_valkey.delete.assert_not_called()

    @pytest.mark.asyncio
    async def test_invalidate_user_valkey_error(self, cache: RecommendCache, mock_valkey: AsyncMock) -> None:
        mock_valkey.scan = AsyncMock(side_effect=ConnectionError("down"))
        await cache.invalidate_user("user1")  # should not raise
        mock_valkey.scan.assert_awaited_once()
