"""Write-rejection paths against a REAL full noeviction Valkey and REAL PostgreSQL.

Opt-in only (gm-catalog-api-u5i2): run with ``just integration-valkey``. The
default checks deselect the ``integration`` marker, and even a bare ``pytest``
skips this module unless ``CATALOG_VALKEY_INTEGRATION=1`` is set and Docker is
available — so nothing here connects to a service by default.

The unit suites (``test_valkey_write_rejection_503.py``,
``test_password_change_valkey_oom.py``) drive the same routes against
``NoEvictionValkey``, a fakeredis proxy. This module checks that fake against
the real server and the routes end to end:

* the exception a real full Valkey raises for SET / SETEX / SET NX / INCR / DECR
  is the class ``api.valkey_errors`` classifies and has the exact class, args
  and message the fake raises;
* GET / GETDEL / DEL / EXPIRE / SCAN keep working while full;
* every ``valkey_write_guard`` site answers 503 without half-written state;
* a rejected ``password_changed`` marker really rolls the Postgres transaction
  back (hash, ``password_changed_at`` and app-token revocation all unchanged)
  and the reset token stays usable for a retry;
* snapshot save leaves the quota counter exact and the sync lock is released.

Valkey runs the production image digest from the deployment repository with
``--maxmemory 4mb --maxmemory-policy noeviction``; PostgreSQL runs the image
the schema's required integration tier targets, with the producer's DDL.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import fakeredis
import httpx
import pytest
import pytest_asyncio
import valkey as valkey_py
import valkey.asyncio as aiovalkey
from common import AsyncPostgreSQLPool
from fastapi import HTTPException
from groovemap_schema.postgres import create_postgres_schema
from psycopg.rows import dict_row
from structlog.testing import capture_logs
from valkey.exceptions import ConnectionError as ValkeyConnectionError
from valkey.exceptions import OutOfMemoryError, ReadOnlyError, ResponseError

import api.api as api_module
import api.dependencies as deps_module
import api.routers.admin as admin_router
import api.routers.auth as auth_router
import api.routers.snapshot as snapshot_router
import api.routers.sync as sync_router
from api.auth import (
    REASON_CREDENTIALS_CHANGED,
    _hash_password,
    _verify_password,
    encrypt_oauth_token,
    get_oauth_encryption_key,
    token_revocation_reason,
)
from api.config import ApiConfig
from api.notifications import LogNotificationChannel
from api.snapshot_store import SnapshotQuotaExceededError, SnapshotStore
from api.valkey_errors import ALERT_VALKEY_WRITE_REJECTED, VALKEY_UNAVAILABLE_ERRORS, valkey_write_guard
from tests.conftest import _TEST_MASTER_KEY, TEST_USER_EMAIL, TEST_USER_ID
from tests.noeviction_valkey import OOM_MESSAGE, NoEvictionValkey
from tests.test_admin_endpoints import TEST_ADMIN_EMAIL, TEST_ADMIN_ID, _make_admin_jwt
from tests.test_sync import _close_coro_and_return
from tests.valkey_noeviction_services import (
    OPT_IN_ENV,
    POSTGRES_DB,
    POSTGRES_PASSWORD,
    POSTGRES_USER,
    VALKEY_MAXMEMORY_BYTES,
    Container,
    DockerUnavailableError,
    FillAfterWrites,
    ValkeyFiller,
    capture_rejection,
    leftover_containers,
    require_docker,
    start_postgres,
    start_valkey,
)


pytestmark = [pytest.mark.integration, pytest.mark.valkey_noeviction]

_PASSWORD = "testpassword"
_NEW_PASSWORD = "newpassword123"
_RESET_TOKEN = "real-reset-token-abc"
_RESET_KEY = f"reset:{_RESET_TOKEN}"
_MARKER_KEY = f"password_changed:{TEST_USER_ID}"
_NODES: list[dict[str, Any]] = [{"id": "1", "type": "artist"}]
_CENTER: dict[str, Any] = {"id": "1", "type": "artist"}
_SNAPSHOT_BODY: dict[str, Any] = {"nodes": _NODES, "center": _CENTER}
_OLD_IAT = 1_000_000_000


# ---------------------------------------------------------------------------
# Containers (session) and per-test clients
# ---------------------------------------------------------------------------


@dataclass
class Services:
    valkey: Container
    postgres: Container


@pytest.fixture(scope="session")
def services(request: pytest.FixtureRequest) -> Iterator[Services]:
    if os.environ.get(OPT_IN_ENV) != "1":
        pytest.skip(f"opt-in: set {OPT_IN_ENV}=1 (use `just integration-valkey`)")
    try:
        require_docker()
    except DockerUnavailableError as exc:
        pytest.skip(str(exc))
    valkey_container = start_valkey()
    try:
        postgres_container = start_postgres()
    except BaseException:
        valkey_container.remove()
        raise
    started = Services(valkey=valkey_container, postgres=postgres_container)
    try:
        _report(request, started)
        yield started
    finally:
        valkey_container.remove()
        postgres_container.remove()
        leftovers = [name for name in leftover_containers() if name in {valkey_container.name, postgres_container.name}]
        assert leftovers == [], f"containers left behind: {leftovers}"


def _report(request: pytest.FixtureRequest, started: Services) -> None:
    """Print the evidence header (versions + digests) once per run."""
    reporter = request.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is None:
        return
    valkey_version = started.valkey.exec("valkey-server", "--version")
    pg_version = started.postgres.exec("postgres", "--version")
    reporter.write_line("")
    reporter.write_line(f"valkey image : {started.valkey.image}")
    reporter.write_line(f"valkey digest: {started.valkey.image_digest()}")
    reporter.write_line(f"valkey       : {valkey_version}")
    reporter.write_line(f"postgres img : {started.postgres.image}")
    reporter.write_line(f"postgres dig : {started.postgres.image_digest()}")
    reporter.write_line(f"postgres     : {pg_version}")
    reporter.write_line(f"valkey-py    : {valkey_py.__version__}")


@pytest.fixture
def config(services: Services) -> ApiConfig:
    return ApiConfig(
        postgres_host=f"{services.postgres.host}:{services.postgres.port}",
        postgres_username=POSTGRES_USER,
        postgres_password=POSTGRES_PASSWORD,
        postgres_database=POSTGRES_DB,
        jwt_secret_key="test-jwt-secret-for-unit-tests",  # noqa: S106 - the conftest test secret
        valkey_url=f"valkey://{services.valkey.host}:{services.valkey.port}/0",
        jwt_expire_minutes=30,
        encryption_master_key=_TEST_MASTER_KEY,
        # Neo4j is never contacted here: the sync router gets a stand-in driver.
        neo4j_host="bolt://127.0.0.1:1",
        neo4j_username="neo4j",
        neo4j_password="never-used",  # noqa: S106
    )


@pytest_asyncio.fixture
async def valkey(config: ApiConfig) -> AsyncIterator[Any]:
    """The client exactly as production builds it (instrumented ``from_url``)."""
    client = await api_module._create_valkey(config)
    try:
        yield client
    finally:
        await client.aclose()


@pytest_asyncio.fixture
async def filler(config: ApiConfig) -> AsyncIterator[ValkeyFiller]:
    client = aiovalkey.from_url(config.valkey_url, decode_responses=True)
    await client.config_set("maxmemory", str(VALKEY_MAXMEMORY_BYTES))
    await client.flushall()
    try:
        yield ValkeyFiller(client)
    finally:
        await client.config_set("maxmemory", str(VALKEY_MAXMEMORY_BYTES))
        await client.flushall()
        await client.aclose()


@pytest_asyncio.fixture
async def pool(config: ApiConfig) -> AsyncIterator[AsyncPostgreSQLPool]:
    host, port = config.postgres_host.split(":")
    pg = AsyncPostgreSQLPool(
        connection_params={
            "host": host,
            "port": int(port),
            "dbname": POSTGRES_DB,
            "user": POSTGRES_USER,
            "password": POSTGRES_PASSWORD,
        },
        min_connections=1,
        max_connections=4,
        max_retries=1,
        health_check_interval=3600,
    )
    await pg.initialize()
    assert await create_postgres_schema(pg) == 0
    async with pg.connection() as conn, conn.cursor() as cur:
        await cur.execute("TRUNCATE users, app_config CASCADE")
        await cur.execute(
            "INSERT INTO users (id, email, hashed_password, is_active, is_admin) VALUES (%s, %s, %s, TRUE, FALSE), (%s, %s, %s, TRUE, TRUE)",
            (TEST_USER_ID, TEST_USER_EMAIL, _hash_password(_PASSWORD), TEST_ADMIN_ID, TEST_ADMIN_EMAIL, _hash_password(_PASSWORD)),
        )
        await cur.execute(
            "INSERT INTO app_tokens (user_id, name, scope, token_hash) VALUES (%s, 'crate', ARRAY['collection:read'], %s)",
            (TEST_USER_ID, "0" * 64),
        )
    try:
        yield pg
    finally:
        await pg.close()


@pytest.fixture
def wire(config: ApiConfig, pool: AsyncPostgreSQLPool, valkey: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Point every router under test at the real pool and real Valkey.

    Returns a function that re-points them at another Valkey-like object (the
    ``FillAfterWrites`` proxy). ``monkeypatch`` restores the module state.
    """

    def _wire(client: Any) -> None:
        for module, name, value in [
            (api_module, "_pool", pool),
            (api_module, "_config", config),
            (api_module, "_valkey", client),
            (deps_module, "_jwt_secret", config.jwt_secret_key),
            (deps_module, "_valkey", client),
            (deps_module, "_pool", pool),
            (auth_router, "_pool", pool),
            (auth_router, "_valkey", client),
            (auth_router, "_config", config),
            (auth_router, "_get_current_user_fn", api_module._get_current_user),
            (auth_router, "_create_access_token_fn", api_module._create_access_token),
            (auth_router, "_notification_channel", LogNotificationChannel()),
            (admin_router, "_pool", pool),
            (admin_router, "_valkey", client),
            (admin_router, "_config", config),
            (sync_router, "_pool", pool),
            (sync_router, "_neo4j", MagicMock(name="neo4j-not-used")),
            (sync_router, "_config", config),
            (sync_router, "_valkey", client),
            (sync_router, "_running_syncs", {}),
            (snapshot_router, "_jwt_secret", config.jwt_secret_key),
            (snapshot_router, "_valkey", client),
            (snapshot_router, "_snapshot_store", SnapshotStore(client, ttl_days=1, max_per_user=5)),
        ]:
            monkeypatch.setattr(module, name, value)

    _wire(valkey)
    return _wire


