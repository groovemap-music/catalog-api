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
    [(q.get_genre_details, "artist_count", 9), (q.get_style_details, "artist_count", 9)],
)
@pytest.mark.asyncio
async def test_details_read_the_stored_count_in_one_statement(fn: Any, key: str, value: int) -> None:
    row = {"id": "x", "name": "x", key: value}
    with patch("api.queries.neo4j_queries.run_single", AsyncMock(return_value=row)) as single:
        assert await fn(object(), "x") == row
    assert single.await_count == 1
    assert "OPTIONAL MATCH" not in single.call_args.args[1]


@pytest.mark.parametrize(("fn", "key"), [(q.get_genre_details, "artist_count"), (q.get_style_details, "artist_count")])
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


# --- get_label_details: stored count, placeholder verdict and longevity, all by label id ---


def _stored_label(release_count: int | None = 9, *, placeholder: bool = False, label_id: str = "7") -> dict[str, Any]:
    # an unswept graph (no stats_version): the longevity fields are computed live
    return {"id": label_id, "name": "Warp", "release_count": release_count, "is_placeholder": placeholder, "maintained": True, "stats_version": None}


def _swept_label(**fields: Any) -> dict[str, Any]:
    return {
        **_stored_label(40),
        "stats_version": 3,
        **{"first_year": None, "last_year": None, "years_active": None, "total_releases": 0, "peak_decade": None},
        **fields,
    }


def _years(**by_year: int) -> list[dict[str, Any]]:
    return [{"year": int(year[1:]) if year != "none" else None, "releases": n} for year, n in by_year.items()]


@pytest.mark.asyncio
async def test_label_details_add_longevity_and_keep_the_stored_count() -> None:
    rows = _years(y1989=2, y1991=5, y1995=3, y2001=10, none=4)
    with (
        patch("api.queries.neo4j_queries.run_single", AsyncMock(return_value=_stored_label(40))) as single,
        patch("api.queries.neo4j_queries.run_query", AsyncMock(return_value=rows)) as query,
    ):
        details = await q.get_label_details(object(), "7")
    assert details == {
        "id": "7",
        "name": "Warp",
        "release_count": 40,  # stored: counts the undated releases too
        "is_placeholder": False,
        "longevity_rank": None,
        "longevity_rank_of": None,
        "first_year": 1989,
        "last_year": 2001,
        "years_active": 13,
        "total_releases": 20,  # dated releases only, as in label-longevity
        "peak_decade": 2000,  # the 2000s hold 10 releases, the 1990s 5 + 3
    }
    assert single.call_args.kwargs["id"] == "7"
    assert query.call_args.kwargs["id"] == "7"  # by id, never by name


@pytest.mark.asyncio
async def test_peak_decade_ties_go_to_the_earlier_decade() -> None:
    rows = _years(y1985=4, y1995=4)
    with (
        patch("api.queries.neo4j_queries.run_single", AsyncMock(return_value=_stored_label())),
        patch("api.queries.neo4j_queries.run_query", AsyncMock(return_value=rows)),
    ):
        assert (await q.get_label_details(object(), "7"))["peak_decade"] == 1980


@pytest.mark.asyncio
async def test_label_without_a_dated_release_has_no_span() -> None:
    with (
        patch("api.queries.neo4j_queries.run_single", AsyncMock(return_value=_stored_label())),
        patch("api.queries.neo4j_queries.run_query", AsyncMock(return_value=_years(none=3, y0=2))),
    ):
        details = await q.get_label_details(object(), "7")
    assert (details["first_year"], details["last_year"], details["years_active"], details["peak_decade"]) == (None,) * 4
    assert details["total_releases"] == 0


@pytest.mark.asyncio
async def test_label_release_count_falls_back_to_the_traversal_when_not_stored() -> None:
    with (
        patch("api.queries.neo4j_queries.run_single", AsyncMock(return_value=_stored_label(None))),
        patch("api.queries.neo4j_queries.run_query", AsyncMock(return_value=_years(y2000=3, none=2))),
    ):
        assert (await q.get_label_details(object(), "7"))["release_count"] == 5


@pytest.mark.asyncio
async def test_two_labels_with_one_name_get_their_own_values() -> None:
    by_id = {"1": _years(y1990=3), "2": _years(y2010=8, y2020=1)}

    async def years(_driver: Any, _cypher: str, **params: Any) -> list[dict[str, Any]]:
        return by_id[params["id"]]

    async def stored(_driver: Any, _cypher: str, **params: Any) -> dict[str, Any]:
        return _stored_label(label_id=params["id"])

    with patch("api.queries.neo4j_queries.run_single", stored), patch("api.queries.neo4j_queries.run_query", years):
        first, second = await q.get_label_details(object(), "1"), await q.get_label_details(object(), "2")
    assert (first["first_year"], first["total_releases"], first["peak_decade"]) == (1990, 3, 1990)
    assert (second["first_year"], second["last_year"], second["total_releases"], second["peak_decade"]) == (2010, 2020, 9, 2010)


