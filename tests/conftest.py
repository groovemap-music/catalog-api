"""Fixtures for API service tests."""

import base64
import hashlib
import hmac
import json
import os


# Set environment variables BEFORE importing api modules
os.environ.setdefault("POSTGRES_HOST", "localhost:5432")
os.environ.setdefault("POSTGRES_USERNAME", "test")
os.environ.setdefault("POSTGRES_PASSWORD", "test")
os.environ.setdefault("POSTGRES_DATABASE", "test")
os.environ.setdefault("JWT_SECRET_KEY", "test-jwt-secret-for-unit-tests")
os.environ.setdefault("REDIS_HOST", "redis://localhost:6379/0")
os.environ.setdefault("NEO4J_HOST", "bolt://localhost:7687")
os.environ.setdefault("NEO4J_USERNAME", "neo4j")
os.environ.setdefault("NEO4J_PASSWORD", "testpassword")

from collections.abc import AsyncGenerator, Generator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, create_autospec

import fakeredis
import fakeredis.aioredis as aioredis_fake
import pytest
from common import AsyncPostgreSQLPool, AsyncResilientNeo4jDriver
from fastapi import FastAPI
from fastapi.testclient import TestClient
from neo4j import AsyncResult, AsyncSession
from psycopg import AsyncConnection, AsyncCursor, AsyncTransaction

from api.config import ApiConfig


_TEST_MASTER_KEY = base64.urlsafe_b64encode(b"test-master-key-padded-to-32!!").decode("ascii")

TEST_JWT_SECRET = "test-jwt-secret-for-unit-tests"
TEST_USER_ID = "00000000-0000-0000-0000-000000000001"
TEST_USER_EMAIL = "test@example.com"
TEST_INTERNAL_SECRET = "test-internal-insights-secret"  # nosec B105


