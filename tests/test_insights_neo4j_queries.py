"""Tests for insights Neo4j query functions."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class _AsyncIterator:
    """Async iterator wrapper for mock Neo4j result records."""

    def __init__(self, items: list[Any]) -> None:
        self._items = iter(items)

    def __aiter__(self) -> _AsyncIterator:
        return self

    async def __anext__(self) -> Any:
        try:
            return next(self._items)
        except StopIteration:
            raise StopAsyncIteration from None

    async def consume(self) -> MagicMock:
        return MagicMock()


class _DictRecord(dict):  # type: ignore[type-arg]
    """A dict subclass that also exposes .data() for backward-compat tests."""

    def data(self) -> dict[str, Any]:
        return dict(self)


def _make_mock_driver(records: list[dict[str, Any]]) -> AsyncMock:
    """Create a mock Neo4j driver that returns the given records."""
    mock_records = [_DictRecord(r) for r in records]

    mock_result = _AsyncIterator(mock_records)

    mock_session = AsyncMock()
    mock_session.run = AsyncMock(return_value=mock_result)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    mock_driver = AsyncMock()
    mock_driver.session = MagicMock(return_value=mock_session)
    return mock_driver


class TestQueryArtistCentrality:
    @pytest.mark.asyncio
    async def test_returns_ranked_artists(self) -> None:
        from api.queries.insights_neo4j_queries import query_artist_centrality

        records = [
            {"artist_id": "a1", "artist_name": "Artist One", "edge_count": 100},
            {"artist_id": "a2", "artist_name": "Artist Two", "edge_count": 50},
        ]
        driver = _make_mock_driver(records)
        result = await query_artist_centrality(driver, limit=100)
        assert len(result) == 2
        assert result[0]["artist_id"] == "a1"
        assert result[0]["edge_count"] == 100

    @pytest.mark.asyncio
    async def test_empty_results(self) -> None:
        from api.queries.insights_neo4j_queries import query_artist_centrality

        driver = _make_mock_driver([])
        result = await query_artist_centrality(driver, limit=100)
        assert result == []


class TestQueryGenreTrends:
    @pytest.mark.asyncio
    async def test_returns_genre_decade_counts(self) -> None:
        from api.queries.insights_neo4j_queries import query_genre_trends

        records = [
            {"genre": "Rock", "decade": 1970, "release_count": 50000},
            {"genre": "Rock", "decade": 1980, "release_count": 75000},
            {"genre": "Jazz", "decade": 1960, "release_count": 30000},
        ]
        driver = _make_mock_driver(records)
        result = await query_genre_trends(driver)
        assert len(result) == 3
        assert result[0]["genre"] == "Rock"

    @pytest.mark.asyncio
    async def test_filters_by_genre(self) -> None:
        from api.queries.insights_neo4j_queries import query_genre_trends

        driver = _make_mock_driver([{"genre": "Jazz", "decade": 1960, "release_count": 30000}])
        result = await query_genre_trends(driver, genre="Jazz")
        assert len(result) == 1
        assert result[0]["genre"] == "Jazz"


class TestQueryStyleYearCounts:
    """Tests for query_style_year_counts."""

    @staticmethod
    def _run_query_for(years: list[dict[str, Any]], cells: list[dict[str, Any]], totals: list[dict[str, Any]]) -> AsyncMock:
        """A run_query stand-in that answers each of the three reads by its Cypher."""

        async def run_query(_driver: Any, cypher: str, **_params: Any) -> list[dict[str, Any]]:
            if "OPTIONAL MATCH (r)-[:DERIVED_FROM]" in cypher:
                return years
            if "(g:Genre)" in cypher:
                return cells
            return totals

        return AsyncMock(side_effect=run_query)

    @pytest.mark.asyncio
    async def test_shares_are_raw_fractions_of_the_style_total(self) -> None:
        """Every co-tagged genre is returned with release_count / style total, unfloored."""
        from api.queries.insights_neo4j_queries import query_style_year_counts

        years = [{"style": "Bop", "year": 1959, "release_count": 4, "master_count": 3}]
        cells = [
            {"style": "Bop", "genre": "Jazz", "release_count": 99, "style_release_count": 0},
            {"style": "Bop", "genre": "Funk / Soul", "release_count": 1},
        ]
        totals = [{"style": "Bop", "style_release_count": 100}]
        with patch("api.queries.insights_neo4j_queries.run_query", self._run_query_for(years, cells, totals)):
            result = await query_style_year_counts(AsyncMock())

        assert result["style_years"] == years
        assert result["style_parents"] == [
            {"style": "Bop", "genre": "Jazz", "release_count": 99, "style_release_count": 100, "share": 0.99},
            {"style": "Bop", "genre": "Funk / Soul", "release_count": 1, "style_release_count": 100, "share": 0.01},
        ]

    @pytest.mark.asyncio
    async def test_each_read_carries_the_long_internal_timeout(self) -> None:
        """All three reads run under the style-year server-side budget, not the driver default."""
        from api.queries.insights_neo4j_queries import STYLE_YEAR_QUERY_TIMEOUT_SECONDS, query_style_year_counts

        run_query = self._run_query_for([], [], [])
        with patch("api.queries.insights_neo4j_queries.run_query", run_query):
            await query_style_year_counts(AsyncMock())

        assert run_query.await_count == 3
        assert {call.kwargs["timeout"] for call in run_query.await_args_list} == {STYLE_YEAR_QUERY_TIMEOUT_SECONDS}
        assert STYLE_YEAR_QUERY_TIMEOUT_SECONDS > 30.0  # the genre-tree route budget

    def test_buckets_by_release_year_not_master_year(self) -> None:
        """The year cell groups on the release's own year; the master is only counted."""
        from api.queries.insights_neo4j_queries import _STYLE_YEAR_CYPHER

        assert "r.year AS year" in _STYLE_YEAR_CYPHER
        assert "m.year" not in _STYLE_YEAR_CYPHER


class TestQueryLabelLongevity:
    @pytest.mark.asyncio
    async def test_returns_ranked_labels(self) -> None:
        from api.queries.insights_neo4j_queries import query_label_longevity

        records = [
            {
                "label_id": "l1",
                "label_name": "Blue Note",
                "first_year": 1939,
                "last_year": 2025,
                "years_active": 86,
                "total_releases": 4500,
                "peak_decade": 1960,
            },
        ]
        driver = _make_mock_driver(records)
        result = await query_label_longevity(driver, limit=50)
        assert len(result) == 1
        assert result[0]["years_active"] == 86


class TestQueryAnniversaries:
    @pytest.mark.asyncio
    async def test_returns_anniversary_releases(self) -> None:
        from api.queries.insights_neo4j_queries import query_monthly_anniversaries

        records = [
            {"master_id": "m1", "title": "OK Computer", "artist_name": "Radiohead", "release_year": 1997},
        ]
        driver = _make_mock_driver(records)
        result = await query_monthly_anniversaries(driver, current_year=2022, current_month=6)
        assert len(result) == 1
        assert result[0]["master_id"] == "m1"

    @pytest.mark.asyncio
    async def test_custom_milestones(self) -> None:
        from api.queries.insights_neo4j_queries import query_monthly_anniversaries

        driver = _make_mock_driver([])
        await query_monthly_anniversaries(driver, current_year=2025, current_month=1, milestone_years=[10, 20])
        # Verify target_years were calculated correctly
        call_args = driver.session.return_value.__aenter__.return_value.run.call_args
        params = call_args[0][1] if len(call_args[0]) > 1 else call_args[1]
        assert params.get("target_years") == [2015, 2005]
