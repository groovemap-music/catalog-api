"""Totals and node details read the enricher's stored counts, and count live only when one is missing (audit H5, H7)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from api.queries import neo4j_queries as q


COUNTS = [
    (q.count_genre_releases, "Genre", "release_count"),
    (q.count_genre_artists, "Genre", "artist_count"),
    (q.count_genre_labels, "Genre", "label_count"),
    (q.count_genre_styles, "Genre", "style_count"),
    (q.count_label_releases, "Label", "release_count"),
    (q.count_label_artists, "Label", "artist_count"),
    (q.count_label_genres, "Label", "genre_count"),
    (q.count_style_releases, "Style", "release_count"),
    (q.count_style_artists, "Style", "artist_count"),
    (q.count_style_labels, "Style", "label_count"),
    (q.count_style_genres, "Style", "genre_count"),
]


@pytest.mark.parametrize(("fn", "label", "prop"), COUNTS)
@pytest.mark.asyncio
async def test_total_without_before_year_is_one_property_read(fn: Any, label: str, prop: str) -> None:
    with (
        patch("api.queries.neo4j_queries.run_single", AsyncMock(return_value={"total": 42})) as single,
        patch("api.queries.neo4j_queries.run_count", AsyncMock(side_effect=AssertionError("live count"))),
    ):
        assert await fn(object(), "X") == 42
    cypher = single.call_args.args[1]
    assert f"(n:{label} {{name: $name}})" in cypher
    assert f"n.{prop}" in cypher


@pytest.mark.parametrize(("fn", "label", "prop"), COUNTS)
@pytest.mark.asyncio
async def test_missing_property_falls_back_to_the_live_count(fn: Any, label: str, prop: str) -> None:  # noqa: ARG001
    with (
        patch("api.queries.neo4j_queries.run_single", AsyncMock(return_value={"total": None})),
        patch("api.queries.neo4j_queries.run_count", AsyncMock(return_value=7)) as live,
    ):
        assert await fn(object(), "X") == 7
    live.assert_awaited_once()


@pytest.mark.asyncio
async def test_absent_node_falls_back_to_live_which_is_zero() -> None:
    with (
        patch("api.queries.neo4j_queries.run_single", AsyncMock(return_value=None)),
        patch("api.queries.neo4j_queries.run_count", AsyncMock(return_value=0)),
    ):
        assert await q.count_genre_releases(object(), "Nope") == 0


@pytest.mark.parametrize("fn", [fn for fn, _, _ in COUNTS])
@pytest.mark.asyncio
async def test_before_year_still_counts_live(fn: Any) -> None:
    with (
        patch("api.queries.neo4j_queries.run_single", AsyncMock(side_effect=AssertionError("stored count used with before_year"))),
        patch("api.queries.neo4j_queries.run_count", AsyncMock(return_value=3)) as live,
    ):
        assert await fn(object(), "X", before_year=1990) == 3
    live.assert_awaited_once()


@pytest.mark.parametrize(
    ("fn", "key", "value"),
    [(q.get_genre_details, "artist_count", 9), (q.get_style_details, "artist_count", 9), (q.get_label_details, "release_count", 9)],
)
@pytest.mark.asyncio
async def test_details_read_the_stored_count_in_one_statement(fn: Any, key: str, value: int) -> None:
    row = {"id": "x", "name": "x", key: value}
    with patch("api.queries.neo4j_queries.run_single", AsyncMock(return_value=row)) as single:
        assert await fn(object(), "x") == row
    assert single.await_count == 1
    assert "OPTIONAL MATCH" not in single.call_args.args[1]


@pytest.mark.parametrize(
    ("fn", "key"), [(q.get_genre_details, "artist_count"), (q.get_style_details, "artist_count"), (q.get_label_details, "release_count")]
)
@pytest.mark.asyncio
async def test_details_fall_back_live_only_when_the_property_is_null(fn: Any, key: str) -> None:
    stored = {"id": "x", "name": "x", key: None}
    live = {"id": "x", "name": "x", key: 5}
    with patch("api.queries.neo4j_queries.run_single", AsyncMock(side_effect=[stored, live])) as single:
        assert await fn(object(), "x") == live
    assert single.await_count == 2
    assert "OPTIONAL MATCH" in single.call_args_list[1].args[1]


@pytest.mark.parametrize("fn", [q.get_genre_details, q.get_style_details, q.get_label_details])
@pytest.mark.asyncio
async def test_details_of_an_unknown_node_are_none_without_a_second_query(fn: Any) -> None:
    with patch("api.queries.neo4j_queries.run_single", AsyncMock(return_value=None)) as single:
        assert await fn(object(), "nope") is None
    assert single.await_count == 1
