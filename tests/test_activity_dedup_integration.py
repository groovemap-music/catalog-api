"""Engine-backed proof that a retried outcome report lands as exactly one event row.

`POST /api/activity/events` keys every outcome on `{event_type}:{impression_id}`, the
natural key of "this kind of outcome happened to this impression", so a client that
retries the report must not grow the record. The proof has to run against the real
schema: `activity.events` is range-partitioned by month on `occurred_at`, so its own unique
key cannot see a retry stamped later, and the deduplication is the schema's
`activity.event_idempotency` claim trigger, keyed (subject_id, idempotency_key) across
every partition. Whether a retry collides is decided by that trigger and the server's
partition routing, not by anything a fake pool can model, and so is what psycopg reports
back for a skipped row — which is what the recorder's written/duplicate counting reads.
Runs through the same `postgres_pool` fixture `tests/test_real_databases.py` uses;
`just test-integration` starts throwaway containers.

The route is driven directly rather than through an ASGI client: the auth dependency is
not under test here, and calling the handler with a resolved `UnifiedAuth` exercises the
exact `record_event` call, idempotency key and insert a real request makes. The clock the
event envelope reads is pinned so a retry can be made to arrive a second, a minute, or a
month after the original, which is the case the partitioned unique key cannot see.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, ClassVar
from uuid import UUID

import common.events
import pytest
import pytest_asyncio
from common.identity import new_id

import api.activity as activity
import api.routers.activity as activity_router
from api.dependencies import UnifiedAuth
from api.models import ActivityOutcomeRequest
from api.routers.activity import record_outcome
from api.telemetry import ACTIVITY_DUPLICATE
from tests.test_real_databases import TEST_USER_ID, postgres_pool


if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from common import AsyncPostgreSQLPool


__all__ = ["postgres_pool"]

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

# Mid-month, so a one-second and a one-minute retry stay in the original's partition and
# only the month case crosses a partition boundary.
ORIGINAL_AT = datetime(2026, 10, 15, 12, 0, 0, tzinfo=UTC)

# `activity.events` carries no foreign key, so the fixture's `TRUNCATE users CASCADE`
# never reaches it; each test empties the behavioural tables itself, before and after, so
# a sibling module asserting an empty `activity.events` is not left reading these rows.
# The key table is named too: TRUNCATE fires no row trigger, so emptying the events alone
# would leave their keys behind.
_RESET_ACTIVITY = "TRUNCATE activity.events, activity.impressions, activity.event_idempotency"


class _PinnedClock:
    """The instant the recorder treats as "now" when a caller passes no ``occurred_at``."""

    current: ClassVar[datetime] = ORIGINAL_AT


_new_event = common.events.new_event


def _new_event_at_pinned_now(**fields: Any) -> common.events.Event:
    """`new_event`, with its "defaults to now" default read from the pinned clock.

    Only the default is pinned: an explicit ``occurred_at`` passes through untouched, and
    everything else — the envelope, its validation, the insert — is the real code path.
    """
    if fields.get("occurred_at") is None:
        fields["occurred_at"] = _PinnedClock.current
    return _new_event(**fields)


@pytest_asyncio.fixture
async def pool(postgres_pool: AsyncPostgreSQLPool, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncPostgreSQLPool]:
    await _reset_activity(postgres_pool)
    _PinnedClock.current = ORIGINAL_AT
    monkeypatch.setattr(activity, "new_event", _new_event_at_pinned_now)
    activity.configure(postgres_pool)
    activity_router.configure(postgres_pool)
    try:
        yield postgres_pool
    finally:
        activity.configure(None)
        activity_router.configure(None)
        await _reset_activity(postgres_pool)


@pytest.fixture
def counted(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """What the recorder counted, in order: an event type per write, `duplicate` per skip."""
    seen: list[str] = []
    monkeypatch.setattr(activity, "record_activity_event", seen.append)
    monkeypatch.setattr(activity, "record_activity_failure", seen.append)
    return seen


async def _reset_activity(pool: AsyncPostgreSQLPool) -> None:
    async with pool.connection() as conn, conn.cursor() as cursor:
        await cursor.execute(_RESET_ACTIVITY)


def _auth() -> UnifiedAuth:
    return UnifiedAuth(user_id=str(TEST_USER_ID), via="jwt", token_id=None, scopes=[])


async def _shown_impression() -> tuple[UUID, UUID]:
    """Record one real recommendation impression and return its id and item id."""
    item_id = new_id()
    [impression_id] = await activity.record_impressions(
        str(TEST_USER_ID),
        activity.SURFACE_RECOMMENDATION,
        activity.POLICY_EXPLORE,
        new_id(),
        [(1, item_id, 0.9, None)],
        occurred_at=ORIGINAL_AT,
    )
    assert impression_id is not None
    return UUID(impression_id), item_id


async def _report(event_type: str, impression_id: UUID, item_id: UUID, at: datetime) -> None:
    _PinnedClock.current = at
    body = ActivityOutcomeRequest(event_type=event_type, impression_id=impression_id, item_id=item_id)
    response = await record_outcome(body, _auth())
    assert response.status_code == 202


async def _event_rows(pool: AsyncPostgreSQLPool, idempotency_key: str) -> list[tuple[Any, ...]]:
    async with pool.connection() as conn, conn.cursor() as cursor:
        await cursor.execute(
            "SELECT tableoid::regclass::text, occurred_at FROM activity.events WHERE idempotency_key = %s ORDER BY occurred_at",
            (idempotency_key,),
        )
        return await cursor.fetchall()


@pytest.mark.parametrize(
    "retry_after",
    [
        pytest.param(timedelta(0), id="same-instant"),
        pytest.param(timedelta(seconds=1), id="later-second"),
        pytest.param(timedelta(minutes=1), id="later-minute"),
        pytest.param(timedelta(days=31), id="later-month-partition"),
    ],
)
async def test_a_retried_outcome_report_is_recorded_once(pool: AsyncPostgreSQLPool, counted: list[str], retry_after: timedelta) -> None:
    impression_id, item_id = await _shown_impression()

    await _report("recommendation.saved", impression_id, item_id, ORIGINAL_AT)
    await _report("recommendation.saved", impression_id, item_id, ORIGINAL_AT + retry_after)

    # One write and one skipped retry, read from what PostgreSQL returned for each insert.
    assert [label for label in counted if label != "impression"] == ["recommendation.saved", ACTIVITY_DUPLICATE]

    rows = await _event_rows(pool, f"recommendation.saved:{impression_id}")
    # Exactly one row, and it is the original: the retry neither adds a row nor moves the
    # recorded occurrence to the retry's arrival time.
    assert [occurred_at for _, occurred_at in rows] == [ORIGINAL_AT]
    assert rows[0][0] == "activity.events_y2026m10"


async def test_concurrent_retries_record_one_row(pool: AsyncPostgreSQLPool) -> None:
    """Two in-flight copies of one report, each stamped a different second, race to one row."""
    impression_id, item_id = await _shown_impression()
    key = f"fit.opened:{impression_id}"
    payload = {"impression_id": str(impression_id), "item_id": str(item_id)}

    await asyncio.gather(
        *(
            activity.record_event(str(TEST_USER_ID), "fit.opened", payload, idempotency_key=key, occurred_at=ORIGINAL_AT + timedelta(seconds=offset))
            for offset in range(4)
        )
    )

    assert len(await _event_rows(pool, key)) == 1


async def test_distinct_outcomes_are_each_recorded(pool: AsyncPostgreSQLPool) -> None:
    """Deduplication is per (outcome kind, impression): anything else is a new fact."""
    first, first_item = await _shown_impression()
    second, second_item = await _shown_impression()

    await _report("recommendation.opened", first, first_item, ORIGINAL_AT)
    await _report("recommendation.saved", first, first_item, ORIGINAL_AT + timedelta(seconds=5))
    await _report("recommendation.opened", second, second_item, ORIGINAL_AT + timedelta(seconds=9))

    for key in (f"recommendation.opened:{first}", f"recommendation.saved:{first}", f"recommendation.opened:{second}"):
        assert len(await _event_rows(pool, key)) == 1


async def test_the_other_writers_on_the_insert_path_keep_recording(pool: AsyncPostgreSQLPool) -> None:
    """The batch writer and the single-event writers with minted keys are unaffected."""
    request_id = str(new_id())
    impressions = [
        (
            "search.result_impression",
            {"impression_id": str(new_id()), "item_id": str(new_id()), "position": position, "request_id": request_id},
            f"search.result_impression:{request_id}:{position}",
        )
        for position in (1, 2, 3)
    ]
    _PinnedClock.current = ORIGINAL_AT
    await activity.record_events(str(TEST_USER_ID), impressions)
    await activity.record_event(
        str(TEST_USER_ID),
        "search.query",
        {"query": "blue note", "filters": [], "result_count": 3, "request_id": request_id},
        idempotency_key=f"search.query:{request_id}",
    )
    # A key minted per call (the recorder's default) never collides with anything.
    await activity.record_event(str(TEST_USER_ID), "consent.granted", {"purpose": "product_analytics"})
    await activity.record_event(str(TEST_USER_ID), "consent.granted", {"purpose": "product_analytics"})

    async with pool.connection() as conn, conn.cursor() as cursor:
        await cursor.execute("SELECT event_type, count(*) FROM activity.events GROUP BY event_type ORDER BY event_type")
        counts = dict(await cursor.fetchall())
    assert counts == {"consent.granted": 2, "search.query": 1, "search.result_impression": 3}


async def test_a_retried_page_counts_each_event_by_its_own_result(pool: AsyncPostgreSQLPool, counted: list[str]) -> None:
    """The batch writer reads one result per statement, through psycopg's real pipeline."""
    request_id = str(new_id())

    def page(positions: tuple[int, ...]) -> list[tuple[str, dict[str, Any], str]]:
        return [
            (
                "search.result_impression",
                {"impression_id": str(new_id()), "item_id": str(new_id()), "position": position, "request_id": request_id},
                f"search.result_impression:{request_id}:{position}",
            )
            for position in positions
        ]

    await activity.record_events(str(TEST_USER_ID), page((1, 2)))
    counted.clear()
    # A month later the client retries the page, which has grown a third result.
    with pytest.MonkeyPatch.context() as later:
        later.setattr(activity, "datetime", _LaterDatetime)
        await activity.record_events(str(TEST_USER_ID), page((1, 2, 3)))

    assert counted == [ACTIVITY_DUPLICATE, ACTIVITY_DUPLICATE, "search.result_impression"]
    async with pool.connection() as conn, conn.cursor() as cursor:
        await cursor.execute("SELECT count(*) FROM activity.events WHERE event_type = 'search.result_impression'")
        assert await cursor.fetchone() == (3,)


