"""Tests for insights Neo4j query functions."""

import re
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from neo4j.exceptions import TransientError

from tests.placeholder_labels import PLACEHOLDER_LABEL_CASES


def _longevity_row(label_id: str, years_active: int, *, placeholder: bool = False) -> dict[str, Any]:
    return {"label_id": label_id, "years_active": years_active, "total_releases": 10, "is_placeholder": placeholder}


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


class TestGenreTrendsBudget:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("genre", [None, "Jazz"])
    async def test_read_carries_the_long_internal_timeout(self, genre: str | None) -> None:
        """The full release-genre scan runs under its own budget, not the 30 s default that cut it off."""
        from api.queries.insights_neo4j_queries import GENRE_TRENDS_QUERY_TIMEOUT_SECONDS, query_genre_trends

        run_query = AsyncMock(return_value=[])
        with patch("api.queries.insights_neo4j_queries.run_query", run_query):
            await query_genre_trends(AsyncMock(), genre=genre)

        assert run_query.await_args.kwargs["timeout"] == GENRE_TRENDS_QUERY_TIMEOUT_SECONDS

    @pytest.mark.asyncio
    async def test_a_timeout_propagates_instead_of_returning_empty_data(self) -> None:
        from api.queries.insights_neo4j_queries import query_genre_trends

        with patch("api.queries.insights_neo4j_queries.run_query", AsyncMock(side_effect=TimeoutError)), pytest.raises(TimeoutError):
            await query_genre_trends(AsyncMock())


class TestQueryStyleYearCounts:
    """Tests for query_style_year_counts."""

    @staticmethod
    def _run_query_for(
        years: list[dict[str, Any]],
        cells: list[dict[str, Any]],
        totals: list[dict[str, Any]],
        genre_years: list[dict[str, Any]] | None = None,
    ) -> AsyncMock:
        """A run_query stand-in that answers each of the four reads by its Cypher."""

        async def run_query(_driver: Any, cypher: str, **_params: Any) -> list[dict[str, Any]]:
            if "OPTIONAL MATCH (r)-[:DERIVED_FROM]" in cypher:
                return years
            if "r.year AS year" in cypher and "(g:Genre)" in cypher:
                return genre_years or []
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
        assert result["genre_years"] == []
        assert result["style_parents"] == [
            {"style": "Bop", "genre": "Jazz", "release_count": 99, "style_release_count": 100, "share": 0.99},
            {"style": "Bop", "genre": "Funk / Soul", "release_count": 1, "style_release_count": 100, "share": 0.01},
        ]

    @pytest.mark.asyncio
    async def test_genre_years_are_passed_through_as_published_rows(self) -> None:
        """The genre-year read is returned as-is beside the style cells."""
        from api.queries.insights_neo4j_queries import query_style_year_counts

        genre_years = [{"genre": "Jazz", "year": 1959, "release_count": 5}, {"genre": "Jazz", "year": 1960, "release_count": 1}]
        with patch("api.queries.insights_neo4j_queries.run_query", self._run_query_for([], [], [], genre_years)):
            result = await query_style_year_counts(AsyncMock())

        assert result["genre_years"] == genre_years

    @pytest.mark.asyncio
    async def test_each_read_carries_the_long_internal_timeout(self) -> None:
        """All four reads run under the style-year server-side budget, not the driver default."""
        from api.queries.insights_neo4j_queries import STYLE_YEAR_QUERY_TIMEOUT_SECONDS, query_style_year_counts

        run_query = self._run_query_for([], [], [])
        with patch("api.queries.insights_neo4j_queries.run_query", run_query):
            await query_style_year_counts(AsyncMock())

        assert run_query.await_count == 4
        assert {call.kwargs["timeout"] for call in run_query.await_args_list} == {STYLE_YEAR_QUERY_TIMEOUT_SECONDS}
        assert STYLE_YEAR_QUERY_TIMEOUT_SECONDS > 30.0  # the genre-tree route budget

    def test_buckets_by_release_year_not_master_year(self) -> None:
        """The year cell groups on the release's own year; the master is only counted."""
        from api.queries.insights_neo4j_queries import _STYLE_YEAR_CYPHER

        assert "r.year AS year" in _STYLE_YEAR_CYPHER
        assert "m.year" not in _STYLE_YEAR_CYPHER

    def test_genre_years_bucket_by_release_year_and_exclude_undated(self) -> None:
        """The genre cell groups on the release's own year and drops undated releases like the style cell."""
        from api.queries.insights_neo4j_queries import _GENRE_YEAR_CYPHER

        assert "r.year AS year" in _GENRE_YEAR_CYPHER
        assert "r.year IS NOT NULL AND r.year > 0" in _GENRE_YEAR_CYPHER
        assert "count(DISTINCT r)" in _GENRE_YEAR_CYPHER