def make_test_jwt(
    user_id: str = TEST_USER_ID,
    email: str = TEST_USER_EMAIL,
    exp: int = 9_999_999_999,
    secret: str = TEST_JWT_SECRET,
) -> str:
    """Create a valid HS256 JWT for testing."""

    def b64url(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")

    header = b64url(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    body = b64url(json.dumps({"sub": user_id, "email": email, "exp": exp}, separators=(",", ":")).encode())
    signing_input = f"{header}.{body}".encode("ascii")
    sig = b64url(hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest())
    return f"{header}.{body}.{sig}"


@pytest.fixture
def mock_cur() -> MagicMock:
    """Autospecced psycopg cursor with await-faithful query methods."""
    cur = create_autospec(AsyncCursor, instance=True, spec_set=True)
    cur.__aenter__.return_value = cur
    cur.__aexit__.return_value = False
    cur.fetchone.return_value = None
    cur.fetchall.return_value = []
    return cur


@pytest.fixture
def mock_transaction() -> MagicMock:
    """Autospecced psycopg transaction context."""
    transaction = create_autospec(AsyncTransaction, instance=True, spec_set=True)
    transaction.__aenter__.return_value = transaction
    transaction.__aexit__.return_value = False
    return transaction


@pytest.fixture
def mock_conn(mock_cur: MagicMock, mock_transaction: MagicMock) -> MagicMock:
    """Autospecced psycopg connection that yields the shared cursor."""
    conn = create_autospec(AsyncConnection, instance=True, spec_set=True)
    conn.__aenter__.return_value = conn
    conn.__aexit__.return_value = False
    conn.cursor.return_value = mock_cur
    conn.transaction.return_value = mock_transaction
    return conn


@pytest.fixture
def mock_pool(mock_conn: MagicMock) -> MagicMock:
    """Autospecced resilient pool that yields the shared connection."""
    pool = create_autospec(AsyncPostgreSQLPool, instance=True, spec_set=True)
    pool.connection.return_value = mock_conn
    return pool


@pytest.fixture
def mock_redis() -> AsyncMock:
    """Mock aioredis client."""
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    redis.setex = AsyncMock()
    redis.delete = AsyncMock()
    redis.aclose = AsyncMock()
    return redis


@pytest.fixture
def mock_neo4j_result() -> MagicMock:
    """Autospecced Neo4j result with empty defaults."""
    result = create_autospec(AsyncResult, instance=True, spec_set=True)
    result.__aiter__.return_value = []
    result.data.return_value = []
    result.single.return_value = None
    result.values.return_value = []
    return result


@pytest.fixture
def mock_neo4j_session(mock_neo4j_result: MagicMock) -> MagicMock:
    """Autospecced Neo4j async session that returns the shared result."""
    session = create_autospec(AsyncSession, instance=True, spec_set=True)
    session.__aenter__.return_value = session
    session.__aexit__.return_value = False
    session.run.return_value = mock_neo4j_result
    return session


@pytest.fixture
def mock_neo4j(mock_neo4j_session: MagicMock) -> MagicMock:
    """Autospecced resilient Neo4j driver that yields the shared session."""
    driver = create_autospec(AsyncResilientNeo4jDriver, instance=True, spec_set=True)
    driver.session.return_value = mock_neo4j_session
    return driver


@pytest.fixture
def test_api_config() -> ApiConfig:
    """Create a test ApiConfig with the test JWT secret."""
    return ApiConfig(
        postgres_host="localhost:5432",
        postgres_username="test",
        postgres_password="test",  # noqa: S106
        postgres_database="test",
        jwt_secret_key=TEST_JWT_SECRET,
        redis_host="redis://localhost:6379/0",
        jwt_expire_minutes=30,
        neo4j_host="bolt://localhost:7687",
        neo4j_username="neo4j",
        neo4j_password="testpassword",  # noqa: S106
        insights_internal_secret=TEST_INTERNAL_SECRET,
    )


@pytest.fixture
def valid_token() -> str:
    """Create a valid JWT token for testing."""
    return make_test_jwt()


@pytest.fixture
def fake_redis_server() -> fakeredis.FakeServer:
    """Shared FakeServer allowing both async and sync fakeredis clients to access the same data."""
    return fakeredis.FakeServer()


@pytest.fixture
def test_client(
    mock_pool: MagicMock,
    mock_redis: AsyncMock,
    mock_neo4j: MagicMock,
    test_api_config: ApiConfig,
    fake_redis_server: fakeredis.FakeServer,
) -> Generator[TestClient]:
    """Create a TestClient with mocked lifespan and module-level state."""
    import api.api as api_module
    from api.api import app

    @asynccontextmanager
    async def mock_lifespan(_app: FastAPI) -> AsyncGenerator[None]:
        yield

    original_lifespan = app.router.lifespan_context
    original_pool = api_module._pool
    original_config = api_module._config
    original_redis = api_module._redis
    original_neo4j = api_module._neo4j

    app.router.lifespan_context = mock_lifespan
    api_module._pool = mock_pool
    api_module._config = test_api_config
    api_module._redis = mock_redis
    api_module._neo4j = mock_neo4j

    import api.routers.admin as _admin_router
    import api.routers.collection as _collection_router
    import api.routers.explore as _explore_router
    import api.routers.label_dna as _label_dna_router
    import api.routers.recommend as _recommend_router
    import api.routers.search as _search_router
    import api.routers.snapshot as _snapshot_router
    import api.routers.sync as _sync_router
    import api.routers.taste as _taste_router
    import api.routers.user as _user_router

    fake_redis = aioredis_fake.FakeRedis(server=fake_redis_server)
    _sync_router.configure(mock_pool, mock_neo4j, test_api_config, api_module._running_syncs, mock_redis)
    _explore_router.configure(mock_neo4j, test_api_config.jwt_secret_key, mock_redis, pg_pool=mock_pool)
    _label_dna_router.configure(mock_neo4j, mock_redis)
    _user_router.configure(mock_neo4j, test_api_config.jwt_secret_key)
    _taste_router.configure(mock_neo4j, test_api_config.jwt_secret_key)
    _collection_router.configure(mock_neo4j, mock_pool, test_api_config.jwt_secret_key)
    _snapshot_router.configure(jwt_secret=TEST_JWT_SECRET, redis_client=fake_redis)
    import api.routers.insights_compute as _insights_compute_router

    _search_router.configure(mock_pool, mock_redis)
    _recommend_router.configure(mock_neo4j, test_api_config.jwt_secret_key, mock_redis)
    _insights_compute_router.configure(mock_neo4j, mock_pool, mock_redis, test_api_config)
    _admin_router.configure(mock_pool, mock_redis, test_api_config, neo4j_driver=mock_neo4j)
    import api.routers.extraction_analysis as _extraction_analysis_router

    _extraction_analysis_router.configure(discogs_root=None, musicbrainz_root=None)

    # Build a dedicated pool for require_admin DB verification that always returns
    # {"is_admin": True} so admin-token tests pass without conflicting with
    # per-test mock_cur.fetchone configuration.
    _admin_verify_cur = create_autospec(AsyncCursor, instance=True, spec_set=True)
    _admin_verify_cur.__aenter__.return_value = _admin_verify_cur
    _admin_verify_cur.__aexit__.return_value = False
    _admin_verify_cur.fetchone.return_value = {"is_admin": True}
    _admin_verify_conn = create_autospec(AsyncConnection, instance=True, spec_set=True)
    _admin_verify_conn.__aenter__.return_value = _admin_verify_conn
    _admin_verify_conn.__aexit__.return_value = False
    _admin_verify_conn.cursor.return_value = _admin_verify_cur
    _admin_verify_pool = create_autospec(AsyncPostgreSQLPool, instance=True, spec_set=True)
    _admin_verify_pool.connection.return_value = _admin_verify_conn

    import api.dependencies as _deps

    _deps.configure(TEST_JWT_SECRET, mock_redis, pool=_admin_verify_pool)

    import api.app_tokens as _app_tokens_module

    _app_tokens_module.configure(mock_pool)

    import api.activity as _activity_module
    import api.identity as _identity_module
    import api.routers.observations as _observations_router

    _identity_module.configure(mock_pool)
    _observations_router.configure(mock_pool)
    _activity_module.configure(mock_pool, mock_redis)

    import api.routers.nlq as _nlq_router
    from api.nlq.config import NLQConfig

    _nlq_router.configure(NLQConfig(), None, mock_redis, jwt_secret=TEST_JWT_SECRET)

    import api.routers.credits as _credits_router
    import api.routers.musicbrainz as _musicbrainz_router
    import api.routers.network as _network_router
    import api.routers.rarity as _rarity_router

    _credits_router.configure(mock_neo4j, mock_redis)
    _musicbrainz_router.configure(mock_pool, mock_neo4j)
    _network_router.configure(mock_neo4j, mock_redis)
    _rarity_router.configure(mock_neo4j, mock_pool, mock_redis)

    # Set up metrics buffer so the metrics middleware records requests
    from api.metrics_collector import MetricsBuffer

    app.state.metrics_buffer = MetricsBuffer()

    import api.routers.auth as _auth_router
    from api.notifications import LogNotificationChannel

    _auth_router.configure(
        mock_pool,
        mock_redis,
        test_api_config,
        api_module._get_current_user,
        api_module._create_access_token,
        notification_channel=LogNotificationChannel(),
    )

    # Default the internal-insights shared secret on every request so the existing
    # /api/internal/insights/* tests keep passing; the header is ignored by all
    # other routers. Tests that assert rejection build their own bare client.
    with TestClient(app, raise_server_exceptions=False, headers={"X-Internal-Secret": TEST_INTERNAL_SECRET}) as client:
        yield client

    # Restore original state
    if hasattr(app.state, "metrics_buffer"):
        del app.state.metrics_buffer
    api_module._pool = original_pool
    api_module._config = original_config
    api_module._redis = original_redis
    api_module._neo4j = original_neo4j
    api_module._running_syncs.clear()
    app.router.lifespan_context = original_lifespan


@pytest.fixture(autouse=True)
def reset_identity_pool() -> Generator[None]:
    """Drop the identity pool between tests.

    `api.identity` holds its pool at module scope, and the `test_client` fixture wires a
    per-test mock into it. Without this, a query-layer test that runs after an endpoint
    test would resolve native ids through a stale mock from a finished test instead of
    through no pool at all.
    """
    yield
    import api.identity as _identity_module

    _identity_module.configure(None)


@pytest.fixture(autouse=True)
def reset_activity_recorder() -> Generator[None]:
    """Unwire the activity recorder between tests.

    `api.activity` holds its pool, its subject cache, and its partition cache at module
    scope. Leaving a finished test's mock pool wired would let a later test resolve a
    subject through it, and leaving the caches populated would hide the get-or-create and
    the partition-ensure the next test is asserting.
    """
    yield
    import api.activity as _activity_module
    import api.syncer as _syncer_module

    _activity_module.configure(None, None)
    _syncer_module.configure(None)


@pytest.fixture(autouse=True)
def scrubbed_otel_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run every test with no OpenTelemetry environment.

    A CI runner may export OTEL_SDK_DISABLED, OTEL_EXPORTER_OTLP_ENDPOINT, or
    OTEL_METRICS_EXPORTER for its own agents. Any of those silently changes what the SDK
    records, so the metric assertions must not inherit them.
    """
    for name in [key for key in os.environ if key.startswith("OTEL_")]:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def reset_telemetry_instruments() -> Generator[None]:
    """Drop cached instruments so each test binds to whatever provider it installs."""
    from api.telemetry import reset_instruments

    reset_instruments()
    yield
    reset_instruments()


@pytest.fixture(autouse=True)
def reset_rate_limits() -> Generator[None]:
    """Reset slowapi rate limiter storage between tests."""
    yield
    try:
        from api.limiter import limiter

        limiter._storage.reset()
    except Exception:  # noqa: S110
        pass


@pytest.fixture
def auth_headers(valid_token: str) -> dict[str, str]:
    """Authorization headers with a valid bearer token."""
    return {"Authorization": f"Bearer {valid_token}"}


@pytest.fixture
def service_token_headers() -> dict[str, str]:
    """X-Service-Token header using the test service token."""
    return {"X-Service-Token": "test-service-token"}


def make_sample_user_row(
    user_id: str = TEST_USER_ID,
    email: str = TEST_USER_EMAIL,
    is_active: bool = True,
    hashed_password: str | None = None,
) -> dict[str, Any]:
    """Create a sample DB user row dict."""
    from datetime import UTC, datetime

    if hashed_password is None:
        # salt:key format
        import os

        salt = os.urandom(32)
        key = hashlib.pbkdf2_hmac("sha256", b"testpassword", salt, 100_000)
        hashed_password = salt.hex() + ":" + key.hex()

    return {
        "id": user_id,
        "email": email,
        "is_active": is_active,
        "hashed_password": hashed_password,
        "created_at": datetime.now(UTC),
    }