class _LaterDatetime(datetime):
    """`record_events` stamps its batch with `datetime.now`; this one is a month on."""

    @classmethod
    def now(cls, tz: Any = None) -> datetime:  # type: ignore[override]
        return datetime.now(tz) + timedelta(days=31)


async def test_erasure_leaves_no_idempotency_key_and_records_how_many_it_removed(pool: AsyncPostgreSQLPool) -> None:
    """The schema releases the keys; the erasure record counts them before it does."""
    for _ in range(3):
        impression_id, item_id = await _shown_impression()
        await _report("recommendation.opened", impression_id, item_id, ORIGINAL_AT)
    subject_id = await activity.subject_for(str(TEST_USER_ID))
    assert subject_id is not None

    async def keys() -> int:
        async with pool.connection() as conn, conn.cursor() as cursor:
            await cursor.execute("SELECT count(*) FROM activity.event_idempotency WHERE subject_id = %s", (subject_id,))
            row = await cursor.fetchone()
        assert row is not None
        return int(row[0])

    assert await keys() == 3

    erasure_id, events_deleted, _impressions_deleted = await activity_router._erase_postgres(str(TEST_USER_ID), subject_id, datetime.now(UTC))
    activity.forget_subject(str(TEST_USER_ID))

    assert events_deleted == 3
    assert await keys() == 0, "no key row survives the erasure"
    async with pool.connection() as conn, conn.cursor() as cursor:
        await cursor.execute("SELECT idempotency_keys_deleted FROM activity.erasures WHERE id = %s", (erasure_id,))
        assert await cursor.fetchone() == (3,)
