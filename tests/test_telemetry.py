"""Behavioral tests for the OpenTelemetry instruments catalog-api records.

Every assertion reads back from an in-memory metric reader, so what is checked is the metric
that would actually be exported: its name, its attribute keys, and its attribute values.
"""

from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import fakeredis.aioredis as aioredis_fake
import httpx
import pytest
import respx
from common import telemetry as common_telemetry
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.metrics import MeterProvider as SdkMeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader, Metric

import api.api as api_module
import api.telemetry as telemetry
from api.cache import RecommendCache
from api.syncer import run_full_sync


CACHE_METRIC = "groovemap.api.cache"
DB_DURATION = "db.client.operation.duration"
NLQ_METRIC = "groovemap.api.nlq.requests"
CLIENT_DURATION = "http.client.request.duration"
SERVER_DURATION = "http.server.request.duration"
SYNC_DURATION = "groovemap.api.sync.duration"

TEST_USER_UUID = UUID("00000000-0000-0000-0000-000000000001")


class Collector:
    """An in-memory MeterProvider whose recorded metrics can be read back by name."""

    def __init__(self) -> None:
        self.reader = InMemoryMetricReader()
        self.provider = SdkMeterProvider(metric_readers=[self.reader])

    def metrics(self) -> dict[str, Metric]:
        data = self.reader.get_metrics_data()
        if data is None:
            return {}
        return {
            metric.name: metric
            for resource_metrics in data.resource_metrics
            for scope_metrics in resource_metrics.scope_metrics
            for metric in scope_metrics.metrics
        }

    def attributes(self, name: str) -> list[dict[str, Any]]:
        """Return one attribute dict per recorded data point of ``name``."""
        metric = self.metrics().get(name)
        return [] if metric is None else [dict(point.attributes) for point in metric.data.data_points]


@pytest.fixture
def collector(monkeypatch: pytest.MonkeyPatch) -> Iterator[Collector]:
    """Install an in-memory provider as the one `common.get_meter` hands out meters from.

    This mirrors how `groovemap-runtime` tests its own instrumentation: `setup_telemetry`
    would build a live OTLP exporter, so the installed provider is replaced directly.
    """
    active = Collector()
    monkeypatch.setattr(common_telemetry, "_provider", active.provider)
    telemetry.reset_instruments()
    yield active
    monkeypatch.setattr(common_telemetry, "_provider", None)
    telemetry.reset_instruments()


class TestCacheInstrument:
    """groovemap.api.cache — Redis cache-aside hits and misses."""

    @pytest.mark.asyncio
    async def test_hit_and_miss_carry_outcome_and_cache(self, collector: Collector) -> None:
        redis = aioredis_fake.FakeRedis(decode_responses=True)
        await redis.set("k", "cached-value")

        assert await telemetry.cache_get(redis, "k", cache=telemetry.CACHE_SEARCH) == "cached-value"
        assert await telemetry.cache_get(redis, "absent", cache=telemetry.CACHE_SEARCH) is None

        assert collector.attributes(CACHE_METRIC) == [
            {"outcome": "hit", "cache": "search"},
            {"outcome": "miss", "cache": "search"},
        ]

    @pytest.mark.asyncio
    async def test_failed_read_counts_a_miss_and_re_raises(self, collector: Collector) -> None:
        redis = AsyncMock()
        redis.get = AsyncMock(side_effect=RuntimeError("redis down"))

        with pytest.raises(RuntimeError):
            await telemetry.cache_get(redis, "k", cache=telemetry.CACHE_NLQ_QUERY)

        assert collector.attributes(CACHE_METRIC) == [{"outcome": "miss", "cache": "nlq_query"}]

    @pytest.mark.asyncio
    async def test_no_redis_records_nothing(self, collector: Collector) -> None:
        assert await telemetry.cache_get(None, "k", cache=telemetry.CACHE_SEARCH) is None

        assert collector.attributes(CACHE_METRIC) == []

    @pytest.mark.asyncio
    async def test_recommend_cache_labels_its_own_reads(self, collector: Collector) -> None:
        redis = aioredis_fake.FakeRedis(decode_responses=True)
        cache = RecommendCache(redis=redis)
        await cache.set("recommend:similar:artist:1", {"similar": []})

        assert await cache.get("recommend:similar:artist:1") == {"similar": []}
        assert await cache.get("recommend:similar:artist:2") is None

        assert collector.attributes(CACHE_METRIC) == [
            {"outcome": "hit", "cache": "recommend"},
            {"outcome": "miss", "cache": "recommend"},
        ]

    @pytest.mark.asyncio
    async def test_recommend_cache_swallows_a_redis_error_after_counting_it(self, collector: Collector) -> None:
        redis = AsyncMock()
        redis.get = AsyncMock(side_effect=RuntimeError("redis down"))

        assert await RecommendCache(redis=redis).get("recommend:explore:u:1") is None

        assert collector.attributes(CACHE_METRIC) == [{"outcome": "miss", "cache": "recommend"}]

    def test_every_cache_label_is_a_distinct_low_cardinality_constant(self) -> None:
        labels = [value for name, value in vars(telemetry).items() if name.startswith("CACHE_") and name not in {"CACHE_HIT", "CACHE_MISS"}]

        assert len(labels) == len(set(labels))
        assert all(label.replace("_", "").isalnum() for label in labels)