@pytest_asyncio.fixture
async def http(wire: Any) -> AsyncIterator[httpx.AsyncClient]:  # noqa: ARG001
    transport = httpx.ASGITransport(app=api_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://catalog-api.test") as client:
        yield client


def _user_headers(issued_at: int | None = None) -> dict[str, str]:
    token, _ = api_module._create_access_token(TEST_USER_ID, TEST_USER_EMAIL, issued_at=issued_at)
    return {"Authorization": f"Bearer {token}"}


async def _keys(valkey: Any, pattern: str) -> list[str]:
    return [key async for key in valkey.scan_iter(match=pattern)]


async def _user_row(pool: AsyncPostgreSQLPool) -> dict[str, Any]:
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT u.hashed_password, u.password_changed_at, "
            "(SELECT count(*) FROM app_tokens t WHERE t.user_id = u.id AND t.revoked_at IS NULL) AS live_app_tokens "
            "FROM users u WHERE u.id = %s",
            (TEST_USER_ID,),
        )
        row = await cur.fetchone()
    assert row is not None
    return dict(row)


async def _old_session_reason(valkey: Any) -> str | None:
    return await token_revocation_reason({"sub": TEST_USER_ID, "iat": _OLD_IAT}, valkey)


def _assert_unavailable(response: httpx.Response, *, detail_key: str = "detail") -> str:
    assert response.status_code == 503, response.text
    assert int(response.headers["Retry-After"]) >= 30
    message = str(response.json()[detail_key])
    assert "try again" in message
    assert "maxmemory" not in message
    assert "OOM" not in message
    return message


