"""Behavioral tests for the consent endpoints.

Consent is enforced twice under ADR 0010: the writer snapshots what was permitted at the
moment of a write, and a training-time reader re-checks the grant table. These tests cover
the half this service owns — the grant table and the endpoints that change it — including
that a repeated request is idempotent and, because it is not a second decision, emits no
second event.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from common.events import consent_purposes
from fastapi.testclient import TestClient


GRANTED_AT = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
REVOKED_AT = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


def grant_row(purpose: str, revoked: bool = False) -> dict[str, Any]:
    return {"purpose": purpose, "granted_at": GRANTED_AT, "revoked_at": REVOKED_AT if revoked else None}


def statements(cur: MagicMock) -> list[str]:
    return [str(call.args[0]) for call in cur.execute.await_args_list]


class TestReadConsent:
    """GET /api/user/consent — both purposes, always."""

    def test_both_purposes_are_reported_even_when_neither_has_a_row(
        self, test_client: TestClient, auth_headers: dict[str, str], mock_cur: MagicMock
    ) -> None:
        mock_cur.fetchall.return_value = []

        response = test_client.get("/api/user/consent", headers=auth_headers)

        assert response.status_code == 200
        purposes = response.json()["purposes"]
        assert [entry["purpose"] for entry in purposes] == list(consent_purposes())
        assert all(entry["granted"] is False for entry in purposes)
        assert all(entry["granted_at"] is None and entry["revoked_at"] is None for entry in purposes)

    def test_a_granted_purpose_reports_its_grant_time(self, test_client: TestClient, auth_headers: dict[str, str], mock_cur: MagicMock) -> None:
        mock_cur.fetchall.return_value = [grant_row("product_analytics")]

        response = test_client.get("/api/user/consent", headers=auth_headers)

        analytics = next(entry for entry in response.json()["purposes"] if entry["purpose"] == "product_analytics")
        assert analytics["granted"] is True
        assert analytics["granted_at"] == GRANTED_AT.isoformat()
        assert analytics["revoked_at"] is None

    def test_a_revoked_purpose_reports_both_times_and_is_not_granted(
        self, test_client: TestClient, auth_headers: dict[str, str], mock_cur: MagicMock
    ) -> None:
        mock_cur.fetchall.return_value = [grant_row("model_training", revoked=True)]

        response = test_client.get("/api/user/consent", headers=auth_headers)

        training = next(entry for entry in response.json()["purposes"] if entry["purpose"] == "model_training")
        assert training["granted"] is False
        assert training["revoked_at"] == REVOKED_AT.isoformat()

    def test_the_read_takes_the_newest_row_per_purpose(self, test_client: TestClient, auth_headers: dict[str, str], mock_cur: MagicMock) -> None:
        mock_cur.fetchall.return_value = []

        test_client.get("/api/user/consent", headers=auth_headers)

        select = next(sql for sql in statements(mock_cur) if "consent_grants" in sql)
        assert "DISTINCT ON (purpose)" in select
        assert "ORDER BY purpose, granted_at DESC" in select

    def test_a_timestamp_that_is_not_a_datetime_still_renders(
        self, test_client: TestClient, auth_headers: dict[str, str], mock_cur: MagicMock
    ) -> None:
        mock_cur.fetchall.return_value = [{"purpose": "product_analytics", "granted_at": "2026-05-01", "revoked_at": None}]

        response = test_client.get("/api/user/consent", headers=auth_headers)

        analytics = next(entry for entry in response.json()["purposes"] if entry["purpose"] == "product_analytics")
        assert analytics["granted_at"] == "2026-05-01"

    def test_the_endpoint_requires_a_user(self, test_client: TestClient) -> None:
        assert test_client.get("/api/user/consent").status_code == 401


class TestUpdateConsent:
    """PUT /api/user/consent/{purpose} — idempotent in both directions."""

    def test_a_grant_writes_a_row_and_emits_the_event(self, test_client: TestClient, auth_headers: dict[str, str], mock_cur: MagicMock) -> None:
        mock_cur.fetchone.return_value = {"id": "1", "granted_at": GRANTED_AT, "revoked_at": None}

        with patch("api.activity.record_event", AsyncMock()) as record_event:
            response = test_client.put("/api/user/consent/product_analytics", json={"granted": True}, headers=auth_headers)

        assert response.status_code == 200
        assert response.json() == {"purpose": "product_analytics", "granted": True, "changed": True}
        assert record_event.await_args.args[1] == "consent.granted"
        assert record_event.await_args.args[2] == {"purpose": "product_analytics"}

    def test_a_repeated_grant_changes_nothing_and_emits_nothing(
        self, test_client: TestClient, auth_headers: dict[str, str], mock_cur: MagicMock
    ) -> None:
        mock_cur.fetchone.return_value = None

        with patch("api.activity.record_event", AsyncMock()) as record_event:
            response = test_client.put("/api/user/consent/product_analytics", json={"granted": True}, headers=auth_headers)

        assert response.status_code == 200
        assert response.json()["changed"] is False
        assert record_event.await_count == 0, "a repeated request is not a second decision"

    def test_the_grant_statement_cannot_write_a_second_active_row(
        self, test_client: TestClient, auth_headers: dict[str, str], mock_cur: MagicMock
    ) -> None:
        mock_cur.fetchone.return_value = None

        with patch("api.activity.record_event", AsyncMock()):
            test_client.put("/api/user/consent/product_analytics", json={"granted": True}, headers=auth_headers)

        insert = next(sql for sql in statements(mock_cur) if "INSERT INTO activity.consent_grants" in sql)
        assert "WHERE NOT EXISTS" in insert
        assert "revoked_at IS NULL" in insert

    def test_a_revocation_closes_the_active_grant_and_emits_the_event(
        self, test_client: TestClient, auth_headers: dict[str, str], mock_cur: MagicMock
    ) -> None:
        mock_cur.fetchone.return_value = {"id": "1"}

        with patch("api.activity.record_event", AsyncMock()) as record_event:
            response = test_client.put("/api/user/consent/model_training", json={"granted": False}, headers=auth_headers)

        assert response.status_code == 200
        assert response.json() == {"purpose": "model_training", "granted": False, "changed": True}
        assert record_event.await_args.args[1] == "consent.revoked"
        assert record_event.await_args.args[2] == {"purpose": "model_training"}

        update = next(sql for sql in statements(mock_cur) if "UPDATE activity.consent_grants" in sql)
        assert "SET revoked_at = NOW()" in update
        assert "revoked_at IS NULL" in update, "an already-revoked grant is not re-closed"

    def test_a_repeated_revocation_changes_nothing_and_emits_nothing(
        self, test_client: TestClient, auth_headers: dict[str, str], mock_cur: MagicMock
    ) -> None:
        mock_cur.fetchone.return_value = None

        with patch("api.activity.record_event", AsyncMock()) as record_event:
            response = test_client.put("/api/user/consent/model_training", json={"granted": False}, headers=auth_headers)

        assert response.json()["changed"] is False
        assert record_event.await_count == 0

    @pytest.mark.parametrize("purpose", ["analytics", "training", "marketing", "product-analytics", "PRODUCT_ANALYTICS"])
    def test_an_unknown_purpose_is_rejected_without_a_write(
        self, test_client: TestClient, auth_headers: dict[str, str], mock_cur: MagicMock, purpose: str
    ) -> None:
        with patch("api.activity.record_event", AsyncMock()) as record_event:
            response = test_client.put(f"/api/user/consent/{purpose}", json={"granted": True}, headers=auth_headers)

        assert response.status_code == 422
        assert not any("consent_grants" in sql for sql in statements(mock_cur))
        assert record_event.await_count == 0

    def test_a_missing_granted_field_is_rejected(self, test_client: TestClient, auth_headers: dict[str, str]) -> None:
        response = test_client.put("/api/user/consent/product_analytics", json={}, headers=auth_headers)
        assert response.status_code == 422

    def test_the_endpoint_requires_a_user(self, test_client: TestClient) -> None:
        assert test_client.put("/api/user/consent/product_analytics", json={"granted": True}).status_code == 401

    def test_both_published_purposes_are_accepted(self, test_client: TestClient, auth_headers: dict[str, str], mock_cur: MagicMock) -> None:
        mock_cur.fetchone.return_value = {"id": "1", "granted_at": GRANTED_AT, "revoked_at": None}

        with patch("api.activity.record_event", AsyncMock()):
            for purpose in consent_purposes():
                assert test_client.put(f"/api/user/consent/{purpose}", json={"granted": True}, headers=auth_headers).status_code == 200


class TestUnconfiguredService:
    """Both endpoints report a missing pool rather than failing on it."""

    def test_the_endpoints_report_a_missing_pool(self, test_client: TestClient, auth_headers: dict[str, str]) -> None:
        import api.routers.activity as activity_router

        original = activity_router._pool
        activity_router.configure(None, None, None)
        try:
            assert test_client.get("/api/user/consent", headers=auth_headers).status_code == 503
            assert test_client.put("/api/user/consent/product_analytics", json={"granted": True}, headers=auth_headers).status_code == 503
        finally:
            activity_router.configure(original, None, None)
