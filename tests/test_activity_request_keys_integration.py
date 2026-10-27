"""Engine-backed proof that the erasure and export events store no account identity.

`activity.events` carries the pseudonymous subject and never the user id, and the schema's
claim trigger copies every event's idempotency key into `activity.event_idempotency` beside
that subject. A user id anywhere in either row — the key included — would name the
pseudonym's owner in plain text, so these tests read back what PostgreSQL actually stored,
every column of both rows, rather than the string the handler built.

They also prove the key still deduplicates: a second write of one request's event is
skipped by the real trigger, and a second request is recorded as a second event.

The handlers are driven directly, as `tests/test_activity_dedup_integration.py` drives the
outcome route: auth is not under test, and calling `request_erasure` and `export_account`
with a resolved caller exercises the exact `record_event` call, key and insert a real
request makes. Erasure deletes its own request event inside `_erase_postgres`, so that step
is wrapped to read the stored rows first and then run unchanged. Runs through the same
`postgres_pool` fixture `tests/test_real_databases.py` uses; `just test-integration` starts
throwaway containers.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID

import psycopg
import pytest
import pytest_asyncio

import api.activity as activity
import api.routers.activity as activity_router
from api.auth import _hash_password
from api.models import ErasureRequest
from api.routers.activity import EVENT_ERASURE_REQUESTED, EVENT_EXPORT_REQUESTED, export_account, request_erasure
from api.telemetry import ACTIVITY_DUPLICATE
from tests.test_real_databases import TEST_USER_ID, postgres_pool


if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from common import AsyncPostgreSQLPool


__all__ = ["postgres_pool"]

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

PASSWORD = "correct horse battery staple"

# The forms a user id could take in a stored string: the canonical hyphenated UUID and its
# bare hex, so a key built from either is caught.
_USER_ID_FORMS = (str(TEST_USER_ID), TEST_USER_ID.hex)

# TRUNCATE fires no row trigger, so the key table is named beside the events it shadows.
_RESET_ACTIVITY = "TRUNCATE activity.events, activity.impressions, activity.event_idempotency"

# Each row whole, as PostgreSQL renders a composite: every column, the key and the payload
# included, so a user id in any of them is in the text.
_EVENT_ROWS = """
SELECT event.event_id, event.idempotency_key, event.payload, event::text
FROM activity.events AS event
WHERE event.event_type = %s
ORDER BY event.occurred_at, event.event_id
"""
_KEY_ROWS = """
SELECT held.event_id, held.idempotency_key, held::text
FROM activity.event_idempotency AS held
ORDER BY held.occurred_at, held.event_id
"""


@pytest_asyncio.fixture
async def pool(postgres_pool: AsyncPostgreSQLPool) -> AsyncIterator[AsyncPostgreSQLPool]:
    await _reset_activity(postgres_pool)
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


def _caller() -> dict[str, Any]:
    return {"sub": str(TEST_USER_ID)}


async def _stored(pool: AsyncPostgreSQLPool, event_type: str) -> tuple[list[tuple[Any, ...]], list[tuple[Any, ...]]]:
    """Return the stored event rows of one type and every stored key row."""
    async with pool.connection() as conn, conn.cursor() as cursor:
        await cursor.execute(_EVENT_ROWS, (event_type,))
        events = await cursor.fetchall()
        await cursor.execute(_KEY_ROWS)
        keys = await cursor.fetchall()
    return events, keys


def _assert_no_user_id(rows: list[tuple[Any, ...]]) -> None:
    for row in rows:
        text = row[-1]
        for form in _USER_ID_FORMS:
            assert form not in text, f"a stored row names the account: {text}"


async def _retry(event_type: str, payload: dict[str, Any], key: str) -> None:
    """Write one request's event again, a minute on, under the key it was stored with."""
    await activity.record_event(str(TEST_USER_ID), event_type, payload, idempotency_key=key, occurred_at=datetime.now(UTC) + timedelta(minutes=1))


async def test_the_stored_export_event_and_its_key_carry_no_user_id(pool: AsyncPostgreSQLPool, counted: list[str]) -> None:
    response = await export_account(_caller())
    assert response.status_code == 200

    events, keys = await _stored(pool, EVENT_EXPORT_REQUESTED)
    [(event_id, key, payload, _row)] = events
    assert key == f"{EVENT_EXPORT_REQUESTED}:{payload['export_id']}", "keyed on the request's own id"
    assert UUID(payload["export_id"]) != TEST_USER_ID
    [(held_event_id, held_key, _held_row)] = keys
    assert (held_event_id, held_key) == (event_id, key), "the claim trigger copied the same key"
    _assert_no_user_id(events)
    _assert_no_user_id(keys)

    # A retried write of the same request's event is skipped by the schema.
    await _retry(EVENT_EXPORT_REQUESTED, payload, key)
    assert counted == [EVENT_EXPORT_REQUESTED, ACTIVITY_DUPLICATE]
    assert len((await _stored(pool, EVENT_EXPORT_REQUESTED))[0]) == 1


