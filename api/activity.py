"""The in-process activity recorder — one writer for every event and impression.

ADR 0010 in the ``design`` repository puts the behavioural record in two append-only,
month-partitioned tables in an ``activity`` schema, and writes them in process from this
service the way ``admin_audit_log`` is already written: a single ``INSERT`` that never
raises to its caller, so a logging failure degrades the record and never the request.

Everything an emission point would otherwise have to remember lives here instead.
:func:`record_event` and :func:`record_impressions` pseudonymise the user through
``activity.user_subjects``, snapshot the consent purposes active at the moment of the
write, build and validate the published envelope through :mod:`common.events`, ensure the
month partition the row lands in, insert, and count the result. A caller passes a user id,
a type, and a payload; it never sees a subject id, a partition, or an exception.

Three details are worth stating because the published contract is narrower than a casual
reading of the ADR's prose:

* **Payloads are closed.** Every payload schema in the vendored vocabulary declares
  ``additionalProperties: false``, so a key the schema does not name is a rejected write
  rather than an ignored extra. :func:`record_event` enforces the schema's ``required``
  set and its closed key set before the row is built, and the conformance test validates
  the same payloads against the vendored schema with a real JSON Schema implementation.
* **Impressions carry a propensity.** The published impression schema requires
  ``propensity`` and bounds it to ``(0, 1]``, so it cannot be null. Every ranking policy
  this service runs today is deterministic — it scores candidates and returns the top
  slice — so the probability the live policy assigned to choosing a shown item is
  :data:`DETERMINISTIC_PROPENSITY`, one. That is a true statement about these policies
  rather than a placeholder, and it stops being true the moment a sampled policy ships,
  which is exactly when the call site should pass its own.
* **Positions are one-based.** The schema says ``minimum: 1``, so rank one is position
  one.

Observability is deliberately thin, as the ADR asks: one counter for what was written,
keyed by event type, and one for what was not, keyed by a closed outcome vocabulary.
Neither carries a subject, a session, an item, or a request id.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final
from uuid import UUID

import structlog
from common.events import (
    Event,
    EventValidationError,
    consent_purposes,
    new_event,
    new_impression,
    payload_schema_for,
)
from common.identity import new_id
from common.query_debug import execute_sql

from api.telemetry import (
    ACTIVITY_INVALID,
    ACTIVITY_NO_NATIVE_ID,
    ACTIVITY_NO_SUBJECT,
    ACTIVITY_NOT_CONFIGURED,
    ACTIVITY_WRITE_FAILED,
    IMPRESSION_EVENT_LABEL,
    record_activity_event,
    record_activity_failure,
)


if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Mapping, Sequence
    from datetime import date


logger = structlog.get_logger(__name__)

__all__ = [
    "DETERMINISTIC_PROPENSITY",
    "EVENTS_TABLE",
    "IMPRESSIONS_TABLE",
    "POLICY_EXPLORE",
    "POLICY_SIMILAR_ARTIST",
    "POLICY_USER_RECOMMENDATIONS_ARTIST",
    "POLICY_USER_RECOMMENDATIONS_MULTI",
    "PRODUCER",
    "SURFACE_RECOMMENDATION",
    "active_purposes",
    "configure",
    "count_unidentified_candidate",
    "ensure_startup_partitions",
    "forget_subject",
    "record_event",
    "record_events",
    "record_impressions",
    "redis_client",
    "reset_caches",
    "stamp_recommendation_impressions",
    "subject_for",
]

# The service that wrote the row. `producer` has no safe default in `common.events`
# precisely so this string is stated by the service that owns the write.
PRODUCER: Final = "catalog-api"

# The two partitioned tables, named the way `activity.ensure_month_partition` expects:
# the bare table name inside the `activity` schema, never a qualified one.
EVENTS_TABLE: Final = "events"
IMPRESSIONS_TABLE: Final = "impressions"

# The one surface this service records impressions against today.
SURFACE_RECOMMENDATION: Final = "recommendation"

# The ranking policy each recommendation surface ran, named and versioned so an offline
# evaluation can tell which decision procedure produced a row. A change to how a surface
# ranks is a new constant, never a redefinition of an old one: the policy id on a stored
# impression is historical data the immutability trigger will not let anyone rewrite.
POLICY_SIMILAR_ARTIST: Final = "similar_artist_weighted_cosine_v1"
POLICY_EXPLORE: Final = "explore_personalized_v1"
POLICY_USER_RECOMMENDATIONS_ARTIST: Final = "user_recommendations_artist_v1"
POLICY_USER_RECOMMENDATIONS_MULTI: Final = "user_recommendations_multi_v1"

# The probability a deterministic top-N policy assigned to choosing a shown item. See the
# module docstring: this is a fact about the policies that exist, not a filler value.
DETERMINISTIC_PROPENSITY: Final = 1.0

# Get-or-create in one round trip. `DO UPDATE ... SET user_id = EXCLUDED.user_id` is what
# makes `RETURNING` fire on the conflicting row as well as on the inserted one, so an
# existing subject costs the same single statement a new one does. The table carries no
# immutability trigger — only `events` and `impressions` do — so the no-op update is free.
_UPSERT_SUBJECT: Final = """
INSERT INTO activity.user_subjects (user_id)
VALUES (%s::uuid)
ON CONFLICT (user_id) DO UPDATE SET user_id = EXCLUDED.user_id
RETURNING subject_id
"""

# The consent snapshot. A purpose is active when it has been granted and not revoked, and
# the same purpose may have been granted more than once over time, so the set is distinct.
_SELECT_ACTIVE_PURPOSES: Final = """
SELECT DISTINCT purpose
FROM activity.consent_grants
WHERE user_id = %s::uuid AND revoked_at IS NULL
"""

# Partition creation is owned by `database-schema` and invoked by the writer before the
# insert, so a write into a month that has no partition creates it rather than landing in
# the default one.
_ENSURE_PARTITION: Final = "SELECT activity.ensure_month_partition(%s, %s)"

# `ON CONFLICT DO NOTHING` is what makes a retried write safe inside the partitioned
# table: the unique key is (occurred_at, idempotency_key), and a second arrival of the
# same logical event is a no-op rather than a duplicate row or a raised error. DO NOTHING
# rather than DO UPDATE because the immutability trigger would reject the update.
_INSERT_EVENT: Final = """
INSERT INTO activity.events (
    event_id, event_type, schema_version, subject_id, session_id,
    occurred_at, recorded_at, producer, consent_purposes,
    model_version, feature_version, idempotency_key, payload
) VALUES (
    %s, %s, %s, %s, %s,
    %s, %s, %s, %s,
    %s, %s, %s, %s::jsonb
)
ON CONFLICT (occurred_at, idempotency_key) DO NOTHING
"""

_INSERT_IMPRESSION: Final = """
INSERT INTO activity.impressions (
    impression_id, subject_id, surface, policy_id, candidate_set_id,
    position, item_id, score, propensity, request_id,
    occurred_at, recorded_at, consent_purposes
) VALUES (
    %s, %s, %s, %s, %s,
    %s, %s, %s, %s, %s,
    %s, %s, %s
)
"""

_pool: Any = None
_redis: Any = None

# The pseudonym is stable for the life of the account, so resolving it once per process
# per user is correct rather than merely cheap. Erasure removes the link row, and the
# process that served the erasure drops its own entry; a peer process holding a stale
# entry would write against a subject id whose rows have already gone, which is why
# `reset_caches` exists and why the erasure path calls `forget_subject`.
_subjects: dict[str, UUID] = {}

# Which (table, month) pairs this process has already asked `ensure_month_partition` for.
# The function is idempotent, so the cache saves a round trip rather than correctness.
_ensured_partitions: set[tuple[str, date]] = set()


def configure(pool: Any, redis: Any = None) -> None:
    """Wire the PostgreSQL pool and Redis client from ``api.api`` startup.

    The recorder itself writes only to PostgreSQL. The Redis handle is held here because
    this module owns the activity plumbing and the erasure procedure's cache closure
    reaches it through :func:`redis_client` rather than growing a second wiring site.
    """
    global _pool, _redis
    _pool = pool
    _redis = redis
    reset_caches()


def redis_client() -> Any:
    """Return the Redis client wired at startup, or ``None`` before configuration."""
    return _redis


def reset_caches() -> None:
    """Drop the per-process subject and partition caches."""
    _subjects.clear()
    _ensured_partitions.clear()


def forget_subject(user_id: str) -> None:
    """Drop one user's cached pseudonym, so a later write re-resolves or re-creates it."""
    _subjects.pop(str(user_id), None)


