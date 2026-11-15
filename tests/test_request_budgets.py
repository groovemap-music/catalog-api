"""Rate limits and work budgets on the expensive graph routes (gm-catalog-api-8fou)."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from api import dependencies, request_budgets
from api.limiter import caller_key_func
from api.queries import neo4j_queries
from api.queries.neo4j_queries import PathTooBroadError, TrendsBusyError
from tests.conftest import TEST_JWT_SECRET, make_test_jwt


def _request(headers: dict[str, str] | None = None, client: str = "203.0.113.9") -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return Request({"type": "http", "headers": raw, "client": (client, 1234), "method": "GET", "path": "/"})


class TestCallerKey:
    def test_anonymous_is_keyed_by_ip(self) -> None:
        assert caller_key_func(_request()) == "ip:203.0.113.9"

    def test_forwarded_for_header_is_ignored(self) -> None:
        """Only the socket peer (rewritten by Uvicorn for trusted proxies) counts, never a raw header."""
        assert caller_key_func(_request({"X-Forwarded-For": "1.2.3.4"})) == "ip:203.0.113.9"

    def test_valid_jwt_is_keyed_by_user(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(dependencies, "_jwt_secret", TEST_JWT_SECRET)
        key = caller_key_func(_request({"Authorization": f"Bearer {make_test_jwt(user_id='u-7')}"}, client="198.51.100.1"))
        assert key == "user:u-7"

    def test_forged_or_expired_jwt_falls_back_to_ip(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(dependencies, "_jwt_secret", TEST_JWT_SECRET)
        forged = make_test_jwt(secret="not-the-secret")  # noqa: S106
        expired = make_test_jwt(exp=1)
        assert caller_key_func(_request({"Authorization": f"Bearer {forged}"})) == "ip:203.0.113.9"
        assert caller_key_func(_request({"Authorization": f"Bearer {expired}"})) == "ip:203.0.113.9"
        assert caller_key_func(_request({"Authorization": "Bearer junk"})) == "ip:203.0.113.9"


class TestSettings:
    def test_defaults(self) -> None:
        assert request_budgets.rate_limit("trends") == request_budgets.TRENDS_RATE_LIMIT
        assert request_budgets.path_timeout_seconds() < 30

    def test_override_and_bad_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("RATE_LIMIT_PATH", "2/minute;20/hour")
        assert request_budgets.rate_limit("path") == "2/minute;20/hour"
        monkeypatch.setenv("RATE_LIMIT_PATH", "lots")
        assert request_budgets.rate_limit("path") == request_budgets.PATH_RATE_LIMIT

    def test_depth_cap_never_exceeds_ceiling(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PATH_SEARCH_MAX_DEPTH", "99")
        assert request_budgets.path_max_depth(10) == 10


class TestRateLimits:
    @pytest.mark.parametrize(
        ("env", "url"),
        [
            ("RATE_LIMIT_EXPLORE", "/api/explore?name=Rock&type=genre"),
            ("RATE_LIMIT_EXPAND", "/api/expand?node_id=1&type=artist&category=releases"),
            ("RATE_LIMIT_TRENDS", "/api/trends?name=Rock&type=genre"),
            ("RATE_LIMIT_PATH", "/api/path?from_name=A&to_name=B"),
        ],
    )
    def test_429_with_retry_after_past_limit(self, test_client: TestClient, monkeypatch: pytest.MonkeyPatch, env: str, url: str) -> None:
        monkeypatch.setenv(env, "2/minute")
        codes = [test_client.get(url).status_code for _ in range(3)]
        assert 429 not in codes[:2]
        assert codes[2] == 429
        blocked = test_client.get(url)
        assert blocked.status_code == 429
        assert int(blocked.headers["Retry-After"]) > 0

    def test_anonymous_callers_are_limited_per_ip(self, test_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("RATE_LIMIT_TRENDS", "1/minute")
        url = "/api/trends?name=Rock&type=genre"
        test_client.get(url)
        assert test_client.get(url).status_code == 429
        # a spoofed header does not get a fresh bucket
        assert test_client.get(url, headers={"X-Forwarded-For": "9.9.9.9"}).status_code == 429

    def test_authenticated_users_have_their_own_bucket(self, test_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(dependencies, "_jwt_secret", TEST_JWT_SECRET)
        monkeypatch.setenv("RATE_LIMIT_TRENDS", "1/minute")
        url = "/api/trends?name=Rock&type=genre"
        test_client.get(url)
        assert test_client.get(url).status_code == 429  # anonymous bucket spent
        signed_in = {"Authorization": f"Bearer {make_test_jwt(user_id='someone')}"}
        assert test_client.get(url, headers=signed_in).status_code != 429
        assert test_client.get(url, headers=signed_in).status_code == 429

    def test_default_limit_covers_undecorated_routes_and_health_is_exempt(self, test_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("RATE_LIMIT_DEFAULT", "2/minute")
        assert [test_client.get("/api/graph/stats").status_code == 429 for _ in range(3)] == [False, False, True]
        assert all(test_client.get("/health").status_code != 429 for _ in range(5))

    def test_normal_journey_is_unaffected(self, test_client: TestClient) -> None:
        for _ in range(5):
            assert test_client.get("/api/trends?name=Rock&type=genre").status_code != 429


class TestPathBounds:
    def test_depth_over_cap_is_422(self, test_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PATH_SEARCH_MAX_DEPTH", "4")
        assert test_client.get("/api/path?from_name=A&to_name=B&max_depth=5").status_code == 422

    def test_depth_within_cap_is_accepted(self, test_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PATH_SEARCH_MAX_DEPTH", "4")
        with (
            patch("api.routers.explore.EXPLORE_DISPATCH", {"artist": AsyncMock(return_value={"id": 1, "name": "x"})}),
            patch("api.routers.explore.find_shortest_path", AsyncMock(return_value=None)),
        ):
            assert test_client.get("/api/path?from_name=A&to_name=B&max_depth=4").status_code == 200

    def test_hub_to_hub_is_a_clear_422(self, test_client: TestClient) -> None:
        with (
            patch("api.routers.explore.EXPLORE_DISPATCH", {"genre": AsyncMock(return_value={"id": "Rock", "name": "Rock"})}),
            patch("api.routers.explore.find_shortest_path", AsyncMock(side_effect=PathTooBroadError(2_000_000, 900_000, 500_000))),
        ):
            response = test_client.get("/api/path?from_name=Rock&from_type=genre&to_name=Pop&to_type=genre")
        assert response.status_code == 422
        assert response.json()["error"] == "path search too broad"

    def test_timeout_is_a_clear_504(self, test_client: TestClient) -> None:
        from neo4j.exceptions import ClientError

        err = ClientError("Neo.ClientError.Transaction.TransactionTimedOut")
        with (
            patch("api.routers.explore.EXPLORE_DISPATCH", {"artist": AsyncMock(return_value={"id": 1, "name": "x"})}),
            patch("api.routers.explore.find_shortest_path", AsyncMock(side_effect=err)),
        ):
            response = test_client.get("/api/path?from_name=A&to_name=B")
        assert response.status_code == 504
        assert response.json()["error"] == "path search too broad"

    @pytest.mark.asyncio
    async def test_query_layer_uses_path_timeout_and_depth_cap(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PATH_SEARCH_TIMEOUT_SECONDS", "7")
        monkeypatch.setenv("PATH_SEARCH_MAX_DEPTH", "3")
        run_single = AsyncMock(side_effect=[{"from_degree": 5, "to_degree": 5}, None])
        with patch.object(neo4j_queries, "run_single", run_single):
            await neo4j_queries.find_shortest_path(MagicMock(), "1", "2", max_depth=10)
        path_call = run_single.call_args_list[-1]
        assert path_call.kwargs["timeout"] == 7.0
        assert "*..3]" in path_call.args[1]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("from_degree", "to_degree", "refused"), [(900_000, 800_000, True), (900_000, 40, False), (30, 20, False)])
    async def test_hub_guard(self, from_degree: int, to_degree: int, refused: bool) -> None:
        run_single = AsyncMock(side_effect=[{"from_degree": from_degree, "to_degree": to_degree}, None])
        with patch.object(neo4j_queries, "run_single", run_single):
            if refused:
                with pytest.raises(PathTooBroadError):
                    await neo4j_queries.find_shortest_path(MagicMock(), "1", "2")
                assert run_single.await_count == 1  # the shortestPath never ran
            else:
                assert await neo4j_queries.find_shortest_path(MagicMock(), "1", "2") is None
                assert run_single.await_count == 2

    @pytest.mark.asyncio
    async def test_hub_guard_can_be_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PATH_HUB_DEGREE_THRESHOLD", "0")
        run_single = AsyncMock(return_value=None)
        with patch.object(neo4j_queries, "run_single", run_single):
            await neo4j_queries.find_shortest_path(MagicMock(), "1", "2")
        assert run_single.await_count == 1


class TestTrendsLiveFallback:
    @pytest.mark.asyncio
    async def test_stored_series_never_runs_the_live_query(self) -> None:
        stored = {"maintained": True, "stats_version": 2, "years": [1999], "counts": [3]}
        live = AsyncMock()
        with patch.object(neo4j_queries, "run_single", AsyncMock(return_value=stored)), patch.object(neo4j_queries, "run_query", live):
            assert await neo4j_queries.trends_genre(MagicMock(), "Rock") == [{"year": 1999, "count": 3}]
        live.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "stored", [None, {"maintained": False, "stats_version": 2, "years": [1], "counts": [1]}, {"maintained": True, "stats_version": 1}]
    )
    async def test_missing_or_stale_series_uses_live_with_short_timeout(self, monkeypatch: pytest.MonkeyPatch, stored: dict | None) -> None:
        monkeypatch.setenv("TRENDS_LIVE_TIMEOUT_SECONDS", "5")
        live = AsyncMock(return_value=[{"year": 1, "count": 1}])
        with patch.object(neo4j_queries, "run_single", AsyncMock(return_value=stored)), patch.object(neo4j_queries, "run_query", live):
            await neo4j_queries.trends_genre(MagicMock(), "Rock")
        assert live.call_args.kwargs["timeout"] == 5.0

    @pytest.mark.asyncio
    async def test_concurrent_live_traversals_are_refused_not_queued(self) -> None:
        release = asyncio.Event()

        async def slow(*_a: object, **_k: object) -> list[dict[str, int]]:
            await release.wait()
            return []

        with patch.object(neo4j_queries, "run_query", slow):
            first = asyncio.create_task(neo4j_queries._run_live_trends(MagicMock(), "q"))
            await asyncio.sleep(0)
            with pytest.raises(TrendsBusyError):
                await neo4j_queries._run_live_trends(MagicMock(), "q")
            release.set()
            await first

    def test_route_returns_503_with_retry_after_when_busy(self, test_client: TestClient) -> None:
        busy = AsyncMock(side_effect=TrendsBusyError())
        with patch.dict("api.routers.explore.TRENDS_DISPATCH", {"genre": busy}):
            response = test_client.get("/api/trends?name=Rock&type=genre")
        assert response.status_code == 503
        assert int(response.headers["Retry-After"]) > 0