def _alerts(captured: Sequence[Mapping[str, Any]], alert: str, operation: str | None = None) -> list[Mapping[str, Any]]:
    return [e for e in captured if e.get("alert") == alert and (operation is None or e.get("operation") == operation)]


# ---------------------------------------------------------------------------
# Real server vs fake: exception class, args, message; commands still accepted
# ---------------------------------------------------------------------------

_REJECTED = {
    "SET": lambda c: c.set("k", "v"),
    "SETEX": lambda c: c.setex("k", 30, "v"),
    "SET NX EX": lambda c: c.set("lock", "1", nx=True, ex=30),
    "INCR": lambda c: c.incr("counter"),
    "DECR": lambda c: c.decr("counter"),
}


class TestRealRejectionMatchesFake:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("command", list(_REJECTED))
    async def test_real_rejection_is_classified_and_identical_to_the_fake(self, command: str, valkey: Any, filler: ValkeyFiller) -> None:
        await filler.fill()
        assert isinstance(filler.first_rejection, OutOfMemoryError)

        real = await capture_rejection(lambda: _REJECTED[command](valkey))

        fake_client = NoEvictionValkey(fakeredis.FakeServer(server_type="valkey"))
        fake_client.full = True
        fake = await capture_rejection(lambda: _REJECTED[command](fake_client))

        assert type(real) is OutOfMemoryError
        assert type(real) is type(fake)
        assert real.args == fake.args == (OOM_MESSAGE,)
        assert str(real) == str(fake)
        assert isinstance(real, VALKEY_UNAVAILABLE_ERRORS)
        with pytest.raises(HTTPException) as info, valkey_write_guard(operation="real", detail="try again"):
            raise real
        assert info.value.status_code == 503

    @pytest.mark.asyncio
    async def test_reads_and_memory_freeing_commands_still_work_when_full(self, valkey: Any, filler: ValkeyFiller) -> None:
        await valkey.set("keep", "v")
        await valkey.set("gone", "v")
        await valkey.set("consume", "v")
        await filler.fill()

        assert await valkey.get("keep") == "v"
        assert await valkey.getdel("consume") == "v"
        assert await valkey.delete("gone") == 1
        assert await valkey.expire("keep", 600, nx=True) is True
        assert "keep" in await _keys(valkey, "kee*")
        # The fake passes exactly these through, too.
        assert {"get", "getdel", "delete", "expire", "scan"}.isdisjoint(NoEvictionValkey.WRITE_COMMANDS)
        # ... and still rejects every write after the reads.
        assert isinstance(await capture_rejection(lambda: valkey.set("k", "v")), OutOfMemoryError)

    @pytest.mark.asyncio
    async def test_other_real_errors_classify_as_the_mapping_expects(self, valkey: Any, filler: ValkeyFiller) -> None:  # noqa: ARG002
        """READONLY and connection-refused are unavailability (503); WRONGTYPE is a bug (500)."""
        await valkey.lpush("a-list", "x")
        wrongtype = await capture_rejection(lambda: valkey.get("a-list"))
        assert type(wrongtype) is ResponseError
        assert not isinstance(wrongtype, VALKEY_UNAVAILABLE_ERRORS)

        refused = aiovalkey.Valkey(host="127.0.0.1", port=1, socket_connect_timeout=2)
        try:
            assert isinstance(await capture_rejection(lambda: refused.set("k", "v")), ValkeyConnectionError)
        finally:
            await refused.aclose()

        # A replica of an unreachable primary refuses writes with READONLY.
        await valkey.execute_command("REPLICAOF", "127.0.0.1", "1")
        try:
            readonly = await capture_rejection(lambda: valkey.set("k", "v"))
            assert type(readonly) is ReadOnlyError
            assert isinstance(readonly, VALKEY_UNAVAILABLE_ERRORS)
        finally:
            await valkey.execute_command("REPLICAOF", "NO", "ONE")


