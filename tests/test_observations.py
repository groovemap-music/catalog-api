"""Tests for api/routers/observations.py — user evidence about a copy they hold (ADR 0009)."""

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

import api.routers.observations as observations_module
from tests.conftest import TEST_JWT_SECRET, make_test_jwt


def _jwt_with_empty_sub() -> str:
    """Craft a valid HS256 JWT whose `sub` claim is literally the empty string.

    `require_user` only blocks a missing `sub`, so an explicit empty one reaches the
    router and its own defensive 401 is the next line of defence.
    """

    def b64url(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")

    header = b64url(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    body = b64url(json.dumps({"sub": "", "email": "x@example.com", "exp": 9_999_999_999}, separators=(",", ":")).encode())
    signing_input = f"{header}.{body}".encode("ascii")
    sig = b64url(hmac.new(TEST_JWT_SECRET.encode("utf-8"), signing_input, hashlib.sha256).digest())
    return f"{header}.{body}.{sig}"


_AUTH_HEADER = {"Authorization": f"Bearer {make_test_jwt()}"}
_AUTH_HEADER_EMPTY_SUB = {"Authorization": f"Bearer {_jwt_with_empty_sub()}"}
_COPY_ID = "22222222-2222-2222-2222-222222222222"
_OTHER_COPY_ID = "33333333-3333-3333-3333-333333333333"
_OBSERVATION_ID = UUID("44444444-4444-4444-4444-444444444444")
_PATH = f"/api/user/copies/{_COPY_ID}/observations"


def _row(**overrides: Any) -> dict[str, Any]:
    row = {
        "id": _OBSERVATION_ID,
        "owned_copy_id": UUID(_COPY_ID),
        "kind": "matrix",
        "value": "A1 MPO 12345",
        "source": "user",
        "confidence": 0.9,
        "observed_at": datetime(2026, 3, 1, 12, 0, tzinfo=UTC),
        "created_at": datetime(2026, 3, 1, 12, 0, 1, tzinfo=UTC),
    }
    row.update(overrides)
    return row


class TestCreateObservation:
    """POST /api/user/copies/{copy_id}/observations."""

    def test_records_an_observation_on_an_owned_copy(self, test_client: TestClient, mock_cur: MagicMock) -> None:
        mock_cur.fetchone.return_value = _row()

        response = test_client.post(
            _PATH,
            headers=_AUTH_HEADER,
            json={"kind": "matrix", "value": "A1 MPO 12345", "source": "user", "confidence": 0.9},
        )

        assert response.status_code == 201
        body = response.json()
        assert body["id"] == str(_OBSERVATION_ID)
        assert body["owned_copy_id"] == _COPY_ID
        assert body["kind"] == "matrix"
        assert body["value"] == "A1 MPO 12345"
        assert body["source"] == "user"
        assert body["confidence"] == 0.9
        assert body["observed_at"].startswith("2026-03-01T12:00:00")

    def test_observed_at_defaults_to_now(self, test_client: TestClient, mock_cur: MagicMock) -> None:
        """An omitted observed_at is bound as NULL and defaulted by the statement."""
        mock_cur.fetchone.return_value = _row(confidence=None)

        response = test_client.post(_PATH, headers=_AUTH_HEADER, json={"kind": "grading", "value": "VG+", "source": "user"})

        assert response.status_code == 201
        assert response.json()["confidence"] is None
        params = mock_cur.execute.call_args.args[1]
        assert params[3] is None  # confidence
        assert params[4] is None  # observed_at, defaulted by COALESCE(..., NOW())

    def test_accepts_an_explicit_observed_at(self, test_client: TestClient, mock_cur: MagicMock) -> None:
        mock_cur.fetchone.return_value = _row()

        response = test_client.post(
            _PATH,
            headers=_AUTH_HEADER,
            json={"kind": "matrix", "value": "A1", "source": "catalog", "observed_at": "2026-01-02T03:04:05Z"},
        )

        assert response.status_code == 201
        assert mock_cur.execute.call_args.args[1][4] == datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)

    def test_foreign_copy_is_404(self, test_client: TestClient, mock_cur: MagicMock) -> None:
        """The ownership predicate lives in the statement, so a miss writes nothing."""
        mock_cur.fetchone.return_value = None

        response = test_client.post(
            f"/api/user/copies/{_OTHER_COPY_ID}/observations",
            headers=_AUTH_HEADER,
            json={"kind": "matrix", "value": "A1", "source": "user"},
        )

        assert response.status_code == 404
        assert response.json()["detail"] == "Copy not found"

    def test_query_is_scoped_to_the_caller(self, test_client: TestClient, mock_cur: MagicMock) -> None:
        from tests.conftest import TEST_USER_ID

        mock_cur.fetchone.return_value = _row()
        test_client.post(_PATH, headers=_AUTH_HEADER, json={"kind": "matrix", "value": "A1", "source": "user"})

        params = mock_cur.execute.call_args.args[1]
        assert params[5] == _COPY_ID
        assert params[6] == TEST_USER_ID

    @pytest.mark.parametrize("source", ["discogs", "guess", "", "CATALOGUE"])
    def test_source_outside_the_vocabulary_is_422(self, test_client: TestClient, source: str) -> None:
        response = test_client.post(_PATH, headers=_AUTH_HEADER, json={"kind": "matrix", "value": "A1", "source": source})
        assert response.status_code == 422

    def test_source_is_normalized_before_validation(self, test_client: TestClient, mock_cur: MagicMock) -> None:
        mock_cur.fetchone.return_value = _row(source="catalog")

        response = test_client.post(_PATH, headers=_AUTH_HEADER, json={"kind": "matrix", "value": "A1", "source": " Catalog "})

        assert response.status_code == 201
        assert mock_cur.execute.call_args.args[1][2] == "catalog"

    @pytest.mark.parametrize(
        "body",
        [
            {"value": "A1", "source": "user"},
            {"kind": "matrix", "source": "user"},
            {"kind": "   ", "value": "A1", "source": "user"},
            {"kind": "matrix", "value": "   ", "source": "user"},
            {"kind": "matrix", "value": "A1", "source": "user", "confidence": 1.5},
        ],
    )
    def test_malformed_body_is_422(self, test_client: TestClient, body: dict[str, Any]) -> None:
        assert test_client.post(_PATH, headers=_AUTH_HEADER, json=body).status_code == 422

    def test_malformed_copy_id_is_404(self, test_client: TestClient) -> None:
        """A non-UUID path segment must not reach the ::uuid cast as a 500."""
        response = test_client.post(
            "/api/user/copies/not-a-uuid/observations",
            headers=_AUTH_HEADER,
            json={"kind": "matrix", "value": "A1", "source": "user"},
        )
        assert response.status_code == 404

    def test_requires_authentication(self, test_client: TestClient) -> None:
        assert test_client.post(_PATH, json={"kind": "matrix", "value": "A1", "source": "user"}).status_code == 401

    def test_empty_subject_claim_is_401(self, test_client: TestClient) -> None:
        response = test_client.post(_PATH, headers=_AUTH_HEADER_EMPTY_SUB, json={"kind": "matrix", "value": "A1", "source": "user"})
        assert response.status_code == 401

    def test_503_when_pool_not_ready(self, test_client: TestClient) -> None:
        original = observations_module._pool
        observations_module._pool = None
        try:
            response = test_client.post(_PATH, headers=_AUTH_HEADER, json={"kind": "matrix", "value": "A1", "source": "user"})
        finally:
            observations_module._pool = original
        assert response.status_code == 503


class TestListObservations:
    """GET /api/user/copies/{copy_id}/observations."""

    def test_lists_observations_for_an_owned_copy(self, test_client: TestClient, mock_cur: MagicMock) -> None:
        mock_cur.fetchone.return_value = {"id": UUID(_COPY_ID)}
        mock_cur.fetchall.return_value = [_row(), _row(id=UUID("55555555-5555-5555-5555-555555555555"), kind="grading", value="NM")]

        response = test_client.get(_PATH, headers=_AUTH_HEADER)

        assert response.status_code == 200
        body = response.json()
        assert body["copy_id"] == _COPY_ID
        assert [observation["kind"] for observation in body["observations"]] == ["matrix", "grading"]

    def test_owned_copy_with_no_observations_is_an_empty_list(self, test_client: TestClient, mock_cur: MagicMock) -> None:
        mock_cur.fetchone.return_value = {"id": UUID(_COPY_ID)}
        mock_cur.fetchall.return_value = []

        response = test_client.get(_PATH, headers=_AUTH_HEADER)

        assert response.status_code == 200
        assert response.json()["observations"] == []

    def test_foreign_copy_is_404(self, test_client: TestClient, mock_cur: MagicMock) -> None:
        """Ownership is established first, so an empty listing is never mistaken for access."""
        mock_cur.fetchone.return_value = None

        response = test_client.get(f"/api/user/copies/{_OTHER_COPY_ID}/observations", headers=_AUTH_HEADER)

        assert response.status_code == 404
        mock_cur.fetchall.assert_not_called()

    def test_malformed_copy_id_is_404(self, test_client: TestClient) -> None:
        assert test_client.get("/api/user/copies/not-a-uuid/observations", headers=_AUTH_HEADER).status_code == 404

    def test_requires_authentication(self, test_client: TestClient) -> None:
        assert test_client.get(_PATH).status_code == 401

    def test_empty_subject_claim_is_401(self, test_client: TestClient) -> None:
        assert test_client.get(_PATH, headers=_AUTH_HEADER_EMPTY_SUB).status_code == 401

    def test_503_when_pool_not_ready(self, test_client: TestClient) -> None:
        original = observations_module._pool
        observations_module._pool = None
        try:
            response = test_client.get(_PATH, headers=_AUTH_HEADER)
        finally:
            observations_module._pool = original
        assert response.status_code == 503


class TestIsoformat:
    """Timestamps are stringified without losing a null."""

    def test_preserves_none(self) -> None:
        assert observations_module._isoformat(None) is None

    def test_stringifies_a_value_without_isoformat(self) -> None:
        assert observations_module._isoformat("2026-03-01") == "2026-03-01"
