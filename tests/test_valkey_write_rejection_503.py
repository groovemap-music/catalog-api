"""User-facing paths under a Valkey that rejects writes (gm-catalog-api-cmb6).

Each test drives the real route against ``NoEvictionValkey`` — a fakeredis
server whose memory-adding commands raise the exact ``OutOfMemoryError`` a full
``noeviction`` Valkey returns — and asserts the END STATE: a clear, non-leaking
503 with ``Retry-After`` (never a raw 500), an alertable error log, and no
half-written state (no orphan keys, quota counter unchanged, lock released).
"""

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import fakeredis
import pytest
from fastapi.testclient import TestClient
from structlog.testing import capture_logs
from valkey.exceptions import OutOfMemoryError, ResponseError

import api.api as api_module
import api.routers.admin as admin_router
import api.routers.auth as auth_router
import api.routers.snapshot as snapshot_router
import api.routers.sync as sync_router
from api.snapshot_store import SnapshotQuotaExceededError, SnapshotStore
from api.valkey_errors import ALERT_VALKEY_WRITE_REJECTED, valkey_write_guard
from tests.conftest import TEST_USER_EMAIL, TEST_USER_ID, make_sample_user_row
from tests.noeviction_valkey import OOM_MESSAGE, NoEvictionValkey
from tests.test_admin_endpoints import _admin_auth_headers
from tests.test_sync import _close_coro_and_return


_SNAPSHOT_BODY = {"nodes": [{"id": "1", "type": "artist"}], "center": {"id": "1", "type": "artist"}}


@pytest.fixture
def store(fake_valkey_server: fakeredis.FakeServer) -> fakeredis.FakeValkey:
    """Synchronous view of the fake server, for seeding and end-state checks."""
    return fakeredis.FakeValkey(server=fake_valkey_server, decode_responses=True)


@pytest.fixture
def oom_valkey(
    test_client: TestClient,  # noqa: ARG001  # configures every router first
    fake_valkey_server: fakeredis.FakeServer,
    monkeypatch: pytest.MonkeyPatch,
) -> NoEvictionValkey:
    """Wire one OOM-capable Valkey into every router under test."""
    valkey = NoEvictionValkey(fake_valkey_server)
    monkeypatch.setattr(auth_router, "_valkey", valkey)
    monkeypatch.setattr(admin_router, "_valkey", valkey)
    monkeypatch.setattr(api_module, "_valkey", valkey)
    monkeypatch.setattr(sync_router, "_valkey", valkey)
    monkeypatch.setattr(snapshot_router, "_snapshot_store", SnapshotStore(valkey, ttl_days=1, max_per_user=5))
    return valkey


def _assert_unavailable(response: Any, *, detail_key: str = "detail") -> str:
    assert response.status_code == 503
    # 30s from the shared mapping; slowapi may raise it to the rate-limit
    # window reset on rate-limited routes that return a Response directly.
    assert int(response.headers["Retry-After"]) >= 30
    message = response.json()[detail_key]
    assert "try again" in message
    # Non-leaking: the Valkey error text never reaches the client.
    assert "maxmemory" not in message
    assert "OOM" not in message
    return str(message)


def _alerts(captured: list[dict[str, Any]], operation: str) -> list[dict[str, Any]]:
    return [e for e in captured if e.get("alert") == ALERT_VALKEY_WRITE_REJECTED and e.get("operation") == operation]


def _assert_one_error_alert(captured: list[dict[str, Any]], operation: str) -> None:
    alerts = _alerts(captured, operation)
    assert len(alerts) == 1
    assert alerts[0]["log_level"] == "error"
    assert alerts[0]["error_type"] == "OutOfMemoryError"


# ---------------------------------------------------------------------------
# Shared mapping
# ---------------------------------------------------------------------------


class TestValkeyWriteGuard:
    def test_maps_oom_to_503_with_retry_after(self) -> None:
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as info, valkey_write_guard(operation="unit", detail="Nope; try again shortly"):
            raise OutOfMemoryError(OOM_MESSAGE)
        assert info.value.status_code == 503
        assert info.value.detail == "Nope; try again shortly"
        assert info.value.headers == {"Retry-After": "30"}

    def test_other_valkey_errors_are_not_masked_as_unavailability(self) -> None:
        """A WRONGTYPE-style error is a bug, not a full Valkey: it must stay a 500."""
        with pytest.raises(ResponseError), valkey_write_guard(operation="unit", detail="x"):
            raise ResponseError("WRONGTYPE Operation against a key holding the wrong kind of value")


# ---------------------------------------------------------------------------
# Login 2FA challenge
# ---------------------------------------------------------------------------


