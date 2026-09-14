"""Behavioral tests for the erasure procedure and the account export.

Erasure is the one claim in this service that has to be literally true, so these tests
assert the statements in order, that the immutability bypass is SET LOCAL inside the
transaction, and that the users row is updated rather than deleted. The Neo4j and Redis
steps are driven to failure as well as to success, because a half-finished erasure that
reports success would be worse than one that fails loudly.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

import api.routers.activity as activity_router
from tests.conftest import TEST_USER_ID


SUBJECT_ID = UUID("11111111-1111-1111-1111-111111111111")
PASSWORD = "testpassword"


def password_hash(password: str = PASSWORD) -> str:
    """Build a salt:key hash in the format `api.auth._verify_password` reads."""
    salt = os.urandom(32)
    key = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 100_000)
    return salt.hex() + ":" + key.hex()


# Stands in for the encrypted TOTP secret column. The tests that use it patch the
# decryption, so the value is never read as a secret — only its presence matters.
ENCRYPTED_TOTP = "encrypted"


def credentials(totp_enabled: bool = False, totp_secret: str | None = None) -> dict[str, Any]:
    return {"hashed_password": password_hash(), "totp_enabled": totp_enabled, "totp_secret": totp_secret}


def statements(cur: MagicMock) -> list[str]:
    return [" ".join(str(call.args[0]).split()) for call in cur.execute.await_args_list]


def index_of(executed: list[str], fragment: str) -> int:
    for position, sql in enumerate(executed):
        if fragment in sql:
            return position
    raise AssertionError(f"no statement contained {fragment!r}")


@pytest.fixture
def erasable(mock_cur: MagicMock, mock_redis: AsyncMock) -> Any:
    """A caller whose password verifies, whose subject resolves, and whose erasure records."""
    mock_cur.fetchone.side_effect = [credentials(), {"id": "99999999-9999-9999-9999-999999999999"}]
    mock_cur.rowcount = 7
    # The cache sweep is verified after it runs, so the scan has to answer.
    mock_redis.scan = AsyncMock(return_value=(0, []))
    with (
        patch("api.activity.subject_for", AsyncMock(return_value=SUBJECT_ID)),
        patch("api.activity.record_event", AsyncMock()) as record_event,
    ):
        yield record_event


@pytest.mark.usefixtures("erasable")
class TestErasureProcedure:
    """POST /api/user/erasure — the ordered, cross-store deletion."""

    def test_the_bypass_is_set_local_inside_the_transaction(
        self, test_client: TestClient, auth_headers: dict[str, str], mock_cur: MagicMock, mock_conn: MagicMock
    ) -> None:
        response = test_client.post("/api/user/erasure", json={"password": PASSWORD}, headers=auth_headers)

        assert response.status_code == 202
        executed = statements(mock_cur)
        assert "SET LOCAL groovemap.erasure = 'on'" in executed
        assert mock_conn.transaction.called, "the bypass and the deletions share one transaction"
        assert index_of(executed, "SET LOCAL groovemap.erasure") < index_of(executed, "DELETE FROM activity.events")

    def test_every_step_runs_in_the_order_the_decision_sets_out(
        self, test_client: TestClient, auth_headers: dict[str, str], mock_cur: MagicMock
    ) -> None:
        test_client.post("/api/user/erasure", json={"password": PASSWORD}, headers=auth_headers)

        executed = statements(mock_cur)
        ordered = [
            "SET LOCAL groovemap.erasure",
            "DELETE FROM activity.events",
            "DELETE FROM activity.impressions",
            "DELETE FROM activity.user_subjects",
            "INSERT INTO activity.erasures",
            "DELETE FROM observations",
            "DELETE FROM collection_snapshots",
            "DELETE FROM owned_copies",
            "DELETE FROM user_collections",
            "DELETE FROM user_wantlists",
            "DELETE FROM sync_history",
            "DELETE FROM app_tokens",
            "DELETE FROM oauth_tokens",
            "UPDATE users",
        ]
        positions = [index_of(executed, fragment) for fragment in ordered]
        assert positions == sorted(positions), "the procedure runs in the decided order"

    def test_the_users_row_is_updated_and_never_deleted(self, test_client: TestClient, auth_headers: dict[str, str], mock_cur: MagicMock) -> None:
        test_client.post("/api/user/erasure", json={"password": PASSWORD}, headers=auth_headers)

        executed = statements(mock_cur)
        assert not any(sql.startswith("DELETE FROM users") for sql in executed)

        soft_erase = executed[index_of(executed, "UPDATE users")]
        assert "is_active = FALSE" in soft_erase
        assert "totp_secret = NULL" in soft_erase
        assert "totp_enabled = FALSE" in soft_erase
        assert "totp_recovery_codes = NULL" in soft_erase
        assert "updated_at = NOW()" in soft_erase

    def test_the_soft_erase_replaces_the_email_and_the_password(
        self, test_client: TestClient, auth_headers: dict[str, str], mock_cur: MagicMock
    ) -> None:
        test_client.post("/api/user/erasure", json={"password": PASSWORD}, headers=auth_headers)

        parameters = next(call.args[1] for call in mock_cur.execute.await_args_list if "UPDATE users" in str(call.args[0]))
        assert parameters[0] == f"erased+{TEST_USER_ID}@invalid.groovemap"
        assert parameters[1] != password_hash(), "the replacement hash is fresh and random"
        assert ":" in parameters[1], "it is still a hash nothing can present"

    def test_the_erasure_record_carries_the_counts_and_an_empty_model_list(
        self, test_client: TestClient, auth_headers: dict[str, str], mock_cur: MagicMock
    ) -> None:
        response = test_client.post("/api/user/erasure", json={"password": PASSWORD}, headers=auth_headers)

        parameters = next(call.args[1] for call in mock_cur.execute.await_args_list if "INSERT INTO activity.erasures" in str(call.args[0]))
        assert parameters[0] == SUBJECT_ID
        assert parameters[2] == 7
        assert parameters[3] == 7
        assert parameters[4] == [], "no model registry exists yet to name the versions"
        assert response.json() == {
            "erasure_id": "99999999-9999-9999-9999-999999999999",
            "events_deleted": 7,
            "impressions_deleted": 7,
            "incomplete": [],
        }

    def test_the_request_event_is_emitted_before_the_procedure_deletes_it(
        self, test_client: TestClient, auth_headers: dict[str, str], erasable: Any
    ) -> None:
        test_client.post("/api/user/erasure", json={"password": PASSWORD}, headers=auth_headers)

        assert erasable.await_args.args[1] == "account.erasure_requested"
        assert set(erasable.await_args.args[2]) == {"erasure_id"}
        assert UUID(erasable.await_args.args[2]["erasure_id"])

    def test_the_neo4j_subgraph_is_detach_deleted(self, test_client: TestClient, auth_headers: dict[str, str], mock_neo4j_session: MagicMock) -> None:
        test_client.post("/api/user/erasure", json={"password": PASSWORD}, headers=auth_headers)

        cypher = str(mock_neo4j_session.run.await_args.args[0])
        assert "MATCH (u:User {id: $user_id}) DETACH DELETE u" in cypher
        assert mock_neo4j_session.run.await_args.kwargs["user_id"] == TEST_USER_ID

    def test_every_per_user_redis_key_is_deleted(self, test_client: TestClient, auth_headers: dict[str, str], mock_redis: AsyncMock) -> None:
        with patch("api.cache.RecommendCache.invalidate_user", AsyncMock()) as invalidate:
            test_client.post("/api/user/erasure", json={"password": PASSWORD}, headers=auth_headers)

        assert invalidate.await_args.args[0] == TEST_USER_ID
        deleted = set(mock_redis.delete.await_args.args)
        assert deleted == {
            f"snapshot:usercount:{TEST_USER_ID}",
            f"sync:lock:{TEST_USER_ID}",
            f"sync:cooldown:{TEST_USER_ID}",
        }

    def test_the_callers_token_is_revoked_last(self, test_client: TestClient, auth_headers: dict[str, str], mock_redis: AsyncMock) -> None:
        test_client.post("/api/user/erasure", json={"password": PASSWORD}, headers=auth_headers)

        # The conftest token carries no jti, so nothing is blacklisted; a token that
        # carries one is revoked the way logout does.
        assert mock_redis.setex.await_count == 0

    def test_a_token_with_a_jti_is_blacklisted(self, test_client: TestClient, mock_redis: AsyncMock) -> None:
        import base64
        import hmac

        from tests.conftest import TEST_JWT_SECRET

        def b64url(data: bytes) -> str:
            return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")

        header = b64url(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
        body = b64url(json.dumps({"sub": TEST_USER_ID, "exp": 9_999_999_999, "jti": "abc"}, separators=(",", ":")).encode())
        signature = b64url(hmac.new(TEST_JWT_SECRET.encode(), f"{header}.{body}".encode("ascii"), hashlib.sha256).digest())

        test_client.post("/api/user/erasure", json={"password": PASSWORD}, headers={"Authorization": f"Bearer {header}.{body}.{signature}"})

        assert mock_redis.setex.await_args.args[0] == "revoked:jti:abc"

    def test_the_cached_pseudonym_is_dropped_so_no_later_write_names_deleted_rows(
        self, test_client: TestClient, auth_headers: dict[str, str]
    ) -> None:
        with patch("api.activity.forget_subject") as forget:
            test_client.post("/api/user/erasure", json={"password": PASSWORD}, headers=auth_headers)

        assert forget.call_args.args[0] == TEST_USER_ID


@pytest.mark.usefixtures("erasable")
class TestErasureFailuresAreReported:
    """A store that did not clear is named in the response, never hidden."""

    def test_a_failing_neo4j_step_is_reported_beside_the_committed_postgres_result(
        self, test_client: TestClient, auth_headers: dict[str, str], mock_neo4j_session: MagicMock
    ) -> None:
        mock_neo4j_session.run.side_effect = RuntimeError("graph unavailable")

        response = test_client.post("/api/user/erasure", json={"password": PASSWORD}, headers=auth_headers)

        assert response.status_code == 202
        body = response.json()
        assert body["events_deleted"] == 7, "the relational half committed and still reports its counts"
        assert any("Neo4j" in failure for failure in body["incomplete"])

    def test_a_failing_redis_step_is_reported(self, test_client: TestClient, auth_headers: dict[str, str], mock_redis: AsyncMock) -> None:
        mock_redis.delete.side_effect = RuntimeError("cache unavailable")

        body = test_client.post("/api/user/erasure", json={"password": PASSWORD}, headers=auth_headers).json()

        assert any("Redis" in failure for failure in body["incomplete"])

    def test_a_cache_key_that_survives_the_sweep_is_reported(
        self, test_client: TestClient, auth_headers: dict[str, str], mock_redis: AsyncMock
    ) -> None:
        """RecommendCache.invalidate_user swallows its own failures; erasure may not."""
        mock_redis.scan = AsyncMock(return_value=(0, [f"recommend:enhanced:{TEST_USER_ID}"]))

        body = test_client.post("/api/user/erasure", json={"password": PASSWORD}, headers=auth_headers).json()

        assert any("survived" in failure for failure in body["incomplete"])

    def test_the_sweep_is_verified_against_every_per_user_recommendation_pattern(
        self, test_client: TestClient, auth_headers: dict[str, str], mock_redis: AsyncMock
    ) -> None:
        test_client.post("/api/user/erasure", json={"password": PASSWORD}, headers=auth_headers)

        verified = {call.kwargs["match"] for call in mock_redis.scan.await_args_list}
        assert f"recommend:explore:{TEST_USER_ID}:*" in verified
        assert f"recommend:enhanced:{TEST_USER_ID}" in verified

    def test_both_failures_are_reported_together(
        self, test_client: TestClient, auth_headers: dict[str, str], mock_neo4j_session: MagicMock, mock_redis: AsyncMock
    ) -> None:
        mock_neo4j_session.run.side_effect = RuntimeError("graph unavailable")
        mock_redis.delete.side_effect = RuntimeError("cache unavailable")

        body = test_client.post("/api/user/erasure", json={"password": PASSWORD}, headers=auth_headers).json()

        assert len(body["incomplete"]) == 2

    def test_an_unconfigured_neo4j_or_redis_is_reported_rather_than_assumed_clean(
        self, test_client: TestClient, auth_headers: dict[str, str]
    ) -> None:
        original = (activity_router._pool, activity_router._redis, activity_router._neo4j_driver, activity_router._config)
        activity_router.configure(original[0], None, None, original[3])
        try:
            with patch("api.activity.redis_client", return_value=None):
                body = test_client.post("/api/user/erasure", json={"password": PASSWORD}, headers=auth_headers).json()
        finally:
            activity_router.configure(*original)

        assert len(body["incomplete"]) == 2


class TestErasureAuthentication:
    """An irreversible deletion is re-authenticated, not taken on the bearer token."""

    def test_a_wrong_password_is_rejected_before_any_deletion(
        self, test_client: TestClient, auth_headers: dict[str, str], mock_cur: MagicMock
    ) -> None:
        mock_cur.fetchone.return_value = credentials()

        response = test_client.post("/api/user/erasure", json={"password": "wrong"}, headers=auth_headers)

        assert response.status_code == 401
        assert not any("DELETE FROM activity.events" in sql for sql in statements(mock_cur))

    def test_an_unknown_user_is_rejected(self, test_client: TestClient, auth_headers: dict[str, str], mock_cur: MagicMock) -> None:
        mock_cur.fetchone.return_value = None

        assert test_client.post("/api/user/erasure", json={"password": PASSWORD}, headers=auth_headers).status_code == 404

    def test_a_2fa_account_must_send_a_totp_code(self, test_client: TestClient, auth_headers: dict[str, str], mock_cur: MagicMock) -> None:
        mock_cur.fetchone.return_value = credentials(totp_enabled=True, totp_secret=ENCRYPTED_TOTP)

        response = test_client.post("/api/user/erasure", json={"password": PASSWORD}, headers=auth_headers)

        assert response.status_code == 400
        assert not any("DELETE FROM activity.events" in sql for sql in statements(mock_cur))

    def test_a_wrong_totp_code_is_rejected(self, test_client: TestClient, auth_headers: dict[str, str], mock_cur: MagicMock) -> None:
        mock_cur.fetchone.return_value = credentials(totp_enabled=True, totp_secret=ENCRYPTED_TOTP)

        with (
            patch("api.routers.activity.get_totp_encryption_key", return_value="key"),
            patch("api.routers.activity.decrypt_totp_secret", return_value="SECRET"),
            patch("api.routers.activity.verify_totp_code", return_value=False),
        ):
            response = test_client.post("/api/user/erasure", json={"password": PASSWORD, "code": "000000"}, headers=auth_headers)

        assert response.status_code == 400

    def test_a_correct_totp_code_lets_the_procedure_run(self, test_client: TestClient, auth_headers: dict[str, str], mock_cur: MagicMock) -> None:
        mock_cur.fetchone.side_effect = [credentials(totp_enabled=True, totp_secret=ENCRYPTED_TOTP), {"id": "1"}]
        mock_cur.rowcount = 0

        with (
            patch("api.routers.activity.get_totp_encryption_key", return_value="key"),
            patch("api.routers.activity.decrypt_totp_secret", return_value="SECRET"),
            patch("api.routers.activity.verify_totp_code", return_value=True),
            patch("api.activity.subject_for", AsyncMock(return_value=SUBJECT_ID)),
            patch("api.activity.record_event", AsyncMock()),
        ):
            response = test_client.post("/api/user/erasure", json={"password": PASSWORD, "code": "123456"}, headers=auth_headers)

        assert response.status_code == 202

    def test_a_missing_encryption_key_is_reported_rather_than_bypassed(
        self, test_client: TestClient, auth_headers: dict[str, str], mock_cur: MagicMock
    ) -> None:
        mock_cur.fetchone.return_value = credentials(totp_enabled=True, totp_secret=ENCRYPTED_TOTP)

        with patch("api.routers.activity.get_totp_encryption_key", return_value=None):
            response = test_client.post("/api/user/erasure", json={"password": PASSWORD, "code": "123456"}, headers=auth_headers)

        assert response.status_code == 503

    def test_the_endpoint_requires_a_user(self, test_client: TestClient) -> None:
        assert test_client.post("/api/user/erasure", json={"password": PASSWORD}).status_code == 401

    def test_an_unresolvable_subject_stops_the_procedure(self, test_client: TestClient, auth_headers: dict[str, str], mock_cur: MagicMock) -> None:
        mock_cur.fetchone.return_value = credentials()

        with patch("api.activity.subject_for", AsyncMock(return_value=None)):
            response = test_client.post("/api/user/erasure", json={"password": PASSWORD}, headers=auth_headers)

        assert response.status_code == 503
        assert not any("DELETE FROM activity.events" in sql for sql in statements(mock_cur))


@pytest.mark.usefixtures("sections")
class TestExport:
    """GET /api/user/export — every row keyed to the caller, in a stable order."""

    @pytest.fixture
    def sections(self, mock_cur: MagicMock) -> Any:
        """One row per exported section, in the order the endpoint reads them."""
        mock_cur.fetchall.side_effect = [
            [{"event_id": UUID(int=1), "event_type": "search.query", "occurred_at": datetime(2026, 1, 1, tzinfo=UTC), "payload": {"query": "x"}}],
            [{"impression_id": UUID(int=2), "policy_id": "p_v1", "position": 1, "score": 0.5}],
            [{"id": UUID(int=3), "release_id": 10, "title": "T"}],
            [{"id": UUID(int=4), "release_id": 11, "title": "W"}],
            [{"id": UUID(int=5), "item_id": UUID(int=6)}],
            [{"id": UUID(int=7), "kind": "matrix", "value": "ABC", "confidence": None}],
            [{"id": UUID(int=8), "taken_at": datetime(2026, 2, 1, tzinfo=UTC), "item_count": 3}],
            [{"id": UUID(int=9), "purpose": "product_analytics", "granted_at": datetime(2026, 3, 1, tzinfo=UTC), "revoked_at": None}],
        ]
        with (
            patch("api.activity.subject_for", AsyncMock(return_value=SUBJECT_ID)),
            patch("api.activity.record_event", AsyncMock()) as record_event,
        ):
            yield record_event

    def read(self, test_client: TestClient, auth_headers: dict[str, str]) -> list[dict[str, Any]]:
        response = test_client.get("/api/user/export", headers=auth_headers)
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/x-ndjson")
        return [json.loads(line) for line in response.text.splitlines() if line]

    def test_the_sections_stream_in_the_decided_order(self, test_client: TestClient, auth_headers: dict[str, str]) -> None:
        lines = self.read(test_client, auth_headers)

        assert [line["kind"] for line in lines] == [
            "event",
            "impression",
            "collection_item",
            "wantlist_item",
            "owned_copy",
            "observation",
            "collection_snapshot",
            "consent_grant",
        ]

    def test_each_line_is_a_kind_and_a_record(self, test_client: TestClient, auth_headers: dict[str, str]) -> None:
        lines = self.read(test_client, auth_headers)

        assert all(set(line) == {"kind", "record"} for line in lines)
        assert lines[0]["record"]["event_type"] == "search.query"
        assert lines[0]["record"]["occurred_at"] == "2026-01-01T00:00:00+00:00"
        assert lines[0]["record"]["event_id"] == str(UUID(int=1))

    def test_snapshots_carry_ids_and_shape_but_not_their_copy_arrays(self, test_client: TestClient, auth_headers: dict[str, str]) -> None:
        snapshot = next(line for line in self.read(test_client, auth_headers) if line["kind"] == "collection_snapshot")

        assert set(snapshot["record"]) == {"id", "taken_at", "item_count"}
        assert "copy_ids" not in snapshot["record"]

    def test_the_activity_sections_are_keyed_by_subject_and_the_rest_by_user(
        self, test_client: TestClient, auth_headers: dict[str, str], mock_cur: MagicMock
    ) -> None:
        self.read(test_client, auth_headers)

        by_statement = {" ".join(str(call.args[0]).split()): call.args[1] for call in mock_cur.execute.await_args_list}
        for sql, parameters in by_statement.items():
            if "activity.events" in sql or "activity.impressions" in sql:
                assert parameters == (SUBJECT_ID,)
            elif "FROM user_collections" in sql or "consent_grants" in sql:
                assert parameters == (TEST_USER_ID,)

    def test_the_export_event_is_recorded_with_its_published_payload(
        self, test_client: TestClient, auth_headers: dict[str, str], sections: Any
    ) -> None:
        self.read(test_client, auth_headers)

        assert sections.await_args.args[1] == "account.export_requested"
        payload = sections.await_args.args[2]
        assert set(payload) == {"export_id", "format"}
        assert payload["format"] == "jsonl"
        assert UUID(payload["export_id"])

    def test_a_caller_with_no_subject_still_exports_the_relational_half(
        self, test_client: TestClient, auth_headers: dict[str, str], mock_cur: MagicMock
    ) -> None:
        mock_cur.fetchall.side_effect = [
            [{"id": UUID(int=3), "release_id": 10}],
            [],
            [],
            [],
            [],
            [{"id": UUID(int=9), "purpose": "product_analytics"}],
        ]
        with patch("api.activity.subject_for", AsyncMock(return_value=None)), patch("api.activity.record_event", AsyncMock()):
            lines = self.read(test_client, auth_headers)

        assert [line["kind"] for line in lines] == ["collection_item", "consent_grant"]

    def test_array_decimal_and_string_columns_all_render_as_json(
        self, test_client: TestClient, auth_headers: dict[str, str], mock_cur: MagicMock
    ) -> None:
        from decimal import Decimal

        mock_cur.fetchall.side_effect = [
            [{"event_id": UUID(int=1), "consent_purposes": ["product_analytics"], "payload": {"item_id": UUID(int=2)}}],
            [],
            [],
            [],
            [],
            [{"id": UUID(int=7), "confidence": Decimal("0.75"), "observed_at": "2026-04-01T00:00:00+00:00"}],
            [],
            [],
        ]
        with patch("api.activity.subject_for", AsyncMock(return_value=SUBJECT_ID)), patch("api.activity.record_event", AsyncMock()):
            lines = self.read(test_client, auth_headers)

        assert lines[0]["record"]["consent_purposes"] == ["product_analytics"]
        assert lines[0]["record"]["payload"] == {"item_id": str(UUID(int=2))}
        observation = next(line for line in lines if line["kind"] == "observation")
        assert observation["record"]["confidence"] == 0.75
        assert observation["record"]["observed_at"] == "2026-04-01T00:00:00+00:00"

    def test_the_endpoint_requires_a_user(self, test_client: TestClient) -> None:
        assert test_client.get("/api/user/export").status_code == 401

    def test_an_unconfigured_pool_is_reported(self, test_client: TestClient, auth_headers: dict[str, str]) -> None:
        original = (activity_router._pool, activity_router._redis, activity_router._neo4j_driver, activity_router._config)
        activity_router.configure(None, None, None, None)
        try:
            assert test_client.get("/api/user/export", headers=auth_headers).status_code == 503
            assert test_client.post("/api/user/erasure", json={"password": PASSWORD}, headers=auth_headers).status_code == 503
        finally:
            activity_router.configure(*original)
