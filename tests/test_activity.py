"""Behavioral tests for the in-process activity recorder.

Every test drives the module the way a request does — through `record_event` and
`record_impressions` with a mocked pool — and asserts on the statement that reached the
cursor and on the metric that was recorded. The recorder's whole contract is that it never
raises into a request, so each failure mode is exercised for its return value and its
counted outcome rather than for an exception.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from importlib.resources import files
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID

import pytest
from common import telemetry as common_telemetry
from common.events import EventValidationError, event_types, payload_schema_for
from jsonschema import Draft202012Validator
from opentelemetry.sdk.metrics import MeterProvider as SdkMeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

import api.activity as activity
import api.telemetry as telemetry


EVENTS_COUNTER = "groovemap.api.activity_events"
FAILURES_COUNTER = "groovemap.api.activity_failures"

USER_ID = "00000000-0000-0000-0000-000000000001"
SUBJECT_ID = UUID("11111111-1111-1111-1111-111111111111")
ITEM_ID = UUID("22222222-2222-2222-2222-222222222222")
OTHER_ITEM_ID = UUID("33333333-3333-3333-3333-333333333333")
CANDIDATE_SET_ID = UUID("44444444-4444-4444-4444-444444444444")
REQUEST_ID = UUID("55555555-5555-5555-5555-555555555555")

VALID_OUTCOME_PAYLOAD = {"impression_id": str(REQUEST_ID), "item_id": str(ITEM_ID)}


class Metrics:
    """An in-memory MeterProvider whose counters can be read back by attribute."""

    def __init__(self) -> None:
        self.reader = InMemoryMetricReader()
        self.provider = SdkMeterProvider(metric_readers=[self.reader])

    def attributes(self, name: str) -> list[dict[str, Any]]:
        data = self.reader.get_metrics_data()
        if data is None:
            return []
        return [
            dict(point.attributes)
            for resource_metrics in data.resource_metrics
            for scope_metrics in resource_metrics.scope_metrics
            for metric in scope_metrics.metrics
            if metric.name == name
            for point in metric.data.data_points
        ]

    def values(self, name: str, attribute: str) -> list[str]:
        return [str(attributes[attribute]) for attributes in self.attributes(name)]


@pytest.fixture
def metrics(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Install an in-memory provider as the one `common.get_meter` hands meters out from."""
    active = Metrics()
    monkeypatch.setattr(common_telemetry, "_provider", active.provider)
    telemetry.reset_instruments()
    yield active
    monkeypatch.setattr(common_telemetry, "_provider", None)
    telemetry.reset_instruments()


@pytest.fixture
def recorder(mock_pool: MagicMock, mock_cur: MagicMock) -> Any:
    """Wire the recorder onto the mocked pool with a resolvable subject.

    Requested by class through ``usefixtures``: every test in these classes needs the
    recorder wired, and only the cursor it writes through is ever asserted on.
    """
    mock_cur.fetchone.return_value = (SUBJECT_ID,)
    mock_cur.fetchall.return_value = [("product_analytics",)]
    activity.configure(mock_pool, None)
    yield mock_pool
    activity.configure(None, None)


def statements(cur: MagicMock) -> list[str]:
    """Return every SQL string that reached the cursor, in order."""
    return [str(call.args[0]) for call in cur.execute.await_args_list]


def parameters_for(cur: MagicMock, fragment: str) -> tuple[Any, ...]:
    """Return the parameters of the first statement containing ``fragment``."""
    for call in cur.execute.await_args_list:
        if fragment in str(call.args[0]):
            return tuple(call.args[1])
    raise AssertionError(f"no statement contained {fragment!r}")


