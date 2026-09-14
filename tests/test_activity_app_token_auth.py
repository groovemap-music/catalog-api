"""The activity, consent, and observation routes under both JWT and app-token auth.

Wave 3 of ADR 0010's programme: an agent acting for a collector has no session, so these
five routes accept a first-party JWT or a `dscg_…` app token carrying the matching scope.
The point each test defends is that the two paths are the *same* route — the recorder and
every owner-scoped query see the token owner's id exactly as they see a session's user id,
so a fact reported by a delegate is the collector's fact.

Erasure and export are the deliberate exception and are tested for their rejection: they
are account-level rights no scope reaches.

Offline throughout — the pool is the shared conftest mock, and the token lookup is the
first `fetchone` on it, so each helper seeds the row the dependency reads before seeding
whatever the handler itself reads.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from api.app_tokens import generate_plaintext_token, hash_token
from tests.conftest import TEST_USER_ID


_APP_USER_ID = "99999999-9999-9999-9999-999999999999"
_APP_TOKEN_ID = "11111111-1111-1111-1111-111111111111"
_COPY_ID = "22222222-2222-2222-2222-222222222222"
_IMPRESSION_ID = "55555555-5555-5555-5555-555555555555"
_ITEM_ID = "66666666-6666-6666-6666-666666666666"
_OBSERVATIONS_PATH = f"/api/user/copies/{_COPY_ID}/observations"

_OUTCOME_BODY = {"event_type": "recommendation.opened", "impression_id": _IMPRESSION_ID, "item_id": _ITEM_ID}
_OBSERVATION_BODY = {"kind": "matrix", "value": "A1 MPO 12345", "source": "user"}


def _token_row(scopes: Sequence[str], token_hash: str) -> dict[str, Any]:
    """An active `app_tokens` row shaped the way `_lookup_active_token` returns one."""
    return {
        "id": UUID(_APP_TOKEN_ID),
        "user_id": UUID(_APP_USER_ID),
        "name": "GRUVAX kiosk",
        "scope": list(scopes),
        "token_hash": token_hash,
    }


def _seed_fetchone(mock_cur: MagicMock, rows: Sequence[Any]) -> None:
    """Return `rows` in order from successive `fetchone` calls, then `None` forever.

    A list `side_effect` would raise `StopIteration` the moment a handler reads one row
    more than a test anticipated; draining into `None` instead keeps a test failing on the
    assertion that actually matters rather than on the mock.
    """
    pending = list(rows)

    def _next(*_args: Any, **_kwargs: Any) -> Any:
        return pending.pop(0) if pending else None

    mock_cur.fetchone = AsyncMock(side_effect=_next)


def _app_token(mock_cur: MagicMock, scopes: Sequence[str], handler_rows: Sequence[Any] = ()) -> dict[str, str]:
    """Headers for a live app token with `scopes`, ahead of the rows the handler reads."""
    plaintext = generate_plaintext_token()
    _seed_fetchone(mock_cur, [_token_row(scopes, hash_token(plaintext)), *handler_rows])
    return {"Authorization": f"Bearer {plaintext}"}


def _revoked_token(mock_cur: MagicMock) -> dict[str, str]:
    """Headers for a token the lookup cannot find — revoked, unknown, or a dead account."""
    _seed_fetchone(mock_cur, [])
    return {"Authorization": f"Bearer {generate_plaintext_token()}"}


def _sql_params(mock_cur: MagicMock) -> list[Any]:
    """Every parameter tuple that reached the cursor, in order."""
    return [call.args[1] for call in mock_cur.execute.await_args_list if len(call.args) > 1]


# ──────────────────────────────────────────────────────────────────────────────
# POST /api/activity/events — activity:write
# ──────────────────────────────────────────────────────────────────────────────


class TestOutcomeEndpointAuth:
    def test_a_jwt_records_the_outcome_against_the_session_user(self, test_client: TestClient, auth_headers: dict[str, str]) -> None:
        with patch("api.activity.record_event", AsyncMock()) as record_event:
            response = test_client.post("/api/activity/events", json=_OUTCOME_BODY, headers=auth_headers)

        assert response.status_code == 202
        assert record_event.await_args.args[0] == TEST_USER_ID

    def test_a_scoped_app_token_records_the_outcome_against_the_token_owner(self, test_client: TestClient, mock_cur: MagicMock) -> None:
        """The delegated write is the owner's write: same recorder, same user id, same row."""
        headers = _app_token(mock_cur, ["activity:write"])

        with patch("api.activity.record_event", AsyncMock()) as record_event:
            response = test_client.post("/api/activity/events", json=_OUTCOME_BODY, headers=headers)

        assert response.status_code == 202
        assert response.json() == {"recorded": True, "event_type": "recommendation.opened", "impression_id": _IMPRESSION_ID}
        assert record_event.await_args.args[0] == _APP_USER_ID
        assert record_event.await_args.kwargs["idempotency_key"] == f"recommendation.opened:{_IMPRESSION_ID}"

    def test_a_token_without_the_scope_is_403_and_writes_nothing(self, test_client: TestClient, mock_cur: MagicMock) -> None:
        headers = _app_token(mock_cur, ["collection:read"])

        with patch("api.activity.record_event", AsyncMock()) as record_event:
            response = test_client.post("/api/activity/events", json=_OUTCOME_BODY, headers=headers)

        assert response.status_code == 403
        assert "activity:write" in response.json()["detail"]
        assert record_event.await_count == 0

    def test_a_revoked_token_is_401(self, test_client: TestClient, mock_cur: MagicMock) -> None:
        response = test_client.post("/api/activity/events", json=_OUTCOME_BODY, headers=_revoked_token(mock_cur))
        assert response.status_code == 401


