"""Every read path carries the native id beside the provider id it always carried.

ADR 0009 is additive: a response gains `gm_id` (or `gm_item_id` and `owned_copy_id` on
the user's own rows) and loses nothing. Each shape is checked twice — once where the
alias table resolves the entity, and once where it does not, because the projection is
incomplete for a long stretch of this program and a consumer must be able to tell the two
apart from the response rather than from a second lookup.
"""

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient


RELEASE_GM = "01890a5d-ac96-774b-bcce-b302099a8057"
ARTIST_GM = "01890a5d-ac96-774b-bcce-b302099a8058"
LABEL_GM = "01890a5d-ac96-774b-bcce-b302099a8059"
COPY_GM = "01890a5d-ac96-774b-bcce-b302099a805a"


class _AsyncIter:
    def __init__(self, records: list[dict[str, Any]]) -> None:
        self._records = records
        self._index = 0

    def __aiter__(self) -> _AsyncIter:
        return self

    async def __anext__(self) -> dict[str, Any]:
        if self._index >= len(self._records):
            raise StopAsyncIteration
        record = self._records[self._index]
        self._index += 1
        return record


class _MockResult:
    def __init__(self, records: list[dict[str, Any]] | None = None, single: dict[str, Any] | None = None) -> None:
        self._records = records or []
        self._single = single

    def __aiter__(self) -> _AsyncIter:
        return _AsyncIter(self._records)

    async def single(self) -> dict[str, Any] | None:
        return self._single

    async def consume(self) -> Any:
        return None


def _driver(results: list[_MockResult]) -> Any:
    """Build a Neo4j driver whose session().run() walks `results` in order."""
    from unittest.mock import MagicMock

    results_iter = iter(results)

    session = AsyncMock()
    session.__aenter__.return_value = session
    session.__aexit__.return_value = False

    async def _run(*_a: Any, **_kw: Any) -> _MockResult:
        return next(results_iter)

    session.run = AsyncMock(side_effect=_run)

    driver = MagicMock()
    driver.session = MagicMock(return_value=session)
    return driver


# ---------------------------------------------------------------------------
# Search hits
# ---------------------------------------------------------------------------


class TestSearchHitNativeId:
    """`GET /api/search` hits gain `gm_id`."""

    def test_format_result_carries_a_resolved_native_id(self) -> None:
        from api.queries.search_queries import _format_result

        row = {"type": "release", "id": "1", "name": "Kid A", "rank": 0.5, "highlight": "Kid A", "year": 2000, "genres": None}
        result = _format_result(row, {("release", "1"): RELEASE_GM})

        assert result["gm_id"] == RELEASE_GM
        # The provider id is untouched beside it.
        assert result["id"] == "1"

    def test_format_result_is_none_without_an_alias(self) -> None:
        from api.queries.search_queries import _format_result

        row = {"type": "release", "id": "1", "name": "Kid A", "rank": 0.5, "highlight": "Kid A", "year": 2000, "genres": None}

        assert _format_result(row, {})["gm_id"] is None
        assert _format_result(row)["gm_id"] is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("resolved", "expected"),
        [({("artist", "7"): ARTIST_GM}, ARTIST_GM), ({}, None)],
    )
    async def test_execute_search_resolves_the_whole_page(self, resolved: dict[tuple[str, str], str], expected: str | None) -> None:
        from api.queries import search_queries

        rows = [{"type": "artist", "id": "7", "name": "Autechre", "rank": 0.9, "highlight": "Autechre", "year": None, "genres": None}]
        with (
            patch.object(search_queries, "_run_results", AsyncMock(return_value=rows)),
            patch.object(search_queries, "_run_total", AsyncMock(return_value=1)),
            patch.object(search_queries, "_run_type_counts", AsyncMock(return_value={"artist": 1})),
            patch.object(search_queries, "_run_genre_facets", AsyncMock(return_value={})),
            patch.object(search_queries, "_run_decade_facets", AsyncMock(return_value={})),
            patch.object(search_queries, "_run_media_facets", AsyncMock(return_value={})),
            patch.object(search_queries, "native_ids_for_pairs", AsyncMock(return_value=resolved)) as lookup,
        ):
            response = await search_queries.execute_search(AsyncMock(), None, "aut", ["artist"], [], None, None, 20, 0)

        assert response["results"][0]["gm_id"] == expected
        assert response["results"][0]["id"] == "7"
        # One lookup for the page, not one per hit.
        assert lookup.await_count == 1


