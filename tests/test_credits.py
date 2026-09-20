"""Unit tests for Credits & Provenance router endpoints.

Every query function is patched on `api.queries.credits_queries` rather than on the router,
because the router reaches all eight of them through the graph-backend seam
(`gm-catalog-api-dl8.1`) instead of importing them. The seam resolves a family to the
target *module*, not to copies of its functions, so patching the module's attribute is
still what the router sees — that patchability is a property the seam promises, and these
tests are where it is relied on.
"""

import json
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient


# --- Tests ---


class TestPersonCreditsEndpoint:
    """Tests for GET /api/credits/person/{name}."""

    @patch("api.queries.credits_queries.get_person_credits")
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

    @patch("api.queries.credits_queries.get_person_credits")
    def test_person_credits_not_found(self, mock_query: AsyncMock, test_client: TestClient) -> None:
        mock_query.return_value = []
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

    @patch("api.queries.credits_queries.get_person_timeline")
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

    @patch("api.queries.credits_queries.get_person_timeline")
    def test_timeline_not_found(self, mock_query: AsyncMock, test_client: TestClient) -> None:
        mock_query.return_value = []
        response = test_client.get("/api/credits/person/Nobody/timeline")
        assert response.status_code == 404


class TestReleaseCreditsEndpoint:
    """Tests for GET /api/credits/release/{release_id}."""

    @patch("api.queries.credits_queries.get_release_credits")
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

    @patch("api.queries.credits_queries.get_release_credits")
    def test_release_credits_not_found(self, mock_query: AsyncMock, test_client: TestClient) -> None:
        mock_query.return_value = []
        response = test_client.get("/api/credits/release/99999")
        assert response.status_code == 404


class TestRoleLeaderboardEndpoint:
    """Tests for GET /api/credits/role/{role}/top."""

    @patch("api.queries.credits_queries.get_role_leaderboard")
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

    @patch("api.queries.credits_queries.get_role_leaderboard")
    def test_leaderboard_with_limit(self, mock_query: AsyncMock, test_client: TestClient) -> None:
        mock_query.return_value = [{"name": "Test", "credit_count": 10}]
        response = test_client.get("/api/credits/role/production/top?limit=5")
        assert response.status_code == 200


class TestSharedCreditsEndpoint:
    """Tests for GET /api/credits/shared."""

    @patch("api.queries.credits_queries.get_shared_credits")
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

    @patch("api.queries.credits_queries.get_shared_credits")
    def test_shared_credits_empty(self, mock_query: AsyncMock, test_client: TestClient) -> None:
        mock_query.return_value = []
        response = test_client.get("/api/credits/shared?person1=A&person2=B")
        assert response.status_code == 200
        assert response.json()["shared_releases"] == []


class TestPersonConnectionsEndpoint:
    """Tests for GET /api/credits/connections/{name}."""

    @patch("api.queries.credits_queries.get_person_connections")
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

    @patch("api.queries.autocomplete_queries.autocomplete_person")
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

    def test_autocomplete_resolves_the_postgres_backend_when_configured(self, test_client: TestClient) -> None:
        """GRAPH_BACKEND=postgres sends the person search to the trigram module."""
        import api.routers.credits as credits_module

        pool = object()
        mock_func = AsyncMock(return_value=[{"name": "Bob Ludwig", "score": 0.4}])
        driver, redis, backend, original_pool = (
            credits_module._neo4j_driver,
            credits_module._redis,
            credits_module._graph_backend,
            credits_module._pg_pool,
        )
        try:
            credits_module.configure(driver, redis, "postgres", pg_pool=pool)
            with patch("api.queries.autocomplete_pg_queries.autocomplete_person", mock_func):
                response = test_client.get("/api/credits/autocomplete?q=bob&limit=4")
        finally:
            credits_module.configure(driver, redis, backend, pg_pool=original_pool)

        assert response.status_code == 200
        mock_func.assert_awaited_once_with(pool, "bob", 4)

    def test_autocomplete_query_too_short(self, test_client: TestClient) -> None:
        response = test_client.get("/api/credits/autocomplete?q=B")
        assert response.status_code == 422  # Validation error


