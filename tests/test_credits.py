"""Unit tests for Credits & Provenance router endpoints."""

import json
from typing import Any, ClassVar
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient


# --- Tests ---


@pytest.fixture(autouse=True)
def _credit_total() -> Any:
    """One credit in total unless a test sets its own; the paging itself is proven on a real Neo4j."""
    with patch("api.routers.credits.count_person_credits", AsyncMock(return_value=1)) as count:
        yield count


class TestPersonCreditsEndpoint:
    """Tests for GET /api/credits/person/{name}."""

    @patch("api.routers.credits.get_person_credits")
    def test_person_credits_success(self, mock_query: AsyncMock, test_client: TestClient) -> None:
        mock_query.return_value = [
            {
                "release_id": "123",
                "title": "Test Release",
                "year": 1995,
                "role": "Mastered By",
                "category": "mastering",
                "artists": ["Artist A"],
                "labels": ["Label X"],
            },
        ]
        response = test_client.get("/api/credits/person/Bob%20Ludwig")
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "Bob Ludwig"
        assert data["total_credits"] == 1
        assert len(data["credits"]) == 1
        assert data["credits"][0]["role"] == "Mastered By"

    @patch("api.routers.credits.get_person_credits")
    def test_person_credits_not_found(self, mock_query: AsyncMock, test_client: TestClient, _credit_total: AsyncMock) -> None:
        mock_query.return_value = []
        _credit_total.return_value = 0
        response = test_client.get("/api/credits/person/Nobody")
        assert response.status_code == 404

    def test_person_credits_service_not_ready(self, test_client: TestClient) -> None:
        import api.routers.credits as credits_router

        original = credits_router._neo4j_driver
        credits_router._neo4j_driver = None
        try:
            response = test_client.get("/api/credits/person/Test")
            assert response.status_code == 503
        finally:
            credits_router._neo4j_driver = original


class TestPersonTimelineEndpoint:
    """Tests for GET /api/credits/person/{name}/timeline."""

    @patch("api.routers.credits.get_person_timeline")
    def test_timeline_success(self, mock_query: AsyncMock, test_client: TestClient) -> None:
        mock_query.return_value = [
            {"year": 1990, "category": "mastering", "count": 5},
            {"year": 1991, "category": "mastering", "count": 8},
        ]
        response = test_client.get("/api/credits/person/Bob%20Ludwig/timeline")
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "Bob Ludwig"
        assert len(data["timeline"]) == 2

    @patch("api.routers.credits.get_person_timeline")
    def test_timeline_not_found(self, mock_query: AsyncMock, test_client: TestClient) -> None:
        mock_query.return_value = []
        response = test_client.get("/api/credits/person/Nobody/timeline")
        assert response.status_code == 404


class TestReleaseCreditsEndpoint:
    """Tests for GET /api/credits/release/{release_id}."""

    @patch("api.routers.credits.get_release_credits")
    def test_release_credits_success(self, mock_query: AsyncMock, test_client: TestClient) -> None:
        mock_query.return_value = [
            {
                "name": "Bob Ludwig",
                "role": "Mastered By",
                "category": "mastering",
                "artist_id": None,
                "artist_name": None,
            },
            {
                "name": "Flood",
                "role": "Producer",
                "category": "production",
                "artist_id": "456",
                "artist_name": "Flood",
            },
        ]
        response = test_client.get("/api/credits/release/123")
        assert response.status_code == 200
        data = response.json()
        assert data["release_id"] == "123"
        assert len(data["credits"]) == 2

    @patch("api.routers.credits.get_release_credits")
    def test_release_credits_not_found(self, mock_query: AsyncMock, test_client: TestClient) -> None:
        mock_query.return_value = []
        response = test_client.get("/api/credits/release/99999")
        assert response.status_code == 404


