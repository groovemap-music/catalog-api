"""Behavioral tests for the OpenTelemetry signals catalog-api emits.

Every assertion reads back from an in-memory metric reader or an in-memory span exporter, so
what is checked is the telemetry that would actually be exported: a metric's name and its
attribute values, a span's name, kind, parent, and attributes.
"""

import asyncio
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
from opentelemetry.instrumentation.system_metrics import SystemMetricsInstrumentor
from opentelemetry.sdk.metrics import MeterProvider as SdkMeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader, Metric
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace import TracerProvider as SdkTracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode

import api.api as api_module
import api.routers.nlq as nlq_router
import api.telemetry as telemetry
from api.cache import RecommendCache
from api.config import ApiConfig
from api.syncer import run_full_sync


CACHE_METRIC = "groovemap.api.cache"
DB_DURATION = "db.client.operation.duration"
NLQ_METRIC = "groovemap.api.nlq.requests"
CLIENT_DURATION = "http.client.request.duration"
SERVER_DURATION = "http.server.request.duration"
SYNC_DURATION = "groovemap.api.sync.duration"
EVENT_LOOP_LAG = "groovemap.runtime.event_loop.lag"

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


class Traces:
    """An in-memory TracerProvider whose finished spans can be read back by name."""

    def __init__(self) -> None:
        self.exporter = InMemorySpanExporter()
        self.provider = SdkTracerProvider()
        self.provider.add_span_processor(SimpleSpanProcessor(self.exporter))

    def spans(self) -> list[ReadableSpan]:
        return list(self.exporter.get_finished_spans())

    def named(self, name: str) -> list[ReadableSpan]:
        return [span for span in self.spans() if span.name == name]


@pytest.fixture
def traces(monkeypatch: pytest.MonkeyPatch) -> Iterator[Traces]:
    """Install an in-memory provider as the one `common.get_tracer` hands out tracers from.

    The tracing half of the `collector` fixture, and installed the same way: `setup_telemetry`
    would build a live OTLP exporter, so the installed provider is replaced directly.
    """
    active = Traces()
    monkeypatch.setattr(common_telemetry, "_tracer_provider", active.provider)
    yield active
    monkeypatch.setattr(common_telemetry, "_tracer_provider", None)
    active.provider.shutdown()


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
            # configure_telemetry instruments httpx process-wide, not per client, so it has
            # to be undone here or the next test binds to this test's provider.
            FastAPIInstrumentor.uninstrument_app(api_module.app)
            HTTPXClientInstrumentor().uninstrument()
            common_telemetry.shutdown_telemetry()
            telemetry.reset_instruments()

    def test_endpoints_still_serve_with_telemetry_off(self, test_client: TestClient) -> None:
        assert test_client.get("/health").json()["service"] == "api"
        assert test_client.get("/api/nlq/status").json() == {"enabled": False}