class TestRedisOperationDuration:
    """db.client.operation.duration — Redis is not behind a shared resilient wrapper."""

    @pytest.mark.asyncio
    async def test_command_records_system_and_operation(self, collector: Collector) -> None:
        redis = telemetry.instrument_redis(aioredis_fake.FakeRedis())

        await redis.set("k", "v")
        assert await redis.get("k") == b"v"

        assert collector.attributes(DB_DURATION) == [
            {"db.system.name": "redis", "db.operation.name": "set"},
            {"db.system.name": "redis", "db.operation.name": "get"},
        ]

    @pytest.mark.asyncio
    async def test_failed_command_records_error_type(self, collector: Collector) -> None:
        client = AsyncMock()
        client.get = AsyncMock(side_effect=TimeoutError("no route"))
        redis = telemetry.instrument_redis(client)

        with pytest.raises(TimeoutError):
            await redis.get("k")

        assert collector.attributes(DB_DURATION) == [{"db.system.name": "redis", "db.operation.name": "get", "error.type": "TimeoutError"}]

    @pytest.mark.asyncio
    async def test_duration_is_recorded_in_seconds(self, collector: Collector) -> None:
        redis = telemetry.instrument_redis(aioredis_fake.FakeRedis())

        await redis.get("k")

        (point,) = collector.metrics()[DB_DURATION].data.data_points
        assert point.count == 1
        assert 0 <= point.sum < 5
        assert collector.metrics()[DB_DURATION].unit == "s"

    @pytest.mark.asyncio
    async def test_proxy_stands_in_for_the_client(self, collector: Collector) -> None:
        client = aioredis_fake.FakeRedis()
        redis = telemetry.instrument_redis(client)

        # Non-command attributes pass through untouched, and closing is lifecycle, not a
        # database operation, so it is not timed.
        assert redis.connection_pool is client.connection_pool
        await redis.aclose()

        assert collector.attributes(DB_DURATION) == []

    @pytest.mark.asyncio
    async def test_pipeline_is_returned_intact_not_wrapped(self, collector: Collector) -> None:
        # A Pipeline is awaitable but is used as an async context manager, so it must come
        # back as itself. Wrapping every awaitable rather than every coroutine breaks this.
        redis = telemetry.instrument_redis(aioredis_fake.FakeRedis(decode_responses=True))

        async with redis.pipeline() as pipe:
            pipe.set("k", "v")
            assert await pipe.execute() == [True]
        assert await redis.get("k") == "v"

        # The pipeline object itself is not proxied, so its batched commands are not timed
        # individually. The service issues no pipelines; what matters here is that one works.
        assert [attributes["db.operation.name"] for attributes in collector.attributes(DB_DURATION)] == ["get"]

    @pytest.mark.asyncio
    async def test_cache_aside_read_through_the_proxy_records_both_metrics(self, collector: Collector) -> None:
        redis = telemetry.instrument_redis(aioredis_fake.FakeRedis())

        await telemetry.cache_get(redis, "absent", cache=telemetry.CACHE_TRENDS)

        assert collector.attributes(CACHE_METRIC) == [{"outcome": "miss", "cache": "trends"}]
        assert collector.attributes(DB_DURATION) == [{"db.system.name": "redis", "db.operation.name": "get"}]