class TestRoleLeaderboardEndpoint:
    """Tests for GET /api/credits/role/{role}/top."""

    @patch("api.routers.credits.get_role_leaderboard")
    def test_leaderboard_success(self, mock_query: AsyncMock, test_client: TestClient) -> None:
        mock_query.return_value = [
            {"name": "Bob Ludwig", "credit_count": 500},
            {"name": "Bernie Grundman", "credit_count": 400},
        ]
        response = test_client.get("/api/credits/role/mastering/top?limit=20")
        assert response.status_code == 200
        data = response.json()
        assert data["category"] == "mastering"
        assert len(data["entries"]) == 2

    def test_leaderboard_invalid_category(self, test_client: TestClient) -> None:
        response = test_client.get("/api/credits/role/invalid_cat/top")
        assert response.status_code == 400
        assert "Invalid role category" in response.json()["error"]

    @patch("api.routers.credits.get_role_leaderboard")
    def test_leaderboard_with_limit(self, mock_query: AsyncMock, test_client: TestClient) -> None:
        mock_query.return_value = [{"name": "Test", "credit_count": 10}]
        response = test_client.get("/api/credits/role/production/top?limit=5")
        assert response.status_code == 200


class TestSharedCreditsEndpoint:
    """Tests for GET /api/credits/shared."""

    @patch("api.routers.credits.get_shared_credits")
    def test_shared_credits_success(self, mock_query: AsyncMock, test_client: TestClient) -> None:
        mock_query.return_value = [
            {
                "release_id": "123",
                "title": "Test Album",
                "year": 1995,
                "person1_role": "Producer",
                "person2_role": "Engineer",
                "artists": ["The Band"],
            },
        ]
        response = test_client.get("/api/credits/shared?person1=Flood&person2=Alan%20Moulder")
        assert response.status_code == 200
        data = response.json()
        assert data["person1"] == "Flood"
        assert data["person2"] == "Alan Moulder"
        assert len(data["shared_releases"]) == 1

    @patch("api.routers.credits.get_shared_credits")
    def test_shared_credits_empty(self, mock_query: AsyncMock, test_client: TestClient) -> None:
        mock_query.return_value = []
        response = test_client.get("/api/credits/shared?person1=A&person2=B")
        assert response.status_code == 200
        assert response.json()["shared_releases"] == []


class TestPersonConnectionsEndpoint:
    """Tests for GET /api/credits/connections/{name}."""

    @patch("api.routers.credits.get_person_connections")
    def test_connections_success(self, mock_query: AsyncMock, test_client: TestClient) -> None:
        mock_query.return_value = [
            {"name": "Connected Person", "shared_count": 10},
        ]
        response = test_client.get("/api/credits/connections/Bob%20Ludwig?depth=1&limit=30")
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "Bob Ludwig"
        assert len(data["connections"]) == 1


class TestCreditsAutocompleteEndpoint:
    """Tests for GET /api/credits/autocomplete."""

    @patch("api.routers.credits.autocomplete_person")
    def test_autocomplete_success(self, mock_query: AsyncMock, test_client: TestClient) -> None:
        mock_query.return_value = [
            {"name": "Bob Ludwig", "score": 5.2},
            {"name": "Bob Marley", "score": 3.1},
        ]
        response = test_client.get("/api/credits/autocomplete?q=Bob")
        assert response.status_code == 200
        data = response.json()
        assert len(data["results"]) == 2
        assert data["results"][0]["name"] == "Bob Ludwig"

    def test_autocomplete_query_too_short(self, test_client: TestClient) -> None:
        response = test_client.get("/api/credits/autocomplete?q=B")
        assert response.status_code == 422  # Validation error