def _month_of(moment: datetime) -> date:
    """Return the first day of the UTC month ``moment`` falls in."""
    return moment.astimezone(UTC).date().replace(day=1)


def _as_uuid(value: Any) -> UUID | None:
    """Coerce a caller-supplied id to a UUID, or ``None`` when there is none."""
    if value is None or isinstance(value, UUID):
        return value
    return UUID(str(value))


def _validate_payload(event_type: str, payload: Mapping[str, Any]) -> None:
    """Enforce the payload schema's required set and its closed key set.

    Every payload schema in the vendored vocabulary declares ``additionalProperties:
    false``, so an unnamed key is a rejection rather than an ignored extra. Checking the
    two structural rules here keeps the guard on the request path free of a JSON Schema
    implementation — ``jsonschema`` is a development dependency, not a runtime one — and
    the conformance test validates the same payloads against the real schema, so the two
    cannot drift without a failing test.
    """
    schema = payload_schema_for(event_type)
    allowed = frozenset(schema.get("properties", {}))
    for name in schema.get("required", ()):
        if name not in payload:
            raise EventValidationError(f"payload.{name}", "is required")
    for name in sorted(payload):
        if name not in allowed:
            raise EventValidationError(f"payload.{name}", f"is not part of the {event_type} payload")


def _build_event(
    event_type: str,
    subject: UUID,
    purposes: tuple[str, ...],
    payload: Mapping[str, Any] | None,
    *,
    session_id: Any = None,
    model_version: str | None = None,
    feature_version: str | None = None,
    idempotency_key: str | None = None,
    occurred_at: datetime | None = None,
) -> Event | None:
    """Validate and mint one event, or count the rejection and return ``None``."""
    try:
        body = dict(payload or {})
        _validate_payload(event_type, body)
        return new_event(
            event_type=event_type,
            subject_id=subject,
            producer=PRODUCER,
            consent_purposes=purposes,
            idempotency_key=idempotency_key or str(new_id()),
            payload=body,
            occurred_at=occurred_at,
            session_id=_as_uuid(session_id),
            model_version=model_version,
            feature_version=feature_version,
        )
    except (EventValidationError, KeyError, TypeError, ValueError) as exc:
        # KeyError is how the vendored vocabulary reports an event type it does not carry,
        # which is a caller mistake in exactly the way a malformed payload is.
        logger.warning("⚠️ Activity event rejected", event_type=event_type, reason=str(exc))
        record_activity_failure(ACTIVITY_INVALID)
        return None