class TestSyncDuration:
    """groovemap.api.sync.duration — one observation per full Discogs sync run."""

    @staticmethod
    def _pool_with_credentials() -> MagicMock:
        cur = AsyncMock()
        cur.execute = AsyncMock()
        cur.fetchone = AsyncMock(return_value={"access_token": "at", "access_secret": "as", "provider_username": "test_dj"})
        cur.fetchall = AsyncMock(
            return_value=[
                {"key": "discogs_consumer_key", "value": "ck"},
                {"key": "discogs_consumer_secret", "value": "cs"},
            ]
        )
        cur_ctx = AsyncMock()
        cur_ctx.__aenter__ = AsyncMock(return_value=cur)
        cur_ctx.__aexit__ = AsyncMock(return_value=False)
        conn = AsyncMock()
        conn.cursor = MagicMock(return_value=cur_ctx)
        conn_ctx = AsyncMock()
        conn_ctx.__aenter__ = AsyncMock(return_value=conn)
        conn_ctx.__aexit__ = AsyncMock(return_value=False)
        pool = MagicMock()
        pool.connection = MagicMock(return_value=conn_ctx)
        return pool

    @pytest.mark.asyncio
    async def test_completed_sync_records_its_outcome(self, collector: Collector) -> None:
        with (
            patch("api.syncer.sync_collection", new_callable=AsyncMock, return_value=10),
            patch("api.syncer.sync_wantlist", new_callable=AsyncMock, return_value=5),
            patch("api.syncer.decrypt_oauth_token", side_effect=lambda val, _key: val),
        ):
            result = await run_full_sync(TEST_USER_UUID, "sync-1", self._pool_with_credentials(), MagicMock(), "TestApp/1.0")

        assert result["status"] == "completed"
        assert collector.attributes(SYNC_DURATION) == [{"outcome": "completed"}]
        assert collector.metrics()[SYNC_DURATION].unit == "s"

    @pytest.mark.asyncio
    async def test_failed_sync_records_its_outcome(self, collector: Collector) -> None:
        with (
            patch("api.syncer.sync_collection", new_callable=AsyncMock, side_effect=RuntimeError("boom")),
            patch("api.syncer.decrypt_oauth_token", side_effect=lambda val, _key: val),
        ):
            result = await run_full_sync(TEST_USER_UUID, "sync-2", self._pool_with_credentials(), MagicMock(), "TestApp/1.0")

        assert result["status"] == "failed"
        assert collector.attributes(SYNC_DURATION) == [{"outcome": "failed"}]

    @pytest.mark.asyncio
    async def test_attributes_carry_no_user_or_sync_identifier(self, collector: Collector) -> None:
        with (
            patch("api.syncer.sync_collection", new_callable=AsyncMock, return_value=1),
            patch("api.syncer.sync_wantlist", new_callable=AsyncMock, return_value=1),
            patch("api.syncer.decrypt_oauth_token", side_effect=lambda val, _key: val),
        ):
            await run_full_sync(TEST_USER_UUID, "sync-3", self._pool_with_credentials(), MagicMock(), "TestApp/1.0")

        (attributes,) = collector.attributes(SYNC_DURATION)
        assert set(attributes) == {"outcome"}


class TestNlqRequests:
    """groovemap.api.nlq.requests — one count per natural-language query request."""

    @pytest.mark.asyncio
    async def test_engine_result_counts_a_success(self, collector: Collector) -> None:
        telemetry.record_nlq_request(telemetry.NLQ_SUCCESS)

        assert collector.attributes(NLQ_METRIC) == [{"outcome": "success"}]

    def test_unavailable_engine_counts_an_outcome(self, test_client: TestClient, collector: Collector) -> None:
        response = test_client.post("/api/nlq/query", json={"query": "who produced this"})

        assert response.status_code == 503
        assert collector.attributes(NLQ_METRIC) == [{"outcome": "unavailable"}]

    def test_rejected_input_counts_an_outcome(self, test_client: TestClient, collector: Collector) -> None:
        response = test_client.post("/api/nlq/query", json={"query": "   "})

        assert response.status_code == 400
        assert collector.attributes(NLQ_METRIC) == [{"outcome": "invalid"}]

    def test_every_nlq_outcome_is_a_distinct_constant(self) -> None:
        outcomes = [value for name, value in vars(telemetry).items() if name.startswith("NLQ_")]

        assert sorted(outcomes) == ["cached", "error", "invalid", "success", "unavailable"]


