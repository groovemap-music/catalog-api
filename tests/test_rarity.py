"""Tests for rarity API endpoints."""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient


_MOCK_RARITY_ROW = {
    "release_id": 456,
    "title": "Test Release",
    "artist_name": "Test Artist",
    "year": 1968,
    "rarity_score": 87.2,
    "tier": "ultra-rare",
    "hidden_gem_score": 72.1,
    "pressing_scarcity": 95.0,
    "label_catalog": 80.0,
    "format_rarity": 70.0,
    "temporal_scarcity": 92.0,
    "graph_isolation": 65.0,
}

# A row as the media-neutral core writes it (ADR 0007): the additive columns are populated,
# and pressing scarcity lives in family_signals rather than on its own column.
_MOCK_MEDIA_ROW = {
    "release_id": 456,
    "title": "Test Release",
    "artist_name": "Test Artist",
    "year": 1968,
    "rarity_score": 87.2,
    "tier": "ultra-rare",
    "hidden_gem_score": 72.1,
    "pressing_scarcity": 95.0,
    "label_catalog": 80.0,
    "format_rarity": 70.0,
    "temporal_scarcity": 92.0,
    "graph_isolation": 65.0,
    "collection_prevalence": 55.0,
    "medium_rarity": 40.0,
    "media_families": ["vinyl"],
    "family_signals": {"grooved": {"pressing_scarcity": 95.0}},
}

# The same release on a medium no family extension claims.
_MOCK_CD_ROW = {
    **_MOCK_MEDIA_ROW,
    "pressing_scarcity": None,
    "medium_rarity": 10.0,
    "media_families": ["optical"],
    "family_signals": {},
}

_MOCK_LIST_ITEM = {
    "release_id": 456,
    "title": "Test Release",
    "artist_name": "Test Artist",
    "year": 1968,
    "rarity_score": 87.2,
    "tier": "ultra-rare",
    "hidden_gem_score": 72.1,
}


class TestGetReleaseRarity:
    def test_success(self, test_client: TestClient) -> None:
        with patch(
            "api.routers.rarity.get_rarity_for_release",
            new=AsyncMock(return_value=_MOCK_RARITY_ROW),
        ):
            response = test_client.get("/api/rarity/456")
        assert response.status_code == 200
        data = response.json()
        assert data["release_id"] == 456
        assert data["tier"] == "ultra-rare"
        assert "breakdown" in data
        # A pre-ADR-0007 row stores its family signals as flat columns; the breakdown
        # attributes them back to the module that declares them.
        assert data["breakdown"]["pressing_scarcity"]["score"] == 95.0
        assert data["family_signals"] == {"grooved": {"pressing_scarcity": 95.0}}

    def test_media_neutral_row_reports_family_signals_and_families(self, test_client: TestClient) -> None:
        with patch(
            "api.routers.rarity.get_rarity_for_release",
            new=AsyncMock(return_value=_MOCK_MEDIA_ROW),
        ):
            response = test_client.get("/api/rarity/456")
        data = response.json()

        assert data["media_families"] == ["vinyl"]
        assert data["family_signals"] == {"grooved": {"pressing_scarcity": 95.0}}
        assert data["breakdown"]["medium_rarity"]["score"] == 40.0
        assert data["breakdown"]["pressing_scarcity"]["weight"] == 0.25
        # The deprecated signal is still reported, but no longer scored.
        assert data["breakdown"]["format_rarity"] == {"score": 70.0, "weight": 0.0}
        scored = {name: signal["weight"] for name, signal in data["breakdown"].items() if name != "format_rarity"}
        assert sum(scored.values()) == pytest.approx(1.0)

    def test_non_grooved_row_has_no_pressing_entry_and_still_sums_to_one(self, test_client: TestClient) -> None:
        with patch(
            "api.routers.rarity.get_rarity_for_release",
            new=AsyncMock(return_value=_MOCK_CD_ROW),
        ):
            response = test_client.get("/api/rarity/456")
        data = response.json()

        assert data["media_families"] == ["optical"]
        assert data["family_signals"] == {}
        assert "pressing_scarcity" not in data["breakdown"]
        scored = {name: signal["weight"] for name, signal in data["breakdown"].items() if name != "format_rarity"}
        assert sum(scored.values()) == pytest.approx(1.0)
        # Renormalised: the five core signals absorb the grooved module's 0.25.
        assert scored["medium_rarity"] == pytest.approx(0.10 / 0.75)

    def test_not_found(self, test_client: TestClient) -> None:
        with patch(
            "api.routers.rarity.get_rarity_for_release",
            new=AsyncMock(return_value=None),
        ):
            response = test_client.get("/api/rarity/999")
        assert response.status_code == 404

    def test_503_when_not_ready(self, test_client: TestClient) -> None:
        import api.routers.rarity as rarity_router

        original = rarity_router._pg_pool
        rarity_router._pg_pool = None
        try:
            response = test_client.get("/api/rarity/456")
            assert response.status_code == 503
        finally:
            rarity_router._pg_pool = original


