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
from fastapi import HTTPException
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
_ITEM_ID = "66666666-6666-6666-6666-666666666666"
_OBSERVATION_ID = UUID("44444444-4444-4444-4444-444444444444")
_PATH = f"/api/user/copies/{_COPY_ID}/observations"
_COPIES_PATH = f"/api/user/items/{_ITEM_ID}/copies"


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


class TestCopySelectionReaders:
    """Bounded item-copy listing and explicit-copy timeline composition."""

    def test_lists_authenticated_owners_copies_for_native_item(self, test_client: TestClient, mock_cur: MagicMock) -> None:
        from tests.conftest import TEST_USER_ID

        second_copy_id = "88888888-8888-8888-8888-888888888888"
        mock_cur.fetchall.return_value = [
            {"id": UUID(_COPY_ID), "item_id": UUID(_ITEM_ID), "collection_row_id": None, "acquired_at": None},
            {"id": UUID(second_copy_id), "item_id": UUID(_ITEM_ID), "collection_row_id": None, "acquired_at": None},
        ]

        response = test_client.get(_COPIES_PATH, headers=_AUTH_HEADER)

        assert response.status_code == 200
        assert response.json() == {
            "item_id": _ITEM_ID,
            "copies": [
                {"id": _COPY_ID, "item_id": _ITEM_ID, "collection_row_id": None, "acquired_at": None, "created_at": None, "updated_at": None},
                {"id": second_copy_id, "item_id": _ITEM_ID, "collection_row_id": None, "acquired_at": None, "created_at": None, "updated_at": None},
            ],
            "truncated": False,
        }
        query, params = mock_cur.execute.call_args.args
        assert "WHERE item_id = %s::uuid AND user_id = %s::uuid" in query
        assert "ORDER BY acquired_at ASC NULLS LAST, id ASC" in query
        assert params == (_ITEM_ID, TEST_USER_ID, 51)

    def test_owner_context_changes_with_first_party_jwt(self, test_client: TestClient, mock_cur: MagicMock) -> None:
        from tests.conftest import make_test_jwt

        mock_cur.fetchall.return_value = []
        response = test_client.get(_COPIES_PATH, headers={"Authorization": f"Bearer {make_test_jwt(user_id='00000000-0000-0000-0000-000000000002')}"})

        assert response.status_code == 200
        assert response.json()["copies"] == []
        assert mock_cur.execute.call_args.args[1][1] == "00000000-0000-0000-0000-000000000002"

    def test_requires_first_party_authentication_and_rejects_app_token(self, test_client: TestClient) -> None:
        assert test_client.get(_COPIES_PATH).status_code == 401
        assert test_client.get(_COPIES_PATH, headers={"Authorization": "Bearer dscg_not-a-jwt"}).status_code == 401

    def test_invalid_native_item_id_is_not_a_copy_lookup(self, test_client: TestClient, mock_cur: MagicMock) -> None:
        response = test_client.get("/api/user/items/not-a-uuid/copies", headers=_AUTH_HEADER)

        assert response.status_code == 404
        mock_cur.execute.assert_not_awaited()

    def test_empty_and_foreign_items_have_the_same_empty_shape(self, test_client: TestClient, mock_cur: MagicMock) -> None:
        mock_cur.fetchall.return_value = []

        response = test_client.get(_COPIES_PATH, headers=_AUTH_HEADER)

        assert response.status_code == 200
        assert response.json() == {"item_id": _ITEM_ID, "copies": [], "truncated": False}
        assert mock_cur.execute.call_args.args[1][1] == "00000000-0000-0000-0000-000000000001"

    @pytest.mark.parametrize("limit", [0, 101])
    def test_route_rejects_limits_outside_one_to_one_hundred(self, test_client: TestClient, limit: int) -> None:
        assert test_client.get(f"{_COPIES_PATH}?limit={limit}", headers=_AUTH_HEADER).status_code == 422

    def test_route_fetches_at_most_limit_plus_one_and_marks_truncated(self, test_client: TestClient, mock_cur: MagicMock) -> None:
        mock_cur.fetchall.return_value = [{"id": UUID(f"00000000-0000-0000-0000-{index:012d}"), "item_id": UUID(_ITEM_ID)} for index in range(1, 4)]

        response = test_client.get(f"{_COPIES_PATH}?limit=2", headers=_AUTH_HEADER)

        assert response.status_code == 200
        assert len(response.json()["copies"]) == 2
        assert response.json()["truncated"] is True
        assert mock_cur.execute.call_args.args[1][2] == 3

    @pytest.mark.asyncio
    async def test_lists_only_owner_item_rows_and_marks_truncation(self, mock_pool: MagicMock, mock_cur: MagicMock) -> None:
        item_id = "66666666-6666-6666-6666-666666666666"
        mock_cur.fetchall.return_value = [
            {"id": UUID(_COPY_ID), "item_id": UUID(item_id), "collection_row_id": UUID("77777777-7777-7777-7777-777777777777"), "acquired_at": None},
            {"id": UUID(_OTHER_COPY_ID), "item_id": UUID(item_id), "collection_row_id": None, "acquired_at": None},
        ]

        result = await observations_module.list_owned_copies(item_id, "00000000-0000-0000-0000-000000000001", limit=1, pool=mock_pool)

        assert [copy["id"] for copy in result["copies"]] == [_COPY_ID]
        assert result["truncated"] is True
        query, params = mock_cur.execute.call_args.args
        assert "WHERE item_id = %s::uuid AND user_id = %s::uuid" in query
        assert params == (item_id, "00000000-0000-0000-0000-000000000001", 2)

    @pytest.mark.asyncio
    async def test_duplicate_copies_have_independent_observation_timelines(self, mock_pool: MagicMock, mock_cur: MagicMock) -> None:
        mock_cur.fetchone.return_value = {"id": UUID(_COPY_ID)}
        second_id = "33333333-3333-3333-3333-333333333333"
        mock_cur.fetchall.side_effect = [[_row()], [_row(owned_copy_id=UUID(second_id), kind="grading", value="VG+")]]

        first = await observations_module._read_copy_timeline(_COPY_ID, "owner-id", mock_pool)
        second = await observations_module._read_copy_timeline(second_id, "owner-id", mock_pool)

        assert [row["owned_copy_id"] for row in first] == [_COPY_ID]
        assert [row["owned_copy_id"] for row in second] == [second_id]
        observation_queries = [call.args[0] for call in mock_cur.execute.await_args_list if "FROM observations AS observation" in call.args[0]]
        assert len(observation_queries) == 2
        assert all("copy.user_id = %s::uuid" in query for query in observation_queries)

    @pytest.mark.asyncio
    async def test_unknown_foreign_and_malformed_selected_copy_share_not_found_semantics(self, mock_pool: MagicMock, mock_cur: MagicMock) -> None:
        from tests.conftest import TEST_USER_ID

        mock_cur.fetchone.return_value = None
        for copy_id in (_OTHER_COPY_ID, "not-a-uuid"):
            with pytest.raises(HTTPException) as error:
                await observations_module._read_copy_timeline(copy_id, TEST_USER_ID, mock_pool)
            assert error.value.status_code == 404
            assert error.value.detail == "Copy not found"
        # The malformed UUID never reaches PostgreSQL; the foreign UUID runs only the owner predicate.
        assert mock_cur.execute.await_count == 1
        assert "user_id = %s::uuid" in mock_cur.execute.await_args.args[0]

    @pytest.mark.asyncio
    async def test_copy_list_rejects_invalid_bounds_and_malformed_item_ids(self, mock_pool: MagicMock, mock_cur: MagicMock) -> None:
        with pytest.raises(ValueError, match="limit"):
            await observations_module.list_owned_copies("66666666-6666-6666-6666-666666666666", "owner", limit=101, pool=mock_pool)
        with pytest.raises(HTTPException) as error:
            await observations_module.list_owned_copies("not-a-uuid", "owner", pool=mock_pool)
        assert error.value.status_code == 404
        mock_cur.execute.assert_not_awaited()
