"""Tests for the insights API proxy router."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient


class TestInsightsConfigure:
    """Tests for the configure() function."""

    def test_configure_updates_base_url(self) -> None:
        """Lines 26-27: configure sets _INSIGHTS_BASE_URL when url provided."""
        import api.routers.insights as mod

        original = mod._INSIGHTS_BASE_URL
        try:
            mod.configure("http://custom-insights:9000")
            assert mod._INSIGHTS_BASE_URL == "http://custom-insights:9000"
        finally:
            mod._INSIGHTS_BASE_URL = original
            mod.configure()  # restore default (no-op, but sets _INSIGHTS_BASE_URL to whatever it already is)
            mod._INSIGHTS_BASE_URL = original

    def test_configure_no_url_keeps_default(self) -> None:
        """configure() with no argument leaves _INSIGHTS_BASE_URL unchanged."""
        import api.routers.insights as mod

        original = mod._INSIGHTS_BASE_URL
        mod.configure()
        assert original == mod._INSIGHTS_BASE_URL


class TestInsightsProxySuccess:
    """Tests for successful proxy responses (success paths)."""

    def test_proxy_top_artists_success(self, test_client: TestClient) -> None:
        """Line 49: proxy_top_artists returns 200 with data on success."""
        mock_data = {"items": [{"artist": "Miles Davis", "count": 100}]}
        with patch("api.routers.insights._forward", new=AsyncMock(return_value=mock_data)):
            response = test_client.get("/api/insights/top-artists")
        assert response.status_code == 200
        assert response.json() == mock_data

    def test_proxy_genre_trends_success(self, test_client: TestClient) -> None:
        """Line 60: proxy_genre_trends returns 200 with data on success."""
        mock_data = {"trends": [{"genre": "Jazz", "year": 1960, "count": 50}]}
        with patch("api.routers.insights._forward", new=AsyncMock(return_value=mock_data)):
            response = test_client.get("/api/insights/genre-trends")
        assert response.status_code == 200
        assert response.json() == mock_data

    def test_proxy_style_atlas_success(self, test_client: TestClient) -> None:
        """proxy_style_atlas returns 200 with data on success."""
        mock_data = {"styles": [{"style": "House", "stage": "mature"}], "layout": []}
        with patch("api.routers.insights._forward", new=AsyncMock(return_value=mock_data)):
            response = test_client.get("/api/insights/style-atlas")
        assert response.status_code == 200
        assert response.json() == mock_data

    def test_proxy_label_longevity_success(self, test_client: TestClient) -> None:
        """Line 71: proxy_label_longevity returns 200 with data on success."""
        mock_data = {"labels": [{"name": "Blue Note", "years": 80}]}
        with patch("api.routers.insights._forward", new=AsyncMock(return_value=mock_data)):
            response = test_client.get("/api/insights/label-longevity")
        assert response.status_code == 200
        assert response.json() == mock_data

    def test_proxy_this_month_success(self, test_client: TestClient) -> None:
        """Line 82: proxy_this_month returns 200 with data on success."""
        mock_data = {"releases": [], "count": 0}
        with patch("api.routers.insights._forward", new=AsyncMock(return_value=mock_data)):
            response = test_client.get("/api/insights/this-month")
        assert response.status_code == 200
        assert response.json() == mock_data

    def test_proxy_data_completeness_success(self, test_client: TestClient) -> None:
        """Line 93: proxy_data_completeness returns 200 with data on success."""
        mock_data = {"completeness": 0.95}
        with patch("api.routers.insights._forward", new=AsyncMock(return_value=mock_data)):
            response = test_client.get("/api/insights/data-completeness")
        assert response.status_code == 200
        assert response.json() == mock_data

    def test_proxy_status_success(self, test_client: TestClient) -> None:
        """Line 104: proxy_status returns 200 with data on success."""
        mock_data = {"status": "ok", "last_run": "2026-03-15T00:00:00Z"}
        with patch("api.routers.insights._forward", new=AsyncMock(return_value=mock_data)):
            response = test_client.get("/api/insights/status")
        assert response.status_code == 200
        assert response.json() == mock_data


class TestForwardStatusCodePreservation:
    """Tests that _forward preserves upstream HTTP status codes."""

    @pytest.mark.asyncio
    async def test_forward_preserves_422_status(self) -> None:
        """_forward returns upstream 422 instead of masking it as 200."""
        from api.routers.insights import _forward

        mock_response = MagicMock()
        mock_response.json.return_value = {"detail": [{"msg": "field required", "type": "missing"}]}
        mock_response.status_code = 422

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        from starlette.datastructures import URL
        from starlette.requests import Request

        mock_request = AsyncMock(spec=Request)
        mock_request.url = URL("http://test/api/insights/genre-trends")

        with patch("api.routers.insights.httpx.AsyncClient", return_value=mock_client):
            result = await _forward(mock_request, "/api/insights/genre-trends")
        assert result.status_code == 422

    def test_proxy_genre_trends_forwards_422(self, test_client: TestClient) -> None:
        """Proxy returns 422 when upstream insights service returns 422."""
        error_response = JSONResponse(
            content={"detail": [{"msg": "field required", "type": "missing"}]},
            status_code=422,
        )
        with patch("api.routers.insights._forward", new=AsyncMock(return_value=error_response)):
            response = test_client.get("/api/insights/genre-trends")
        assert response.status_code == 422


class TestForwardWithQueryString:
    """Tests for _forward appending query strings."""

    @pytest.mark.asyncio
    async def test_forward_appends_query_string(self) -> None:
        """_forward appends ?query when request has a query string."""
        from api.routers.insights import _forward

        mock_response = MagicMock()
        mock_response.json.return_value = {"items": []}
        mock_response.status_code = 200

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        from starlette.datastructures import URL
        from starlette.requests import Request

        mock_request = AsyncMock(spec=Request)
        mock_request.url = URL("http://test/api/insights/genre-trends?genre=Jazz")

        with patch("api.routers.insights.httpx.AsyncClient", return_value=mock_client):
            result = await _forward(mock_request, "/api/insights/genre-trends")
        assert result.status_code == 200
        assert result.body == JSONResponse(content={"items": []}).body
        mock_client.get.assert_awaited_once_with("http://insights:8008/api/insights/genre-trends?genre=Jazz")


class TestStyleAtlasForwarding:
    """The style atlas proxy reaches the analytics-engine route and relays its payload."""

    def test_style_atlas_forwards_to_upstream_path(self, test_client: TestClient) -> None:
        mock_response = MagicMock()
        mock_response.json.return_value = {"styles": []}
        mock_response.status_code = 200

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("api.routers.insights.httpx.AsyncClient", return_value=mock_client):
            response = test_client.get("/api/insights/style-atlas")
        assert response.status_code == 200
        assert response.json() == {"styles": []}
        mock_client.get.assert_awaited_once_with("http://insights:8008/api/insights/style-atlas")

    def test_style_atlas_503_when_upstream_connection_fails(self, test_client: TestClient) -> None:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=OSError("Connection refused"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("api.routers.insights.httpx.AsyncClient", return_value=mock_client):
            response = test_client.get("/api/insights/style-atlas")
        assert response.status_code == 503
        assert response.json() == {"error": "Insights service unavailable"}


class TestStyleAtlasCotagsForwarding:
    """The co-tag proxy reaches the analytics-engine route with its style and relays the payload."""

    def test_cotags_forward_the_style_to_the_upstream_path(self, test_client: TestClient) -> None:
        mock_response = MagicMock()
        mock_response.json.return_value = {"style": "Bop", "cotags": []}
        mock_response.status_code = 200

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("api.routers.insights.httpx.AsyncClient", return_value=mock_client):
            response = test_client.get("/api/insights/style-atlas/cotags", params={"style": "Hard Bop"})
        assert response.status_code == 200
        assert response.json() == {"style": "Bop", "cotags": []}
        mock_client.get.assert_awaited_once_with("http://insights:8008/api/insights/style-atlas/cotags?style=Hard+Bop")

    def test_cotags_relay_an_upstream_status(self, test_client: TestClient) -> None:
        mock_response = MagicMock()
        mock_response.json.return_value = {"error": "Unknown style"}
        mock_response.status_code = 404

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("api.routers.insights.httpx.AsyncClient", return_value=mock_client):
            response = test_client.get("/api/insights/style-atlas/cotags", params={"style": "Nope"})
        assert response.status_code == 404
        assert response.json() == {"error": "Unknown style"}

    def test_cotags_503_when_upstream_connection_fails(self, test_client: TestClient) -> None:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=OSError("Connection refused"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("api.routers.insights.httpx.AsyncClient", return_value=mock_client):
            response = test_client.get("/api/insights/style-atlas/cotags", params={"style": "Bop"})
        assert response.status_code == 503
        assert response.json() == {"error": "Insights service unavailable"}

    def test_cotags_require_a_style(self, test_client: TestClient) -> None:
        """A request without ``style`` is rejected before anything is forwarded."""
        with patch("api.routers.insights._forward", side_effect=AssertionError("must not forward")):
            assert test_client.get("/api/insights/style-atlas/cotags").status_code == 422
            assert test_client.get("/api/insights/style-atlas/cotags", params={"style": ""}).status_code == 422


class TestInsightsProxy:
    def test_proxy_top_artists_503_when_unavailable(self, test_client: TestClient) -> None:
        """Verify /api/insights/top-artists returns 503 when insights service is down."""
        with patch("api.routers.insights._forward", side_effect=Exception("Connection refused")):
            response = test_client.get("/api/insights/top-artists")
        assert response.status_code == 503

    def test_proxy_genre_trends_503_when_unavailable(self, test_client: TestClient) -> None:
        with patch("api.routers.insights._forward", side_effect=Exception("Connection refused")):
            response = test_client.get("/api/insights/genre-trends?genre=Jazz")
        assert response.status_code == 503

    def test_proxy_style_atlas_503_when_unavailable(self, test_client: TestClient) -> None:
        with patch("api.routers.insights._forward", side_effect=Exception("Connection refused")):
            response = test_client.get("/api/insights/style-atlas")
        assert response.status_code == 503

    def test_proxy_label_longevity_503_when_unavailable(self, test_client: TestClient) -> None:
        with patch("api.routers.insights._forward", side_effect=Exception("Connection refused")):
            response = test_client.get("/api/insights/label-longevity")
        assert response.status_code == 503

    def test_proxy_this_month_503_when_unavailable(self, test_client: TestClient) -> None:
        with patch("api.routers.insights._forward", side_effect=Exception("Connection refused")):
            response = test_client.get("/api/insights/this-month")
        assert response.status_code == 503

    def test_proxy_data_completeness_503_when_unavailable(self, test_client: TestClient) -> None:
        with patch("api.routers.insights._forward", side_effect=Exception("Connection refused")):
            response = test_client.get("/api/insights/data-completeness")
        assert response.status_code == 503

    def test_proxy_status_503_when_unavailable(self, test_client: TestClient) -> None:
        with patch("api.routers.insights._forward", side_effect=Exception("Connection refused")):
            response = test_client.get("/api/insights/status")
        assert response.status_code == 503

    @pytest.mark.asyncio
    async def test_forward_success(self) -> None:
        """Test _forward returns JSON from insights service."""
        from api.routers.insights import _forward

        mock_response = MagicMock()
        mock_response.json.return_value = {"items": [], "count": 0}
        mock_response.status_code = 200

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        from starlette.datastructures import URL
        from starlette.requests import Request

        mock_request = AsyncMock(spec=Request)
        mock_request.url = URL("http://test/api/insights/top-artists")

        with patch("api.routers.insights.httpx.AsyncClient", return_value=mock_client):
            result = await _forward(mock_request, "/api/insights/top-artists")
        assert result.status_code == 200
        assert result.body == JSONResponse(content={"items": [], "count": 0}).body
        mock_client.get.assert_awaited_once_with("http://insights:8008/api/insights/top-artists")


class TestInsightsForwardClient:
    """Tests that _forward uses a per-request httpx.AsyncClient (lines 33-34)."""

    @pytest.mark.asyncio
    async def test_forward_creates_per_request_client(self) -> None:
        """_forward creates a new httpx.AsyncClient as a context manager for each request."""
        from api.routers.insights import _forward

        mock_response = MagicMock()
        mock_response.json.return_value = {"data": "test"}
        mock_response.status_code = 200

        from starlette.datastructures import URL
        from starlette.requests import Request

        mock_request = AsyncMock(spec=Request)
        mock_request.url = URL("http://test/api/insights/top-artists")

        mock_instance = AsyncMock()
        mock_instance.get = AsyncMock(return_value=mock_response)
        mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
        mock_instance.__aexit__ = AsyncMock(return_value=False)

        with patch("api.routers.insights.httpx.AsyncClient", return_value=mock_instance) as mock_cls:
            result = await _forward(mock_request, "/api/insights/top-artists")

        mock_cls.assert_called_once_with(timeout=30.0)
        mock_instance.__aenter__.assert_awaited_once()
        mock_instance.__aexit__.assert_awaited_once()
        assert result.status_code == 200


class TestInsightsConfiguredUrl:
    """The configured analytics-engine URL is what outbound requests use."""

    @pytest.mark.asyncio
    async def test_forward_uses_configured_base_url(self) -> None:
        import api.routers.insights as mod

        original = mod._INSIGHTS_BASE_URL
        captured: list[str] = []

        class FakeClient:
            def __init__(self, *_a: object, **_k: object) -> None:
                pass

            async def __aenter__(self) -> FakeClient:
                return self

            async def __aexit__(self, *_a: object) -> None:
                return None

            async def get(self, url: str) -> MagicMock:
                captured.append(url)
                resp = MagicMock()
                resp.json.return_value = {}
                resp.status_code = 200
                return resp

        request = MagicMock()
        request.url.query = "limit=5"
        try:
            mod.configure("http://analytics.internal:9100/")
            with patch("api.routers.insights.httpx.AsyncClient", FakeClient):
                await mod._forward(request, "/api/insights/status")
        finally:
            mod._INSIGHTS_BASE_URL = original
        assert captured == ["http://analytics.internal:9100/api/insights/status?limit=5"]

    def test_default_url_unchanged(self) -> None:
        import api.routers.insights as mod
        from api.config import DEFAULT_INSIGHTS_BASE_URL

        assert DEFAULT_INSIGHTS_BASE_URL == "http://insights:8008"
        assert mod._INSIGHTS_BASE_URL == "http://insights:8008"

    def test_from_env_reads_insights_base_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from api.config import ApiConfig

        for k, v in {
            "POSTGRES_HOST": "h",
            "POSTGRES_USERNAME": "u",
            "POSTGRES_PASSWORD": "p",
            "POSTGRES_DATABASE": "d",
            "JWT_SECRET_KEY": "x" * 32,
            "NEO4J_HOST": "bolt://n:7687",
            "NEO4J_USERNAME": "n",
            "NEO4J_PASSWORD": "p",
        }.items():
            monkeypatch.setenv(k, v)
        monkeypatch.delenv("INSIGHTS_BASE_URL", raising=False)
        assert ApiConfig.from_env().insights_base_url == "http://insights:8008"
        monkeypatch.setenv("INSIGHTS_BASE_URL", "http://analytics.internal:9100/")
        assert ApiConfig.from_env().insights_base_url == "http://analytics.internal:9100"

    @pytest.mark.asyncio
    async def test_lifespan_calls_configure_with_config_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import dataclasses

        import api.api as api_module
        import api.routers.insights as mod
        from api.api import lifespan
        from tests.test_api import TestLifespanShutdownClosesAnthropicClient as Helper

        monkeypatch.setenv("NLQ_ENABLED", "false")
        monkeypatch.delenv("NLQ_API_KEY", raising=False)
        config = dataclasses.replace(Helper._make_config(), insights_base_url="http://analytics.internal:9100")
        health = MagicMock()
        pool = MagicMock()
        pool.initialize = AsyncMock()
        pool.close = AsyncMock()
        valkey = AsyncMock()
        neo4j = MagicMock()
        neo4j.close = AsyncMock()
        original = mod._INSIGHTS_BASE_URL
        try:
            with (
                patch("api.api.ApiConfig.from_env", return_value=config),
                patch("api.api.HealthServer", return_value=health),
                patch("api.api.AsyncPostgreSQLPool", return_value=pool),
                patch("api.api.reconcile_stale_sync_history", new_callable=AsyncMock),
                patch("api.api.aiovalkey.from_url", new_callable=AsyncMock, return_value=valkey),
                patch("api.api.AsyncResilientNeo4jDriver", return_value=neo4j),
                patch("api.api._prewarm_search_cache", new_callable=AsyncMock),
            ):
                async with lifespan(MagicMock()):
                    assert mod._INSIGHTS_BASE_URL == "http://analytics.internal:9100"
        finally:
            mod._INSIGHTS_BASE_URL = original
            api_module._pool = api_module._config = api_module._valkey = api_module._neo4j = None