@pytest.mark.usefixtures("recorder")
class TestSubject:
    """The pseudonym link, which is what keeps account identity out of the event tables."""

    @pytest.mark.asyncio
    async def test_get_or_create_returns_the_subject_and_caches_it(self, mock_cur: MagicMock) -> None:
        assert await activity.subject_for(USER_ID) == SUBJECT_ID
        assert await activity.subject_for(USER_ID) == SUBJECT_ID

        upserts = [sql for sql in statements(mock_cur) if "activity.user_subjects" in sql]
        assert len(upserts) == 1, "the second resolution must come from the process cache"
        assert "ON CONFLICT (user_id) DO UPDATE" in upserts[0]
        assert "RETURNING subject_id" in upserts[0]

    @pytest.mark.asyncio
    async def test_an_unconfigured_pool_resolves_no_subject(self) -> None:
        activity.configure(None, None)
        assert await activity.subject_for(USER_ID) is None

    @pytest.mark.asyncio
    async def test_a_failing_lookup_returns_none_rather_than_raising(self, mock_cur: MagicMock) -> None:
        mock_cur.execute.side_effect = RuntimeError("subjects unavailable")
        assert await activity.subject_for(USER_ID) is None

    @pytest.mark.asyncio
    async def test_an_empty_result_resolves_no_subject(self, mock_cur: MagicMock) -> None:
        mock_cur.fetchone.return_value = None
        assert await activity.subject_for(USER_ID) is None

    @pytest.mark.asyncio
    async def test_forgetting_a_subject_makes_the_next_write_resolve_it_again(self, mock_cur: MagicMock) -> None:
        await activity.subject_for(USER_ID)
        activity.forget_subject(USER_ID)
        await activity.subject_for(USER_ID)

        assert len([sql for sql in statements(mock_cur) if "activity.user_subjects" in sql]) == 2


@pytest.mark.usefixtures("recorder")
class TestConsentSnapshot:
    """The purposes written onto the row, which make it interpretable years later."""

    @pytest.mark.asyncio
    async def test_active_purposes_come_back_in_vocabulary_order(self, mock_cur: MagicMock) -> None:
        mock_cur.fetchall.return_value = [("model_training",), ("product_analytics",)]

        assert await activity.active_purposes(USER_ID) == ("product_analytics", "model_training")

        grants = next(sql for sql in statements(mock_cur) if "consent_grants" in sql)
        assert "revoked_at IS NULL" in grants, "a revoked grant is not an active purpose"
        assert "DISTINCT" in grants, "the same purpose may have been granted more than once"

    @pytest.mark.asyncio
    async def test_an_unconfigured_pool_snapshots_nothing(self) -> None:
        activity.configure(None, None)
        assert await activity.active_purposes(USER_ID) == ()

    @pytest.mark.asyncio
    async def test_an_unreadable_grant_table_snapshots_nothing(self, mock_cur: MagicMock) -> None:
        mock_cur.execute.side_effect = [None, RuntimeError("grants unavailable")]
        await activity.subject_for(USER_ID)
        assert await activity.active_purposes(USER_ID) == ()