class TestLogin2faChallenge:
    def test_rejected_challenge_write_returns_503_without_a_challenge(
        self, test_client: TestClient, oom_valkey: NoEvictionValkey, mock_cur: MagicMock, store: fakeredis.FakeValkey
    ) -> None:
        user_row = make_sample_user_row()
        user_row["totp_enabled"] = True
        mock_cur.fetchone = AsyncMock(return_value=user_row)
        oom_valkey.full = True

        with capture_logs() as captured:
            response = test_client.post("/api/auth/login", json={"email": TEST_USER_EMAIL, "password": "testpassword"})

        _assert_unavailable(response)
        assert "challenge_token" not in response.json()
        assert store.keys("2fa_challenge:*") == []
        _assert_one_error_alert(captured, "login_2fa_challenge")

    def test_healthy_valkey_still_issues_the_challenge(
        self, test_client: TestClient, oom_valkey: NoEvictionValkey, mock_cur: MagicMock, store: fakeredis.FakeValkey
    ) -> None:
        user_row = make_sample_user_row()
        user_row["totp_enabled"] = True
        mock_cur.fetchone = AsyncMock(return_value=user_row)

        response = test_client.post("/api/auth/login", json={"email": TEST_USER_EMAIL, "password": "testpassword"})

        assert response.status_code == 200
        assert response.json()["requires_2fa"] is True
        assert len(store.keys("2fa_challenge:*")) == 1
        assert oom_valkey.events[0].startswith("setex:2fa_challenge:")


# ---------------------------------------------------------------------------
# Logout / admin logout revocation
# ---------------------------------------------------------------------------


class TestLogoutRevocation:
    def test_rejected_revocation_returns_503_and_says_the_session_is_still_active(
        self, test_client: TestClient, oom_valkey: NoEvictionValkey, store: fakeredis.FakeValkey
    ) -> None:
        token, _ = api_module._create_access_token(TEST_USER_ID, TEST_USER_EMAIL)
        oom_valkey.full = True

        with capture_logs() as captured:
            response = test_client.post("/api/auth/logout", headers={"Authorization": f"Bearer {token}"})

        message = _assert_unavailable(response)
        assert "still active" in message
        assert "logged_out" not in response.json()
        assert store.keys("revoked:jti:*") == []
        _assert_one_error_alert(captured, "logout")

    def test_healthy_valkey_revokes_the_token(self, test_client: TestClient, oom_valkey: NoEvictionValkey, store: fakeredis.FakeValkey) -> None:  # noqa: ARG002
        token, _ = api_module._create_access_token(TEST_USER_ID, TEST_USER_EMAIL)

        response = test_client.post("/api/auth/logout", headers={"Authorization": f"Bearer {token}"})

        assert response.status_code == 200
        assert len(store.keys("revoked:jti:*")) == 1


class TestAdminLogoutRevocation:
    @patch("api.routers.admin.record_audit_entry", new_callable=AsyncMock)
    def test_rejected_revocation_returns_503_and_records_no_logout(
        self, mock_audit: AsyncMock, test_client: TestClient, oom_valkey: NoEvictionValkey, store: fakeredis.FakeValkey
    ) -> None:
        oom_valkey.full = True

        with capture_logs() as captured:
            response = test_client.post("/api/admin/auth/logout", headers=_admin_auth_headers())

        message = _assert_unavailable(response)
        assert "still active" in message
        assert store.keys("revoked:jti:*") == []
        # The token is still valid, so the audit log must not claim a logout.
        assert mock_audit.await_count == 0
        _assert_one_error_alert(captured, "admin_logout")


# ---------------------------------------------------------------------------
# Discogs OAuth state
# ---------------------------------------------------------------------------


class TestDiscogsOAuthState:
    def test_rejected_state_write_returns_503_without_an_authorize_url(
        self,
        test_client: TestClient,
        oom_valkey: NoEvictionValkey,
        mock_cur: MagicMock,
        auth_headers: dict[str, str],
        store: fakeredis.FakeValkey,
    ) -> None:
        mock_cur.fetchall.return_value = [
            {"key": "discogs_consumer_key", "value": "ckey"},
            {"key": "discogs_consumer_secret", "value": "csecret"},
        ]
        oom_valkey.full = True

        mock_request = AsyncMock(return_value={"oauth_token": "reqtok", "oauth_token_secret": "reqsec"})
        with patch("api.api.request_oauth_token", new=mock_request), capture_logs() as captured:
            response = test_client.get("/api/oauth/authorize/discogs", headers=auth_headers)

        _assert_unavailable(response)
        assert "authorize_url" not in response.json()
        assert store.keys("*reqtok*") == []
        _assert_one_error_alert(captured, "discogs_oauth_state")


# ---------------------------------------------------------------------------
# Snapshot save
# ---------------------------------------------------------------------------


def _count_key(user_id: str = TEST_USER_ID) -> str:
    return f"{SnapshotStore._USER_COUNT_KEY_PREFIX}{user_id}"


