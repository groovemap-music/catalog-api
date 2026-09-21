"""Unit tests for the PostgreSQL variable-length path adapter."""

import pytest

from api.queries.paths_pg_queries import (
    EXPLORE_TRAVERSAL_SQL,
    FIND_SHORTEST_PATH_SQL,
    HYDRATE_PATH_NODES_SQL,
    RESOLVE_PATH_KIND_SQL,
    find_shortest_path,
    get_explore_traversal,
)
from tests.fake_postgres import FakePool


@pytest.mark.asyncio
async def test_shortest_path_maps_kind_key_nodes_hydrates_names_and_preserves_rels() -> None:
    pool = FakePool(
        [
            [(True, 2, ["a:1", "r:101", "l:501"], ["BY", "ON"])],
            [("1", "Anchor", "a"), ("101", "Release 101", "r"), ("501", "Fixture Label", "l")],
        ]
    )

    result = await find_shortest_path(pool, "1", "501", max_depth=6, from_type="artist", to_type="label")

    assert result == {
        "nodes": [
            {"id": "1", "name": "Anchor", "labels": ["Artist"]},
            {"id": "101", "name": "Release 101", "labels": ["Release"]},
            {"id": "501", "name": "Fixture Label", "labels": ["Label"]},
        ],
        "rels": ["BY", "ON"],
    }
    assert pool.calls[0].sql == FIND_SHORTEST_PATH_SQL
    assert pool.calls[1].sql == HYDRATE_PATH_NODES_SQL
    assert pool.calls[0].params["from_kind"] == "a"
    assert pool.calls[0].params["to_kind"] == "l"


@pytest.mark.asyncio
async def test_shortest_path_normalizes_the_found_false_row_to_none() -> None:
    pool = FakePool([[(False, None, None, None)]])
    assert await find_shortest_path(pool, "1", "999", from_type="artist", to_type="artist") is None
    assert len(pool.calls) == 1


@pytest.mark.asyncio
async def test_shortest_path_resolves_an_omitted_kind_before_calling_the_function() -> None:
    pool = FakePool([[("a",)], [("l",)], [(False, None, None, None)]])
    assert await find_shortest_path(pool, "1", "501") is None
    assert pool.calls[0].sql == RESOLVE_PATH_KIND_SQL
    assert pool.calls[1].sql == RESOLVE_PATH_KIND_SQL
    assert pool.calls[2].sql == FIND_SHORTEST_PATH_SQL
    assert pool.calls[2].params["from_kind"] == "a"
    assert pool.calls[2].params["to_kind"] == "l"


@pytest.mark.asyncio
async def test_explore_always_passes_the_mandatory_row_limit() -> None:
    pool = FakePool([[("2", "Path Two", "artist", ["Path Zero", "Release 1301", "Path Two"], ["BY", "BY"], 2)]])

    result = await get_explore_traversal(pool, "artist", "1201", hops=3, row_limit=37)

    assert result[0]["dist"] == 2
    assert pool.calls[0].sql == EXPLORE_TRAVERSAL_SQL
    assert pool.calls[0].params == {"from_kind": "a", "from_key": "1201", "hops": 3, "row_limit": 37}


@pytest.mark.asyncio
async def test_explore_normalizes_an_out_of_range_hop_count_to_the_default() -> None:
    pool = FakePool([[]])

    assert await get_explore_traversal(pool, "artist", "1201", hops=99) == []
    assert pool.calls[0].params["hops"] == 2