class TestDomainSpans:
    """api.sync and api.nlq — the two root spans this service opens itself."""

    @pytest.mark.asyncio
    async def test_completed_sync_opens_one_api_sync_span_carrying_its_outcome(self, collector: Collector, traces: Traces) -> None:
        with (
            patch("api.syncer.sync_collection", new_callable=AsyncMock, return_value=10),
            patch("api.syncer.sync_wantlist", new_callable=AsyncMock, return_value=5),
            patch("api.syncer.decrypt_oauth_token", side_effect=lambda val, _key: val),
        ):
            await run_full_sync(TEST_USER_UUID, "sync-1", TestSyncDuration._pool_with_credentials(), MagicMock(), "TestApp/1.0")

        (span,) = traces.named(telemetry.SPAN_SYNC)
        assert dict(span.attributes or {}) == {"outcome": "completed"}
        assert span.parent is None
        assert span.status.status_code is not StatusCode.ERROR
        # The span and the histogram report the same run and agree about how it ended.
        assert collector.attributes(SYNC_DURATION) == [{"outcome": "completed"}]

    @pytest.mark.asyncio
    async def test_failed_sync_span_reports_the_failure_as_an_outcome(self, collector: Collector, traces: Traces) -> None:
        with (
            patch("api.syncer.sync_collection", new_callable=AsyncMock, side_effect=RuntimeError("boom")),
            patch("api.syncer.decrypt_oauth_token", side_effect=lambda val, _key: val),
        ):
            await run_full_sync(TEST_USER_UUID, "sync-2", TestSyncDuration._pool_with_credentials(), MagicMock(), "TestApp/1.0")

        (span,) = traces.named(telemetry.SPAN_SYNC)
        assert dict(span.attributes or {}) == {"outcome": "failed"}
        assert collector.attributes(SYNC_DURATION) == [{"outcome": "failed"}]

    def test_rejected_query_opens_an_api_nlq_span_with_its_outcome(self, test_client: TestClient, collector: Collector, traces: Traces) -> None:
        assert test_client.post("/api/nlq/query", json={"query": "   "}).status_code == 400

        (span,) = traces.named(telemetry.SPAN_NLQ)
        assert dict(span.attributes or {}) == {"outcome": "invalid"}
        assert collector.attributes(NLQ_METRIC) == [{"outcome": "invalid"}]

    def test_unavailable_engine_opens_an_api_nlq_span_with_its_outcome(self, test_client: TestClient, collector: Collector, traces: Traces) -> None:
        assert test_client.post("/api/nlq/query", json={"query": "who produced this"}).status_code == 503

        (span,) = traces.named(telemetry.SPAN_NLQ)
        assert dict(span.attributes or {}) == {"outcome": "unavailable"}
        assert collector.attributes(NLQ_METRIC) == [{"outcome": "unavailable"}]

    @pytest.mark.asyncio
    async def test_streaming_query_opens_its_span_inside_the_event_generator(self, collector: Collector, traces: Traces) -> None:
        # A streaming response is produced after the handler has returned, so the span has to
        # be opened by the generator or it would close before the first event was yielded.
        cached = {"query": "who produced Thriller", "summary": "Quincy Jones", "entities": [], "tools_used": [], "actions": []}
        response = nlq_router._stream_response("who produced Thriller", None, None, cached=cached)

        events = [event async for event in response.body_iterator]

        assert [event.get("event") for event in events] == ["actions", "result"]
        (span,) = traces.named(telemetry.SPAN_NLQ)
        assert dict(span.attributes or {}) == {"outcome": "cached"}
        assert collector.attributes(NLQ_METRIC) == [{"outcome": "cached"}]

    def test_a_domain_span_carries_an_outcome_and_nothing_else(self, test_client: TestClient, collector: Collector, traces: Traces) -> None:  # noqa: ARG002 -- the metric side is exercised by its own tests
        test_client.post("/api/nlq/query", json={"query": "who produced this"})

        (span,) = traces.named(telemetry.SPAN_NLQ)
        assert set(span.attributes or {}) == {"outcome"}
        assert span.kind is SpanKind.INTERNAL

    @pytest.mark.asyncio
    async def test_a_raised_error_fails_the_span_with_its_type_only(self, collector: Collector, traces: Traces) -> None:  # noqa: ARG002 -- installs the provider the instruments bind to
        with pytest.raises(TimeoutError), telemetry.api_span(telemetry.SPAN_NLQ):
            raise TimeoutError("upstream is gone")

        (span,) = traces.named(telemetry.SPAN_NLQ)
        assert dict(span.attributes or {}) == {"error.type": "TimeoutError"}
        assert span.status.status_code is StatusCode.ERROR
        # The conventions allow a status and an error type, never a message or a stack trace.
        assert len(span.events) == 0
        assert "upstream is gone" not in str(span.status.description)

    @pytest.mark.asyncio
    async def test_an_outcome_is_never_stamped_on_a_span_this_service_did_not_open(self, collector: Collector, traces: Traces) -> None:  # noqa: ARG002 -- installs the provider the instruments bind to
        # Recording outside a domain span must not decorate whatever span happens to be
        # current — an HTTP server span, for instance — with an application outcome.
        with telemetry.get_tracer(telemetry.TRACER_NAME).start_as_current_span("http.server.request") as server_span:
            telemetry.record_nlq_request(telemetry.NLQ_SUCCESS)

            assert dict(server_span.attributes or {}) == {}


class TestTracePropagation:
    """W3C TraceContext leaves this service on every outbound httpx call."""

    @pytest.mark.asyncio
    @respx.mock
    async def test_outbound_call_carries_the_traceparent_of_the_domain_span(self, collector: Collector, traces: Traces) -> None:  # noqa: ARG002 -- installs the provider the instruments bind to
        route = respx.get("https://api.discogs.com/users/test_dj/collection").respond(200, json={"releases": []})
        assert api_module.instrument_httpx() is True
        try:
            with telemetry.api_span(telemetry.SPAN_SYNC) as domain_span:
                domain_context = domain_span.get_span_context()
                async with httpx.AsyncClient(timeout=30.0) as client:
                    assert (await client.get("https://api.discogs.com/users/test_dj/collection")).status_code == 200
        finally:
            HTTPXClientInstrumentor().uninstrument()

        # The header the collector will stitch the downstream service onto.
        version, trace_id, span_id, _flags = route.calls.last.request.headers["traceparent"].split("-")
        assert version == "00"
        assert trace_id == format(domain_context.trace_id, "032x")

        # And it names this service's own CLIENT span, which hangs off the domain span.
        (client_span,) = [span for span in traces.spans() if span.kind is SpanKind.CLIENT]
        assert span_id == format(client_span.context.span_id, "016x")
        assert client_span.parent is not None
        assert client_span.parent.span_id == domain_context.span_id