class TestQueryStyleLandmarks:
    """Tests for query_style_landmarks."""

    @pytest.mark.asyncio
    async def test_rows_are_returned_as_published_landmarks(self) -> None:
        """The ranked rows are wrapped under ``landmarks`` unchanged."""
        from api.queries.insights_neo4j_queries import query_style_landmarks

        rows = [{"style": "Bop", "master_id": "m1", "title": "Blue Train", "year": None, "release_count": 4}]
        with patch("api.queries.insights_neo4j_queries.run_query", AsyncMock(return_value=rows)):
            assert await query_style_landmarks(AsyncMock()) == {"landmarks": rows}

    @pytest.mark.asyncio
    async def test_read_carries_the_cap_and_the_long_internal_timeout(self) -> None:
        """The read passes the per-style cap as a parameter and runs under the landmark budget."""
        from api.queries.insights_neo4j_queries import (
            LANDMARKS_PER_STYLE,
            STYLE_LANDMARK_QUERY_TIMEOUT_SECONDS,
            query_style_landmarks,
        )

        run_query = AsyncMock(return_value=[])
        with patch("api.queries.insights_neo4j_queries.run_query", run_query):
            await query_style_landmarks(AsyncMock())

        run_query.assert_awaited_once()
        assert run_query.await_args.kwargs["timeout"] == STYLE_LANDMARK_QUERY_TIMEOUT_SECONDS
        assert run_query.await_args.kwargs["limit"] == LANDMARKS_PER_STYLE == 25
        assert STYLE_LANDMARK_QUERY_TIMEOUT_SECONDS > 30.0  # the genre-tree route budget

    def test_only_releases_with_a_master_count_and_ties_break_on_master_id(self) -> None:
        """The master match is required, and the ranking is release count descending then master id."""
        from api.queries.insights_neo4j_queries import _STYLE_LANDMARK_CYPHER

        assert "MATCH (r)-[:DERIVED_FROM]->(m:Master)" in _STYLE_LANDMARK_CYPHER
        assert "OPTIONAL MATCH" not in _STYLE_LANDMARK_CYPHER
        assert "count(DISTINCT r)" in _STYLE_LANDMARK_CYPHER
        assert "ORDER BY release_count DESC, m.id" in _STYLE_LANDMARK_CYPHER
        assert "LIMIT $limit" in _STYLE_LANDMARK_CYPHER

    def test_the_count_runs_one_style_at_a_time(self) -> None:
        """Grouping over the whole graph exhausted the shared transaction pool on the full catalog."""
        from api.queries.insights_neo4j_queries import _STYLE_LANDMARK_CYPHER

        assert "CALL (s) {" in _STYLE_LANDMARK_CYPHER
        assert _STYLE_LANDMARK_CYPHER.index("CALL (s) {") < _STYLE_LANDMARK_CYPHER.index("count(DISTINCT r)")