class TestPersonProfileEndpoint:
    """Tests for GET /api/credits/person/{name}/profile."""

    def test_profile_success(self, test_client: TestClient) -> None:
        with (
            patch("api.routers.credits.get_person_profile") as mock_profile,
            patch("api.routers.credits.get_person_role_breakdown") as mock_breakdown,
        ):
            mock_profile.return_value = {
                "name": "Bob Ludwig",
                "total_credits": 500,
                "categories": ["mastering"],
                "first_year": 1970,
                "last_year": 2020,
                "artist_id": None,
                "artist_name": None,
            }
            mock_breakdown.return_value = [{"category": "mastering", "count": 500}]
            response = test_client.get("/api/credits/person/Bob%20Ludwig/profile")
            assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
            data = response.json()
            assert data["name"] == "Bob Ludwig"
            assert data["total_credits"] == 500
            assert len(data["role_breakdown"]) == 1

    @patch("api.routers.credits.get_person_profile")
    def test_profile_not_found(self, mock_profile: AsyncMock, test_client: TestClient) -> None:
        mock_profile.return_value = None
        response = test_client.get("/api/credits/person/Nobody/profile")
        assert response.status_code == 404


class TestCreditsServiceNotReady:
    """Tests for 503 responses when Neo4j driver is not configured."""

    def _with_driver_none(self, test_client: TestClient, path: str) -> int:
        import api.routers.credits as credits_router

        original = credits_router._neo4j_driver
        credits_router._neo4j_driver = None
        try:
            return test_client.get(path).status_code
        finally:
            credits_router._neo4j_driver = original

    def test_timeline_service_not_ready(self, test_client: TestClient) -> None:
        assert self._with_driver_none(test_client, "/api/credits/person/Test/timeline") == 503

    def test_profile_service_not_ready(self, test_client: TestClient) -> None:
        assert self._with_driver_none(test_client, "/api/credits/person/Test/profile") == 503

    def test_release_service_not_ready(self, test_client: TestClient) -> None:
        assert self._with_driver_none(test_client, "/api/credits/release/123") == 503

    def test_leaderboard_service_not_ready(self, test_client: TestClient) -> None:
        assert self._with_driver_none(test_client, "/api/credits/role/mastering/top") == 503

    def test_shared_service_not_ready(self, test_client: TestClient) -> None:
        assert self._with_driver_none(test_client, "/api/credits/shared?person1=A&person2=B") == 503

    def test_connections_service_not_ready(self, test_client: TestClient) -> None:
        assert self._with_driver_none(test_client, "/api/credits/connections/Test") == 503

    def test_autocomplete_service_not_ready(self, test_client: TestClient) -> None:
        assert self._with_driver_none(test_client, "/api/credits/autocomplete?q=Test") == 503