def _event_parameters(event: Event) -> tuple[Any, ...]:
    """Return one event's row as the parameter tuple :data:`_INSERT_EVENT` expects."""
    row = event.to_row()
    return (
        row["event_id"],
        row["event_type"],
        row["schema_version"],
        row["subject_id"],
        row["session_id"],
        row["occurred_at"],
        row["recorded_at"],
        row["producer"],
        row["consent_purposes"],
        row["model_version"],
        row["feature_version"],
        row["idempotency_key"],
        json.dumps(row["payload"]),
    )


async def subject_for(user_id: str) -> UUID | None:
    """Return the pseudonymous subject for a user, creating the link on first use.

    Events and impressions reference the subject only and never the user id, so the
    behavioural tables can be read and analysed without carrying account identity, and
    erasure removes one row to make the pseudonym unre-associable.

    Never raises: a lookup that fails returns ``None`` and the caller drops the write.
    """
    key = str(user_id)
    cached = _subjects.get(key)
    if cached is not None:
        return cached
    if _pool is None:
        return None
    try:
        async with _pool.connection() as conn, conn.cursor() as cursor:
            await execute_sql(cursor, _UPSERT_SUBJECT, (key,))
            row = await cursor.fetchone()
    except Exception:
        logger.warning("⚠️ Could not resolve an activity subject", exc_info=True)
        return None
    if not row:
        return None
    subject = _as_uuid(row[0])
    if subject is not None:
        _subjects[key] = subject
    return subject