class TestPersonProfileEndpoint:
    """Tests for GET /api/credits/person/{name}/profile."""

    def test_profile_success(self, test_client: TestClient) -> None:
        with (
            patch("api.queries.credits_queries.get_person_profile") as mock_profile,
            patch("api.queries.credits_queries.get_person_role_breakdown") as mock_breakdown,
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

    @patch("api.queries.credits_queries.get_person_profile")
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


class TestCreditsRedisCaching:
    """Tests for Redis cache hit/miss paths."""

    @patch("api.queries.credits_queries.get_person_credits")
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
        original_redis = credits_router._redis
        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(return_value=json.dumps(cached_data))
        credits_router._redis = mock_redis
        try:
            response = test_client.get("/api/credits/person/Bob%20Ludwig")
            assert response.status_code == 200
            assert response.json()["credits"][0]["title"] == "Cached"
            mock_query.assert_not_called()
        finally:
            credits_router._redis = original_redis

    @patch("api.queries.credits_queries.get_person_credits")
    def test_person_credits_cache_miss_sets_cache(self, mock_query: AsyncMock, test_client: TestClient) -> None:
        """Test that cache miss queries Neo4j and stores result."""
        import api.routers.credits as credits_router

        mock_query.return_value = [
            {"release_id": "1", "title": "Fresh", "year": 2000, "role": "Producer", "category": "production", "artists": [], "labels": []},
        ]
        original_redis = credits_router._redis
        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(return_value=None)
        mock_redis.setex = AsyncMock()
        credits_router._redis = mock_redis
        try:
            response = test_client.get("/api/credits/person/Test")
            assert response.status_code == 200
            mock_redis.setex.assert_called_once()
        finally:
            credits_router._redis = original_redis

    @patch("api.queries.credits_queries.get_person_credits")
    def test_person_credits_cache_get_error(self, mock_query: AsyncMock, test_client: TestClient) -> None:
        """Test that Redis get error falls through to Neo4j query."""
        import api.routers.credits as credits_router

        mock_query.return_value = [
            {"release_id": "1", "title": "Fallback", "year": 2000, "role": "Engineer", "category": "engineering", "artists": [], "labels": []},
        ]
        original_redis = credits_router._redis
        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(side_effect=Exception("Redis down"))
        mock_redis.setex = AsyncMock(side_effect=Exception("Redis down"))
        credits_router._redis = mock_redis
        try:
            response = test_client.get("/api/credits/person/Test")
            assert response.status_code == 200
            assert response.json()["credits"][0]["title"] == "Fallback"
        finally:
            credits_router._redis = original_redis

    @patch("api.queries.credits_queries.get_role_leaderboard")
    def test_leaderboard_cache_hit(self, mock_query: AsyncMock, test_client: TestClient) -> None:
        """Test leaderboard returns cached data."""
        import api.routers.credits as credits_router

        cached_data = {"category": "mastering", "entries": [{"name": "Cached Person", "credit_count": 999}]}
        original_redis = credits_router._redis
        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(return_value=json.dumps(cached_data))
        credits_router._redis = mock_redis
        try:
            response = test_client.get("/api/credits/role/mastering/top")
            assert response.status_code == 200
            assert response.json()["entries"][0]["name"] == "Cached Person"
            mock_query.assert_not_called()
        finally:
            credits_router._redis = original_redis

    @patch("api.queries.credits_queries.get_role_leaderboard")
    def test_leaderboard_cache_error(self, mock_query: AsyncMock, test_client: TestClient) -> None:
        """Test leaderboard falls through on Redis error."""
        import api.routers.credits as credits_router

        mock_query.return_value = [{"name": "Test", "credit_count": 10}]
        original_redis = credits_router._redis
        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(side_effect=Exception("Redis down"))
        mock_redis.setex = AsyncMock(side_effect=Exception("Redis down"))
        credits_router._redis = mock_redis
        try:
            response = test_client.get("/api/credits/role/mastering/top")
            assert response.status_code == 200
        finally:
            credits_router._redis = original_redis


class TestCreditsBackendErrorMapping:
    """The backend-neutral error mapping, on every endpoint this router serves.

    `gm-catalog-api-dl8.1` routed the eight traversals through the graph-backend seam, and
    the seam is what makes a failure's *kind* readable without knowing which engine raised
    it: `api.graph_backend.is_graph_query_timeout` for a statement that ran out of time,
    `is_graph_backend_unavailable` for a backend that could not be reached at all, and
    neither for a genuine bug, which still reaches the 500 both backends always produced.

    Every endpoint is covered rather than one of them, because each has its own `except`
    and a handler that forgot one would be invisible here otherwise.
    """

    # (patched function, request) — one per `except GRAPH_BACKEND_ERROR_TYPES` clause.
    ENDPOINTS: tuple[tuple[str, str], ...] = (
        ("get_person_timeline", "/api/credits/person/Tessa%20Vance/timeline"),
        ("get_person_profile", "/api/credits/person/Tessa%20Vance/profile"),
        ("get_person_credits", "/api/credits/person/Tessa%20Vance"),
        ("get_release_credits", "/api/credits/release/701"),
        ("get_role_leaderboard", "/api/credits/role/mastering/top"),
        ("get_shared_credits", "/api/credits/shared?person1=Tessa%20Vance&person2=Marlon%20Hale"),
        ("get_person_connections", "/api/credits/connections/Tessa%20Vance"),
    )
    ENDPOINT_IDS: tuple[str, ...] = tuple(function for function, _ in ENDPOINTS)

    @pytest.mark.parametrize(("function", "url"), ENDPOINTS, ids=ENDPOINT_IDS)
    def test_a_neo4j_transaction_timeout_is_504(self, function: str, url: str, test_client: TestClient) -> None:
        from neo4j.exceptions import ClientError as Neo4jClientError

        with patch(f"api.queries.credits_queries.{function}", new_callable=AsyncMock, side_effect=Neo4jClientError("TransactionTimedOut")):
            response = test_client.get(url)
        assert response.status_code == 504

    @pytest.mark.parametrize(("function", "url"), ENDPOINTS, ids=ENDPOINT_IDS)
    def test_an_unreachable_backend_is_503(self, function: str, url: str, test_client: TestClient) -> None:
        """Not 504: no query ran, so "try again with a narrower request" would be advice
        about something that never happened."""
        from neo4j.exceptions import ServiceUnavailable

        with patch(f"api.queries.credits_queries.{function}", new_callable=AsyncMock, side_effect=ServiceUnavailable("no reachable server")):
            response = test_client.get(url)
        assert response.status_code == 503
        assert "narrower" not in response.json()["error"]

    def test_the_person_search_maps_its_failures_the_same_way(self, test_client: TestClient) -> None:
        """The autocomplete endpoint reaches a backend too, through its own family."""
        from neo4j.exceptions import ClientError as Neo4jClientError

        with patch(
            "api.queries.autocomplete_queries.autocomplete_person",
            new_callable=AsyncMock,
            side_effect=Neo4jClientError("TransactionTimedOut"),
        ):
            response = test_client.get("/api/credits/autocomplete?q=bob")
        assert response.status_code == 504

    def test_any_other_backend_error_still_reaches_the_500_it_always_did(self, test_client: TestClient) -> None:
        from neo4j.exceptions import ClientError as Neo4jClientError

        with patch("api.queries.credits_queries.get_person_credits", new_callable=AsyncMock, side_effect=Neo4jClientError("SomeOtherError")):
            response = test_client.get("/api/credits/person/Tessa%20Vance")
        assert response.status_code == 500

    def test_a_postgres_statement_timeout_is_the_same_504(self, test_client: TestClient) -> None:
        """The whole point of the mapping: the same contract from the other engine."""
        import psycopg

        import api.routers.credits as credits_module

        saved = (credits_module._neo4j_driver, credits_module._redis, credits_module._graph_backend, credits_module._pg_pool)
        try:
            credits_module.configure(saved[0], saved[1], "postgres", pg_pool=AsyncMock())
            with patch(
                "api.queries.credits_pg_queries.get_person_credits",
                new_callable=AsyncMock,
                side_effect=psycopg.errors.QueryCanceled("canceling statement due to statement timeout"),
            ):
                response = test_client.get("/api/credits/person/Tessa%20Vance")
        finally:
            credits_module.configure(saved[0], saved[1], saved[2], pg_pool=saved[3])
        assert response.status_code == 504

    def test_the_postgres_pool_giving_up_is_503_not_504(self, test_client: TestClient) -> None:
        from common.db_resilience import ConnectionEstablishmentError

        import api.routers.credits as credits_module

        saved = (credits_module._neo4j_driver, credits_module._redis, credits_module._graph_backend, credits_module._pg_pool)
        try:
            credits_module.configure(saved[0], saved[1], "postgres", pg_pool=AsyncMock())
            with patch(
                "api.queries.credits_pg_queries.get_person_connections",
                new_callable=AsyncMock,
                side_effect=ConnectionEstablishmentError("Failed to get PostgreSQL connection after 5 attempts"),
            ):
                response = test_client.get("/api/credits/connections/Tessa%20Vance")
        finally:
            credits_module.configure(saved[0], saved[1], saved[2], pg_pool=saved[3])
        assert response.status_code == 503


class TestCreditsBackendResolution:
    """`GRAPH_BACKEND=postgres` sends the eight traversals to the SQL/PGQ module."""

    def test_the_seam_resolves_the_postgres_credits_module(self, test_client: TestClient) -> None:
        import api.routers.credits as credits_module

        rows = [{"category": "mastering", "count": 4}]
        saved = (credits_module._neo4j_driver, credits_module._redis, credits_module._graph_backend, credits_module._pg_pool)
        try:
            credits_module.configure(saved[0], saved[1], "postgres", pg_pool=AsyncMock())
            profile = {
                "name": "Tessa Vance",
                "total_credits": 4,
                "categories": ["mastering"],
                "first_year": 1963,
                "last_year": 1972,
                "artist_id": "801",
                "artist_name": "Vance Machine",
            }
            with (
                patch("api.queries.credits_pg_queries.get_person_role_breakdown", new_callable=AsyncMock, return_value=rows) as postgres_call,
                patch("api.queries.credits_pg_queries.get_person_profile", new_callable=AsyncMock, return_value=profile),
            ):
                response = test_client.get("/api/credits/person/Tessa%20Vance/profile")
        finally:
            credits_module.configure(saved[0], saved[1], saved[2], pg_pool=saved[3])

        assert response.status_code == 200
        assert response.json()["role_breakdown"] == [{"category": "mastering", "count": 4}]
        postgres_call.assert_awaited_once()