@pytest.mark.asyncio
async def test_label_placeholder_verdict_uses_the_longevity_rule_and_parameters() -> None:
    from api.queries import insights_neo4j_queries as insights

    with patch("api.queries.neo4j_queries.run_single", AsyncMock(return_value=None)) as single:
        assert await q.get_label_details(object(), "1818") is None
    kwargs = single.call_args.kwargs
    assert kwargs["placeholder_name"] == insights.PLACEHOLDER_LABEL_NAME
    assert kwargs["placeholder_ids"] == list(insights.PLACEHOLDER_LABEL_IDS)
    assert kwargs["real_lookalike_ids"] == list(insights.REAL_LABELS_NAMED_LIKE_PLACEHOLDERS)
    # the same expression as the ranking statement's
    assert "l.id IN $placeholder_ids" in single.call_args.args[1]
    assert "l.name =~ $placeholder_name AND NOT l.id IN $real_lookalike_ids" in single.call_args.args[1]


def test_graph_explorer_label_node_promises_the_longevity_fields() -> None:
    """The label node route promises is_placeholder and the longevity fields, and get_label_details returns them."""
    import asyncio
    import json
    from pathlib import Path

    from api.queries import neo4j_queries

    contract_path = Path(__file__).parents[1] / "api/contracts/graph-explorer/v1/routes.json"
    operation = json.loads(contract_path.read_text())["operations"]["node_label"]
    assert operation["query"] == {"type": "label"}
    stored = {"id": "7", "name": "Warp", "release_count": 3, "is_placeholder": False}
    with (
        patch.object(neo4j_queries, "run_single", AsyncMock(return_value=stored)),
        patch.object(neo4j_queries, "run_query", AsyncMock(return_value=[{"year": 1999, "releases": 3}])),
    ):
        details = asyncio.run(neo4j_queries.get_label_details(object(), "7"))  # type: ignore[arg-type]
    assert details is not None
    assert set(operation["response_fields"]) == set(details)


_SWEPT = {"first_year": 1989, "last_year": 2001, "years_active": 13, "total_releases": 20, "peak_decade": 2000}


@pytest.mark.asyncio
async def test_swept_label_reads_the_stored_longevity_without_a_traversal() -> None:
    with (
        patch("api.queries.neo4j_queries.run_single", AsyncMock(return_value=_swept_label(**_SWEPT))),
        patch("api.queries.neo4j_queries.run_query", AsyncMock()) as query,
    ):
        details = await q.get_label_details(object(), "7")
    assert query.await_count == 0
    assert details == {
        "id": "7",
        "name": "Warp",
        "release_count": 40,
        "is_placeholder": False,
        "longevity_rank": None,
        "longevity_rank_of": None,
        **_SWEPT,
    }


@pytest.mark.parametrize(
    ("gate", "expected"),
    [
        ({"stats_version": 3}, (None, None)),
        ({"stats_version": 4}, (12, 900)),
        ({"stats_version": 4, "longevity_rank": None}, (None, 900)),
        ({"stats_version": 4, "maintained": False}, (None, None)),
        ({"stats_version": None}, (None, None)),
    ],
)
@pytest.mark.asyncio
async def test_longevity_rank_is_read_from_the_graph_by_gate(gate: dict[str, Any], expected: tuple[int | None, int | None]) -> None:
    stored = _swept_label(**_SWEPT) | {"longevity_rank": 12, "longevity_rank_of": 900} | gate
    with (
        patch("api.queries.neo4j_queries.run_single", AsyncMock(return_value=stored)) as single,
        patch("api.queries.neo4j_queries.run_query", AsyncMock(return_value=_years(y2000=3))),
    ):
        details = await q.get_label_details(object(), "7")
    assert (details["longevity_rank"], details["longevity_rank_of"]) == expected
    assert single.await_count == 1  # the rank rides the existing label query: no extra round trip, no live rank


@pytest.mark.asyncio
async def test_stored_and_live_longevity_are_equal() -> None:
    rows = _years(y1989=2, y1991=5, y1995=3, y2001=10, none=4)
    with (
        patch("api.queries.neo4j_queries.run_single", AsyncMock(return_value=_swept_label(**_SWEPT))),
        patch("api.queries.neo4j_queries.run_query", AsyncMock(return_value=rows)),
    ):
        assert await q.get_label_details(object(), "7") == await _live(rows)


async def _live(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    with (
        patch("api.queries.neo4j_queries.run_single", AsyncMock(return_value=_stored_label(40))),
        patch("api.queries.neo4j_queries.run_query", AsyncMock(return_value=rows)),
    ):
        return await q.get_label_details(object(), "7")


@pytest.mark.asyncio
async def test_swept_label_without_dated_releases_has_total_zero_and_no_span() -> None:
    with (
        patch("api.queries.neo4j_queries.run_single", AsyncMock(return_value=_swept_label())),
        patch("api.queries.neo4j_queries.run_query", AsyncMock()) as query,
    ):
        details = await q.get_label_details(object(), "7")
    assert query.await_count == 0
    assert details["total_releases"] == 0
    assert (details["first_year"], details["last_year"], details["years_active"], details["peak_decade"]) == (None,) * 4


@pytest.mark.parametrize(
    "gate",
    [{"stats_version": 2}, {"stats_version": None}, {"maintained": False}, {"total_releases": None}],
)
@pytest.mark.asyncio
async def test_unswept_gate_falls_back_to_the_live_traversal(gate: dict[str, Any]) -> None:
    stored = _swept_label(**_SWEPT) | gate
    rows = _years(y2000=3)
    with (
        patch("api.queries.neo4j_queries.run_single", AsyncMock(return_value=stored)),
        patch("api.queries.neo4j_queries.run_query", AsyncMock(return_value=rows)) as query,
    ):
        details = await q.get_label_details(object(), "7")
    assert query.await_count == 1
    assert (details["first_year"], details["total_releases"]) == (2000, 3)
