"""Tests for insights PostgreSQL queries."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from api.queries.insights_pg_queries import (
    _COMBINED_QUERIES,
    _COMPLETENESS_FIELDS,
    ARTIST_CENTRALITY_SQL,
    GENRE_TRENDS_FILTERED_SQL,
    GENRE_TRENDS_SQL,
    LABEL_LONGEVITY_SQL,
    MONTHLY_ANNIVERSARIES_SQL,
    query_artist_centrality,
    query_data_completeness,
    query_genre_trends,
    query_label_longevity,
    query_monthly_anniversaries,
)
from tests.fake_postgres import FakePool


class TestGraphInsightsQueries:
    @pytest.mark.asyncio
    async def test_artist_centrality_reads_the_indexed_counter_and_keeps_isolates(self) -> None:
        pool = FakePool([[("7", "Artist", 42), ("8", "Isolate", 0)]])
        assert await query_artist_centrality(pool, limit=2) == [
            {"artist_id": "7", "artist_name": "Artist", "edge_count": 42},
            {"artist_id": "8", "artist_name": "Isolate", "edge_count": 0},
        ]
        assert "FROM graph.artist_degree AS degree" in ARTIST_CENTRALITY_SQL
        assert "NOT EXISTS" in ARTIST_CENTRALITY_SQL
        assert pool.params == {"limit": 2}

    @pytest.mark.asyncio
    async def test_genre_trends_selects_the_filtered_statement_and_binds_genre(self) -> None:
        pool = FakePool([[("Jazz", 1960, 12)]])
        assert await query_genre_trends(pool, genre="Jazz") == [{"genre": "Jazz", "decade": 1960, "release_count": 12}]
        assert pool.sql == GENRE_TRENDS_FILTERED_SQL
        assert pool.params == {"genre": "Jazz"}

    @pytest.mark.asyncio
    async def test_genre_trends_without_filter_has_no_parameters(self) -> None:
        pool = FakePool([[]])
        assert await query_genre_trends(pool) == []
        assert pool.sql == GENRE_TRENDS_SQL
        assert pool.params == {}

    @pytest.mark.asyncio
    async def test_label_longevity_maps_the_cypher_projection(self) -> None:
        pool = FakePool([[("501", "Blue Note", 1939, 2025, 87, 4500, 1960)]])
        assert await query_label_longevity(pool, limit=1) == [
            {
                "label_id": "501",
                "label_name": "Blue Note",
                "first_year": 1939,
                "last_year": 2025,
                "years_active": 87,
                "total_releases": 4500,
                "peak_decade": 1960,
            }
        ]
        assert pool.sql == LABEL_LONGEVITY_SQL
        assert pool.params == {"limit": 1}

    @pytest.mark.asyncio
    async def test_anniversaries_binds_derived_target_years(self) -> None:
        pool = FakePool([[("601", "Fixture Master", "Anchor", 2000)]])
        assert await query_monthly_anniversaries(pool, 2025, 9, [25]) == [
            {"master_id": "601", "title": "Fixture Master", "artist_name": "Anchor", "release_year": 2000}
        ]
        assert pool.sql == MONTHLY_ANNIVERSARIES_SQL
        assert pool.params == {"target_years": [2000]}


class TestConstants:
    def test_combined_queries_built_for_each_entity_type(self) -> None:
        for table in _COMPLETENESS_FIELDS:
            assert table in _COMBINED_QUERIES
            assert "count(*) AS total_count" in _COMBINED_QUERIES[table]
            assert f"FROM {table}" in _COMBINED_QUERIES[table]

    def test_combined_queries_include_filter_clauses(self) -> None:
        for table, fields in _COMPLETENESS_FIELDS.items():
            for field_name, jsonb_key in fields:
                assert f"AS {field_name}" in _COMBINED_QUERIES[table]
                assert f"data->>'{jsonb_key}'" in _COMBINED_QUERIES[table]

    def test_completeness_fields_cover_all_entity_types(self) -> None:
        assert set(_COMPLETENESS_FIELDS.keys()) == {"artists", "labels", "masters", "releases"}

    def test_releases_has_most_fields(self) -> None:
        assert len(_COMPLETENESS_FIELDS["releases"]) == 4
        field_names = [f[0] for f in _COMPLETENESS_FIELDS["releases"]]
        assert "with_year" in field_names
        assert "with_country" in field_names
        assert "with_genre" in field_names
        assert "with_image" in field_names


def _make_pool_with_fetchone_sequence(sequence: list[tuple[int, ...] | None]) -> MagicMock:
    """Create a mock pool where fetchone returns rows from the sequence in order."""
    idx = 0

    async def mock_fetchone() -> tuple[int, ...] | None:
        nonlocal idx
        result = sequence[idx] if idx < len(sequence) else None
        idx += 1
        return result

    mock_cursor = AsyncMock()
    mock_cursor.execute = AsyncMock()
    mock_cursor.fetchone = AsyncMock(side_effect=mock_fetchone)
    mock_cursor.__aenter__ = AsyncMock(return_value=mock_cursor)
    mock_cursor.__aexit__ = AsyncMock(return_value=False)

    mock_conn = AsyncMock()
    mock_conn.cursor = MagicMock(return_value=mock_cursor)
    mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_conn.__aexit__ = AsyncMock(return_value=False)

    pool = MagicMock()
    pool.connection = MagicMock(return_value=mock_conn)
    return pool


class TestQueryDataCompleteness:
    @pytest.mark.asyncio
    async def test_returns_results_for_all_entity_types(self) -> None:
        # Each entity type returns one row: (total, field1, field2, ...)
        sequence: list[tuple[int, ...] | None] = [
            (1000, 800),  # artists: total, with_image
            (1000, 800),  # labels: total, with_image
            (1000, 800, 800, 800),  # masters: total, with_year, with_genre, with_image
            (1000, 800, 800, 800, 800),  # releases: total, with_year, with_country, with_genre, with_image
        ]
        pool = _make_pool_with_fetchone_sequence(sequence)
        results = await query_data_completeness(pool)

        assert len(results) == 4
        entity_types = {r["entity_type"] for r in results}
        assert entity_types == {"artists", "labels", "masters", "releases"}

    @pytest.mark.asyncio
    async def test_calculates_completeness_percentage(self) -> None:
        # 80/100 = 80% for each field
        sequence: list[tuple[int, ...] | None] = [
            (100, 80),  # artists
            (100, 80),  # labels
            (100, 80, 80, 80),  # masters
            (100, 80, 80, 80, 80),  # releases
        ]
        pool = _make_pool_with_fetchone_sequence(sequence)
        results = await query_data_completeness(pool)

        for result in results:
            assert result["completeness_pct"] == 80.0

    @pytest.mark.asyncio
    async def test_zero_total_count_returns_zero_completeness(self) -> None:
        # Each entity returns 0 total (remaining columns don't matter)
        sequence: list[tuple[int, ...] | None] = [
            (0, 0),
            (0, 0),
            (0, 0, 0, 0),
            (0, 0, 0, 0, 0),
        ]
        pool = _make_pool_with_fetchone_sequence(sequence)
        results = await query_data_completeness(pool)

        for result in results:
            assert result["total_count"] == 0
            assert result["completeness_pct"] == 0.0

    @pytest.mark.asyncio
    async def test_none_fetchone_returns_zero_count(self) -> None:
        sequence: list[tuple[int, ...] | None] = [None, None, None, None]
        pool = _make_pool_with_fetchone_sequence(sequence)
        results = await query_data_completeness(pool)

        for result in results:
            assert result["total_count"] == 0

    @pytest.mark.asyncio
    async def test_result_contains_expected_fields(self) -> None:
        sequence: list[tuple[int, ...] | None] = [
            (100, 50),
            (100, 50),
            (100, 50, 50, 50),
            (100, 50, 50, 50, 50),
        ]
        pool = _make_pool_with_fetchone_sequence(sequence)
        results = await query_data_completeness(pool)

        for result in results:
            assert "entity_type" in result
            assert "total_count" in result
            assert "completeness_pct" in result
            assert "with_image" in result