async def active_purposes(user_id: str) -> tuple[str, ...]:
    """Return the consent purposes active for a user, in vocabulary order.

    This is the snapshot written onto the row, which is what makes an old row
    interpretable years later without reconstructing the grant history. A training-time
    reader re-checks the grant table as well; neither check replaces the other.

    Never raises: an unreadable grant table snapshots no purposes, which is the
    conservative reading and leaves the row usable for neither analytics nor training.
    """
    if _pool is None:
        return ()
    try:
        async with _pool.connection() as conn, conn.cursor() as cursor:
            await execute_sql(cursor, _SELECT_ACTIVE_PURPOSES, (str(user_id),))
            rows = await cursor.fetchall()
    except Exception:
        logger.warning("⚠️ Could not read active consent purposes", exc_info=True)
        return ()
    granted = {row[0] for row in rows}
    return tuple(purpose for purpose in consent_purposes() if purpose in granted)


async def _ensure_partition(cursor: Any, table_name: str, moment: datetime) -> None:
    """Ensure the month partition ``moment`` lands in, once per process per month."""
    month = _month_of(moment)
    key = (table_name, month)
    if key in _ensured_partitions:
        return
    await execute_sql(cursor, _ENSURE_PARTITION, (table_name, month))
    _ensured_partitions.add(key)


async def ensure_startup_partitions(now: datetime | None = None) -> None:
    """Ensure this month's and next month's partitions for both tables at startup.

    Doing it once at startup means the first write of a month does not pay for a DDL
    statement, and a month boundary crossed by a long-running process is still covered by
    the per-write ensure.

    Never raises: a service that cannot create partitions still starts, and the default
    partition on both tables means writes land rather than fail.
    """
    if _pool is None:
        return
    moment = now if now is not None else datetime.now(UTC)
    next_month = _month_of(moment) + timedelta(days=32)
    months = (moment, datetime.combine(next_month.replace(day=1), datetime.min.time(), tzinfo=UTC))
    try:
        async with _pool.connection() as conn, conn.cursor() as cursor:
            for table_name in (EVENTS_TABLE, IMPRESSIONS_TABLE):
                for month in months:
                    await _ensure_partition(cursor, table_name, month)
    except Exception:
        logger.warning("⚠️ Could not ensure the activity partitions at startup", exc_info=True)
        return
    logger.info("🗂️ Activity partitions ensured", months=len(months))


async def record_event(
    user_id: str,
    event_type: str,
    payload: Mapping[str, Any] | None = None,
    *,
    session_id: Any = None,
    model_version: str | None = None,
    feature_version: str | None = None,
    idempotency_key: str | None = None,
    occurred_at: datetime | None = None,
) -> None:
    """Record one first-party event. Never raises into the request.

    The signature's first three parameters are positional so this function satisfies
    :data:`api.syncer.EventRecorder` directly, which is how the collection sync emits its
    change events without importing the activity plumbing.

    Args:
        user_id: The account the event belongs to. Pseudonymised before it is stored.
        event_type: A type from the closed version 1 vocabulary.
        payload: The type-specific body, which must conform to that type's payload schema.
        session_id: The session it happened in, when there is one.
        model_version: The model version in play, when there is one.
        feature_version: The feature version in play, when there is one.
        idempotency_key: The key a retried write deduplicates on, unique with
            ``occurred_at``. Defaults to a fresh id, which makes the write safe but not
            deduplicable; an emission point with a natural key should pass it.
        occurred_at: When the thing happened. Defaults to now.
    """
    pool = _pool
    if pool is None:
        record_activity_failure(ACTIVITY_NOT_CONFIGURED)
        return

    subject = await subject_for(user_id)
    if subject is None:
        logger.warning("⚠️ Activity event dropped: no subject", event_type=event_type)
        record_activity_failure(ACTIVITY_NO_SUBJECT)
        return

    event = _build_event(
        event_type,
        subject,
        await active_purposes(user_id),
        payload,
        session_id=session_id,
        model_version=model_version,
        feature_version=feature_version,
        idempotency_key=idempotency_key,
        occurred_at=occurred_at,
    )
    if event is None:
        return

    try:
        async with pool.connection() as conn, conn.cursor() as cursor:
            await _ensure_partition(cursor, EVENTS_TABLE, event.occurred_at)
            await execute_sql(cursor, _INSERT_EVENT, _event_parameters(event))
    except Exception:
        logger.warning("⚠️ Activity event write failed", event_type=event_type, exc_info=True)
        record_activity_failure(ACTIVITY_WRITE_FAILED)
        return

    record_activity_event(event_type)


