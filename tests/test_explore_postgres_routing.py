"""The Explore entry points must run without a Neo4j handle in PostgreSQL mode."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from api.nlq.tools import NLQToolRunner
from api.queries import explore_pg_queries, genre_tree_pg_queries, neo4j_pg_queries


def test_rest_explore_family_routes_to_postgres_without_neo4j(test_client: TestClient) -> None:
    import api.routers.explore as route

    pool = object()
    saved = (route._neo4j_driver, route._redis, route._pg_pool, route._graph_backend)
    try:
        route.configure(None, None, None, pg_pool=pool, graph_backend="postgres")
        with (
            patch.object(
                explore_pg_queries,
                "explore_artist",
                AsyncMock(return_value={"id": "1", "name": "Artist", "release_count": 1, "label_count": 0, "alias_count": 0}),
            ) as center,
            patch.object(
                explore_pg_queries,
                "expand_artist_releases",
                AsyncMock(return_value=[{"id": "10", "name": "Release", "type": "release", "year": 2000}]),
            ) as expand,
            patch.object(explore_pg_queries, "count_artist_releases", AsyncMock(return_value=1)) as count,
            patch.object(
                explore_pg_queries,
                "get_artist_details",
                AsyncMock(return_value={"id": "1", "name": "Artist", "genres": [], "styles": [], "release_count": 1, "groups": []}),
            ) as details,
            patch.object(explore_pg_queries, "trends_artist", AsyncMock(return_value=[{"year": 2000, "count": 1}])) as trends,
            patch.object(explore_pg_queries, "get_genre_emergence", AsyncMock(return_value={"genres": [], "styles": []})) as emergence,
            patch.object(genre_tree_pg_queries, "get_genre_tree", AsyncMock(return_value=[])) as tree,
            patch.object(neo4j_pg_queries, "get_year_range", AsyncMock(return_value={"min_year": 2000, "max_year": 2000})) as years,
            patch.object(neo4j_pg_queries, "get_graph_stats", AsyncMock(return_value={"artists": 1})) as stats,
        ):
            assert test_client.get("/api/explore?name=Artist&type=artist").status_code == 200
            assert test_client.get("/api/expand?node_id=Artist&type=artist&category=releases").json()["total"] == 1
            assert test_client.get("/api/node/1?type=artist").status_code == 200
            assert test_client.get("/api/trends?name=Artist&type=artist").json()["data"] == [{"year": 2000, "count": 1}]
            assert test_client.get("/api/explore/genre-emergence?before_year=2000").status_code == 200
            assert test_client.get("/api/genre-tree").json() == {"genres": []}
            assert test_client.get("/api/explore/year-range").json() == {"min_year": 2000, "max_year": 2000}
            assert test_client.get("/api/graph/stats").json() == {"total_entities": 1, "counts": {"artists": 1}}
        center.assert_awaited_once_with(pool, "Artist")
        expand.assert_awaited_once_with(pool, "Artist", 50, 0, before_year=None)
        count.assert_awaited_once_with(pool, "Artist", before_year=None)
        details.assert_awaited_once_with(pool, "1")
        trends.assert_awaited_once_with(pool, "Artist")
        emergence.assert_awaited_once_with(pool, 2000)
        tree.assert_awaited_once_with(pool)
        years.assert_awaited_once_with(pool)
        stats.assert_awaited_once_with(pool)
    finally:
        route.configure(saved[0], None, saved[1], pg_pool=saved[2], graph_backend=saved[3])


def test_expand_forwards_nondefault_pagination_and_year_to_postgres(test_client: TestClient) -> None:
    import api.routers.explore as route

    pool = object()
    saved = (route._neo4j_driver, route._redis, route._pg_pool, route._graph_backend)
    try:
        route.configure(None, None, None, pg_pool=pool, graph_backend="postgres")
        with (
            patch.object(explore_pg_queries, "expand_artist_releases", AsyncMock(return_value=[])) as expand,
            patch.object(explore_pg_queries, "count_artist_releases", AsyncMock(return_value=0)) as count,
        ):
            response = test_client.get("/api/expand?node_id=Artist&type=artist&category=releases&limit=7&offset=3&before_year=2001")
        assert response.status_code == 200
        expand.assert_awaited_once_with(pool, "Artist", 7, 3, before_year=2001)
        count.assert_awaited_once_with(pool, "Artist", before_year=2001)
    finally:
        route.configure(saved[0], None, saved[1], pg_pool=saved[2], graph_backend=saved[3])


@pytest.mark.asyncio
async def test_nlq_explore_trends_tree_and_stats_use_postgres_pool() -> None:
    pool = object()
    runner = NLQToolRunner(None, pool, None, graph_backend="postgres")
    with (
        patch("common.agent_tools.get_artist_details", new=AsyncMock(return_value={"id": "1"})) as explore,
        patch("common.agent_tools.get_trends", new=AsyncMock(return_value={"trends": []})) as trends,
        patch("common.agent_tools.get_genre_tree", new=AsyncMock(return_value={"genres": []})) as tree,
        patch("common.agent_tools.get_graph_stats", new=AsyncMock(return_value={"artists": 1})) as stats,
    ):
        await runner._handle_explore_entity({"type": "artist", "name": "Artist"}, None)
        await runner._handle_get_trends({"type": "artist", "name": "Artist"}, None)
        await runner._handle_get_genre_tree({}, None)
        await runner._handle_get_graph_stats({}, None)
    assert explore.await_args.kwargs == {"driver": pool, "name": "Artist", "handler": explore_pg_queries.explore_artist}
    assert trends.await_args.kwargs == {"driver": pool, "entity_type": "artist", "name": "Artist", "handler": explore_pg_queries.trends_artist}
    assert tree.await_args.kwargs == {"driver": pool, "tree_fn": genre_tree_pg_queries.get_genre_tree}
    assert stats.await_args.kwargs == {"driver": pool, "stats_fn": neo4j_pg_queries.get_graph_stats}