# ──────────────────────────────────────────────────────────────────────────────
# GET /api/user/consent — consent:read
# ──────────────────────────────────────────────────────────────────────────────


class TestReadConsentAuth:
    def test_a_jwt_reads_the_session_users_grants(self, test_client: TestClient, auth_headers: dict[str, str], mock_cur: MagicMock) -> None:
        mock_cur.fetchall.return_value = []

        response = test_client.get("/api/user/consent", headers=auth_headers)

        assert response.status_code == 200
        assert (TEST_USER_ID,) in _sql_params(mock_cur)

    def test_a_scoped_app_token_reads_the_token_owners_grants(self, test_client: TestClient, mock_cur: MagicMock) -> None:
        headers = _app_token(mock_cur, ["consent:read"])
        mock_cur.fetchall.return_value = [{"purpose": "product_analytics", "granted_at": datetime(2026, 5, 1, tzinfo=UTC), "revoked_at": None}]

        response = test_client.get("/api/user/consent", headers=headers)

        assert response.status_code == 200
        analytics = next(entry for entry in response.json()["purposes"] if entry["purpose"] == "product_analytics")
        assert analytics["granted"] is True
        # The grant query is scoped to the token's owner, not to whoever presented it.
        assert (_APP_USER_ID,) in _sql_params(mock_cur)

    def test_a_token_without_the_scope_is_403(self, test_client: TestClient, mock_cur: MagicMock) -> None:
        response = test_client.get("/api/user/consent", headers=_app_token(mock_cur, ["consent:write"]))
        assert response.status_code == 403
        assert "consent:read" in response.json()["detail"]

    def test_a_revoked_token_is_401(self, test_client: TestClient, mock_cur: MagicMock) -> None:
        assert test_client.get("/api/user/consent", headers=_revoked_token(mock_cur)).status_code == 401


# ──────────────────────────────────────────────────────────────────────────────
# PUT /api/user/consent/{purpose} — consent:write
# ──────────────────────────────────────────────────────────────────────────────