class TestCreditsValkeyCaching:
    """Tests for Valkey cache hit/miss paths."""

    @patch("api.routers.credits.get_person_credits")
    def test_person_credits_cache_hit(self, mock_query: AsyncMock, test_client: TestClient) -> None:
        """Test that cached data is returned without querying Neo4j."""
        import api.routers.credits as credits_router

        cached_data = {
            "name": "Bob Ludwig",
            "total_credits": 1,
            "credits": [
                {"release_id": "1", "title": "Cached", "year": 2000, "role": "Mastered By", "category": "mastering", "artists": [], "labels": []}
            ],
        }
        original_valkey = credits_router._valkey
        mock_valkey = AsyncMock()
        mock_valkey.get = AsyncMock(return_value=json.dumps(cached_data))
        credits_router._valkey = mock_valkey
        try:
            response = test_client.get("/api/credits/person/Bob%20Ludwig")
            assert response.status_code == 200
            assert response.json()["credits"][0]["title"] == "Cached"
            mock_query.assert_not_called()
        finally:
            credits_router._valkey = original_valkey

    @patch("api.routers.credits.get_person_credits")
    def test_person_credits_cache_miss_sets_cache(self, mock_query: AsyncMock, test_client: TestClient, _credit_total: AsyncMock) -> None:
        """Test that cache miss queries Neo4j and stores result (an expensive person's default page)."""
        _credit_total.return_value = 5000
        import api.routers.credits as credits_router

        mock_query.return_value = [
            {"release_id": "1", "title": "Fresh", "year": 2000, "role": "Producer", "category": "production", "artists": [], "labels": []},
        ]
        original_valkey = credits_router._valkey
        mock_valkey = AsyncMock()
        mock_valkey.get = AsyncMock(return_value=None)
        mock_valkey.setex = AsyncMock()
        credits_router._valkey = mock_valkey
        try:
            response = test_client.get("/api/credits/person/Test")
            assert response.status_code == 200
            mock_valkey.setex.assert_called_once()
        finally:
            credits_router._valkey = original_valkey

    @patch("api.routers.credits.get_person_credits")
    def test_person_credits_cache_get_error(self, mock_query: AsyncMock, test_client: TestClient) -> None:
        """Test that Valkey get error falls through to Neo4j query."""
        import api.routers.credits as credits_router

        mock_query.return_value = [
            {"release_id": "1", "title": "Fallback", "year": 2000, "role": "Engineer", "category": "engineering", "artists": [], "labels": []},
        ]
        original_valkey = credits_router._valkey
        mock_valkey = AsyncMock()
        mock_valkey.get = AsyncMock(side_effect=Exception("Valkey down"))
        mock_valkey.setex = AsyncMock(side_effect=Exception("Valkey down"))
        credits_router._valkey = mock_valkey
        try:
            response = test_client.get("/api/credits/person/Test")
            assert response.status_code == 200
            assert response.json()["credits"][0]["title"] == "Fallback"
        finally:
            credits_router._valkey = original_valkey

    @patch("api.routers.credits.get_role_leaderboard")
    def test_leaderboard_cache_hit(self, mock_query: AsyncMock, test_client: TestClient) -> None:
        """Test leaderboard returns cached data."""
        import api.routers.credits as credits_router

        cached_data = {"category": "mastering", "entries": [{"name": "Cached Person", "credit_count": 999}]}
        original_valkey = credits_router._valkey
        mock_valkey = AsyncMock()
        mock_valkey.get = AsyncMock(return_value=json.dumps(cached_data))
        credits_router._valkey = mock_valkey
        try:
            response = test_client.get("/api/credits/role/mastering/top")
            assert response.status_code == 200
            assert response.json()["entries"][0]["name"] == "Cached Person"
            mock_query.assert_not_called()
        finally:
            credits_router._valkey = original_valkey

    @patch("api.routers.credits.get_role_leaderboard")
    def test_leaderboard_cache_error(self, mock_query: AsyncMock, test_client: TestClient) -> None:
        """Test leaderboard falls through on Valkey error."""
        import api.routers.credits as credits_router

        mock_query.return_value = [{"name": "Test", "credit_count": 10}]
        original_valkey = credits_router._valkey
        mock_valkey = AsyncMock()
        mock_valkey.get = AsyncMock(side_effect=Exception("Valkey down"))
        mock_valkey.setex = AsyncMock(side_effect=Exception("Valkey down"))
        credits_router._valkey = mock_valkey
        try:
            response = test_client.get("/api/credits/role/mastering/top")
            assert response.status_code == 200
        finally:
            credits_router._valkey = original_valkey