# ---------------------------------------------------------------------------
# Collection, wantlist, gaps
# ---------------------------------------------------------------------------


class TestCollectionNativeIds:
    """Collection rows gain both `gm_item_id` and `owned_copy_id`."""

    @pytest.mark.asyncio
    async def test_resolved_item_and_copy(self) -> None:
        from api.queries import user_queries

        rows = [{"id": "1", "title": "Kid A", "year": 2000, "rating": 5}]
        driver = _driver([_MockResult(records=rows), _MockResult(single={"total": 1})])

        with (
            patch.object(user_queries, "native_ids_for", AsyncMock(return_value={"1": RELEASE_GM})),
            patch.object(user_queries, "lookup_owned_copy_ids", AsyncMock(return_value={"1": COPY_GM})) as copies,
        ):
            results, total = await user_queries.get_user_collection(driver, "user-1", limit=50, offset=0)

        assert results[0]["gm_item_id"] == RELEASE_GM
        assert results[0]["owned_copy_id"] == COPY_GM
        assert results[0]["title"] == "Kid A"
        assert total == 1
        # The copy lookup is owner-scoped.
        assert copies.await_args.args[0] == "user-1"

    @pytest.mark.asyncio
    async def test_unresolved_item_and_copy_are_none(self) -> None:
        from api.queries import user_queries

        rows = [{"id": "1", "title": "Kid A", "year": 2000, "rating": 5}]
        driver = _driver([_MockResult(records=rows), _MockResult(single={"total": 1})])

        with (
            patch.object(user_queries, "native_ids_for", AsyncMock(return_value={})),
            patch.object(user_queries, "lookup_owned_copy_ids", AsyncMock(return_value={})),
        ):
            results, _total = await user_queries.get_user_collection(driver, "user-1", limit=50, offset=0)

        assert results[0]["gm_item_id"] is None
        assert results[0]["owned_copy_id"] is None
        assert results[0]["rating"] == 5

    @pytest.mark.asyncio
    async def test_empty_collection_needs_no_lookup(self) -> None:
        from api.queries import user_queries

        driver = _driver([_MockResult(records=[]), _MockResult(single={"total": 0})])

        with patch.object(user_queries, "native_ids_for", AsyncMock()) as lookup:
            results, total = await user_queries.get_user_collection(driver, "user-1", limit=50, offset=0)

        assert (results, total) == ([], 0)
        lookup.assert_not_awaited()


class TestWantlistNativeIds:
    """A wantlist row names a release the user does not hold, so there is no copy."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("resolved", "expected"), [({"2": RELEASE_GM}, RELEASE_GM), ({}, None)])
    async def test_gm_item_id_only(self, resolved: dict[str, str], expected: str | None) -> None:
        from api.queries import user_queries

        rows = [{"id": "2", "title": "Amnesiac", "year": 2001}]
        driver = _driver([_MockResult(records=rows), _MockResult(single={"total": 1})])

        with (
            patch.object(user_queries, "native_ids_for", AsyncMock(return_value=resolved)),
            patch.object(user_queries, "lookup_owned_copy_ids", AsyncMock()) as copies,
        ):
            results, _total = await user_queries.get_user_wantlist(driver, "user-1", limit=50, offset=0)

        assert results[0]["gm_item_id"] == expected
        assert "owned_copy_id" not in results[0]
        copies.assert_not_awaited()


class TestGapNativeIds:
    """Gap rows name releases the user does not own, so they gain `gm_item_id` alone."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("getter", ["get_label_gaps", "get_artist_gaps", "get_master_gaps"])
    @pytest.mark.parametrize(("resolved", "expected"), [({"9": RELEASE_GM}, RELEASE_GM), ({}, None)])
    async def test_gap_rows_carry_gm_item_id(self, getter: str, resolved: dict[str, str], expected: str | None) -> None:
        from api.queries import gap_queries

        rows = [{"id": "9", "title": "Blue Monday", "year": 1983, "on_wantlist": False}]
        driver = _driver([_MockResult(records=rows), _MockResult(single={"total": 1})])

        with patch.object(gap_queries, "native_ids_for", AsyncMock(return_value=resolved)):
            results, total = await getattr(gap_queries, getter)(driver, "user-1", "entity-1", limit=50, offset=0)

        assert results[0]["gm_item_id"] == expected
        assert results[0]["on_wantlist"] is False
        assert "owned_copy_id" not in results[0]
        assert total == 1

    @pytest.mark.asyncio
    async def test_empty_gaps_need_no_lookup(self) -> None:
        from api.queries import gap_queries

        driver = _driver([_MockResult(records=[]), _MockResult(single={"total": 0})])

        with patch.object(gap_queries, "native_ids_for", AsyncMock()) as lookup:
            results, _total = await gap_queries.get_label_gaps(driver, "user-1", "label-1")

        assert results == []
        lookup.assert_not_awaited()