class TestUpdateConsentAuth:
    def test_a_jwt_grants_for_the_session_user(self, test_client: TestClient, auth_headers: dict[str, str], mock_cur: MagicMock) -> None:
        mock_cur.fetchone.return_value = {"id": "1", "granted_at": datetime(2026, 5, 1, tzinfo=UTC), "revoked_at": None}

        with patch("api.activity.record_event", AsyncMock()) as record_event:
            response = test_client.put("/api/user/consent/product_analytics", json={"granted": True}, headers=auth_headers)

        assert response.status_code == 200
        assert record_event.await_args.args[0] == TEST_USER_ID

    def test_a_scoped_app_token_grants_for_the_token_owner(self, test_client: TestClient, mock_cur: MagicMock) -> None:
        headers = _app_token(mock_cur, ["consent:write"], handler_rows=[{"id": "1", "granted_at": None, "revoked_at": None}])

        with patch("api.activity.record_event", AsyncMock()) as record_event:
            response = test_client.put("/api/user/consent/product_analytics", json={"granted": True}, headers=headers)

        assert response.status_code == 200
        assert response.json() == {"purpose": "product_analytics", "granted": True, "changed": True}
        assert record_event.await_args.args[0] == _APP_USER_ID
        assert record_event.await_args.args[1] == "consent.granted"

    def test_a_scoped_app_token_revokes_for_the_token_owner(self, test_client: TestClient, mock_cur: MagicMock) -> None:
        headers = _app_token(mock_cur, ["consent:write"], handler_rows=[{"id": "1"}])

        with patch("api.activity.record_event", AsyncMock()) as record_event:
            response = test_client.put("/api/user/consent/model_training", json={"granted": False}, headers=headers)

        assert response.status_code == 200
        assert record_event.await_args.args[1] == "consent.revoked"
        assert record_event.await_args.args[0] == _APP_USER_ID

    def test_a_token_without_the_scope_is_403_and_writes_nothing(self, test_client: TestClient, mock_cur: MagicMock) -> None:
        headers = _app_token(mock_cur, ["consent:read"])

        with patch("api.activity.record_event", AsyncMock()) as record_event:
            response = test_client.put("/api/user/consent/product_analytics", json={"granted": True}, headers=headers)

        assert response.status_code == 403
        assert "consent:write" in response.json()["detail"]
        assert record_event.await_count == 0
        # Read access is not write access: the grant statement never ran.
        assert not any("consent_grants" in str(call.args[0]) for call in mock_cur.execute.await_args_list)

    def test_a_revoked_token_is_401(self, test_client: TestClient, mock_cur: MagicMock) -> None:
        response = test_client.put("/api/user/consent/product_analytics", json={"granted": True}, headers=_revoked_token(mock_cur))
        assert response.status_code == 401


# ──────────────────────────────────────────────────────────────────────────────
# POST /api/user/copies/{copy_id}/observations — observations:write
# ──────────────────────────────────────────────────────────────────────────────


def _observation_row() -> dict[str, Any]:
    return {
        "id": UUID("44444444-4444-4444-4444-444444444444"),
        "owned_copy_id": UUID(_COPY_ID),
        "kind": "matrix",
        "value": "A1 MPO 12345",
        "source": "user",
        "confidence": None,
        "observed_at": datetime(2026, 3, 1, 12, 0, tzinfo=UTC),
        "created_at": datetime(2026, 3, 1, 12, 0, 1, tzinfo=UTC),
    }