class TestTracingConfiguration:
    """The env-var contract: metrics and traces are turned on and off independently."""

    def test_traces_off_with_an_endpoint_set_exports_metrics_and_no_spans(self, monkeypatch: pytest.MonkeyPatch) -> None:
        active = Collector()
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4318")
        monkeypatch.setenv("OTEL_TRACES_EXPORTER", "none")
        # Stand in for the OTLP push exporter so the metrics the SDK really records can be
        # read back here; every tracing decision below is the library's own.
        monkeypatch.setattr(common_telemetry, "_build_sdk_provider", lambda *_args, **_kwargs: active.provider)
        telemetry.reset_instruments()
        try:
            api_module.configure_telemetry()

            assert common_telemetry._sdk_provider is active.provider
            assert common_telemetry._sdk_tracer_provider is None

            with telemetry.api_span(telemetry.SPAN_SYNC) as span:
                telemetry.record_sync_duration(0.25, "completed")

            # Metrics flow, including the process view the `otel` extra now installs.
            assert active.attributes(SYNC_DURATION) == [{"outcome": "completed"}]
            assert "process.cpu.time" in active.metrics()
            # No span was created at all: the no-op tracer hands back an invalid context.
            assert span.get_span_context().is_valid is False
        finally:
            FastAPIInstrumentor.uninstrument_app(api_module.app)
            HTTPXClientInstrumentor().uninstrument()
            SystemMetricsInstrumentor().uninstrument()
            common_telemetry.shutdown_telemetry()
            telemetry.reset_instruments()


class TestEventLoopMonitor:
    """groovemap.runtime.event_loop.lag — started by the lifespan, stopped with the providers."""

    @staticmethod
    def _make_config() -> ApiConfig:
        return ApiConfig(
            postgres_host="localhost:5432",
            postgres_username="u",
            postgres_password="p",  # nosec B106  # noqa: S106  # test fixture value, not a real credential
            postgres_database="db",
            jwt_secret_key="x" * 32,
            # No Neo4j and no NLQ: the lifespan under test is the telemetry part of it.
            neo4j_host="",
            neo4j_username="",
            neo4j_password="",  # nosec B106  # test fixture value, not a real credential
            resend_api_key=None,
        )

    @pytest.mark.asyncio
    async def test_lifespan_starts_the_monitor_and_shutdown_cancels_it(self, monkeypatch: pytest.MonkeyPatch, collector: Collector) -> None:
        from api.api import lifespan

        # The monitor samples only when metrics are actually being exported, which is what
        # the SDK provider being installed means.
        monkeypatch.setattr(common_telemetry, "_sdk_provider", collector.provider)
        monkeypatch.setenv("NLQ_ENABLED", "false")
        monkeypatch.delenv("NLQ_API_KEY", raising=False)

        mock_health = MagicMock()
        mock_pool = MagicMock()
        mock_pool.initialize = AsyncMock()
        mock_pool.close = AsyncMock()
        mock_redis = AsyncMock()
        mock_redis.aclose = AsyncMock()
        app = MagicMock()

        with (
            patch("api.api.ApiConfig.from_env", return_value=self._make_config()),
            patch("api.api.HealthServer", return_value=mock_health),
            patch("api.api.AsyncPostgreSQLPool", return_value=mock_pool),
            patch("api.api.reconcile_stale_sync_history", new_callable=AsyncMock),
            patch("api.api.aioredis.from_url", new_callable=AsyncMock, return_value=mock_redis),
            patch("api.api.run_collector", new_callable=AsyncMock),
            patch("api.api._prewarm_search_cache", new_callable=AsyncMock),
        ):
            async with lifespan(app):
                monitor = app.state.event_loop_monitor
                assert monitor is not None
                assert monitor.get_name() == "groovemap-event-loop-monitor"
                assert not monitor.done()

        # shutdown_telemetry() runs at the end of the lifespan: it cancels the sampler and
        # drops both providers, so nothing is still recording when the process exits.
        await asyncio.gather(monitor, return_exceptions=True)
        assert monitor.cancelled()
        assert common_telemetry._sdk_provider is None

        api_module._pool = None
        api_module._config = None
        api_module._redis = None
        api_module._neo4j = None

    @pytest.mark.asyncio
    async def test_monitor_is_not_started_when_metrics_are_not_exported(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # With no endpoint configured there is nothing to sample into, so the lifespan gets
        # None back and the service runs exactly as it did before.
        monkeypatch.setattr(common_telemetry, "_sdk_provider", None)

        assert common_telemetry.start_event_loop_monitor() is None