# ---------------------------------------------------------------------------
# Password reset / change: the Postgres transaction really rolls back
# ---------------------------------------------------------------------------


async def _seed_reset_token(valkey: Any) -> None:
    await valkey.setex(_RESET_KEY, 900, json.dumps({"user_id": TEST_USER_ID, "email": TEST_USER_EMAIL}))


def _assert_nothing_changed(before: dict[str, Any], after: dict[str, Any]) -> None:
    assert after == before
    assert after["password_changed_at"] is None
    assert after["live_app_tokens"] == 1
    assert _verify_password(_PASSWORD, after["hashed_password"])


def _assert_changed(before: dict[str, Any], after: dict[str, Any]) -> None:
    assert after["hashed_password"] != before["hashed_password"]
    assert _verify_password(_NEW_PASSWORD, after["hashed_password"])
    assert after["password_changed_at"] is not None
    assert after["live_app_tokens"] == 0


class TestPasswordChangeRollsBackInPostgres:
    @pytest.mark.asyncio
    async def test_reset_confirm_rolls_back_and_the_same_token_works_after_drain(
        self, http: httpx.AsyncClient, valkey: Any, filler: ValkeyFiller, pool: AsyncPostgreSQLPool
    ) -> None:
        await _seed_reset_token(valkey)
        before = await _user_row(pool)
        await filler.fill()

        with capture_logs() as captured:
            response = await http.post("/api/auth/reset-confirm", json={"token": _RESET_TOKEN, "new_password": _NEW_PASSWORD})

        assert "nothing was changed" in _assert_unavailable(response)
        assert response.headers["Retry-After"] == "30"
        _assert_nothing_changed(before, await _user_row(pool))
        assert await valkey.get(_RESET_KEY) is not None
        assert await valkey.get(_MARKER_KEY) is None
        assert await _old_session_reason(valkey) is None
        marker_alerts = _alerts(captured, "password_changed_marker_write_failed", "reset_confirm")
        assert [a["error_type"] for a in marker_alerts] == ["OutOfMemoryError"]

        await filler.drain()
        retry = await http.post("/api/auth/reset-confirm", json={"token": _RESET_TOKEN, "new_password": _NEW_PASSWORD})

        assert retry.status_code == 200, retry.text
        _assert_changed(before, await _user_row(pool))
        assert await valkey.get(_RESET_KEY) is None
        assert await valkey.get(_MARKER_KEY) is not None
        assert await _old_session_reason(valkey) == REASON_CREDENTIALS_CHANGED

    @pytest.mark.asyncio
    async def test_change_password_rolls_back_and_succeeds_after_drain(
        self, http: httpx.AsyncClient, valkey: Any, filler: ValkeyFiller, pool: AsyncPostgreSQLPool
    ) -> None:
        before = await _user_row(pool)
        headers = _user_headers()
        body = {"current_password": _PASSWORD, "new_password": _NEW_PASSWORD}
        await filler.fill()

        response = await http.post("/api/auth/change-password", json=body, headers=headers)

        assert "nothing was changed" in _assert_unavailable(response)
        _assert_nothing_changed(before, await _user_row(pool))
        assert await valkey.get(_MARKER_KEY) is None
        assert await _old_session_reason(valkey) is None

        await filler.drain()
        retry = await http.post("/api/auth/change-password", json=body, headers=headers)

        assert retry.status_code == 200, retry.text
        _assert_changed(before, await _user_row(pool))
        assert await _old_session_reason(valkey) == REASON_CREDENTIALS_CHANGED

    @pytest.mark.asyncio
    async def test_post_commit_restamp_rejection_keeps_the_committed_change(
        self, http: httpx.AsyncClient, valkey: Any, filler: ValkeyFiller, pool: AsyncPostgreSQLPool, wire: Any
    ) -> None:
        await _seed_reset_token(valkey)
        before = await _user_row(pool)
        proxy = FillAfterWrites(valkey, filler, after=1)  # pre-commit marker accepted, then full
        wire(proxy)

        with capture_logs() as captured:
            response = await http.post("/api/auth/reset-confirm", json={"token": _RESET_TOKEN, "new_password": _NEW_PASSWORD})

        assert response.status_code == 200, response.text
        assert proxy.events == [f"setex:{_MARKER_KEY}", f"setex-rejected:{_MARKER_KEY}"]
        _assert_changed(before, await _user_row(pool))
        assert await valkey.get(_RESET_KEY) is None
        assert await _old_session_reason(valkey) == REASON_CREDENTIALS_CHANGED
        assert any(e["log_level"] == "warning" and e.get("operation") == "reset_confirm" for e in captured)

    @pytest.mark.asyncio
    async def test_reset_request_rejection_is_an_error_alert_and_stores_no_token(
        self, http: httpx.AsyncClient, valkey: Any, filler: ValkeyFiller
    ) -> None:
        await filler.fill()

        with capture_logs() as captured:
            response = await http.post("/api/auth/reset-request", json={"email": TEST_USER_EMAIL})

        assert response.status_code == 200
        assert await _keys(valkey, "reset:*") == []
        alerts = _alerts(captured, "password_reset_request_failed")
        assert len(alerts) == 1
        assert alerts[0]["log_level"] == "error"
        assert alerts[0]["stage"] == "store_reset_token"
        assert alerts[0]["error_type"] == "OutOfMemoryError"