class TestCreateObservationAuth:
    def test_a_jwt_writes_against_the_session_user(self, test_client: TestClient, auth_headers: dict[str, str], mock_cur: MagicMock) -> None:
        _seed_fetchone(mock_cur, [_observation_row()])

        response = test_client.post(_OBSERVATIONS_PATH, json=_OBSERVATION_BODY, headers=auth_headers)

        assert response.status_code == 201
        assert any(TEST_USER_ID in params for params in _sql_params(mock_cur))

    def test_a_scoped_app_token_writes_against_the_token_owner(self, test_client: TestClient, mock_cur: MagicMock) -> None:
        headers = _app_token(mock_cur, ["observations:write"], handler_rows=[_observation_row()])

        response = test_client.post(_OBSERVATIONS_PATH, json=_OBSERVATION_BODY, headers=headers)

        assert response.status_code == 201
        assert response.json()["owned_copy_id"] == _COPY_ID
        # The ownership predicate carries the token owner's id, so a delegate can no more
        # write against someone else's copy than a session can.
        assert any(_APP_USER_ID in params for params in _sql_params(mock_cur))

    def test_a_token_without_the_scope_is_403(self, test_client: TestClient, mock_cur: MagicMock) -> None:
        headers = _app_token(mock_cur, ["observations:read"])

        response = test_client.post(_OBSERVATIONS_PATH, json=_OBSERVATION_BODY, headers=headers)

        assert response.status_code == 403
        assert "observations:write" in response.json()["detail"]
        assert not any("INSERT INTO observations" in str(call.args[0]) for call in mock_cur.execute.await_args_list)

    def test_a_revoked_token_is_401(self, test_client: TestClient, mock_cur: MagicMock) -> None:
        response = test_client.post(_OBSERVATIONS_PATH, json=_OBSERVATION_BODY, headers=_revoked_token(mock_cur))
        assert response.status_code == 401

    def test_a_copy_the_token_owner_does_not_own_is_404(self, test_client: TestClient, mock_cur: MagicMock) -> None:
        """Owner scoping is not relaxed for a delegate: no row written, same 404 as a session."""
        headers = _app_token(mock_cur, ["observations:write"], handler_rows=[None])

        response = test_client.post(_OBSERVATIONS_PATH, json=_OBSERVATION_BODY, headers=headers)

        assert response.status_code == 404


# ──────────────────────────────────────────────────────────────────────────────
# GET /api/user/copies/{copy_id}/observations — observations:read
# ──────────────────────────────────────────────────────────────────────────────


class TestListObservationsAuth:
    def test_a_jwt_lists_for_the_session_user(self, test_client: TestClient, auth_headers: dict[str, str], mock_cur: MagicMock) -> None:
        _seed_fetchone(mock_cur, [{"id": UUID(_COPY_ID)}])
        mock_cur.fetchall.return_value = [_observation_row()]

        response = test_client.get(_OBSERVATIONS_PATH, headers=auth_headers)

        assert response.status_code == 200
        assert len(response.json()["observations"]) == 1
        assert any(TEST_USER_ID in params for params in _sql_params(mock_cur))

    def test_a_scoped_app_token_lists_for_the_token_owner(self, test_client: TestClient, mock_cur: MagicMock) -> None:
        headers = _app_token(mock_cur, ["observations:read"], handler_rows=[{"id": UUID(_COPY_ID)}])
        mock_cur.fetchall.return_value = [_observation_row()]

        response = test_client.get(_OBSERVATIONS_PATH, headers=headers)

        assert response.status_code == 200
        assert response.json()["copy_id"] == _COPY_ID
        assert any(_APP_USER_ID in params for params in _sql_params(mock_cur))

    def test_a_token_without_the_scope_is_403(self, test_client: TestClient, mock_cur: MagicMock) -> None:
        response = test_client.get(_OBSERVATIONS_PATH, headers=_app_token(mock_cur, ["observations:write"]))
        assert response.status_code == 403
        assert "observations:read" in response.json()["detail"]

    def test_a_revoked_token_is_401(self, test_client: TestClient, mock_cur: MagicMock) -> None:
        assert test_client.get(_OBSERVATIONS_PATH, headers=_revoked_token(mock_cur)).status_code == 401


# ──────────────────────────────────────────────────────────────────────────────
# Erasure and export stay first-party
# ──────────────────────────────────────────────────────────────────────────────


class TestAccountRightsRejectAppTokens:
    """No scope reaches erasure or export, and a live token buys nothing there."""

    @pytest.mark.parametrize(
        "scopes",
        [
            ["activity:write", "consent:read", "consent:write", "observations:read", "observations:write"],
            ["collection:read"],
        ],
    )
    def test_erasure_rejects_an_app_token_whatever_it_carries(self, test_client: TestClient, mock_cur: MagicMock, scopes: list[str]) -> None:
        headers = _app_token(mock_cur, scopes)

        with patch("api.activity.record_event", AsyncMock()) as record_event:
            response = test_client.post("/api/user/erasure", json={"password": "hunter2"}, headers=headers)

        assert response.status_code == 401
        assert record_event.await_count == 0

    def test_export_rejects_an_app_token_whatever_it_carries(self, test_client: TestClient, mock_cur: MagicMock) -> None:
        headers = _app_token(mock_cur, ["activity:write", "consent:read", "observations:read"])

        with patch("api.activity.record_event", AsyncMock()) as record_event:
            response = test_client.get("/api/user/export", headers=headers)

        assert response.status_code == 401
        assert record_event.await_count == 0