@pytest.mark.usefixtures("recorder")
class TestRecordEvent:
    """One event, from a user id and a payload to a row."""

    @pytest.mark.asyncio
    async def test_a_valid_event_is_inserted_with_its_snapshot_and_partition(self, mock_cur: MagicMock, metrics: Any) -> None:
        await activity.record_event(USER_ID, "recommendation.opened", VALID_OUTCOME_PAYLOAD, idempotency_key="opened:1")

        executed = statements(mock_cur)
        assert any("ensure_month_partition" in sql for sql in executed)
        assert any("INSERT INTO activity.events" in sql for sql in executed)

        ensure = parameters_for(mock_cur, "ensure_month_partition")
        assert ensure[0] == "events"
        assert ensure[1].day == 1, "the partition is ensured for the first of the occurrence month"

        row = parameters_for(mock_cur, "INSERT INTO activity.events")
        assert row[1] == "recommendation.opened"
        assert row[3] == SUBJECT_ID, "the row references the subject, never the user id"
        assert row[7] == activity.PRODUCER
        assert row[8] == ["product_analytics"]
        assert row[11] == "opened:1"
        assert json.loads(row[12]) == VALID_OUTCOME_PAYLOAD
        assert metrics.values(EVENTS_COUNTER, "event_type") == ["recommendation.opened"]

    @pytest.mark.asyncio
    async def test_the_insert_is_idempotent_on_the_occurrence_scoped_key(self, mock_cur: MagicMock) -> None:
        await activity.record_event(USER_ID, "recommendation.opened", VALID_OUTCOME_PAYLOAD)

        insert = next(sql for sql in statements(mock_cur) if "INSERT INTO activity.events" in sql)
        assert "ON CONFLICT (occurred_at, idempotency_key) DO NOTHING" in insert

    @pytest.mark.asyncio
    async def test_the_partition_is_ensured_once_per_process_per_month(self, mock_cur: MagicMock) -> None:
        moment = datetime(2026, 3, 4, 12, 0, tzinfo=UTC)
        await activity.record_event(USER_ID, "recommendation.opened", VALID_OUTCOME_PAYLOAD, occurred_at=moment)
        await activity.record_event(USER_ID, "recommendation.saved", VALID_OUTCOME_PAYLOAD, occurred_at=moment)
        await activity.record_event(USER_ID, "recommendation.saved", VALID_OUTCOME_PAYLOAD, occurred_at=moment.replace(month=4))

        ensures = [sql for sql in statements(mock_cur) if "ensure_month_partition" in sql]
        assert len(ensures) == 2, "one per month, not one per write"

    @pytest.mark.asyncio
    async def test_a_session_id_and_the_version_fields_reach_the_row(self, mock_cur: MagicMock) -> None:
        session = UUID("66666666-6666-6666-6666-666666666666")
        await activity.record_event(
            USER_ID,
            "recommendation.opened",
            VALID_OUTCOME_PAYLOAD,
            session_id=str(session),
            model_version="ranker-v2",
            feature_version="features-v7",
        )

        row = parameters_for(mock_cur, "INSERT INTO activity.events")
        assert row[4] == session
        assert row[9] == "ranker-v2"
        assert row[10] == "features-v7"

    @pytest.mark.asyncio
    async def test_an_unknown_event_type_is_counted_and_never_written(self, mock_cur: MagicMock, metrics: Any) -> None:
        await activity.record_event(USER_ID, "recommendation.invented", {})

        assert not any("INSERT INTO activity.events" in sql for sql in statements(mock_cur))
        assert metrics.values(FAILURES_COUNTER, "outcome") == [telemetry.ACTIVITY_INVALID]

    @pytest.mark.asyncio
    async def test_a_payload_key_the_schema_does_not_name_is_rejected(self, mock_cur: MagicMock, metrics: Any) -> None:
        await activity.record_event(USER_ID, "recommendation.opened", {**VALID_OUTCOME_PAYLOAD, "provider_id": "12345"})

        assert not any("INSERT INTO activity.events" in sql for sql in statements(mock_cur))
        assert metrics.values(FAILURES_COUNTER, "outcome") == [telemetry.ACTIVITY_INVALID]

    @pytest.mark.asyncio
    async def test_a_missing_required_payload_key_is_rejected(self, mock_cur: MagicMock, metrics: Any) -> None:
        await activity.record_event(USER_ID, "recommendation.opened", {"impression_id": str(REQUEST_ID)})

        assert not any("INSERT INTO activity.events" in sql for sql in statements(mock_cur))
        assert metrics.values(FAILURES_COUNTER, "outcome") == [telemetry.ACTIVITY_INVALID]

    @pytest.mark.asyncio
    async def test_a_failing_write_is_counted_and_does_not_raise(self, mock_cur: MagicMock, metrics: Any) -> None:
        def fail_on_insert(sql: Any, *_args: Any, **_kwargs: Any) -> None:
            if "INSERT INTO activity.events" in str(sql):
                raise RuntimeError("events unavailable")

        mock_cur.execute.side_effect = fail_on_insert

        await activity.record_event(USER_ID, "recommendation.opened", VALID_OUTCOME_PAYLOAD)

        assert metrics.values(FAILURES_COUNTER, "outcome") == [telemetry.ACTIVITY_WRITE_FAILED]
        assert metrics.values(EVENTS_COUNTER, "event_type") == []

    @pytest.mark.asyncio
    async def test_an_unconfigured_recorder_counts_and_drops(self, metrics: Any) -> None:
        activity.configure(None, None)

        await activity.record_event(USER_ID, "recommendation.opened", VALID_OUTCOME_PAYLOAD)

        assert metrics.values(FAILURES_COUNTER, "outcome") == [telemetry.ACTIVITY_NOT_CONFIGURED]

    @pytest.mark.asyncio
    async def test_an_unresolvable_subject_counts_and_drops(self, mock_cur: MagicMock, metrics: Any) -> None:
        mock_cur.fetchone.return_value = None

        await activity.record_event(USER_ID, "recommendation.opened", VALID_OUTCOME_PAYLOAD)

        assert metrics.values(FAILURES_COUNTER, "outcome") == [telemetry.ACTIVITY_NO_SUBJECT]

    @pytest.mark.asyncio
    async def test_a_default_idempotency_key_is_minted_when_none_is_given(self, mock_cur: MagicMock) -> None:
        await activity.record_event(USER_ID, "recommendation.opened", VALID_OUTCOME_PAYLOAD)

        row = parameters_for(mock_cur, "INSERT INTO activity.events")
        assert UUID(row[11]), "the default key is a fresh id, so the write is safe but not deduplicable"