# ---------------------------------------------------------------------------
# Other valkey_write_guard sites
# ---------------------------------------------------------------------------


class TestGuardedSitesAgainstRealValkey:
    @pytest.mark.asyncio
    async def test_login_2fa_challenge(self, http: httpx.AsyncClient, valkey: Any, filler: ValkeyFiller, pool: AsyncPostgreSQLPool) -> None:
        async with pool.connection() as conn, conn.cursor() as cur:
            await cur.execute("UPDATE users SET totp_enabled = TRUE WHERE id = %s", (TEST_USER_ID,))
        await filler.fill()

        with capture_logs() as captured:
            response = await http.post("/api/auth/login", json={"email": TEST_USER_EMAIL, "password": _PASSWORD})

        _assert_unavailable(response)
        assert "challenge_token" not in response.json()
        assert await _keys(valkey, "2fa_challenge:*") == []
        assert len(_alerts(captured, ALERT_VALKEY_WRITE_REJECTED, "login_2fa_challenge")) == 1

        await filler.drain()
        ok = await http.post("/api/auth/login", json={"email": TEST_USER_EMAIL, "password": _PASSWORD})
        assert ok.status_code == 200
        assert ok.json()["requires_2fa"] is True
        assert len(await _keys(valkey, "2fa_challenge:*")) == 1

    @pytest.mark.asyncio
    async def test_logout_keeps_the_session_active_and_says_so(self, http: httpx.AsyncClient, valkey: Any, filler: ValkeyFiller) -> None:
        headers = _user_headers()
        await filler.fill()

        response = await http.post("/api/auth/logout", headers=headers)

        assert "still active" in _assert_unavailable(response)
        assert await _keys(valkey, "revoked:jti:*") == []
        assert (await http.get("/api/auth/me", headers=headers)).status_code == 200

        await filler.drain()
        assert (await http.post("/api/auth/logout", headers=headers)).status_code == 200
        assert (await http.get("/api/auth/me", headers=headers)).status_code == 401

    @pytest.mark.asyncio
    async def test_admin_logout_records_no_audit_entry(
        self, http: httpx.AsyncClient, valkey: Any, filler: ValkeyFiller, pool: AsyncPostgreSQLPool
    ) -> None:
        headers = {"Authorization": f"Bearer {_make_admin_jwt(jti='real-admin-jti')}"}
        await filler.fill()

        response = await http.post("/api/admin/auth/logout", headers=headers)

        assert "still active" in _assert_unavailable(response)
        assert await valkey.get("revoked:jti:real-admin-jti") is None
        async with pool.connection() as conn, conn.cursor() as cur:
            await cur.execute("SELECT count(*) FROM admin_audit_log WHERE action = 'admin.logout'")
            row = await cur.fetchone()
        assert row is not None
        assert row[0] == 0

    @pytest.mark.asyncio
    async def test_discogs_oauth_state(
        self, http: httpx.AsyncClient, valkey: Any, filler: ValkeyFiller, pool: AsyncPostgreSQLPool, config: ApiConfig
    ) -> None:
        key = get_oauth_encryption_key(config.encryption_master_key)
        assert key is not None
        async with pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO app_config (key, value) VALUES ('discogs_consumer_key', %s), ('discogs_consumer_secret', %s)",
                (encrypt_oauth_token("ckey", key), encrypt_oauth_token("csecret", key)),
            )
        await filler.fill()
        # Discogs itself is never contacted: only the Valkey state write is under test.
        discogs = AsyncMock(return_value={"oauth_token": "reqtok", "oauth_token_secret": "reqsec"})

        with patch("api.api.request_oauth_token", new=discogs):
            response = await http.get("/api/oauth/authorize/discogs", headers=_user_headers())

        _assert_unavailable(response)
        assert "authorize_url" not in response.json()
        assert await _keys(valkey, "*reqtok*") == []