class TestHttpServerInstrumentation:
    """http.server.request.duration — emitted for routers, not for the health probe."""

    @pytest.fixture
    def instrumented_app(self, collector: Collector) -> Iterator[FastAPI]:  # noqa: ARG002 -- installs the in-memory provider the instrumentation binds to
        """A real catalog-api router mounted on its own app, instrumented in place.

        The router is left unconfigured so every request short-circuits to 503 without a
        backing store. What is under test is the route attribute, not the handler.
        """
        import api.routers.network as network_router

        previous = (network_router._neo4j, network_router._redis)
        network_router.configure(None, None)

        app = FastAPI()
        app.include_router(network_router.router)

        @app.get("/health")
        async def health_check() -> JSONResponse:
            return JSONResponse(content={"status": "healthy"})

        assert api_module.instrument_fastapi_app(app) is True
        yield app
        FastAPIInstrumentor.uninstrument_app(app)
        network_router.configure(*previous)

    def test_route_attribute_is_the_template_not_the_request_path(self, instrumented_app: FastAPI, collector: Collector) -> None:
        with TestClient(instrumented_app) as client:
            client.get("/api/network/artist/a-very-specific-artist-id/centrality")

        (attributes,) = collector.attributes(SERVER_DURATION)
        assert attributes["http.route"] == "/api/network/artist/{artist_id}/centrality"
        assert attributes["http.request.method"] == "GET"
        assert attributes["http.response.status_code"] == 503

    def test_health_probe_is_excluded(self, instrumented_app: FastAPI, collector: Collector) -> None:
        with TestClient(instrumented_app) as client:
            assert client.get("/health").status_code == 200

        assert collector.attributes(SERVER_DURATION) == []


class TestHttpClientInstrumentation:
    """http.client.request.duration — every outbound httpx call in the process."""

    @pytest.mark.asyncio
    @respx.mock
    async def test_outbound_call_records_server_address_and_status(self, collector: Collector) -> None:
        # The service's outbound clients are built inside the functions that call out
        # (Discogs in api/syncer.py, analytics-engine in api/routers/insights.py, and the
        # Anthropic SDK's own client), so the entrypoint instruments httpx globally rather
        # than client by client.
        respx.get("https://api.discogs.com/users/test_dj/collection").respond(200, json={"releases": []})
        assert api_module.instrument_httpx() is True
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get("https://api.discogs.com/users/test_dj/collection")
        finally:
            HTTPXClientInstrumentor().uninstrument()

        assert response.status_code == 200
        (attributes,) = collector.attributes(CLIENT_DURATION)
        assert attributes["server.address"] == "api.discogs.com"
        assert attributes["http.request.method"] == "GET"
        assert attributes["http.response.status_code"] == 200
        # The path carries a Discogs username; only the host may appear in an attribute.
        assert "test_dj" not in str(attributes)


class TestEntrypointWiring:
    """The entrypoint installs the provider before it instruments anything."""

    def test_configure_telemetry_sets_up_then_instruments(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[tuple[str, Any]] = []
        monkeypatch.setattr(api_module, "setup_telemetry", lambda name, **kw: calls.append(("setup", (name, kw))))
        monkeypatch.setattr(api_module, "instrument_fastapi_app", lambda app: calls.append(("fastapi", app)))
        monkeypatch.setattr(api_module, "instrument_httpx", lambda: calls.append(("httpx", None)))

        api_module.configure_telemetry()

        assert [name for name, _ in calls] == ["setup", "fastapi", "httpx"]
        assert calls[0][1] == ("api", {"service_version": api_module.__version__})
        assert calls[1][1] is api_module.app


class TestDisabledTelemetry:
    """With no collector configured the service behaves exactly as it did before."""

    def test_setup_without_an_endpoint_installs_a_no_op_provider(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(common_telemetry, "_provider", None)
        monkeypatch.setattr(common_telemetry, "_sdk_provider", None)
        telemetry.reset_instruments()
        try:
            api_module.configure_telemetry()

            # Nothing raised, no exporter was built, and recording is inert.
            assert common_telemetry._sdk_provider is None
            telemetry.record_nlq_request(telemetry.NLQ_SUCCESS)
            telemetry.record_sync_duration(0.1, "completed")
            telemetry.record_cache(telemetry.CACHE_SEARCH, hit=True)
        finally:
            FastAPIInstrumentor.uninstrument_app(api_module.app)
            common_telemetry.shutdown_telemetry()
            telemetry.reset_instruments()

    def test_endpoints_still_serve_with_telemetry_off(self, test_client: TestClient) -> None:
        assert test_client.get("/health").json()["service"] == "api"
        assert test_client.get("/api/nlq/status").json() == {"enabled": False}