def _snapshot_keys(store: fakeredis.FakeValkey) -> list[str]:
    return [k for k in store.keys(f"{SnapshotStore._KEY_PREFIX}*") if not k.startswith(SnapshotStore._USER_COUNT_KEY_PREFIX)]


class TestSnapshotSave:
    def test_rejected_save_returns_503_and_leaves_the_quota_untouched(
        self,
        test_client: TestClient,
        oom_valkey: NoEvictionValkey,
        auth_headers: dict[str, str],
        store: fakeredis.FakeValkey,
    ) -> None:
        store.set(_count_key(), 3)
        oom_valkey.full = True

        with capture_logs() as captured:
            response = test_client.post("/api/snapshot", json=_SNAPSHOT_BODY, headers=auth_headers)

        _assert_unavailable(response, detail_key="error")
        assert store.get(_count_key()) == "3"
        assert _snapshot_keys(store) == []
        _assert_one_error_alert(captured, "snapshot_save")

    @pytest.mark.asyncio
    async def test_rejected_save_reserves_nothing_and_needs_no_compensating_write(self, fake_valkey_server: fakeredis.FakeServer) -> None:
        """The old INCR-then-SET order needed a DECR to undo the INCR, and that DECR is
        itself rejected at OOM. The snapshot is now written first, so a rejected SET
        has reserved no slot at all."""
        valkey = NoEvictionValkey(fake_valkey_server)
        store = SnapshotStore(valkey, ttl_days=1, max_per_user=5)
        valkey.full = True

        for _ in range(10):
            with pytest.raises(OutOfMemoryError):
                await store.save(_SNAPSHOT_BODY["nodes"], _SNAPSHOT_BODY["center"], user_id="user-1")

        sync_view = fakeredis.FakeValkey(server=fake_valkey_server, decode_responses=True)
        assert sync_view.get(_count_key("user-1")) is None
        assert all(not e.startswith(("incr", "decr")) for e in valkey.events)

        # Once Valkey has room again the full quota is available.
        valkey.full = False
        for _ in range(5):
            await store.save(_SNAPSHOT_BODY["nodes"], _SNAPSHOT_BODY["center"], user_id="user-1")
        assert sync_view.get(_count_key("user-1")) == "5"

    @pytest.mark.asyncio
    async def test_rejected_quota_incr_deletes_the_written_snapshot(self, fake_valkey_server: fakeredis.FakeServer) -> None:
        """SET accepted, then Valkey fills before the INCR: the snapshot is removed
        with DEL (never rejected for memory) so nothing unaccounted survives."""
        valkey = NoEvictionValkey(fake_valkey_server)
        store = SnapshotStore(valkey, ttl_days=1, max_per_user=5)
        sync_view = fakeredis.FakeValkey(server=fake_valkey_server, decode_responses=True)
        sync_view.set(_count_key("user-1"), 2)
        valkey.fill_after_writes = 1

        with pytest.raises(OutOfMemoryError):
            await store.save(_SNAPSHOT_BODY["nodes"], _SNAPSHOT_BODY["center"], user_id="user-1")

        assert [e.split(":")[0] for e in valkey.events] == ["set", "incr-rejected"]
        assert sync_view.get(_count_key("user-1")) == "2"
        assert _snapshot_keys(sync_view) == []

    @pytest.mark.asyncio
    async def test_user_at_cap_is_rejected_without_any_write(self, fake_valkey_server: fakeredis.FakeServer) -> None:
        valkey = NoEvictionValkey(fake_valkey_server)
        store = SnapshotStore(valkey, ttl_days=1, max_per_user=2)
        sync_view = fakeredis.FakeValkey(server=fake_valkey_server, decode_responses=True)
        sync_view.set(_count_key("user-1"), 2)
        valkey.full = True  # would reject any write

        with pytest.raises(SnapshotQuotaExceededError):
            await store.save(_SNAPSHOT_BODY["nodes"], _SNAPSHOT_BODY["center"], user_id="user-1")

        assert valkey.events == []
        assert sync_view.get(_count_key("user-1")) == "2"

    @pytest.mark.asyncio
    async def test_lost_race_for_the_last_slot_removes_the_snapshot_and_the_over_count(self, fake_valkey_server: fakeredis.FakeServer) -> None:
        valkey = NoEvictionValkey(fake_valkey_server)
        store = SnapshotStore(valkey, ttl_days=1, max_per_user=2)
        sync_view = fakeredis.FakeValkey(server=fake_valkey_server, decode_responses=True)
        sync_view.set(_count_key("user-1"), 2)

        # The pre-check read a stale (pre-race) count.
        with patch.object(valkey._inner, "get", AsyncMock(return_value="1")), pytest.raises(SnapshotQuotaExceededError):
            await store.save(_SNAPSHOT_BODY["nodes"], _SNAPSHOT_BODY["center"], user_id="user-1")

        assert sync_view.get(_count_key("user-1")) == "2"
        assert _snapshot_keys(sync_view) == []


