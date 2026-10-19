"""Password reset/change under a Valkey that rejects writes (gm-catalog-api-yf7b).

Production Valkey runs ``maxmemory-policy noeviction``: once full, memory-adding
commands (SET/SETEX/SET NX/INCR) fail with ``OOM command not allowed`` while
reads, GETDEL, DEL and EXPIRE keep working. These tests drive the real routes
against a fakeredis server wrapped so its write commands raise that exact
``OutOfMemoryError``, and assert the END STATE:

* reset-confirm / change-password: a rejected ``password_changed`` write rolls
  the Postgres change back (password unchanged, sessions untouched), returns a
  clear 503, and leaves the reset token usable for a retry — never "password
  changed but old sessions still valid".
* reset-request: the background failure is logged at error level under a stable
  alert key with the failing stage.
"""

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import fakeredis
import fakeredis as fake_store
import pytest
from fastapi.testclient import TestClient
from structlog.testing import capture_logs
from valkey.exceptions import OutOfMemoryError

import api.routers.auth as auth_router
from api.auth import REASON_CREDENTIALS_CHANGED, token_revocation_reason
from tests.conftest import TEST_USER_EMAIL, TEST_USER_ID, make_sample_user_row


_OOM_MESSAGE = "command not allowed when used memory > 'maxmemory'."
_RESET_TOKEN = "reset-token-abc"
_RESET_KEY = f"reset:{_RESET_TOKEN}"
_MARKER_KEY = f"password_changed:{TEST_USER_ID}"
_ALERT_RESET_REQUEST = "password_reset_request_failed"


class _NoEvictionValkey:
    """FakeAsyncValkey proxy emulating a full ``noeviction`` Valkey.

    While ``full`` is set, memory-adding commands raise the server's OOM
    response error; reads, GETDEL and DEL pass through, exactly as verified
    against valkey/valkey:9-alpine in the deployment write-site audit.
    """

    _WRITE_COMMANDS = frozenset({"set", "setex", "incr", "decr"})

    def __init__(self, server: fakeredis.FakeServer, events: list[str]) -> None:
        self._inner = fake_store.FakeAsyncValkey(server=server, decode_responses=True)
        self._events = events
        self.full = False
        # Optional hook: flip `full` after the Nth successful write.
        self.fill_after_writes: int | None = None
        self._writes = 0

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._inner, name)
        if name not in self._WRITE_COMMANDS:
            return attr

        async def _write(key: str, *args: Any, **kwargs: Any) -> Any:
            if self.full:
                self._events.append(f"{name}-rejected:{key}")
                raise OutOfMemoryError(_OOM_MESSAGE)
            result = await attr(key, *args, **kwargs)
            self._events.append(f"{name}:{key}")
            self._writes += 1
            if self.fill_after_writes is not None and self._writes >= self.fill_after_writes:
                self.full = True
            return result

        return _write


@pytest.fixture
def events() -> list[str]:
    return []


@pytest.fixture
def store(fake_valkey_server: fakeredis.FakeServer) -> fakeredis.FakeValkey:
    """Synchronous view of the same fake server, for seeding and end-state checks."""
    return fakeredis.FakeValkey(server=fake_valkey_server, decode_responses=True)


