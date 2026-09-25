"""SQL shape, assembly, cache and backend-routing contracts for family 7."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
from starlette.requests import Request

from api.dependencies import UnifiedAuth
from api.queries import fit_pg_queries as fit
from api.queries import recommend_pg_queries as recommend
from api.queries import user_pg_queries as users
from api.queries.fit_queries import empty_collection
from tests.fake_postgres import FakePool


pytestmark = pytest.mark.asyncio
USER = "00000000-0000-0000-0000-000000000001"


async def test_candidate_query_keeps_all_four_cost_caps_and_batches_profiles() -> None:
    sql = recommend.CANDIDATE_ARTISTS_SQL
    assert "ORDER BY release_id LIMIT 100000" in sql
    assert "rank_in_genre <= 500" in sql
    assert "LIMIT 200" in sql and "LIMIT 50" in sql
    assert "count(DISTINCT sample.release_id)" in sql
    assert "artist.name IS NOT NULL" in sql
    pool = FakePool(
        [
            [("1303", "Candidate", 3)],
            [("1303", "Electronic", 3)],
            [("1303", "Shared Style", 3)],
            [("1303", "Label", 3)],
            [],
        ]
    )
    assert await recommend.get_candidate_artists(pool, "1301") == [
        {
            "artist_id": "1303",
            "artist_name": "Candidate",
            "release_count": 3,
            "genres": [{"name": "Electronic", "count": 3}],
            "styles": [{"name": "Shared Style", "count": 3}],
            "labels": [{"name": "Label", "count": 3}],
            "collaborators": [],
        }
    ]
    assert len(pool.calls) == 5  # one candidate scan plus four dimension batches
    assert all(call.params == {"artist_ids": ["1303"]} for call in pool.calls[1:])


async def test_recommendation_anti_joins_and_assembly() -> None:
    label_sql = recommend.LABEL_AFFINITY_SQL
    assert "owned.user_id = %(user_id)s::uuid" in label_sql
    assert "wanted.user_id = %(user_id)s::uuid" in label_sql
    assert "owned.release_id::text = release.release_id" in label_sql
    assert "wanted.release_id::text = release.release_id" in label_sql
    assert "ORDER BY favorite.label_count DESC, release.release_id" in label_sql
    assert "owned.release_id::text = by_artist.release_id" in recommend.BLINDSPOT_CANDIDATES_SQL
    assert "owned_genre.genre_name = genre.genre_name" in recommend.BLINDSPOT_CANDIDATES_SQL

    label_pool = FakePool([[("1205", "Five", "Artist", "Label", 1995, ["Jazz"], 4)]])
    assert await recommend.get_label_affinity_candidates(label_pool, USER, limit=1) == [
        {
            "id": "1205",
            "title": "Five",
            "artist": "Artist",
            "label": "Label",
            "year": 1995,
            "genres": ["Jazz"],
            "score": 4,
            "source": "label: Label (top label)",
        }
    ]
    assert label_pool.params == {"user_id": USER, "limit": 1}
    blind_pool = FakePool([[("1221", "Rock", "Artist", None, None, "Rock", 1)]])
    assert await recommend.get_blindspot_candidates(blind_pool, USER) == [
        {
            "id": "1221",
            "title": "Rock",
            "artist": "Artist",
            "label": None,
            "year": None,
            "genres": ["Rock"],
            "score": 1,
            "source": "blind_spot: Rock",
        }
    ]
    counts = FakePool([[("1201", 3), ("1241", 0)]])
    assert await recommend.get_collector_counts(counts, ["1201", "1241"]) == {"1201": 3, "1241": 0}
    assert await recommend.get_collector_counts(FakePool(), []) == {}


async def test_fit_collection_uses_distinct_physical_release_and_preserves_cache_contract() -> None:
    assert "SELECT DISTINCT collected.release_id::text" in fit.COLLECTION_SQL
    assert "FROM graph.collected collected" in fit.COLLECTION_SQL
    cache = AsyncMock()
    cache.get.return_value = None
    pool = FakePool([[("1201", "One", ["1301"], ["1101"], ["Electronic"], ["Style"], [])]])
    folded = await fit.get_collection_ids(pool, USER, cache=cache)
    assert folded["release_ids"] == ["1201"]
    assert folded["artist_counts"] == {"1301": 1}
    cache.set.assert_awaited_once_with(f"recommend:explore:{USER}:fit:collection", folded, ttl=600)
    cache.get.return_value = folded
    assert await fit.get_collection_ids(FakePool(), USER, cache=cache) == folded


async def test_fit_context_nulls_siblings_and_missing_release() -> None:
    assert "sibling.release_id <> release.release_id" in fit.RELEASE_CONTEXT_SQL
    assert "LEFT JOIN graph.derived_from" in fit.RELEASE_CONTEXT_SQL
    pool = FakePool(
        [
            [
                (
                    "1203",
                    "Three",
                    1993,
                    [{"id": "1301", "name": "Artist"}],
                    [{"id": "1101", "name": "Label"}],
                    ["Electronic"],
                    [],
                    ["vinyl"],
                    "1401",
                    "Master",
                    [{"id": "1204", "title": "Four", "year": None, "media_families": []}],
                )
            ]
        ]
    )
    context = await fit.get_release_context(pool, "1203")
    assert context == {
        "id": "1203",
        "title": "Three",
        "year": 1993,
        "artists": [{"id": "1301", "name": "Artist"}],
        "labels": [{"id": "1101", "name": "Label"}],
        "genres": ["Electronic"],
        "styles": [],
        "media_families": ["vinyl"],
        "master_id": "1401",
        "master_title": "Master",
        "siblings": [{"id": "1204", "title": "Four", "year": None, "media_families": []}],
    }
    assert await fit.get_release_context(FakePool([[]]), "missing") is None
    no_master = FakePool([[("1241", "No Master", None, [], [], [], [], [], None, None, [])]])
    assert (await fit.get_release_context(no_master, "1241"))["master_id"] is None  # type: ignore[index]


async def test_postgres_routes_select_pool_without_touching_neo4j() -> None:
    import api.routers.fit as fit_router
    import api.routers.recommend as recommend_router
    import api.routers.user as user_router
    from api.nlq.tools import NLQToolRunner

    neo4j = object()
    pool = object()
    saved_fit = (fit_router._neo4j_driver, fit_router._pool, fit_router._graph_backend, fit_router._fit_backend, fit_router._cache)
    saved_recommend = (
        recommend_router._neo4j_driver,
        recommend_router._pg_pool,
        recommend_router._graph_backend,
        recommend_router._recommendations_backend,
        recommend_router._taste_backend,
        recommend_router._cache,
    )
    saved_user = (
        user_router._neo4j_driver,
        user_router._pg_pool,
        user_router._graph_backend,
        user_router._user_backend,
        user_router._recommendations_backend,
    )
    try:
        fit_router.configure(neo4j, pool, None, "postgres")
        recommend_router.configure(neo4j, None, None, "postgres", pool)
        user_router.configure(neo4j, None, "postgres", pool)
        fit_router._cache = None
        recommend_router._cache = None
        assert fit_router._fit_backend is fit
        assert recommend_router._recommend_handle() is pool
        assert recommend_router._recommend_query("get_candidate_artists") is recommend.get_candidate_artists
        assert user_router._handle() is pool
        assert user_router._recommend_query("get_label_affinity_candidates") is recommend.get_label_affinity_candidates

        runner = NLQToolRunner(neo4j, pool, None, graph_backend="postgres")
        with (
            patch.object(recommend, "get_artist_profile", AsyncMock(return_value={"genres": []})) as profile,
            patch.object(recommend, "get_candidate_artists", AsyncMock(return_value=[])) as candidates,
        ):
            assert await runner._handle_get_similar_artists({"artist_id": "1301"}, None) == {"artist_id": "1301", "similar": []}
            profile.assert_awaited_once_with(pool, "1301")
            candidates.assert_awaited_once_with(pool, "1301")

        request = Request({"type": "http", "method": "GET", "path": "/api/fit/release/1203"})
        context = {
            "id": "1203",
            "title": "Three",
            "year": 1993,
            "artists": [],
            "labels": [],
            "genres": [],
            "styles": [],
            "media_families": [],
            "master_id": None,
            "master_title": None,
            "siblings": [],
        }
        with (
            patch.object(fit, "get_release_context", AsyncMock(return_value=context)) as get_context,
            patch.object(fit, "get_collection_ids", AsyncMock(return_value=empty_collection())) as get_collection,
            patch.object(fit_router, "get_release_rarity", AsyncMock(return_value=None)),
            patch.object(fit_router, "native_ids_for", AsyncMock(return_value={})),
            patch.object(fit_router, "_stamp_impression", AsyncMock()),
        ):
            auth = UnifiedAuth(user_id=USER, via="jwt", token_id=None, scopes=[])
            response = await fit_router.release_fit(request, "1203", auth)
            assert response.status_code == 200
            get_context.assert_awaited_once_with(pool, "1203")
            get_collection.assert_awaited_once_with(pool, USER, cache=None)

        similar_request = Request({"type": "http", "method": "GET", "path": "/api/recommend/similar/artist/1301"})
        with (
            patch.object(
                recommend, "get_artist_identity", AsyncMock(return_value={"artist_id": "1301", "artist_name": "One", "release_count": 3})
            ) as identity,
            patch.object(recommend, "get_artist_profile", AsyncMock(return_value={"genres": []})) as profile,
            patch.object(recommend, "get_candidate_artists", AsyncMock(return_value=[])) as candidates,
            patch.object(recommend_router, "native_ids_for", AsyncMock(return_value={})),
            patch.object(recommend_router, "_record_similar_impressions", AsyncMock()),
        ):
            response = await recommend_router.similar_artists(similar_request, "1301", None, 20)
            assert response.status_code == 200
            identity.assert_awaited_once_with(pool, "1301")
            profile.assert_awaited_once_with(pool, "1301")
            candidates.assert_awaited_once_with(pool, "1301")

        with (
            patch.object(users, "get_user_recommendations", AsyncMock(return_value=[{"id": "1241", "title": "Candidate", "score": 1}])),
            patch.object(recommend, "get_label_affinity_candidates", AsyncMock(return_value=[])) as labels,
            patch.object(recommend, "get_blindspot_candidates", AsyncMock(return_value=[])) as blindspots,
            patch.object(recommend, "get_collector_counts", AsyncMock(return_value={"1241": 0})) as counts,
            patch.object(user_router, "native_ids_for", AsyncMock(return_value={})),
            patch.object(user_router.activity, "stamp_recommendation_impressions", AsyncMock()),
        ):
            response = await user_router.user_recommendations({"sub": USER}, 20, "multi")
            assert json.loads(response.body)["recommendations"][0]["id"] == "1241"
            labels.assert_awaited_once_with(pool, USER, limit=50)
            blindspots.assert_awaited_once_with(pool, USER, limit=50)
            counts.assert_awaited_once_with(pool, ["1241"])
    finally:
        fit_router._neo4j_driver, fit_router._pool, fit_router._graph_backend, fit_router._fit_backend, fit_router._cache = saved_fit
        (
            recommend_router._neo4j_driver,
            recommend_router._pg_pool,
            recommend_router._graph_backend,
            recommend_router._recommendations_backend,
            recommend_router._taste_backend,
            recommend_router._cache,
        ) = saved_recommend
        (
            user_router._neo4j_driver,
            user_router._pg_pool,
            user_router._graph_backend,
            user_router._user_backend,
            user_router._recommendations_backend,
        ) = saved_user