# ---------------------------------------------------------------------------
# Sync lock / cooldown
# ---------------------------------------------------------------------------


def _started_task() -> MagicMock:
    task = MagicMock(spec=asyncio.Task)
    task.done.return_value = False
    return task


class TestSyncLockAndCooldown:
    _LOCK_KEY = f"sync:lock:{TEST_USER_ID}"
    _COOLDOWN_KEY = f"sync:cooldown:{TEST_USER_ID}"

    def test_rejected_lock_write_returns_503_and_starts_nothing(
        self,
        test_client: TestClient,
        oom_valkey: NoEvictionValkey,
        mock_cur: MagicMock,
        auth_headers: dict[str, str],
    ) -> None:
        oom_valkey.full = True
        create_task = MagicMock()

        with patch("api.routers.sync.asyncio.create_task", create_task), capture_logs() as captured:
            response = test_client.post("/api/sync", headers=auth_headers)

        _assert_unavailable(response)
        assert create_task.call_count == 0
        assert mock_cur.execute.await_count == 0  # no sync_history row
        _assert_one_error_alert(captured, "sync_lock")

    def test_rejected_cooldown_write_does_not_turn_a_started_sync_into_an_error(
        self,
        test_client: TestClient,
        oom_valkey: NoEvictionValkey,
        mock_cur: MagicMock,
        auth_headers: dict[str, str],
        store: fakeredis.FakeValkey,
    ) -> None:
        mock_cur.fetchone.return_value = {"id": "new-sync-id"}
        oom_valkey.fill_after_writes = 1  # the lock SET is accepted, the cooldown SETEX is not

        with (
            patch("api.routers.sync.asyncio.create_task", side_effect=_close_coro_and_return(_started_task())),
            capture_logs() as captured,
        ):
            response = test_client.post("/api/sync", headers=auth_headers)

        assert response.status_code == 202
        assert response.json() == {"sync_id": "new-sync-id", "status": "started"}
        assert oom_valkey.events == [f"set:{self._LOCK_KEY}", f"setex-rejected:{self._COOLDOWN_KEY}"]
        # The lock is released even though the cooldown could not be written.
        assert store.get(self._LOCK_KEY) is None
        warnings = [e for e in captured if e.get("operation") == "sync_cooldown"]
        assert len(warnings) == 1
        assert warnings[0]["log_level"] == "warning"
        assert warnings[0]["alert"] == ALERT_VALKEY_WRITE_REJECTED

    def test_healthy_valkey_sets_cooldown_and_releases_lock(
        self,
        test_client: TestClient,
        oom_valkey: NoEvictionValkey,
        mock_cur: MagicMock,
        auth_headers: dict[str, str],
        store: fakeredis.FakeValkey,
    ) -> None:
        mock_cur.fetchone.return_value = {"id": "new-sync-id"}

        with patch("api.routers.sync.asyncio.create_task", side_effect=_close_coro_and_return(_started_task())):
            response = test_client.post("/api/sync", headers=auth_headers)

        assert response.status_code == 202
        assert oom_valkey.events == [f"set:{self._LOCK_KEY}", f"setex:{self._COOLDOWN_KEY}"]
        assert store.get(self._COOLDOWN_KEY) == "1"
        assert store.get(self._LOCK_KEY) is None

    def test_lock_is_released_when_starting_the_sync_fails(
        self,
        test_client: TestClient,
        oom_valkey: NoEvictionValkey,
        mock_cur: MagicMock,
        auth_headers: dict[str, str],
        store: fakeredis.FakeValkey,
    ) -> None:
        mock_cur.fetchone.return_value = None  # sync_history insert returned nothing -> 500

        response = test_client.post("/api/sync", headers=auth_headers)

        assert response.status_code == 500
        assert store.get(self._LOCK_KEY) is None
        assert store.get(self._COOLDOWN_KEY) is None
        assert oom_valkey.events == [f"set:{self._LOCK_KEY}"]

    def test_lock_is_released_when_a_sync_is_already_running(
        self,
        test_client: TestClient,
        oom_valkey: NoEvictionValkey,  # noqa: ARG002
        mock_cur: MagicMock,
        auth_headers: dict[str, str],
        store: fakeredis.FakeValkey,
    ) -> None:
        mock_cur.fetchone.return_value = None
        api_module._running_syncs[TEST_USER_ID] = _started_task()

        response = test_client.post("/api/sync", headers=auth_headers)

        assert response.status_code == 202
        assert response.json() == {"status": "already_running"}
        assert store.get(self._LOCK_KEY) is None
        assert store.get(self._COOLDOWN_KEY) is None