class TestQueryStyleCotags:
    """Tests for query_style_cotags."""

    @pytest.mark.asyncio
    async def test_rows_are_returned_as_published_cotags(self) -> None:
        """The ranked rows are wrapped under ``cotags`` unchanged."""
        from api.queries.insights_neo4j_queries import query_style_cotags

        rows = [{"style": "Bop", "other": "Cool", "release_count": 4}]
        with patch("api.queries.insights_neo4j_queries.run_query", AsyncMock(return_value=rows)):
            assert await query_style_cotags(AsyncMock()) == {"cotags": rows}

    @pytest.mark.asyncio
    async def test_read_carries_the_cap_and_the_long_internal_timeout(self) -> None:
        """The read passes the per-style cap as a parameter and runs under the co-tag budget."""
        from api.queries.insights_neo4j_queries import (
            COTAGS_PER_STYLE,
            STYLE_COTAG_QUERY_TIMEOUT_SECONDS,
            query_style_cotags,
        )

        run_query = AsyncMock(return_value=[])
        with patch("api.queries.insights_neo4j_queries.run_query", run_query):
            await query_style_cotags(AsyncMock())

        run_query.assert_awaited_once()
        assert run_query.await_args.kwargs["timeout"] == STYLE_COTAG_QUERY_TIMEOUT_SECONDS
        assert run_query.await_args.kwargs["limit"] == COTAGS_PER_STYLE == 25
        assert STYLE_COTAG_QUERY_TIMEOUT_SECONDS > 30.0  # the genre-tree route budget

    def test_pairs_count_releases_once_exclude_self_and_tie_break_on_name(self) -> None:
        """Self pairs are filtered, releases are counted distinct, and ties sort by the other style's name."""
        from api.queries.insights_neo4j_queries import _STYLE_COTAG_CYPHER

        assert "WHERE other <> s" in _STYLE_COTAG_CYPHER
        assert "count(DISTINCT r)" in _STYLE_COTAG_CYPHER
        assert "ORDER BY release_count DESC, other.name" in _STYLE_COTAG_CYPHER
        assert "LIMIT $limit" in _STYLE_COTAG_CYPHER

    def test_the_count_runs_one_style_at_a_time(self) -> None:
        """Grouping every pair over the whole graph exhausted the shared transaction pool in 14 s."""
        from api.queries.insights_neo4j_queries import _STYLE_COTAG_CYPHER

        assert "CALL (s) {" in _STYLE_COTAG_CYPHER
        assert _STYLE_COTAG_CYPHER.index("CALL (s) {") < _STYLE_COTAG_CYPHER.index("count(DISTINCT r)")


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

    @pytest.mark.parametrize(("label_id", "name", "placeholder"), PLACEHOLDER_LABEL_CASES)
    def test_placeholder_rule_on_real_catalog_names(self, label_id: str, name: str, placeholder: bool) -> None:
        """Every name here is from the Lux catalog: placeholders go, and real lookalikes stay."""
        from api.queries.insights_neo4j_queries import PLACEHOLDER_LABEL_IDS, PLACEHOLDER_LABEL_NAME, REAL_LABELS_NAMED_LIKE_PLACEHOLDERS

        matched = label_id in PLACEHOLDER_LABEL_IDS or (
            re.fullmatch(PLACEHOLDER_LABEL_NAME, name) is not None and label_id not in REAL_LABELS_NAMED_LIKE_PLACEHOLDERS
        )
        assert matched is placeholder

    @pytest.mark.asyncio
    async def test_default_is_one_read_of_real_labels(self) -> None:
        """An analytics-engine that knows nothing of placeholders gets real labels, at the old cost."""
        from api.queries.insights_neo4j_queries import query_label_longevity

        run_query = AsyncMock(return_value=[_longevity_row("1", 50)])
        with patch("api.queries.insights_neo4j_queries.run_query", run_query):
            assert await query_label_longevity(AsyncMock(), limit=1) == [_longevity_row("1", 50)]

        run_query.assert_awaited_once()
        assert run_query.await_args.kwargs["placeholders"] is False

    @pytest.mark.asyncio
    async def test_included_placeholders_take_no_place_from_a_real_label(self) -> None:
        """Real rows and their order are untouched; a placeholder ranks among them, or stays out."""
        from api.queries.insights_neo4j_queries import query_label_longevity

        real = [_longevity_row("1", 90), _longevity_row("2", 80)]
        inside = _longevity_row("1818", 85, placeholder=True)
        below = _longevity_row("7", 70, placeholder=True)

        async def read(*_args: object, placeholders: bool, **_kwargs: object) -> list[dict[str, Any]]:
            return [inside, below] if placeholders else real

        with patch("api.queries.insights_neo4j_queries.run_query", read):
            rows = await query_label_longevity(AsyncMock(), limit=2, include_placeholders=True)

        assert rows == [real[0], inside, real[1]]
        assert [row for row in rows if not row["is_placeholder"]] == real

    @pytest.mark.asyncio
    async def test_a_short_ranking_keeps_every_placeholder_it_read(self) -> None:
        """With fewer real labels than the limit, no real label marks where the ranking ends."""
        from api.queries.insights_neo4j_queries import query_label_longevity

        real = _longevity_row("1", 90)
        below = _longevity_row("7", 70, placeholder=True)

        async def read(*_args: object, placeholders: bool, **_kwargs: object) -> list[dict[str, Any]]:
            return [below] if placeholders else [real]

        with patch("api.queries.insights_neo4j_queries.run_query", read):
            assert await query_label_longevity(AsyncMock(), limit=5, include_placeholders=True) == [real, below]

    @pytest.mark.asyncio
    async def test_a_failed_placeholder_read_fails_the_whole_read(self) -> None:
        """A partial ranking must not pass for the complete one."""
        from api.queries.insights_neo4j_queries import query_label_longevity

        async def read(*_args: object, placeholders: bool, **_kwargs: object) -> list[dict[str, Any]]:
            if placeholders:
                raise TransientError("out of memory")
            return [_longevity_row("1", 90)]

        with patch("api.queries.insights_neo4j_queries.run_query", read), pytest.raises(ExceptionGroup):
            await query_label_longevity(AsyncMock(), limit=1, include_placeholders=True)

    @pytest.mark.asyncio
    async def test_read_carries_the_limit_and_the_long_internal_timeout(self) -> None:
        """The read runs under its own budget, not the 30 s default that cut it off."""
        from api.queries.helpers import DEFAULT_READ_TIMEOUT_SECONDS
        from api.queries.insights_neo4j_queries import LABEL_LONGEVITY_QUERY_TIMEOUT_SECONDS, query_label_longevity

        run_query = AsyncMock(return_value=[])
        with patch("api.queries.insights_neo4j_queries.run_query", run_query):
            await query_label_longevity(AsyncMock(), limit=7)

        assert run_query.await_args.kwargs["timeout"] == LABEL_LONGEVITY_QUERY_TIMEOUT_SECONDS > DEFAULT_READ_TIMEOUT_SECONDS
        assert run_query.await_args.kwargs["limit"] == 7

    @pytest.mark.asyncio
    async def test_a_timeout_propagates_instead_of_returning_empty_data(self) -> None:
        from api.queries.insights_neo4j_queries import query_label_longevity

        with patch("api.queries.insights_neo4j_queries.run_query", AsyncMock(side_effect=TimeoutError)), pytest.raises(TimeoutError):
            await query_label_longevity(AsyncMock())


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