@pytest.mark.usefixtures("recorder")
class TestRecordImpressions:
    """One ranked list as it was shown, and the ids the client reports outcomes against."""

    @pytest.mark.asyncio
    async def test_a_batch_is_inserted_once_and_returns_one_id_per_item(self, mock_cur: MagicMock, metrics: Any) -> None:
        identifiers = await activity.record_impressions(
            USER_ID,
            activity.SURFACE_RECOMMENDATION,
            "similar_artist_weighted_cosine_v1",
            CANDIDATE_SET_ID,
            [(1, ITEM_ID, 0.91, None), (2, OTHER_ITEM_ID, 0.42, None)],
            request_id=REQUEST_ID,
        )

        assert [identifier is not None for identifier in identifiers] == [True, True]
        assert mock_cur.executemany.await_count == 1
        sql, rows = mock_cur.executemany.await_args.args
        assert "INSERT INTO activity.impressions" in str(sql)
        assert [row[5] for row in rows] == [1, 2], "positions are one-based ranks"
        assert [row[6] for row in rows] == [ITEM_ID, OTHER_ITEM_ID]
        assert [row[3] for row in rows] == ["similar_artist_weighted_cosine_v1"] * 2
        assert [row[4] for row in rows] == [CANDIDATE_SET_ID] * 2
        assert [row[9] for row in rows] == [REQUEST_ID] * 2
        assert [row[8] for row in rows] == [activity.DETERMINISTIC_PROPENSITY] * 2
        assert [row[12] for row in rows] == [["product_analytics"]] * 2
        assert metrics.values(EVENTS_COUNTER, "event_type") == [telemetry.IMPRESSION_EVENT_LABEL]

    @pytest.mark.asyncio
    async def test_the_impression_partition_is_ensured_before_the_batch(self, mock_cur: MagicMock) -> None:
        await activity.record_impressions(
            USER_ID,
            activity.SURFACE_RECOMMENDATION,
            "explore_personalized_v1",
            CANDIDATE_SET_ID,
            [(1, ITEM_ID, 0.5, None)],
        )

        ensure = parameters_for(mock_cur, "ensure_month_partition")
        assert ensure[0] == "impressions"

    @pytest.mark.asyncio
    async def test_an_explicit_propensity_is_kept(self, mock_cur: MagicMock) -> None:
        await activity.record_impressions(
            USER_ID,
            activity.SURFACE_RECOMMENDATION,
            "explore_personalized_v1",
            CANDIDATE_SET_ID,
            [(1, ITEM_ID, 0.5, 0.25)],
        )

        _sql, rows = mock_cur.executemany.await_args.args
        assert rows[0][8] == 0.25

    @pytest.mark.asyncio
    async def test_an_invalid_item_is_skipped_and_the_rest_are_written(self, mock_cur: MagicMock, metrics: Any) -> None:
        identifiers = await activity.record_impressions(
            USER_ID,
            activity.SURFACE_RECOMMENDATION,
            "explore_personalized_v1",
            CANDIDATE_SET_ID,
            [(0, ITEM_ID, 0.5, None), (1, OTHER_ITEM_ID, 0.5, None)],
        )

        assert identifiers[0] is None, "position zero is below the schema's one-based minimum"
        assert identifiers[1] is not None
        _sql, rows = mock_cur.executemany.await_args.args
        assert [row[6] for row in rows] == [OTHER_ITEM_ID]
        assert telemetry.ACTIVITY_INVALID in metrics.values(FAILURES_COUNTER, "outcome")

    @pytest.mark.asyncio
    async def test_a_failing_batch_returns_no_ids(self, mock_cur: MagicMock, metrics: Any) -> None:
        mock_cur.executemany.side_effect = RuntimeError("impressions unavailable")

        identifiers = await activity.record_impressions(
            USER_ID,
            activity.SURFACE_RECOMMENDATION,
            "explore_personalized_v1",
            CANDIDATE_SET_ID,
            [(1, ITEM_ID, 0.5, None)],
        )

        assert identifiers == [None], "a client can never report an outcome against a row that was not written"
        assert metrics.values(FAILURES_COUNTER, "outcome") == [telemetry.ACTIVITY_WRITE_FAILED]

    @pytest.mark.asyncio
    async def test_every_item_invalid_writes_nothing(self, mock_cur: MagicMock) -> None:
        identifiers = await activity.record_impressions(
            USER_ID,
            activity.SURFACE_RECOMMENDATION,
            "explore_personalized_v1",
            CANDIDATE_SET_ID,
            [(0, ITEM_ID, 0.5, None)],
        )

        assert identifiers == [None]
        assert mock_cur.executemany.await_count == 0

    @pytest.mark.asyncio
    async def test_an_empty_batch_writes_nothing(self, mock_cur: MagicMock) -> None:
        assert await activity.record_impressions(USER_ID, activity.SURFACE_RECOMMENDATION, "p", CANDIDATE_SET_ID, []) == []
        assert mock_cur.executemany.await_count == 0

    @pytest.mark.asyncio
    async def test_an_unconfigured_recorder_counts_and_drops(self, metrics: Any) -> None:
        activity.configure(None, None)

        assert await activity.record_impressions(USER_ID, activity.SURFACE_RECOMMENDATION, "p", CANDIDATE_SET_ID, [(1, ITEM_ID, 0.5, None)]) == [None]
        assert metrics.values(FAILURES_COUNTER, "outcome") == [telemetry.ACTIVITY_NOT_CONFIGURED]

    @pytest.mark.asyncio
    async def test_an_unresolvable_subject_counts_and_drops(self, mock_cur: MagicMock, metrics: Any) -> None:
        mock_cur.fetchone.return_value = None

        assert await activity.record_impressions(USER_ID, activity.SURFACE_RECOMMENDATION, "p", CANDIDATE_SET_ID, [(1, ITEM_ID, 0.5, None)]) == [None]
        assert metrics.values(FAILURES_COUNTER, "outcome") == [telemetry.ACTIVITY_NO_SUBJECT]

    @pytest.mark.asyncio
    async def test_a_missing_candidate_set_and_request_id_are_minted(self, mock_cur: MagicMock) -> None:
        await activity.record_impressions(USER_ID, activity.SURFACE_RECOMMENDATION, "p", None, [(1, ITEM_ID, 0.5, None)])

        _sql, rows = mock_cur.executemany.await_args.args
        assert isinstance(rows[0][4], UUID)
        assert isinstance(rows[0][9], UUID)


