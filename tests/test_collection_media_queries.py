"""Tests for api/queries/collection_media_queries.py."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from api.queries.collection_media_queries import get_collection_media_summary


def _make_pool(family_rows: list[dict[str, Any]], medium_rows: list[dict[str, Any]]) -> MagicMock:
    cur = AsyncMock()
    cur.execute = AsyncMock()
    cur.fetchall = AsyncMock(side_effect=[family_rows, medium_rows])

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


class TestGetCollectionMediaSummary:
    @pytest.mark.asyncio
    async def test_returns_families_and_mediums_with_labels(self) -> None:
        pool = _make_pool(
            family_rows=[{"family": "vinyl", "count": 3}],
            medium_rows=[{"family": "vinyl", "medium": "vinyl_12", "count": 3}],
        )
        result = await get_collection_media_summary(pool, "user-1")
        assert result == {
            "families": [{"id": "vinyl", "count": 3}],
            "mediums": [{"id": "vinyl_12", "label": '12" vinyl', "family": "vinyl", "count": 3}],
        }

    @pytest.mark.asyncio
    async def test_empty_collection_returns_empty_lists(self) -> None:
        pool = _make_pool(family_rows=[], medium_rows=[])
        result = await get_collection_media_summary(pool, "user-1")
        assert result == {"families": [], "mediums": []}

    @pytest.mark.asyncio
    async def test_unknown_medium_id_falls_back_to_raw_id_as_label(self) -> None:
        """A medium id the collection carries but the vendored taxonomy no longer knows."""
        pool = _make_pool(
            family_rows=[{"family": "vinyl", "count": 1}],
            medium_rows=[{"family": "vinyl", "medium": "vinyl_totally_made_up", "count": 1}],
        )
        result = await get_collection_media_summary(pool, "user-1")
        assert result["mediums"] == [{"id": "vinyl_totally_made_up", "label": "vinyl_totally_made_up", "family": "vinyl", "count": 1}]