class TestRarityLeaderboard:
    def test_success(self, test_client: TestClient) -> None:
        with patch(
            "api.routers.rarity.get_rarity_leaderboard",
            new=AsyncMock(return_value=([_MOCK_LIST_ITEM], 100)),
        ):
            response = test_client.get("/api/rarity/leaderboard")
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 100
        assert len(data["items"]) == 1

    def test_pagination(self, test_client: TestClient) -> None:
        with patch(
            "api.routers.rarity.get_rarity_leaderboard",
            new=AsyncMock(return_value=([], 0)),
        ):
            response = test_client.get("/api/rarity/leaderboard?page=2&page_size=10")
        assert response.status_code == 200

    def test_tier_filter(self, test_client: TestClient) -> None:
        with patch(
            "api.routers.rarity.get_rarity_leaderboard",
            new=AsyncMock(return_value=([_MOCK_LIST_ITEM], 1)),
        ):
            response = test_client.get("/api/rarity/leaderboard?tier=ultra-rare")
        assert response.status_code == 200

    def test_503_when_not_ready(self, test_client: TestClient) -> None:
        import api.routers.rarity as rarity_router

        original = rarity_router._pg_pool
        rarity_router._pg_pool = None
        try:
            response = test_client.get("/api/rarity/leaderboard")
            assert response.status_code == 503
        finally:
            rarity_router._pg_pool = original


class TestHiddenGems:
    def test_success(self, test_client: TestClient) -> None:
        with patch(
            "api.routers.rarity.get_rarity_hidden_gems",
            new=AsyncMock(return_value=([_MOCK_LIST_ITEM], 50)),
        ):
            response = test_client.get("/api/rarity/hidden-gems")
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 50

    def test_min_rarity_param(self, test_client: TestClient) -> None:
        with patch(
            "api.routers.rarity.get_rarity_hidden_gems",
            new=AsyncMock(return_value=([], 0)),
        ):
            response = test_client.get("/api/rarity/hidden-gems?min_rarity=61")
        assert response.status_code == 200

    def test_503_when_not_ready(self, test_client: TestClient) -> None:
        import api.routers.rarity as rarity_router

        original = rarity_router._pg_pool
        rarity_router._pg_pool = None
        try:
            response = test_client.get("/api/rarity/hidden-gems")
            assert response.status_code == 503
        finally:
            rarity_router._pg_pool = original


class TestArtistRarity:
    def test_success(self, test_client: TestClient) -> None:
        with patch(
            "api.routers.rarity.get_rarity_by_artist",
            new=AsyncMock(return_value=([_MOCK_LIST_ITEM], 5)),
        ):
            response = test_client.get("/api/rarity/artist/123")
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 5

    def test_not_found(self, test_client: TestClient) -> None:
        with patch(
            "api.routers.rarity.get_rarity_by_artist",
            new=AsyncMock(return_value=None),
        ):
            response = test_client.get("/api/rarity/artist/nonexistent")
        assert response.status_code == 404

    def test_503_when_not_ready(self, test_client: TestClient) -> None:
        import api.routers.rarity as rarity_router

        original_pool = rarity_router._pg_pool
        rarity_router._pg_pool = None
        try:
            response = test_client.get("/api/rarity/artist/123")
            assert response.status_code == 503
        finally:
            rarity_router._pg_pool = original_pool


class TestLabelRarity:
    def test_success(self, test_client: TestClient) -> None:
        with patch(
            "api.routers.rarity.get_rarity_by_label",
            new=AsyncMock(return_value=([_MOCK_LIST_ITEM], 10)),
        ):
            response = test_client.get("/api/rarity/label/456")
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 10

    def test_not_found(self, test_client: TestClient) -> None:
        with patch(
            "api.routers.rarity.get_rarity_by_label",
            new=AsyncMock(return_value=None),
        ):
            response = test_client.get("/api/rarity/label/nonexistent")
        assert response.status_code == 404

    def test_503_when_not_ready(self, test_client: TestClient) -> None:
        import api.routers.rarity as rarity_router

        original_pool = rarity_router._pg_pool
        rarity_router._pg_pool = None
        try:
            response = test_client.get("/api/rarity/label/456")
            assert response.status_code == 503
        finally:
            rarity_router._pg_pool = original_pool
