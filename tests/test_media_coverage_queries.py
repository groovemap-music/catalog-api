"""Tests for api/queries/media_coverage_queries.py."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.queries.media_coverage_queries import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    UnknownProviderError,
    _coverage_query,
    _release_table,
    _top_names_query,
    get_unmapped_media,
    known_providers,
)


def _mock_pool_with_rows(*query_results: list[dict]) -> tuple[MagicMock, Any, list]:
    """Build a mock pool whose cursor answers successive execute() calls in order.

    Returns ``(pool, execute_side_effect, calls)`` — patch
    ``api.queries.media_coverage_queries.execute_sql`` with the side effect, and read
    ``calls`` for the ``(query, params)`` pairs the function issued.
    """
    results_iter = iter(query_results)
    calls: list[tuple[Any, Any]] = []

    mock_cur = AsyncMock()
    mock_cur._current_result = []

    async def _fetchone():
        return mock_cur._current_result[0] if mock_cur._current_result else None

    async def _fetchall():
        return mock_cur._current_result

    mock_cur.fetchone = AsyncMock(side_effect=_fetchone)
    mock_cur.fetchall = AsyncMock(side_effect=_fetchall)

    mock_cur_ctx = MagicMock()
    mock_cur_ctx.__aenter__ = AsyncMock(return_value=mock_cur)
    mock_cur_ctx.__aexit__ = AsyncMock(return_value=False)

    mock_conn = AsyncMock()
    mock_conn.cursor = MagicMock(return_value=mock_cur_ctx)

    mock_conn_ctx = AsyncMock()
    mock_conn_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_conn_ctx.__aexit__ = AsyncMock(return_value=False)

    mock_pool = MagicMock()
    mock_pool.connection = MagicMock(return_value=mock_conn_ctx)

    async def _execute_sql(_cur, query, params=None):
        calls.append((query, params))
        mock_cur._current_result = next(results_iter)

    return mock_pool, _execute_sql, calls


def _rendered(query: Any) -> str:
    """Render a psycopg composable to its SQL text."""
    return query.as_string(None)


class TestKnownProviders:
    def test_lists_both_providers_sorted(self) -> None:
        assert known_providers() == ["discogs", "musicbrainz"]


class TestReleaseTable:
    def test_discogs_uses_the_default_schema(self) -> None:
        assert _rendered(_release_table("discogs")) == '"releases"'

    def test_musicbrainz_is_schema_qualified(self) -> None:
        assert _rendered(_release_table("musicbrainz")) == '"musicbrainz"."releases"'

    def test_unknown_provider_raises(self) -> None:
        with pytest.raises(UnknownProviderError) as exc_info:
            _release_table("bandcamp")
        assert exc_info.value.provider == "bandcamp"
        # The message names the accepted values so a 422 body is actionable.
        assert "discogs" in str(exc_info.value)
        assert "musicbrainz" in str(exc_info.value)

    def test_unknown_provider_error_is_a_value_error(self) -> None:
        """The router maps ValueError to 422; the subclass must stay under it."""
        assert issubclass(UnknownProviderError, ValueError)


class TestQueryShape:
    @pytest.mark.parametrize("provider", ["discogs", "musicbrainz"])
    def test_non_array_unmapped_is_guarded(self, provider: str) -> None:
        """A block whose `unmapped` list is missing or malformed must not raise in SQL."""
        for query in (_coverage_query(_release_table(provider)), _top_names_query(_release_table(provider))):
            text = _rendered(query)
            assert text.count("jsonb_typeof") == 2
            assert text.count("ELSE '[]'::jsonb") == 2

    @pytest.mark.parametrize("provider", ["discogs", "musicbrainz"])
    def test_every_release_reference_is_qualified(self, provider: str) -> None:
        """`releases` carries its own `name` column, so a bare reference would be ambiguous."""
        text = _rendered(_top_names_query(_release_table(provider)))
        assert "unmapped.name AS name" in text
        assert " media" not in text.replace('"r".media', "")

    def test_top_names_covers_both_unmapped_lists(self) -> None:
        text = _rendered(_top_names_query(_release_table("discogs")))
        assert "'format'" in text
        assert "'description'" in text
        assert "UNION ALL" in text

    def test_top_names_ranks_by_release_count(self) -> None:
        text = _rendered(_top_names_query(_release_table("discogs")))
        assert "ORDER BY releases DESC, kind ASC, name ASC" in text


class TestGetUnmappedMedia:
    @pytest.mark.asyncio
    async def test_discogs_aggregation(self) -> None:
        pool, execute_side_effect, calls = _mock_pool_with_rows(
            [{"media_tagged_releases": 500, "releases_with_unmapped": 125}],
            [
                {"kind": "format", "name": "Lathe Cut", "releases": 80},
                {"kind": "description", "name": "Hand-Numbered", "releases": 45},
            ],
        )

        with patch("api.queries.media_coverage_queries.execute_sql", side_effect=execute_side_effect):
            result = await get_unmapped_media(pool, "discogs")

        assert result["provider"] == "discogs"
        assert result["media_tagged_releases"] == 500
        assert result["releases_with_unmapped"] == 125
        assert result["unmapped_rate"] == pytest.approx(0.25)
        assert result["limit"] == DEFAULT_LIMIT
        assert result["top_unmapped"] == [
            {"kind": "format", "name": "Lathe Cut", "releases": 80},
            {"kind": "description", "name": "Hand-Numbered", "releases": 45},
        ]
        # The Discogs table is the unqualified one.
        assert '"releases"' in _rendered(calls[0][0])
        assert "musicbrainz" not in _rendered(calls[0][0])

    @pytest.mark.asyncio
    async def test_musicbrainz_aggregation(self) -> None:
        pool, execute_side_effect, calls = _mock_pool_with_rows(
            [{"media_tagged_releases": 40, "releases_with_unmapped": 10}],
            [{"kind": "format", "name": "DAT", "releases": 7}],
        )

        with patch("api.queries.media_coverage_queries.execute_sql", side_effect=execute_side_effect):
            result = await get_unmapped_media(pool, "musicbrainz")

        assert result["provider"] == "musicbrainz"
        assert result["media_tagged_releases"] == 40
        assert result["unmapped_rate"] == pytest.approx(0.25)
        assert result["top_unmapped"] == [{"kind": "format", "name": "DAT", "releases": 7}]
        # Both statements must target the MusicBrainz schema, not the Discogs table.
        for query, _params in calls:
            assert '"musicbrainz"."releases"' in _rendered(query)

    @pytest.mark.asyncio
    async def test_empty_result(self) -> None:
        """No media-tagged releases: zeroed counts, no division by zero, no entries."""
        pool, execute_side_effect, _calls = _mock_pool_with_rows(
            [{"media_tagged_releases": 0, "releases_with_unmapped": 0}],
            [],
        )

        with patch("api.queries.media_coverage_queries.execute_sql", side_effect=execute_side_effect):
            result = await get_unmapped_media(pool, "musicbrainz")

        assert result["media_tagged_releases"] == 0
        assert result["releases_with_unmapped"] == 0
        assert result["unmapped_rate"] == 0.0
        assert result["top_unmapped"] == []

    @pytest.mark.asyncio
    async def test_tagged_releases_with_no_unmapped_names(self) -> None:
        """Full coverage is a real answer: releases exist, none carry an unmapped name."""
        pool, execute_side_effect, _calls = _mock_pool_with_rows(
            [{"media_tagged_releases": 900, "releases_with_unmapped": 0}],
            [],
        )

        with patch("api.queries.media_coverage_queries.execute_sql", side_effect=execute_side_effect):
            result = await get_unmapped_media(pool, "discogs")

        assert result["media_tagged_releases"] == 900
        assert result["unmapped_rate"] == 0.0
        assert result["top_unmapped"] == []

    @pytest.mark.asyncio
    async def test_missing_coverage_row_is_tolerated(self) -> None:
        """A summary query returning no row must not crash the aggregation."""
        pool, execute_side_effect, _calls = _mock_pool_with_rows([], [])

        with patch("api.queries.media_coverage_queries.execute_sql", side_effect=execute_side_effect):
            result = await get_unmapped_media(pool, "discogs")

        assert result["media_tagged_releases"] == 0
        assert result["unmapped_rate"] == 0.0

    @pytest.mark.asyncio
    async def test_unknown_provider_raises_before_touching_the_pool(self) -> None:
        pool, execute_side_effect, _calls = _mock_pool_with_rows([], [])

        with (
            patch("api.queries.media_coverage_queries.execute_sql", side_effect=execute_side_effect),
            pytest.raises(UnknownProviderError),
        ):
            await get_unmapped_media(pool, "bandcamp")

        pool.connection.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("requested", "applied"),
        [(0, 1), (-5, 1), (1, 1), (50, 50), (MAX_LIMIT + 1, MAX_LIMIT)],
    )
    async def test_limit_is_clamped_and_bound_as_a_parameter(self, requested: int, applied: int) -> None:
        pool, execute_side_effect, calls = _mock_pool_with_rows(
            [{"media_tagged_releases": 10, "releases_with_unmapped": 1}],
            [],
        )

        with patch("api.queries.media_coverage_queries.execute_sql", side_effect=execute_side_effect):
            result = await get_unmapped_media(pool, "discogs", requested)

        assert result["limit"] == applied
        # The limit travels as a bound parameter, never interpolated into the SQL.
        assert calls[1][1] == (applied,)

    @pytest.mark.asyncio
    async def test_rate_is_rounded_to_four_places(self) -> None:
        pool, execute_side_effect, _calls = _mock_pool_with_rows(
            [{"media_tagged_releases": 3, "releases_with_unmapped": 1}],
            [],
        )

        with patch("api.queries.media_coverage_queries.execute_sql", side_effect=execute_side_effect):
            result = await get_unmapped_media(pool, "discogs")

        assert result["unmapped_rate"] == pytest.approx(round(1 / 3, 4))