async def record_events(user_id: str, events: Sequence[tuple[str, Mapping[str, Any], str | None]]) -> None:
    """Record a batch of events in one round trip. Never raises into the request.

    A search page emits one ``search.result_impression`` per hit shown, so the batch is
    what keeps a twenty-result page from costing twenty round trips on the request path.
    The subject and the consent snapshot are resolved once for the whole batch, which is
    correct as well as cheaper: every row of one batch describes one moment.

    An event that fails validation is dropped and counted; the rest of the batch is still
    written, because one malformed payload is not a reason to lose the page.

    Args:
        user_id: The account every event in the batch belongs to.
        events: ``(event_type, payload, idempotency_key)`` per event. A ``None`` key mints
            a fresh one.
    """
    pool = _pool
    if pool is None:
        record_activity_failure(ACTIVITY_NOT_CONFIGURED)
        return
    if not events:
        return

    subject = await subject_for(user_id)
    if subject is None:
        logger.warning("⚠️ Activity events dropped: no subject", count=len(events))
        record_activity_failure(ACTIVITY_NO_SUBJECT)
        return

    purposes = await active_purposes(user_id)
    moment = datetime.now(UTC)
    built = [
        event
        for event_type, payload, idempotency_key in events
        if (event := _build_event(event_type, subject, purposes, payload, idempotency_key=idempotency_key, occurred_at=moment)) is not None
    ]
    if not built:
        return

    try:
        async with pool.connection() as conn, conn.cursor() as cursor:
            await _ensure_partition(cursor, EVENTS_TABLE, moment)
            await cursor.executemany(_INSERT_EVENT, [_event_parameters(event) for event in built])
    except Exception:
        logger.warning("⚠️ Activity event batch write failed", count=len(built), exc_info=True)
        record_activity_failure(ACTIVITY_WRITE_FAILED)
        return

    for event in built:
        record_activity_event(event.event_type)


async def record_impressions(
    user_id: str,
    surface: str,
    policy_id: str,
    candidate_set_id: Any,
    items: Sequence[tuple[int, Any, float, float | None]],
    *,
    request_id: Any = None,
    occurred_at: datetime | None = None,
) -> list[str | None]:
    """Record one ranked list as it was shown. Never raises into the request.

    Args:
        user_id: The account the list was shown to.
        surface: A surface from the vendored vocabulary.
        policy_id: The ranking policy that made the decision.
        candidate_set_id: The candidate set the decision was made over, one per request.
        items: ``(position, item_id, score, propensity)`` per shown item. ``position`` is
            one-based, ``item_id`` is the ADR 0009 native id, and a ``propensity`` of
            ``None`` means the deterministic :data:`DETERMINISTIC_PROPENSITY`.
        request_id: The request the list was built for. Defaults to a fresh id.
        occurred_at: When the list was shown. Defaults to now.

    Returns:
        One impression id per input item, in input order, so the caller can stamp each
        returned item with the id a client reports outcomes against. An entry is ``None``
        when that item was not recorded, and every entry is ``None`` when the batch was
        not written, so a client can never report an outcome against a row that does not
        exist.
    """
    dropped: list[str | None] = [None] * len(items)
    pool = _pool
    if pool is None:
        record_activity_failure(ACTIVITY_NOT_CONFIGURED)
        return dropped
    if not items:
        return dropped

    subject = await subject_for(user_id)
    if subject is None:
        logger.warning("⚠️ Impressions dropped: no subject", policy_id=policy_id)
        record_activity_failure(ACTIVITY_NO_SUBJECT)
        return dropped

    purposes = await active_purposes(user_id)
    candidate_set = _as_uuid(candidate_set_id) or new_id()
    request = _as_uuid(request_id) or new_id()
    moment = occurred_at if occurred_at is not None else datetime.now(UTC)

    impressions = []
    identifiers: list[str | None] = []
    for position, item_id, score, propensity in items:
        try:
            # An item with no native id has no impression: the published schema requires
            # `item_id` and a caller that reached here with none is rejected, counted, and
            # skipped like any other invalid item.
            native_item_id = _as_uuid(item_id)
            if native_item_id is None:
                raise EventValidationError("item_id", "is required")
            impression = new_impression(
                subject_id=subject,
                surface=surface,
                policy_id=policy_id,
                candidate_set_id=candidate_set,
                position=position,
                item_id=native_item_id,
                score=float(score),
                propensity=DETERMINISTIC_PROPENSITY if propensity is None else propensity,
                request_id=request,
                consent_purposes=purposes,
                occurred_at=moment,
            )
        except (EventValidationError, TypeError, ValueError) as exc:
            logger.warning("⚠️ Impression rejected", policy_id=policy_id, position=position, reason=str(exc))
            record_activity_failure(ACTIVITY_INVALID)
            identifiers.append(None)
            continue
        impressions.append(impression)
        identifiers.append(str(impression.impression_id))

    if not impressions:
        return dropped

    rows = [impression.to_row() for impression in impressions]
    try:
        async with pool.connection() as conn, conn.cursor() as cursor:
            await _ensure_partition(cursor, IMPRESSIONS_TABLE, moment)
            await cursor.executemany(
                _INSERT_IMPRESSION,
                [
                    (
                        row["impression_id"],
                        row["subject_id"],
                        row["surface"],
                        row["policy_id"],
                        row["candidate_set_id"],
                        row["position"],
                        row["item_id"],
                        row["score"],
                        row["propensity"],
                        row["request_id"],
                        row["occurred_at"],
                        row["recorded_at"],
                        row["consent_purposes"],
                    )
                    for row in rows
                ],
            )
    except Exception:
        logger.warning("⚠️ Impression write failed", policy_id=policy_id, count=len(rows), exc_info=True)
        record_activity_failure(ACTIVITY_WRITE_FAILED)
        return dropped

    for _ in rows:
        record_activity_event(IMPRESSION_EVENT_LABEL)
    return identifiers