@pytest.fixture
def oom_valkey(
    test_client: TestClient,  # noqa: ARG001  # configures auth_router first
    fake_valkey_server: fakeredis.FakeServer,
    events: list[str],
    mock_cur: MagicMock,
    mock_transaction: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> _NoEvictionValkey:
    """Wire the OOM-capable Valkey into the auth router and record PG events in order."""
    valkey = _NoEvictionValkey(fake_valkey_server, events)
    monkeypatch.setattr(auth_router, "_valkey", valkey)

    async def _execute(query: str, *_args: Any, **_kwargs: Any) -> None:
        if query.startswith("UPDATE users"):
            events.append("pg:update_password")
        elif query.startswith("UPDATE app_tokens"):
            events.append("pg:revoke_app_tokens")

    async def _tx_exit(exc_type: type[BaseException] | None, *_args: Any) -> bool:
        events.append("pg:commit" if exc_type is None else "pg:rollback")
        return False

    mock_cur.execute.side_effect = _execute
    mock_transaction.__aexit__.side_effect = _tx_exit
    return valkey


def _committed(events: list[str]) -> bool:
    return "pg:commit" in events


def _old_session_reason(server: fakeredis.FakeServer) -> str | None:
    """Revocation verdict for a session issued long before the change."""
    payload = {"sub": TEST_USER_ID, "iat": 1_000_000_000}

    async def _check() -> str | None:
        return await token_revocation_reason(payload, fake_store.FakeAsyncValkey(server=server, decode_responses=True))

    return asyncio.run(_check())


def _seed_reset_token(store: fakeredis.FakeValkey) -> None:
    store.setex(_RESET_KEY, 900, json.dumps({"user_id": TEST_USER_ID, "email": TEST_USER_EMAIL}))


def _reset_confirm(client: TestClient) -> Any:
    return client.post("/api/auth/reset-confirm", json={"token": _RESET_TOKEN, "new_password": "newpassword123"})


def _change_password(client: TestClient, headers: dict[str, str]) -> Any:
    return client.post(
        "/api/auth/change-password",
        json={"current_password": "testpassword", "new_password": "newpassword123"},
        headers=headers,
    )


def _assert_consistent_503(response: Any) -> None:
    assert response.status_code == 503
    assert "nothing was changed" in response.json()["detail"]
    assert response.headers["Retry-After"] == "30"


class TestResetConfirmUnderOOM:
    def test_rejected_marker_rolls_back_and_keeps_token_usable(
        self,
        test_client: TestClient,
        oom_valkey: _NoEvictionValkey,
        store: fakeredis.FakeValkey,
        events: list[str],
        fake_valkey_server: fakeredis.FakeServer,
    ) -> None:
        _seed_reset_token(store)
        oom_valkey.full = True

        response = _reset_confirm(test_client)

        _assert_consistent_503(response)
        # Password change never committed; token still there; no marker.
        assert events == ["pg:update_password", "pg:revoke_app_tokens", f"setex-rejected:{_MARKER_KEY}", "pg:rollback"]
        assert not _committed(events)
        assert store.get(_RESET_KEY) is not None
        assert store.get(_MARKER_KEY) is None
        # Password unchanged, so old sessions legitimately stay valid.
        assert _old_session_reason(fake_valkey_server) is None

        # Once Valkey accepts writes again the SAME token completes the reset.
        oom_valkey.full = False
        events.clear()
        retry = _reset_confirm(test_client)

        assert retry.status_code == 200
        assert store.get(_RESET_KEY) is None
        assert _committed(events)
        assert _old_session_reason(fake_valkey_server) == REASON_CREDENTIALS_CHANGED

    def test_marker_is_written_before_commit(
        self,
        test_client: TestClient,
        oom_valkey: _NoEvictionValkey,  # noqa: ARG002
        store: fakeredis.FakeValkey,
        events: list[str],
    ) -> None:
        _seed_reset_token(store)

        response = _reset_confirm(test_client)

        assert response.status_code == 200
        assert events == [
            "pg:update_password",
            "pg:revoke_app_tokens",
            f"setex:{_MARKER_KEY}",
            "pg:commit",
            f"setex:{_MARKER_KEY}",  # post-commit re-stamp
        ]
        assert store.get(_RESET_KEY) is None

    def test_post_commit_restamp_rejection_keeps_sessions_revoked(
        self,
        test_client: TestClient,
        oom_valkey: _NoEvictionValkey,
        store: fakeredis.FakeValkey,
        events: list[str],
        fake_valkey_server: fakeredis.FakeServer,
    ) -> None:
        """Valkey filling up between the pre-commit marker and the re-stamp must
        not fail a committed change: the first marker already revokes sessions."""
        _seed_reset_token(store)
        oom_valkey.fill_after_writes = 1

        with capture_logs() as captured:
            response = _reset_confirm(test_client)

        assert response.status_code == 200
        assert _committed(events)
        assert events[-1] == f"setex-rejected:{_MARKER_KEY}"
        assert store.get(_MARKER_KEY) is not None
        assert _old_session_reason(fake_valkey_server) == REASON_CREDENTIALS_CHANGED
        assert any(e["log_level"] == "warning" and e.get("operation") == "reset_confirm" for e in captured)

    def test_token_consumed_concurrently_rolls_back(
        self,
        test_client: TestClient,
        oom_valkey: _NoEvictionValkey,
        store: fakeredis.FakeValkey,
        events: list[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A concurrent reset that consumed the token first wins; this one rolls back."""
        _seed_reset_token(store)
        monkeypatch.setattr(oom_valkey, "getdel", AsyncMock(return_value=None), raising=False)

        response = _reset_confirm(test_client)

        assert response.status_code == 400
        assert events[-1] == "pg:rollback"
        assert not _committed(events)

    def test_rejected_marker_is_logged_at_error_level(
        self,
        test_client: TestClient,
        oom_valkey: _NoEvictionValkey,
        store: fakeredis.FakeValkey,
    ) -> None:
        _seed_reset_token(store)
        oom_valkey.full = True

        with capture_logs() as captured:
            _reset_confirm(test_client)

        errors = [e for e in captured if e.get("alert") == "password_changed_marker_write_failed"]
        assert len(errors) == 1
        assert errors[0]["log_level"] == "error"
        assert errors[0]["operation"] == "reset_confirm"
        assert errors[0]["user_id"] == TEST_USER_ID
        assert errors[0]["error_type"] == "OutOfMemoryError"


class TestChangePasswordUnderOOM:
    def test_rejected_marker_rolls_back(
        self,
        test_client: TestClient,
        oom_valkey: _NoEvictionValkey,
        store: fakeredis.FakeValkey,
        events: list[str],
        mock_cur: MagicMock,
        auth_headers: dict[str, str],
        fake_valkey_server: fakeredis.FakeServer,
    ) -> None:
        mock_cur.fetchone.return_value = make_sample_user_row()
        oom_valkey.full = True

        response = _change_password(test_client, auth_headers)

        _assert_consistent_503(response)
        assert events == ["pg:update_password", "pg:revoke_app_tokens", f"setex-rejected:{_MARKER_KEY}", "pg:rollback"]
        assert store.get(_MARKER_KEY) is None
        assert _old_session_reason(fake_valkey_server) is None

        oom_valkey.full = False
        events.clear()
        retry = _change_password(test_client, auth_headers)

        assert retry.status_code == 200
        assert events[:4] == ["pg:update_password", "pg:revoke_app_tokens", f"setex:{_MARKER_KEY}", "pg:commit"]
        assert _old_session_reason(fake_valkey_server) == REASON_CREDENTIALS_CHANGED


class TestResetRequestUnderOOM:
    @staticmethod
    def _post(client: TestClient, channel: Any) -> tuple[Any, list[dict[str, Any]]]:
        original = auth_router._notification_channel
        auth_router._notification_channel = channel
        try:
            with capture_logs() as captured:
                response = client.post("/api/auth/reset-request", json={"email": TEST_USER_EMAIL})
        finally:
            auth_router._notification_channel = original
        return response, captured

    @staticmethod
    def _alerts(captured: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [e for e in captured if e.get("alert") == _ALERT_RESET_REQUEST]

    def test_rejected_token_write_is_logged_at_error_level(
        self,
        test_client: TestClient,
        oom_valkey: _NoEvictionValkey,
        store: fakeredis.FakeValkey,
        mock_cur: MagicMock,
    ) -> None:
        mock_cur.fetchone.return_value = make_sample_user_row()
        oom_valkey.full = True
        channel = AsyncMock()

        response, captured = self._post(test_client, channel)

        # Response is unchanged (no account-existence oracle) ...
        assert response.status_code == 200
        # ... no token exists and no (dead) link was emailed ...
        assert store.keys("reset:*") == []
        channel.send_password_reset.assert_not_called()
        # ... and the failure is an alertable error, not a silent traceback.
        alerts = self._alerts(captured)
        assert len(alerts) == 1
        assert alerts[0]["log_level"] == "error"
        assert alerts[0]["stage"] == "store_reset_token"
        assert alerts[0]["user_id"] == TEST_USER_ID
        assert alerts[0]["error_type"] == "OutOfMemoryError"

    def test_email_send_failure_is_logged_at_error_level(
        self,
        test_client: TestClient,
        oom_valkey: _NoEvictionValkey,  # noqa: ARG002
        mock_cur: MagicMock,
    ) -> None:
        mock_cur.fetchone.return_value = make_sample_user_row()
        channel = AsyncMock()
        channel.send_password_reset.side_effect = RuntimeError("smtp down")

        response, captured = self._post(test_client, channel)

        assert response.status_code == 200
        alerts = self._alerts(captured)
        assert len(alerts) == 1
        assert alerts[0]["log_level"] == "error"
        assert alerts[0]["stage"] == "send_reset_email"

    def test_healthy_path_logs_no_alert(
        self,
        test_client: TestClient,
        oom_valkey: _NoEvictionValkey,  # noqa: ARG002
        store: fakeredis.FakeValkey,
        mock_cur: MagicMock,
    ) -> None:
        mock_cur.fetchone.return_value = make_sample_user_row()
        channel = AsyncMock()

        response, captured = self._post(test_client, channel)

        assert response.status_code == 200
        assert len(store.keys("reset:*")) == 1
        channel.send_password_reset.assert_called_once()
        assert self._alerts(captured) == []