async def test_a_second_export_request_is_recorded_as_a_second_event(pool: AsyncPostgreSQLPool) -> None:
    await export_account(_caller())
    await export_account(_caller())

    events, keys = await _stored(pool, EVENT_EXPORT_REQUESTED)
    assert len(events) == 2
    assert len({key for _, key, _, _ in events}) == 2, "each request mints its own key"
    _assert_no_user_id(events)
    _assert_no_user_id(keys)


async def test_the_stored_erasure_event_and_its_key_carry_no_user_id(
    pool: AsyncPostgreSQLPool, counted: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    async with pool.connection() as conn, conn.cursor() as cursor:
        await cursor.execute("UPDATE users SET hashed_password = %s WHERE id = %s", (_hash_password(PASSWORD), TEST_USER_ID))

    # The procedure deletes the request event it follows, so what the request stored is
    # read between the emission and the deletion; the deletion itself runs unchanged.
    seen: dict[str, Any] = {}
    erase_postgres = activity_router._erase_postgres

    async def read_then_erase(user_id: str, subject_id: UUID, requested_at: datetime) -> tuple[str, int, int]:
        seen["events"], seen["keys"] = await _stored(pool, EVENT_ERASURE_REQUESTED)
        [(_event_id, key, payload, _row)] = seen["events"]
        await _retry(EVENT_ERASURE_REQUESTED, payload, key)
        seen["after_retry"] = len((await _stored(pool, EVENT_ERASURE_REQUESTED))[0])
        return await erase_postgres(user_id, subject_id, requested_at)

    monkeypatch.setattr(activity_router, "_erase_postgres", read_then_erase)

    response = await request_erasure(ErasureRequest(password=PASSWORD), _caller())
    assert response.status_code == 202

    [(event_id, key, payload, _row)] = seen["events"]
    assert key == f"{EVENT_ERASURE_REQUESTED}:{payload['erasure_id']}", "keyed on the request's own id"
    [(held_event_id, held_key, _held_row)] = seen["keys"]
    assert (held_event_id, held_key) == (event_id, key), "the claim trigger copied the same key"
    _assert_no_user_id(seen["events"])
    _assert_no_user_id(seen["keys"])

    # The retry under the same key was skipped while the subject link still stood.
    assert counted == [EVENT_ERASURE_REQUESTED, ACTIVITY_DUPLICATE]
    assert seen["after_retry"] == 1

    # And the procedure removed the request event and its key along with the rest.
    events, keys = await _stored(pool, EVENT_ERASURE_REQUESTED)
    assert events == []
    assert keys == []


async def test_rows_keyed_on_the_user_id_before_the_fix_persist_until_erasure_removes_them(
    pool: AsyncPostgreSQLPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What happens to an event an earlier build stored under `{type}:{user_id}:{ts}`.

    Nothing rewrites it in place: the immutability trigger rejects the UPDATE outside the
    erasure bypass, and so does the key table while the event is stored. It stays, user id
    and all, until the account's erasure deletes every event for the subject, which the
    release triggers follow by removing its key.
    """
    async with pool.connection() as conn, conn.cursor() as cursor:
        await cursor.execute("UPDATE users SET hashed_password = %s WHERE id = %s", (_hash_password(PASSWORD), TEST_USER_ID))
    legacy_key = f"{EVENT_EXPORT_REQUESTED}:{TEST_USER_ID}:{datetime(2026, 10, 1, tzinfo=UTC).isoformat()}"
    await activity.record_event(
        str(TEST_USER_ID), EVENT_EXPORT_REQUESTED, {"export_id": str(UUID(int=1)), "format": "jsonl"}, idempotency_key=legacy_key
    )
    events, keys = await _stored(pool, EVENT_EXPORT_REQUESTED)
    assert [key for _, key, _, _ in events] == [legacy_key]
    assert [key for _, key, _ in keys] == [legacy_key]

    for statement in (
        "UPDATE activity.events SET idempotency_key = 'rewritten' WHERE idempotency_key = %s",
        "UPDATE activity.event_idempotency SET idempotency_key = 'rewritten' WHERE idempotency_key = %s",
    ):
        with pytest.raises(psycopg.errors.RestrictViolation):
            async with pool.connection() as conn, conn.cursor() as cursor:
                await cursor.execute(statement, (legacy_key,))

    monkeypatch.setattr(activity_router, "_erase_neo4j", _no_failure)
    monkeypatch.setattr(activity_router, "_erase_valkey", _no_failure)
    response = await request_erasure(ErasureRequest(password=PASSWORD), _caller())
    assert response.status_code == 202

    async with pool.connection() as conn, conn.cursor() as cursor:
        await cursor.execute("SELECT count(*) FROM activity.events WHERE idempotency_key LIKE %s", (f"%{TEST_USER_ID}%",))
        assert await cursor.fetchone() == (0,)
        await cursor.execute("SELECT count(*) FROM activity.event_idempotency WHERE idempotency_key LIKE %s", (f"%{TEST_USER_ID}%",))
        assert await cursor.fetchone() == (0,)


async def _no_failure(_user_id: str) -> None:
    return None