# ---------------------------------------------------------------------------
# Recommendations
# ---------------------------------------------------------------------------


_IDENTITY = {"artist_id": "a1", "artist_name": "Test Artist", "release_count": 20}
_PROFILE = {
    "genres": [{"name": "Rock", "count": 80}],
    "styles": [{"name": "Punk", "count": 40}],
    "labels": [{"name": "Sub Pop", "count": 10}],
    "collaborators": [],
}
_CANDIDATES = [
    {
        "artist_id": "a2",
        "artist_name": "Similar Artist",
        "release_count": 15,
        "genres": [{"name": "Rock", "count": 60}],
        "styles": [{"name": "Punk", "count": 30}],
        "labels": [{"name": "Sub Pop", "count": 8}],
        "collaborators": [],
    },
]


class TestSimilarArtistNativeId:
    """`SimilarArtist` carries `gm_id` beside the Discogs `artist_id`."""

    @pytest.mark.parametrize(("resolved", "expected"), [({"a2": ARTIST_GM}, ARTIST_GM), ({}, None)])
    def test_similar_artists(self, test_client: TestClient, resolved: dict[str, str], expected: str | None) -> None:
        with (
            patch("api.routers.recommend.get_artist_identity", AsyncMock(return_value=_IDENTITY)),
            patch("api.routers.recommend.get_artist_profile", AsyncMock(return_value=_PROFILE)),
            patch("api.routers.recommend.get_candidate_artists", AsyncMock(return_value=_CANDIDATES)),
            patch("api.routers.recommend.native_ids_for", AsyncMock(return_value=resolved)),
        ):
            response = test_client.get("/api/recommend/similar/artist/a1?limit=5")

        assert response.status_code == 200
        similar = response.json()["similar"][0]
        assert similar["gm_id"] == expected
        assert similar["artist_id"] == "a2"


class TestExploreNativeIds:
    """`EntityRef` and `DiscoveryNode` both carry `gm_id`."""

    def _run(self, test_client: TestClient, auth_headers: dict[str, str], resolved: dict[tuple[str, str], str], path: str) -> dict[str, Any]:
        traversal = [
            {
                "id": "l1",
                "name": "Warp Records",
                "type": "label",
                "path_names": ["Aphex Twin", "SAW", "Warp Records"],
                "rel_types": ["BY", "ON"],
                "dist": 2,
            },
        ]
        with (
            patch("api.routers.recommend.get_explore_traversal", AsyncMock(return_value=traversal)),
            patch("api.routers.recommend.get_taste_heatmap", AsyncMock(return_value=([{"genre": "Electronic", "decade": 1990, "count": 10}], 10))),
            patch("api.routers.recommend.get_blind_spots", AsyncMock(return_value=[])),
            patch("api.routers.recommend.native_ids_for_pairs", AsyncMock(return_value=resolved)),
        ):
            response = test_client.get(path, headers=auth_headers)
        assert response.status_code == 200
        return dict(response.json())

    def test_resolved_entity_and_discovery(self, test_client: TestClient, auth_headers: dict[str, str]) -> None:
        data = self._run(
            test_client,
            auth_headers,
            {("artist", "a1"): ARTIST_GM, ("label", "l1"): LABEL_GM},
            "/api/recommend/explore/artist/a1?hops=2&limit=5",
        )

        assert data["from"]["gm_id"] == ARTIST_GM
        assert data["from"]["id"] == "a1"
        assert data["discoveries"][0]["gm_id"] == LABEL_GM
        assert data["discoveries"][0]["id"] == "l1"

    def test_unresolved_entity_and_discovery(self, test_client: TestClient, auth_headers: dict[str, str]) -> None:
        data = self._run(test_client, auth_headers, {}, "/api/recommend/explore/artist/a1?hops=2&limit=5")

        assert data["from"]["gm_id"] is None
        assert data["discoveries"][0]["gm_id"] is None

    def test_name_keyed_entity_never_carries_one(self, test_client: TestClient, auth_headers: dict[str, str]) -> None:
        """A genre is name-keyed rather than a catalog entity, so it can have no native id."""
        data = self._run(
            test_client,
            auth_headers,
            {("label", "l1"): LABEL_GM},
            "/api/recommend/explore/genre/Rock?hops=2&limit=5",
        )

        assert data["from"]["type"] == "genre"
        assert data["from"]["gm_id"] is None
        assert data["discoveries"][0]["gm_id"] == LABEL_GM


