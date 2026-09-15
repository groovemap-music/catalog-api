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


_IDENTIFIERS: dict[str, Any] = {
    "identifiers_version": "1",
    "items": [
        {"type": "barcode", "value": "5 012394 144777", "description": None},
        {"type": "matrix_runout", "value": "PB 41447-A2", "description": "A side runout"},
    ],
    "types": ["barcode", "matrix_runout"],
    "aliases": [{"provider": "barcode", "external_id": "5012394144777"}],
    "unmapped": {"types": []},
}
_COMPANIES: dict[str, Any] = {
    "companies_version": "1",
    "items": [{"name": "Damont", "discogs_id": 12345, "role": "Pressed By", "role_category": "pressing", "catno": None}],
    "role_categories": ["pressing"],
    "unmapped": {"roles": []},
}


class TestGetReleaseCatalogBlocks:
    """The ADR 0011 identifiers, companies, and country read out of `releases.data`."""

    @pytest.mark.asyncio
    async def test_returns_the_items_and_country_when_present(self) -> None:
        from api.queries.release_media_queries import get_release_catalog_blocks

        pool = _make_pool({"identifiers": _IDENTIFIERS, "companies": _COMPANIES, "country": "UK"})
        result = await get_release_catalog_blocks(pool, "10")

        assert result["identifiers"] == _IDENTIFIERS["items"]
        assert result["companies"] == _COMPANIES["items"]
        assert result["country"] == "UK"

    @pytest.mark.asyncio
    async def test_release_without_blocks_yields_empty_lists_and_no_country(self) -> None:
        from api.queries.release_media_queries import get_release_catalog_blocks

        pool = _make_pool({"identifiers": None, "companies": None, "country": None})
        assert await get_release_catalog_blocks(pool, "10") == {"identifiers": [], "companies": [], "country": None}

    @pytest.mark.asyncio
    async def test_missing_row_yields_the_same_empty_answer(self) -> None:
        from api.queries.release_media_queries import get_release_catalog_blocks

        pool = _make_pool(None)
        assert await get_release_catalog_blocks(pool, "nope") == {"identifiers": [], "companies": [], "country": None}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("block", [{}, {"items": None}, {"items": "not-a-list"}, [], "string"])
    async def test_malformed_block_costs_the_field_not_the_response(self, block: Any) -> None:
        """A row written before ADR 0011, or mid-rollout, must not fail a detail response."""
        from api.queries.release_media_queries import get_release_catalog_blocks

        pool = _make_pool({"identifiers": block, "companies": block, "country": "UK"})
        result = await get_release_catalog_blocks(pool, "10")

        assert result == {"identifiers": [], "companies": [], "country": "UK"}

    @pytest.mark.asyncio
    async def test_non_object_items_are_skipped(self) -> None:
        from api.queries.release_media_queries import get_release_catalog_blocks

        pool = _make_pool({"identifiers": {"items": [{"type": "barcode"}, "junk", None]}, "companies": None, "country": None})
        result = await get_release_catalog_blocks(pool, "10")

        assert result["identifiers"] == [{"type": "barcode"}]