@pytest.mark.usefixtures("recorder")
class TestStartupPartitions:
    """Both tables, this month and next, ensured once before the first request."""

    @pytest.mark.asyncio
    async def test_startup_ensures_two_months_for_both_tables(self, mock_cur: MagicMock) -> None:
        await activity.ensure_startup_partitions(datetime(2026, 12, 20, tzinfo=UTC))

        ensured = [tuple(call.args[1]) for call in mock_cur.execute.await_args_list if "ensure_month_partition" in str(call.args[0])]
        assert ensured == [
            ("events", datetime(2026, 12, 1).date()),
            ("events", datetime(2027, 1, 1).date()),
            ("impressions", datetime(2026, 12, 1).date()),
            ("impressions", datetime(2027, 1, 1).date()),
        ]

    @pytest.mark.asyncio
    async def test_startup_defaults_to_now(self, mock_cur: MagicMock) -> None:
        await activity.ensure_startup_partitions()

        ensured = [sql for sql in statements(mock_cur) if "ensure_month_partition" in sql]
        assert len(ensured) == 4

    @pytest.mark.asyncio
    async def test_an_unconfigured_pool_ensures_nothing(self) -> None:
        activity.configure(None, None)
        await activity.ensure_startup_partitions()

    @pytest.mark.asyncio
    async def test_a_failing_ensure_does_not_raise(self, mock_cur: MagicMock) -> None:
        mock_cur.execute.side_effect = RuntimeError("no DDL permission")
        await activity.ensure_startup_partitions()