class TestPersonCreditsPaging:
    """Pagination of GET /api/credits/person/{name}: every credit stays reachable through ``total_credits``."""

    ROW: ClassVar[dict[str, Any]] = {
        "release_id": "1",
        "title": "T",
        "year": 2000,
        "role": "Producer",
        "category": "production",
        "artists": [],
        "labels": [],
    }

    def _get(self, test_client: TestClient, query: str, total: int, rows: int) -> Any:
        with (
            patch("api.routers.credits.get_person_credits", AsyncMock(return_value=[dict(self.ROW) for _ in range(rows)])) as page,
            patch("api.routers.credits.count_person_credits", AsyncMock(return_value=total)),
        ):
            response = test_client.get(f"/api/credits/person/Bob{query}")
        return response, page

    def test_first_page_reports_the_whole_total_and_more_to_come(self, test_client: TestClient) -> None:
        response, page = self._get(test_client, "", total=250, rows=100)
        data = response.json()
        assert (data["total_credits"], len(data["credits"]), data["limit"], data["offset"], data["has_more"]) == (250, 100, 100, 0, True)
        assert page.await_args.args[1:] == ("Bob", 100, 0)

    def test_last_page_has_no_more(self, test_client: TestClient) -> None:
        response, _ = self._get(test_client, "?limit=100&offset=200", total=250, rows=50)
        assert response.json()["has_more"] is False

    def test_an_offset_past_the_end_is_an_empty_page_not_a_404(self, test_client: TestClient) -> None:
        response, _ = self._get(test_client, "?offset=5000", total=250, rows=0)
        assert response.status_code == 200
        assert response.json()["credits"] == []
        assert response.json()["total_credits"] == 250

    def test_no_credits_at_all_is_404(self, test_client: TestClient) -> None:
        response, _ = self._get(test_client, "", total=0, rows=0)
        assert response.status_code == 404

    @pytest.mark.parametrize("query", ["?limit=0", "?limit=501", "?offset=-1"])
    def test_bounds_are_validated(self, test_client: TestClient, query: str) -> None:
        assert test_client.get(f"/api/credits/person/Bob{query}").status_code == 422

    def test_a_walk_over_offset_reaches_every_credit_once(self, test_client: TestClient) -> None:
        everything = [{**self.ROW, "release_id": str(i)} for i in range(230)]

        async def page(_driver: Any, _name: str, limit: int, offset: int) -> list[dict[str, Any]]:
            return everything[offset : offset + limit]

        seen: list[str] = []
        offset = 0
        with (
            patch("api.routers.credits.get_person_credits", page),
            patch("api.routers.credits.count_person_credits", AsyncMock(return_value=230)),
        ):
            while True:
                data = test_client.get(f"/api/credits/person/Bob?limit=100&offset={offset}").json()
                seen += [c["release_id"] for c in data["credits"]]
                if not data["has_more"]:
                    break
                offset += 100
        assert seen == [str(i) for i in range(230)]

    def test_only_expensive_default_pages_are_cached(self, test_client: TestClient) -> None:
        import api.routers.credits as credits_router

        valkey = AsyncMock()
        valkey.get = AsyncMock(return_value=None)
        valkey.setex = AsyncMock()
        original = credits_router._valkey
        credits_router._valkey = valkey
        try:
            self._get(test_client, "", total=50, rows=50)  # a small person: not cached
            assert valkey.setex.await_count == 0
            self._get(test_client, "?limit=50", total=5000, rows=50)  # not the default page size
            self._get(test_client, "?offset=100000", total=5000, rows=0)  # past the first pages
            assert valkey.setex.await_count == 0
            self._get(test_client, "", total=5000, rows=100)  # default page of a big person
            assert valkey.setex.await_count == 1
        finally:
            credits_router._valkey = original


class TestConnectionsDepth:
    @patch("api.routers.credits.get_person_connections")
    def test_depth_3_is_rejected_not_answered_as_2(self, mock_query: AsyncMock, test_client: TestClient) -> None:
        response = test_client.get("/api/credits/connections/Bob?depth=3")
        assert response.status_code == 422
        mock_query.assert_not_called()

    @patch("api.routers.credits.get_person_connections")
    def test_second_hops_reach_the_response(self, mock_query: AsyncMock, test_client: TestClient) -> None:
        mock_query.return_value = [{"name": "Alice", "shared_count": 4, "second_hops": [{"name": "Carol", "via": "Alice", "shared": 2}]}]
        data = test_client.get("/api/credits/connections/Bob?depth=2").json()
        assert data["connections"][0]["second_hops"] == [{"name": "Carol", "via": "Alice", "shared": 2}]

    @patch("api.routers.credits.get_person_connections")
    def test_depth_1_rows_have_no_second_hops(self, mock_query: AsyncMock, test_client: TestClient) -> None:
        mock_query.return_value = [{"name": "Alice", "shared_count": 4}]
        assert test_client.get("/api/credits/connections/Bob?depth=1").json()["connections"][0]["second_hops"] == []