# ---------------------------------------------------------------------------
# Snapshot quota counter
# ---------------------------------------------------------------------------


def _count_key(user_id: str = TEST_USER_ID) -> str:
    return f"{SnapshotStore._USER_COUNT_KEY_PREFIX}{user_id}"


async def _snapshot_keys(valkey: Any) -> list[str]:
    return [k for k in await _keys(valkey, f"{SnapshotStore._KEY_PREFIX}*") if not k.startswith(SnapshotStore._USER_COUNT_KEY_PREFIX)]


class TestSnapshotQuotaAgainstRealValkey:
    @pytest.mark.asyncio
    async def test_rejected_save_returns_503_and_leaves_the_quota_exact(self, http: httpx.AsyncClient, valkey: Any, filler: ValkeyFiller) -> None:
        await valkey.set(_count_key(), 3)
        await filler.fill()

        with capture_logs() as captured:
            response = await http.post("/api/snapshot", json=_SNAPSHOT_BODY, headers=_user_headers())

        _assert_unavailable(response, detail_key="error")
        assert await valkey.get(_count_key()) == "3"
        assert await _snapshot_keys(valkey) == []
        assert len(_alerts(captured, ALERT_VALKEY_WRITE_REJECTED, "snapshot_save")) == 1

        await filler.drain()
        ok = await http.post("/api/snapshot", json=_SNAPSHOT_BODY, headers=_user_headers())
        assert ok.status_code == 201, ok.text
        assert await valkey.get(_count_key()) == "4"
        assert len(await _snapshot_keys(valkey)) == 1

    @pytest.mark.asyncio
    async def test_rejected_incr_after_accepted_set_deletes_the_snapshot(self, valkey: Any, filler: ValkeyFiller) -> None:
        await valkey.set(_count_key("user-1"), 2)
        proxy = FillAfterWrites(valkey, filler, after=1)  # snapshot SET accepted, then full
        store = SnapshotStore(proxy, ttl_days=1, max_per_user=5)  # type: ignore[arg-type]

        with pytest.raises(OutOfMemoryError):
            await store.save(_NODES, _CENTER, user_id="user-1")

        assert [e.split(":")[0] for e in proxy.events] == ["set", "incr-rejected"]
        assert await valkey.get(_count_key("user-1")) == "2"
        assert await _snapshot_keys(valkey) == []  # removed with DEL while full

    @pytest.mark.asyncio
    async def test_repeated_rejections_reserve_nothing(self, valkey: Any, filler: ValkeyFiller) -> None:
        store = SnapshotStore(valkey, ttl_days=1, max_per_user=5)
        await filler.fill()

        for _ in range(10):
            with pytest.raises(OutOfMemoryError):
                await store.save(_NODES, _CENTER, user_id="user-1")
        assert await valkey.get(_count_key("user-1")) is None

        await filler.drain()
        for _ in range(5):
            await store.save(_NODES, _CENTER, user_id="user-1")
        assert await valkey.get(_count_key("user-1")) == "5"
        assert await valkey.ttl(_count_key("user-1")) > 0
        with pytest.raises(SnapshotQuotaExceededError):
            await store.save(_NODES, _CENTER, user_id="user-1")

    @pytest.mark.asyncio
    async def test_user_at_cap_gets_quota_error_not_503_when_full(self, valkey: Any, filler: ValkeyFiller) -> None:
        await valkey.set(_count_key("user-1"), 2)
        store = SnapshotStore(valkey, ttl_days=1, max_per_user=2)
        await filler.fill()

        with pytest.raises(SnapshotQuotaExceededError):
            await store.save(_NODES, _CENTER, user_id="user-1")
        assert await valkey.get(_count_key("user-1")) == "2"