class TestUserRecommendationNativeIds:
    """Both `/api/user/recommendations` strategies carry `gm_id` per recommendation."""

    @pytest.mark.parametrize(("resolved", "expected"), [({"r1": RELEASE_GM}, RELEASE_GM), ({}, None)])
    def test_artist_strategy(self, test_client: TestClient, auth_headers: dict[str, str], resolved: dict[str, str], expected: str | None) -> None:
        rows = [{"id": "r1", "title": "Kid A", "artist": "Radiohead", "score": 4}]
        with (
            patch("api.routers.user.get_user_recommendations", AsyncMock(return_value=rows)),
            patch("api.routers.user.native_ids_for", AsyncMock(return_value=resolved)),
        ):
            response = test_client.get("/api/user/recommendations?strategy=artist", headers=auth_headers)

        assert response.status_code == 200
        recommendation = response.json()["recommendations"][0]
        assert recommendation["gm_id"] == expected
        assert recommendation["id"] == "r1"

    @pytest.mark.parametrize(("resolved", "expected"), [({"r1": RELEASE_GM}, RELEASE_GM), ({}, None)])
    def test_multi_strategy(self, test_client: TestClient, auth_headers: dict[str, str], resolved: dict[str, str], expected: str | None) -> None:
        rows = [{"id": "r1", "title": "Kid A", "artist": "Radiohead", "score": 4}]
        with (
            patch("api.routers.user.get_user_recommendations", AsyncMock(return_value=rows)),
            patch("api.routers.user.get_label_affinity_candidates", AsyncMock(return_value=[])),
            patch("api.routers.user.get_blindspot_candidates", AsyncMock(return_value=[])),
            patch("api.routers.user.get_collector_counts", AsyncMock(return_value={})),
            patch("api.routers.user.native_ids_for", AsyncMock(return_value=resolved)),
        ):
            response = test_client.get("/api/user/recommendations?strategy=multi", headers=auth_headers)

        assert response.status_code == 200
        recommendation = response.json()["recommendations"][0]
        assert recommendation["gm_id"] == expected
        assert recommendation["id"] == "r1"


class TestModelDefaults:
    """The four models default `gm_id` to None, so an existing constructor still works."""

    def test_models_default_to_none(self) -> None:
        from api.models import DiscoveryNode, EnhancedRecommendation, EntityRef, SimilarArtist

        assert EntityRef(id="a1", name="X", type="artist").gm_id is None
        assert DiscoveryNode(id="l1", name="Warp", type="label", score=0.5, path=[], reason="graph_proximity").gm_id is None
        assert EnhancedRecommendation(id="r1", score=0.5).gm_id is None
        assert (
            SimilarArtist(artist_id="a2", artist_name="Y", similarity=0.5, breakdown={}, release_count=1, shared_genres=[], shared_labels=[]).gm_id
            is None
        )

    def test_enhanced_recommendation_accepts_a_native_id(self) -> None:
        from api.models import EnhancedRecommendation

        assert EnhancedRecommendation(id="r1", score=0.5, gm_id=RELEASE_GM).gm_id == RELEASE_GM
