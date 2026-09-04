"""Tests for api/queries/release_media_queries.py."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from api.queries.release_media_queries import get_release_media


def _make_pool(fetchone_result: dict[str, Any] | None) -> MagicMock:
    cur = AsyncMock()
    cur.execute = AsyncMock()
    cur.fetchone = AsyncMock(return_value=fetchone_result)

    cur_ctx = AsyncMock()
    cur_ctx.__aenter__ = AsyncMock(return_value=cur)
    cur_ctx.__aexit__ = AsyncMock(return_value=False)

    conn = AsyncMock()
    conn.cursor = MagicMock(return_value=cur_ctx)

    conn_ctx = AsyncMock()
    conn_ctx.__aenter__ = AsyncMock(return_value=conn)
    conn_ctx.__aexit__ = AsyncMock(return_value=False)

    pool = MagicMock()
    pool.connection = MagicMock(return_value=conn_ctx)
    return pool


class TestGetReleaseMedia:
    @pytest.mark.asyncio
    async def test_returns_media_block_when_present(self) -> None:
        media = {"taxonomy_version": "1", "items": [{"family": "vinyl", "medium": "vinyl_12"}], "families": ["vinyl"]}
        pool = _make_pool({"media": media})
        result = await get_release_media(pool, "10")
        assert result == media

    @pytest.mark.asyncio
    async def test_returns_none_when_row_missing(self) -> None:
        pool = _make_pool(None)
        result = await get_release_media(pool, "does-not-exist")
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_media_column_is_null(self) -> None:
        pool = _make_pool({"media": None})
        result = await get_release_media(pool, "10")
        assert result is None