# ---------------------------------------------------------------------------
# Sync lock / cooldown
# ---------------------------------------------------------------------------


def _started_task() -> MagicMock:
    task = MagicMock(spec=asyncio.Task)
    task.done.return_value = False
    return task


async def _sync_rows(pool: AsyncPostgreSQLPool) -> int:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("SELECT count(*) FROM sync_history WHERE user_id = %s", (TEST_USER_ID,))
        row = await cur.fetchone()
    assert row is not None
    return int(row[0])


class TestSyncLockAgainstRealValkey:
    _LOCK_KEY = f"sync:lock:{TEST_USER_ID}"
    _COOLDOWN_KEY = f"sync:cooldown:{TEST_USER_ID}"

    @pytest.mark.asyncio
    async def test_rejected_lock_returns_503_and_starts_nothing(
        self, http: httpx.AsyncClient, valkey: Any, filler: ValkeyFiller, pool: AsyncPostgreSQLPool
    ) -> None:
        await filler.fill()
        create_task = MagicMock()

        with patch("api.routers.sync.asyncio.create_task", create_task), capture_logs() as captured:
            response = await http.post("/api/sync", headers=_user_headers())

        _assert_unavailable(response)
        assert create_task.call_count == 0
        assert await _sync_rows(pool) == 0
        assert await valkey.get(self._LOCK_KEY) is None
        assert len(_alerts(captured, ALERT_VALKEY_WRITE_REJECTED, "sync_lock")) == 1

    @pytest.mark.asyncio
    async def test_rejected_cooldown_keeps_the_started_sync_and_releases_the_lock(
        self, http: httpx.AsyncClient, valkey: Any, filler: ValkeyFiller, pool: AsyncPostgreSQLPool, wire: Any
    ) -> None:
        proxy = FillAfterWrites(valkey, filler, after=1)  # lock SET NX accepted, then full
        wire(proxy)

        with (
            patch("api.routers.sync.asyncio.create_task", side_effect=_close_coro_and_return(_started_task())),
            capture_logs() as captured,
        ):
            response = await http.post("/api/sync", headers=_user_headers())

        assert response.status_code == 202, response.text
        assert response.json()["status"] == "started"
        assert proxy.events == [f"set:{self._LOCK_KEY}", f"setex-rejected:{self._COOLDOWN_KEY}"]
        assert await _sync_rows(pool) == 1
        assert await valkey.get(self._LOCK_KEY) is None  # DEL accepted while full
        assert await valkey.get(self._COOLDOWN_KEY) is None
        warnings = [e for e in captured if e.get("operation") == "sync_cooldown"]
        assert [w["log_level"] for w in warnings] == ["warning"]
