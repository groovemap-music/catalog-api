"""Unit coverage for the PostgreSQL collection, taste, and gap families."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from api.queries import gap_pg_queries as gaps
from api.queries import taste_pg_queries as taste
from api.queries import user_pg_queries as users
from tests.fake_postgres import FakePool


pytestmark = pytest.mark.asyncio
USER = "00000000-0000-0000-0000-000000000001"


async def _unchanged(rows: list[dict[str, object]], **_kwargs: object) -> list[dict[str, object]]:
    return rows


class TestStatementContracts:
    async def test_collection_and_wantlist_read_views_and_catalog_number(self) -> None:
        assert "FROM graph.collected" in users.USER_COLLECTION_SQL
        assert "FROM graph.wants" in users.USER_WANTLIST_SQL
        assert "release.catalog_number" in users.USER_COLLECTION_SQL
        assert "release.catalog_number" in users.USER_WANTLIST_SQL

    async def test_recommendation_anti_joins_both_endpoint_sets(self) -> None:
        sql = users.USER_RECOMMENDATIONS_SQL
        assert sql.count("NOT EXISTS") == 2
        assert "owned.release_id::text = by_artist.release_id" in sql
        assert "wanted.release_id::text = by_artist.release_id" in sql

    async def test_blind_spots_have_candidate_and_zero_count_anti_joins(self) -> None:
        assert taste.BLIND_SPOTS_SQL.count("NOT EXISTS") == 2
        assert "owned.release_id::text = release.release_id" in taste.BLIND_SPOTS_SQL
        assert "owned_genre.genre_name = candidate.genre" in taste.BLIND_SPOTS_SQL

    async def test_gap_filters_keep_owned_and_wanted_endpoints_distinct(self) -> None:
        page, count = gaps._gap_sql(
            "on_label",
            "label_id",
            "NULL AS artist",
            exclude_wantlist=True,
            families=True,
            mediums=True,
        )
        for sql in (page, count):
            assert "owned.release_id::text = release.release_id" in sql
            assert "wanted.release_id::text = release.release_id" in sql
            assert "release.media_families && %(families)s::text[]" in sql
            assert "media.medium_id = ANY(%(mediums)s::text[])" in sql


class TestUserCollectionBackend:
    async def test_collection_and_wantlist_rows_and_totals(self) -> None:
        collection = [("1", "One", 2001, "CAT", "Artist", "Label", ["Genre"], ["Style"], 5, "2020-01-01T00:00:00Z", 2)]
        wantlist: list[tuple[Any, ...]] = [("2", "Two", None, None, None, None, [], [], 0, None)]
        with patch.object(users, "attach_release_identity", AsyncMock(side_effect=_unchanged)):
            assert await users.get_user_collection(FakePool([collection, [(1,)]]), USER) == (
                [
                    {
                        "id": "1",
                        "title": "One",
                        "year": 2001,
                        "catalog_number": "CAT",
                        "artist": "Artist",
                        "label": "Label",
                        "genres": ["Genre"],
                        "styles": ["Style"],
                        "rating": 5,
                        "date_added": "2020-01-01T00:00:00Z",
                        "folder_id": 2,
                    }
                ],
                1,
            )
            assert await users.get_user_wantlist(FakePool([wantlist, [(1,)]]), USER) == (
                [
                    {
                        "id": "2",
                        "title": "Two",
                        "year": None,
                        "catalog_number": None,
                        "artist": None,
                        "label": None,
                        "genres": [],
                        "styles": [],
                        "rating": 0,
                        "date_added": None,
                    }
                ],
                1,
            )

    async def test_recommendations_stats_and_status(self) -> None:
        recommendation = await users.get_user_recommendations(FakePool([[("3", "Three", 2003, "A", "L", ["G"], 2)]]), USER)
        assert recommendation == [{"id": "3", "title": "Three", "year": 2003, "artist": "A", "label": "L", "genres": ["G"], "score": 2}]
        stats = await users.get_user_collection_stats(
            FakePool([[(3, 2, 1, 4.5, [{"name": "G", "count": 3}], [{"decade": 2000, "count": 3}], [{"name": "L", "count": 3}])]]), USER
        )
        assert stats["average_rating"] == 4.5
        assert stats["by_decade"] == [{"decade": 2000, "count": 3}]
        assert await users.check_releases_user_status(FakePool([[("1", True, False), ("2", False, True)]]), USER, ["1", "2"]) == {
            "1": {"in_collection": True, "in_wantlist": False},
            "2": {"in_collection": False, "in_wantlist": True},
        }
        assert await users.check_releases_user_status(FakePool([]), USER, []) == {}

    async def test_timeline_evolution_and_invalid_metric(self) -> None:
        rows = [(2000, 2, [["Electronic"], ["Jazz"]], [["Ambient"], ["Techno"]], [["Label"], ["Label"]])]
        result = await users.get_user_collection_timeline(FakePool([rows]), USER)
        assert result["timeline"] == [
            {"year": 2000, "count": 2, "genres": {"Electronic": 1, "Jazz": 1}, "top_labels": ["Label"], "top_styles": ["Ambient", "Techno"]}
        ]
        assert result["insights"]["genre_diversity_score"] == 1.0
        evolution = await users.get_user_collection_evolution(FakePool([[("2001", "Electronic", 2), ("2002", "Jazz", 1)]]), USER, "genre")
        assert evolution["summary"] == {"total_years": 2, "unique_values": 2}
        with pytest.raises(ValueError, match="Invalid metric"):
            await users.get_user_collection_evolution(FakePool([]), USER, "country")


class TestTasteBackend:
    async def test_all_assemblies_and_empty_obscurity(self) -> None:
        assert await taste.get_collection_count(FakePool([[(3,)]]), USER) == 3
        assert await taste.get_taste_heatmap(FakePool([[("Electronic", 2000, 2)], [(3,)]]), USER) == (
            [{"genre": "Electronic", "decade": 2000, "count": 2}],
            3,
        )
        assert await taste.get_obscurity_score(FakePool([[(0,), (1,), (2,)]]), USER) == {
            "score": 0.5,
            "median_collectors": 1.0,
            "total_releases": 3,
        }
        assert await taste.get_obscurity_score(FakePool([[]]), USER) == {"score": 1.0, "median_collectors": 0.0, "total_releases": 0}
        assert await taste.get_taste_drift(FakePool([[("2020", "Jazz", 2)]]), USER) == [{"year": "2020", "top_genre": "Jazz", "count": 2}]
        assert await taste.get_blind_spots(FakePool([[("Ambient", 1, "Example")]]), USER) == [
            {"genre": "Ambient", "artist_overlap": 1, "example_release": "Example"}
        ]
        assert await taste.get_top_labels(FakePool([[("Label", 3)]]), USER) == [{"label": "Label", "count": 3}]


class TestGapBackend:
    @pytest.mark.parametrize(
        ("function", "args", "page", "expected"),
        [
            (gaps.get_label_gaps, (USER, "10"), [("1", "One", 2001, ["Vinyl"], "Artist", ["G"], True)], {"artist": "Artist"}),
            (gaps.get_artist_gaps, (USER, "20"), [("1", "One", 2001, ["Vinyl"], "Label", ["G"], False)], {"label": "Label"}),
            (
                gaps.get_master_gaps,
                (USER, "30"),
                [("1", "One", 2001, ["Vinyl"], "Artist", "Label", ["G"], False)],
                {"artist": "Artist", "label": "Label"},
            ),
        ],
    )
    async def test_gap_row_assembly(
        self, function: object, args: tuple[str, str], page: list[tuple[object, ...]], expected: dict[str, object]
    ) -> None:
        with patch.object(gaps, "attach_gap_identity", AsyncMock(side_effect=_unchanged)):
            results, total = await function(FakePool([page, [(1,)]]), *args)  # type: ignore[operator]
        assert total == 1
        assert results[0].items() >= expected.items()

    @pytest.mark.parametrize(
        ("function", "entity"),
        [
            (gaps.get_label_gap_summary, "10"),
            (gaps.get_artist_gap_summary, "20"),
            (gaps.get_master_gap_summary, "30"),
        ],
    )
    async def test_summaries(self, function: object, entity: str) -> None:
        assert await function(FakePool([[(5, 2)]]), USER, entity) == {"total": 5, "owned": 2, "missing": 3}  # type: ignore[operator]


class TestPostgresRouting:
    async def test_rest_routers_select_the_pool_and_postgres_modules(self) -> None:
        import api.routers.collection as collection_router
        import api.routers.recommend as recommend_router
        import api.routers.taste as taste_router
        import api.routers.user as user_router

        neo4j, pool = object(), object()
        saved_user = (user_router._neo4j_driver, user_router._pg_pool, user_router._graph_backend, user_router._user_backend)
        saved_taste = (taste_router._neo4j_driver, taste_router._pg_pool, taste_router._graph_backend, taste_router._taste_backend)
        saved_collection = (
            collection_router._neo4j_driver,
            collection_router._pg_pool,
            collection_router._graph_backend,
            collection_router._gap_backend,
            collection_router._metadata_backend,
        )
        saved_recommend = (
            recommend_router._neo4j_driver,
            recommend_router._pg_pool,
            recommend_router._graph_backend,
            recommend_router._taste_backend,
            recommend_router._cache,
        )
        try:
            user_router.configure(neo4j, None, "postgres", pool)
            taste_router.configure(neo4j, None, "postgres", pool)
            collection_router.configure(neo4j, pool, None, "postgres")
            recommend_router.configure(neo4j, None, None, "postgres", pool)
            assert user_router._handle() is pool
            assert user_router._query("get_user_collection") is users.get_user_collection
            assert taste_router._handle() is pool
            assert taste_router._query("get_taste_heatmap") is taste.get_taste_heatmap
            assert collection_router._handle() is pool
            assert collection_router._gap_query("get_label_gaps") is gaps.get_label_gaps
            assert recommend_router._taste_handle() is pool
            assert recommend_router._taste_query("get_blind_spots") is taste.get_blind_spots
        finally:
            user_router._neo4j_driver, user_router._pg_pool, user_router._graph_backend, user_router._user_backend = saved_user
            taste_router._neo4j_driver, taste_router._pg_pool, taste_router._graph_backend, taste_router._taste_backend = saved_taste
            (
                collection_router._neo4j_driver,
                collection_router._pg_pool,
                collection_router._graph_backend,
                collection_router._gap_backend,
                collection_router._metadata_backend,
            ) = saved_collection
            (
                recommend_router._neo4j_driver,
                recommend_router._pg_pool,
                recommend_router._graph_backend,
                recommend_router._taste_backend,
                recommend_router._cache,
            ) = saved_recommend

    async def test_nlq_tools_pass_the_pool_to_postgres_backends(self) -> None:
        from api.nlq.tools import NLQToolRunner

        neo4j, pool = object(), object()
        runner = NLQToolRunner(neo4j, pool, None, graph_backend="postgres")
        with patch.object(taste, "get_collection_count", AsyncMock(return_value=3)) as count:
            assert await runner._handle_get_collection_stats({}, USER) == {"collection_count": 3}
            count.assert_awaited_once_with(pool, USER)
        with patch.object(gaps, "get_label_gaps", AsyncMock(return_value=([{"id": "1"}], 1))) as label_gaps:
            assert await runner._handle_get_collection_gaps({"entity_type": "label", "entity_id": "10"}, USER) == {
                "gaps": [{"id": "1"}],
                "total": 1,
            }
            label_gaps.assert_awaited_once_with(pool, USER, "10", limit=50, families=[], mediums=[])
