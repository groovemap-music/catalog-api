"""GET /api/recommend/similar/artist/{id} with the precomputed lists (gm-catalog-api-2zsq.1).

Every branch: switch off, switch on with a usable list (pure and fused), and each fallback --
no current release, artist absent from the release, tables missing, unreadable vectors.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from psycopg import errors

import api.routers.recommend as mod
from api.queries.similar_precomputed_queries import PrecomputedList
from tests.fake_postgres import FakePool


IDENTITY = {"artist_id": "a1", "artist_name": "Target", "release_count": 20}
TARGET_PROFILE = {"genres": [{"name": "Rock", "count": 10}], "styles": [], "labels": [], "collaborators": []}


def _profiled(artist_id: str, name: str, genre_count: int) -> dict[str, Any]:
    return {
        "artist_id": artist_id,
        "artist_name": name,
        "release_count": 9,
        "genres": [{"name": "Rock", "count": genre_count}] if genre_count else [{"name": "Jazz", "count": 3}],
        "styles": [],
        "labels": [],
        "collaborators": [],
    }


LIST = PrecomputedList(release_id=7, model_version="fastrp-v3", neighbours=(("n1", 0.9), ("n2", 0.6)))
HEURISTIC_CANDIDATES = [_profiled("h1", "Heuristic Only", 10)]
EXTRA_PROFILES = [_profiled("n1", "Neighbour One", 0), _profiled("n2", "Neighbour Two", 10)]


@pytest.fixture
def serve(test_client: TestClient) -> Iterator[Any]:
    """Configure the switch/alpha for one request and patch the graph and PostgreSQL reads."""
    saved = (mod._precomputed_enabled, mod._fusion_alpha, mod._pg_pool, mod._cache)
    mod._cache = None

    def _call(*, enabled: bool, alpha: float = 0.8, plist: PrecomputedList | None = LIST, cosines: dict[str, float] | None = None) -> Any:
        mod._precomputed_enabled = enabled
        mod._fusion_alpha = alpha
        mod._pg_pool = object()
        lookup = AsyncMock(return_value=plist)
        with (
            patch("api.routers.recommend.get_current_list", lookup),
            patch("api.routers.recommend.get_artist_identity", AsyncMock(return_value=IDENTITY)),
            patch("api.routers.recommend.get_artist_profile", AsyncMock(return_value=TARGET_PROFILE)),
            patch("api.routers.recommend.get_candidate_artists", AsyncMock(return_value=HEURISTIC_CANDIDATES)),
            patch("api.routers.recommend.get_profiled_artists", AsyncMock(return_value=EXTRA_PROFILES)),
            patch("api.routers.recommend.get_embedding_cosines", AsyncMock(return_value={"h1": 0.5} if cosines is None else cosines)),
        ):
            response = test_client.get("/api/recommend/similar/artist/a1")
        return response, lookup

    yield _call
    mod._precomputed_enabled, mod._fusion_alpha, mod._pg_pool, mod._cache = saved


def _ids(response: Any) -> list[str]:
    return [row["artist_id"] for row in response.json()["similar"]]


def test_switch_off_serves_the_heuristic_and_never_reads_the_lists(serve: Any) -> None:
    response, lookup = serve(enabled=False)
    assert response.status_code == 200
    assert _ids(response) == ["h1"]
    lookup.assert_not_called()


def test_pure_list_is_top_k_by_stored_score_in_list_order(serve: Any) -> None:
    response, _ = serve(enabled=True, alpha=1.0)
    rows = response.json()["similar"]
    assert [(r["artist_id"], r["similarity"]) for r in rows] == [("n1", 0.9), ("n2", 0.6)]
    assert rows[0]["artist_name"] == "Neighbour One"
    assert rows[0]["breakdown"]["embedding"] == 0.9


def test_fused_ranks_the_union_by_alpha_weighted_score(serve: Any) -> None:
    response, _ = serve(enabled=True, alpha=0.8)
    rows = {r["artist_id"]: r for r in response.json()["similar"]}
    # n1: cosine .9, no heuristic overlap (Jazz vs Rock) -> .8*.9; n2: .8*.6 + .2*heuristic;
    # h1: cosine from stored vectors .5, heuristic from the graph.
    assert set(rows) == {"n1", "n2", "h1"}
    assert rows["n1"]["similarity"] == pytest.approx(0.72, abs=1e-4)
    assert rows["h1"]["breakdown"]["embedding"] == 0.5
    assert rows["n2"]["similarity"] > 0.8 * 0.6
    scores = [r["similarity"] for r in response.json()["similar"]]
    assert scores == sorted(scores, reverse=True)


def test_fused_heuristic_only_candidate_without_a_vector_gets_cosine_zero(serve: Any) -> None:
    response, _ = serve(enabled=True, alpha=0.8, cosines={})
    h1 = next(r for r in response.json()["similar"] if r["artist_id"] == "h1")
    assert h1["breakdown"]["embedding"] == 0.0


@pytest.mark.parametrize("plist", [None], ids=["no-current-release-or-artist-absent-or-tables-missing"])
def test_no_usable_list_falls_back_to_the_heuristic(serve: Any, plist: None) -> None:
    response, lookup = serve(enabled=True, plist=plist)
    lookup.assert_awaited_once()
    assert response.status_code == 200
    assert _ids(response) == ["h1"]
    assert "embedding" not in response.json()["similar"][0]["breakdown"]


def test_unreadable_vectors_fall_back_to_the_heuristic(test_client: TestClient, serve: Any) -> None:  # noqa: ARG001 -- serve restores the router state
    with patch("api.routers.recommend.get_embedding_cosines", AsyncMock(return_value=None)):
        mod._precomputed_enabled, mod._fusion_alpha = True, 0.8
        with (
            patch("api.routers.recommend.get_current_list", AsyncMock(return_value=LIST)),
            patch("api.routers.recommend.get_artist_identity", AsyncMock(return_value=IDENTITY)),
            patch("api.routers.recommend.get_artist_profile", AsyncMock(return_value=TARGET_PROFILE)),
            patch("api.routers.recommend.get_candidate_artists", AsyncMock(return_value=HEURISTIC_CANDIDATES)),
            patch("api.routers.recommend.get_profiled_artists", AsyncMock(return_value=EXTRA_PROFILES)),
        ):
            response = test_client.get("/api/recommend/similar/artist/a1")
    assert response.status_code == 200
    assert _ids(response) == ["h1"]


@pytest.mark.parametrize(
    "pool",
    [
        FakePool([[]]),  # no current release
        FakePool([[(7, "fastrp-v3", None, None)]]),  # artist absent from the release
        FakePool(raise_on={"": errors.UndefinedTable("artist_embedding_releases")}),  # tables missing
    ],
    ids=["no-release", "artist-absent", "tables-missing"],
)
def test_fallbacks_end_to_end_through_the_real_lookup(test_client: TestClient, pool: FakePool) -> None:
    saved = (mod._precomputed_enabled, mod._pg_pool, mod._cache)
    mod._precomputed_enabled, mod._pg_pool, mod._cache = True, pool, None
    try:
        with (
            patch("api.routers.recommend.get_artist_identity", AsyncMock(return_value=IDENTITY)),
            patch("api.routers.recommend.get_artist_profile", AsyncMock(return_value=TARGET_PROFILE)),
            patch("api.routers.recommend.get_candidate_artists", AsyncMock(return_value=HEURISTIC_CANDIDATES)),
        ):
            response = test_client.get("/api/recommend/similar/artist/a1")
    finally:
        mod._precomputed_enabled, mod._pg_pool, mod._cache = saved
    assert response.status_code == 200
    assert _ids(response) == ["h1"]


def test_precomputed_and_heuristic_bodies_are_cached_under_different_keys() -> None:
    mod._fusion_alpha = 0.8
    assert mod._similar_cache_key("a1", None) == "recommend:similar:artist:a1"
    assert mod._similar_cache_key("a1", LIST) == "recommend:similar:artist:a1:precomputed:fastrp-v3:0.8"
    assert mod._similar_cache_key("a1", LIST) != mod._similar_cache_key("a1", PrecomputedList(8, "fastrp-v4", ()))


def test_precomputed_impressions_use_their_own_policy(serve: Any) -> None:
    with patch("api.activity.stamp_recommendation_impressions", AsyncMock()) as stamp:
        serve(enabled=True, alpha=1.0)
        assert stamp.await_args.args[1] == "similar_artist_precomputed_fused_v1"
        serve(enabled=False)
        assert stamp.await_args.args[1] == "similar_artist_weighted_cosine_v1"