class TestConfiguration:
    """The wiring the rest of the service reaches the recorder through."""

    def test_configure_holds_the_redis_client_for_the_erasure_closure(self, mock_pool: MagicMock) -> None:
        sentinel = object()
        activity.configure(mock_pool, sentinel)
        assert activity.redis_client() is sentinel
        activity.configure(None, None)
        assert activity.redis_client() is None

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("recorder")
    async def test_reconfiguring_drops_the_caches(self, mock_cur: MagicMock, mock_pool: MagicMock) -> None:
        await activity.subject_for(USER_ID)
        activity.configure(mock_pool, None)
        await activity.subject_for(USER_ID)

        assert len([sql for sql in statements(mock_cur) if "activity.user_subjects" in sql]) == 2

    def test_the_counted_outcomes_are_a_closed_low_cardinality_set(self) -> None:
        outcomes = {
            telemetry.ACTIVITY_INVALID,
            telemetry.ACTIVITY_NOT_CONFIGURED,
            telemetry.ACTIVITY_NO_NATIVE_ID,
            telemetry.ACTIVITY_NO_SUBJECT,
            telemetry.ACTIVITY_WRITE_FAILED,
        }
        assert len(outcomes) == 5
        assert all(outcome.replace("_", "").isalpha() for outcome in outcomes)

    def test_counting_an_unidentified_candidate_reports_the_native_id_gap(self, metrics: Any) -> None:
        activity.count_unidentified_candidate()

        assert metrics.values(FAILURES_COUNTER, "outcome") == [telemetry.ACTIVITY_NO_NATIVE_ID]


SAMPLE_PAYLOADS: dict[str, dict[str, Any]] = {
    "search.query": {"query": "miles davis", "filters": ["type:artist"], "result_count": 3, "request_id": str(REQUEST_ID)},
    "search.result_impression": {"impression_id": str(REQUEST_ID), "item_id": str(ITEM_ID), "position": 1, "request_id": str(REQUEST_ID)},
    "recommendation.shown": {"impression_id": str(REQUEST_ID), "candidate_set_id": str(CANDIDATE_SET_ID), "policy_id": "p_v1", "item_count": 2},
    "recommendation.opened": VALID_OUTCOME_PAYLOAD,
    "recommendation.saved": VALID_OUTCOME_PAYLOAD,
    "recommendation.dismissed": VALID_OUTCOME_PAYLOAD,
    "recommendation.hidden": VALID_OUTCOME_PAYLOAD,
    "fit.shown": {"impression_id": str(REQUEST_ID), "candidate_set_id": str(CANDIDATE_SET_ID), "policy_id": "cratefit_v0", "item_count": 1},
    "fit.opened": VALID_OUTCOME_PAYLOAD,
    "fit.saved": VALID_OUTCOME_PAYLOAD,
    "fit.dismissed": VALID_OUTCOME_PAYLOAD,
    "fit.hidden": VALID_OUTCOME_PAYLOAD,
    "collection.item_added": {"item_id": str(ITEM_ID), "artifact_id": None, "owned_copy_id": str(OTHER_ITEM_ID)},
    "collection.item_removed": {"item_id": str(ITEM_ID), "artifact_id": None, "owned_copy_id": None},
    "collection.item_updated": {"item_id": str(ITEM_ID), "artifact_id": None, "owned_copy_id": None, "changed_fields": ["rating"]},
    "wantlist.item_added": {"item_id": str(ITEM_ID)},
    "wantlist.item_removed": {"item_id": str(ITEM_ID)},
    "consent.granted": {"purpose": "product_analytics"},
    "consent.revoked": {"purpose": "model_training"},
    "account.export_requested": {"export_id": str(REQUEST_ID), "format": "jsonl"},
    "account.erasure_requested": {"erasure_id": str(REQUEST_ID)},
}