async def stamp_recommendation_impressions(
    user_id: str,
    policy_id: str,
    items: Sequence[dict[str, Any]],
    *,
    score_key: str = "score",
) -> None:
    """Record one impression per served candidate and stamp each item with its id.

    This runs per request served rather than per candidate list computed. Two of the three
    recommendation surfaces cache their response body in Redis, and an impression is a
    record of a list having been *shown*: reusing the ids from the request that filled the
    cache would report one showing where there were many, and would hand every later
    viewer an id belonging to somebody else's impression. So the cached body never carries
    an ``impression_id``, and the ids are minted here, after the cache is read.

    Every item ends up with an ``impression_id`` key. It is ``None`` when the candidate had
    no native id, or when the write failed, so a client can never report an outcome
    against a row that does not exist.

    Args:
        user_id: The account the list was shown to. An empty id records nothing.
        policy_id: The ranking policy constant for this surface.
        items: The served items, mutated in place. Each needs ``gm_id`` and a score.
        score_key: Which key on an item carries the score the policy gave it.
    """
    for item in items:
        item["impression_id"] = None
    if not user_id or not items:
        return

    entries: list[tuple[int, Any, float, float | None]] = []
    identified: list[dict[str, Any]] = []
    for position, item in enumerate(items, start=1):
        native_id = item.get("gm_id")
        if not native_id:
            count_unidentified_candidate()
            continue
        entries.append((position, native_id, float(item.get(score_key) or 0.0), None))
        identified.append(item)

    if not entries:
        return

    identifiers = await record_impressions(
        user_id,
        SURFACE_RECOMMENDATION,
        policy_id,
        new_id(),
        entries,
    )
    for item, impression_id in zip(identified, identifiers, strict=True):
        item["impression_id"] = impression_id


def count_unidentified_candidate() -> None:
    """Count one candidate that was not recorded because it has no native id.

    ADR 0010 keys an impression on the native identifier from ADR 0009, and the published
    impression schema requires it. The epic design would have logged such a candidate as a
    ``recommendation.shown`` event carrying the provider id instead, but that payload
    schema is closed over ``impression_id``, ``candidate_set_id``, ``policy_id``, and
    ``item_count`` and has nowhere to put one. So the candidate is skipped and counted
    here, which keeps the gap visible without inventing a key the vocabulary does not
    carry.
    """
    record_activity_failure(ACTIVITY_NO_NATIVE_ID)