# ──────────────────────────────────────────────────────────────────────────────
# The scope registry
# ──────────────────────────────────────────────────────────────────────────────


_NEW_SCOPES = ["activity:write", "consent:read", "consent:write", "observations:read", "observations:write"]


class TestScopeRegistry:
    def test_the_registry_carries_every_wave_three_scope(self) -> None:
        from api.routers.app_tokens import ALLOWED_SCOPES

        assert set(_NEW_SCOPES) <= ALLOWED_SCOPES
        assert "collection:read" in ALLOWED_SCOPES, "the existing scope must not be displaced"

    def test_the_registry_stays_closed(self) -> None:
        """No erasure or export scope exists — the rejection above is not merely a routing choice."""
        from api.routers.app_tokens import ALLOWED_SCOPES

        assert ALLOWED_SCOPES == {"collection:read", *_NEW_SCOPES}

    @pytest.mark.parametrize("scope", _NEW_SCOPES)
    def test_minting_accepts_each_new_scope(
        self, test_client: TestClient, auth_headers: dict[str, str], mock_pool: Any, monkeypatch: pytest.MonkeyPatch, scope: str
    ) -> None:
        import api.app_tokens as app_tokens_module

        app_tokens_module._pool = mock_pool
        token_id = UUID("11111111-1111-1111-1111-111111111111")

        async def _mint(user_id: str, name: str, scopes: list[str]) -> tuple[UUID, str]:  # noqa: ARG001
            return token_id, "dscg_PLAINTEXT_SECRET"

        async def _list(user_id: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:  # noqa: ARG001
            return ([{"id": token_id, "name": "agent", "scope": [scope], "created_at": datetime(2026, 5, 26, tzinfo=UTC)}], [])

        monkeypatch.setattr("api.routers.app_tokens._mint_token", _mint)
        monkeypatch.setattr("api.routers.app_tokens._list_user_tokens", _list)

        response = test_client.post("/api/user/app-tokens", json={"name": "agent", "scopes": [scope]}, headers=auth_headers)

        assert response.status_code == 201
        assert response.json()["scopes"] == [scope]

    def test_minting_accepts_the_whole_new_set_at_once(
        self, test_client: TestClient, auth_headers: dict[str, str], mock_pool: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import api.app_tokens as app_tokens_module

        app_tokens_module._pool = mock_pool
        token_id = UUID("11111111-1111-1111-1111-111111111111")

        async def _mint(user_id: str, name: str, scopes: list[str]) -> tuple[UUID, str]:  # noqa: ARG001
            return token_id, "dscg_PLAINTEXT_SECRET"

        async def _list(user_id: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:  # noqa: ARG001
            return ([{"id": token_id, "name": "agent", "scope": _NEW_SCOPES, "created_at": datetime(2026, 5, 26, tzinfo=UTC)}], [])

        monkeypatch.setattr("api.routers.app_tokens._mint_token", _mint)
        monkeypatch.setattr("api.routers.app_tokens._list_user_tokens", _list)

        response = test_client.post("/api/user/app-tokens", json={"name": "agent", "scopes": _NEW_SCOPES}, headers=auth_headers)

        assert response.status_code == 201
        assert response.json()["scopes"] == _NEW_SCOPES

    @pytest.mark.parametrize("scope", ["erasure:write", "export:read", "account:delete", "observations:admin"])
    def test_minting_still_rejects_a_scope_outside_the_registry(self, test_client: TestClient, auth_headers: dict[str, str], scope: str) -> None:
        response = test_client.post("/api/user/app-tokens", json={"name": "agent", "scopes": [scope]}, headers=auth_headers)

        assert response.status_code == 400
        assert scope in response.json()["detail"]