def schema_validator(event_type: str) -> Draft202012Validator:
    """Build a real validator for one payload type.

    ``payload_schema_for`` returns a fragment of the vendored document whose ``$ref``s
    still point at that document's ``$defs``, so the fragment is recombined with them
    before it can resolve.
    """
    vocabulary = json.loads((files("common.event_vocabulary") / "event-types.json").read_text(encoding="utf-8"))
    return Draft202012Validator({**payload_schema_for(event_type), "$defs": vocabulary["$defs"]})


class TestPayloadConformance:
    """The structural guard on the request path agrees with the published schema.

    The recorder enforces the payload schema's ``required`` set and its
    ``additionalProperties: false`` without a JSON Schema implementation, because
    ``jsonschema`` is a development dependency rather than a runtime one. These tests hold
    the two verdicts together: the real validator runs here over the same payloads, so the
    hand-written guard cannot drift from the vendored schema unnoticed.
    """

    def test_every_published_type_has_a_sample_payload(self) -> None:
        assert set(SAMPLE_PAYLOADS) == set(event_types())

    @pytest.mark.parametrize("event_type", event_types())
    def test_both_validators_accept_the_sample_payload(self, event_type: str) -> None:
        payload = SAMPLE_PAYLOADS[event_type]
        schema_validator(event_type).validate(payload)
        activity._validate_payload(event_type, payload)

    @pytest.mark.parametrize("event_type", event_types())
    def test_both_validators_reject_an_unnamed_key(self, event_type: str) -> None:
        payload = {**SAMPLE_PAYLOADS[event_type], "definitely_not_a_field": 1}
        assert dict(payload_schema_for(event_type))["additionalProperties"] is False
        assert not schema_validator(event_type).is_valid(payload)

        with pytest.raises(EventValidationError, match="is not part of"):
            activity._validate_payload(event_type, payload)

    @pytest.mark.parametrize("event_type", event_types())
    def test_both_validators_reject_a_missing_required_key(self, event_type: str) -> None:
        required = list(payload_schema_for(event_type).get("required", []))
        payload = {key: value for key, value in SAMPLE_PAYLOADS[event_type].items() if key != required[0]}
        assert not schema_validator(event_type).is_valid(payload)

        with pytest.raises(EventValidationError, match="is required"):
            activity._validate_payload(event_type, payload)


class TestServiceWiring:
    """The recorder reaches the sync and the partitions through `api.api` startup."""

    def test_startup_makes_the_recorder_the_syncs_event_hook(self, mock_pool: MagicMock, mock_redis: Any) -> None:
        """The collection sync holds a no-op recorder until startup hands it the real one.

        That indirection is what keeps `api.syncer` free of any dependency on the activity
        plumbing, and it only pays off if something actually replaces the default.
        """
        import api.api as api_module
        import api.syncer as syncer

        original = syncer._event_recorder
        try:
            syncer.configure(None)
            assert syncer._event_recorder is syncer._discard_event

            api_module._configure_routers(api_module.ApiConfig.from_env(), mock_pool, mock_redis, None)

            assert syncer._event_recorder is activity.record_event
        finally:
            syncer.configure(None if original is syncer._discard_event else original)
