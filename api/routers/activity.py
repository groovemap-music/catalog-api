"""The endpoints ADR 0010 puts in front of the activity record.

One client-facing outcome endpoint, the two consent endpoints, and the erasure and export
endpoints all live here because they are one decision's surface: what is recorded, what
the user permitted, and how the user gets it back or has it removed.

Every write here goes through :mod:`api.activity`, which never raises into a request, so a
degraded behavioural record never costs a caller their response.
"""

from __future__ import annotations

import contextlib
import json
import secrets
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Annotated, Any
from uuid import UUID

import structlog
from common.events import consent_purposes
from common.identity import new_id
from common.query_debug import execute_sql
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse, StreamingResponse
from psycopg.rows import dict_row

import api.activity as activity
from api.auth import _hash_password, _verify_password, decrypt_totp_secret, get_totp_encryption_key, verify_totp_code
from api.cache import RecommendCache
from api.dependencies import UnifiedAuth, require_user, require_user_or_app_token
from api.models import ActivityOutcomeRequest, ConsentUpdateRequest, ErasureRequest
from api.snapshot_store import SnapshotStore


if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import AsyncIterator

    from api.config import ApiConfig


logger = structlog.get_logger(__name__)

router = APIRouter()

_pool: Any = None
_redis: Any = None
_neo4j_driver: Any = None
_config: ApiConfig | None = None


def configure(pool: Any, redis: Any = None, neo4j: Any = None, config: ApiConfig | None = None) -> None:
    """Wire the three stores and the config the activity endpoints reach, from startup.

    The config is here for one reason: erasure re-authenticates with a TOTP code when the
    account has 2FA enabled, and decrypting the stored secret needs the master key.
    """
    global _pool, _redis, _neo4j_driver, _config
    _pool = pool
    _redis = redis
    _neo4j_driver = neo4j
    _config = config


def _caller_id(current_user: dict[str, Any]) -> str:
    user_id: str = current_user.get("sub", "")
    return user_id


@router.post("/api/activity/events", status_code=status.HTTP_202_ACCEPTED)
async def record_outcome(
    body: ActivityOutcomeRequest,
    auth: Annotated[UnifiedAuth, Depends(require_user_or_app_token(["activity:write"]))],
) -> JSONResponse:
    """Record one client-reported outcome against a recommendation that was shown.

    ADR 0010 keeps outcomes out of the impression row: opened, saved, dismissed, and
    hidden are events carrying the ``impression_id``, which is what lets one impression
    accrue several outcomes over time while the row itself stays immutable.

    The body carries ``item_id`` as well as ``impression_id`` because the published
    ``impression_outcome`` payload requires both and is closed over them. Reading the item
    back from ``activity.impressions`` instead would mean scanning a table partitioned by
    occurrence time for a key that does not carry the partition column.

    Any type outside the client-reportable outcomes — the four for `recommendation` and
    the four for `fit` — is rejected by the request model as a 422, so a client can never
    reach the recorder with a type from elsewhere in the vocabulary.

    Returns 202: the row is written before the response, but the caller is being told the
    outcome was accepted, not that an analysis has seen it.

    A delegated agent reports outcomes with an ``activity:write`` app token instead of a
    session, and the recorder sees the token owner's id — the same id the owner's own
    session would carry — so an outcome reported on the user's behalf is the user's
    outcome, indistinguishable in the record from one they reported themselves.
    """
    user_id = auth.user_id
    impression_id = str(body.impression_id)

    await activity.record_event(
        user_id,
        body.event_type,
        {"impression_id": impression_id, "item_id": str(body.item_id)},
        # One outcome of one kind against one impression is the same fact however many
        # times a client reports it, so the natural key makes a retry a no-op.
        idempotency_key=f"{body.event_type}:{impression_id}",
    )

    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={"recorded": True, "event_type": body.event_type, "impression_id": impression_id},
    )


# ---------------------------------------------------------------------------
# Consent (ADR 0010)
# ---------------------------------------------------------------------------

EVENT_CONSENT_GRANTED = "consent.granted"
EVENT_CONSENT_REVOKED = "consent.revoked"

# The latest state of each purpose. A revocation sets `revoked_at` rather than deleting
# the grant, so the same purpose accumulates rows over time and the newest one is the
# current answer. DISTINCT ON is what picks it without a self-join.
_SELECT_CONSENT = """
SELECT DISTINCT ON (purpose) purpose, granted_at, revoked_at
FROM activity.consent_grants
WHERE user_id = %s::uuid
ORDER BY purpose, granted_at DESC, id DESC
"""

# A grant is idempotent because it writes only when no active grant exists: the
# `WHERE NOT EXISTS` makes the second identical request a no-op rather than a second row,
# and `RETURNING` is what tells the handler whether anything actually changed.
_INSERT_GRANT = """
INSERT INTO activity.consent_grants (user_id, purpose)
SELECT %s::uuid, %s
WHERE NOT EXISTS (
    SELECT 1 FROM activity.consent_grants
    WHERE user_id = %s::uuid AND purpose = %s AND revoked_at IS NULL
)
RETURNING id, granted_at, revoked_at
"""

# A revocation closes every active grant for the purpose, which keeps the state
# single-valued even if two concurrent grants ever raced past the guard above.
_REVOKE_GRANT = """
UPDATE activity.consent_grants
SET revoked_at = NOW()
WHERE user_id = %s::uuid AND purpose = %s AND revoked_at IS NULL
RETURNING id
"""


def _isoformat(value: Any) -> str | None:
    """Best-effort ISO-8601 stringification for timestamps, preserving null."""
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return str(value.isoformat())
    return str(value)


def _require_pool() -> Any:
    if _pool is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Service not ready")
    return _pool


def _validated_purpose(purpose: str) -> str:
    """Reject a purpose outside the two the published vocabulary carries.

    422 rather than 404: the path names a purpose the caller may grant, and an unknown one
    is an unprocessable request rather than a missing resource.
    """
    if purpose not in consent_purposes():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Unknown purpose {purpose!r}; must be one of: {', '.join(consent_purposes())}",
        )
    return purpose


def _consent_state(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Render both purposes, in vocabulary order, whether or not either has a row.

    A purpose nobody has ever acted on is reported as not granted rather than omitted, so
    a client renders the same two controls before and after the first decision.
    """
    latest = {row["purpose"]: row for row in rows}
    state = []
    for purpose in consent_purposes():
        row = latest.get(purpose)
        granted = row is not None and row["revoked_at"] is None
        state.append(
            {
                "purpose": purpose,
                "granted": granted,
                "granted_at": _isoformat(row["granted_at"]) if row else None,
                "revoked_at": _isoformat(row["revoked_at"]) if row else None,
            }
        )
    return state


@router.get("/api/user/consent")
async def get_consent(auth: Annotated[UnifiedAuth, Depends(require_user_or_app_token(["consent:read"]))]) -> JSONResponse:
    """Return both consent purposes with their current grant and revocation times.

    Readable with a ``consent:read`` app token: an agent that writes on a user's behalf
    has to be able to see what the user permitted before it writes.
    """
    user_id = auth.user_id
    pool = _require_pool()

    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await execute_sql(cur, _SELECT_CONSENT, (user_id,))
        rows = await cur.fetchall()

    return JSONResponse(content={"purposes": _consent_state(list(rows))})


@router.put("/api/user/consent/{purpose}")
async def set_consent(
    purpose: str,
    body: ConsentUpdateRequest,
    auth: Annotated[UnifiedAuth, Depends(require_user_or_app_token(["consent:write"]))],
) -> JSONResponse:
    """Grant or revoke consent for one purpose.

    Idempotent in both directions: granting what is already granted and revoking what is
    already revoked both succeed and change nothing. The event is emitted only when the
    state actually changed, because ADR 0010 makes each change itself an event and a
    repeated request is not a second decision.
    """
    user_id = auth.user_id
    purpose = _validated_purpose(purpose)
    pool = _require_pool()

    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        if body.granted:
            await execute_sql(cur, _INSERT_GRANT, (user_id, purpose, user_id, purpose))
        else:
            await execute_sql(cur, _REVOKE_GRANT, (user_id, purpose))
        changed = await cur.fetchone() is not None

    if changed:
        event_type = EVENT_CONSENT_GRANTED if body.granted else EVENT_CONSENT_REVOKED
        await activity.record_event(
            user_id,
            event_type,
            {"purpose": purpose},
            idempotency_key=f"{event_type}:{purpose}:{datetime.now(UTC).isoformat()}",
        )
        # `via` names the auth path, never the credential: an app token's plaintext is
        # not in the process and its id is not the fact an operator reading a consent
        # change needs — whether the decision came from a session or a delegate is.
        logger.info("🔏 Consent updated", purpose=purpose, granted=body.granted, via=auth.via)

    return JSONResponse(content={"purpose": purpose, "granted": body.granted, "changed": changed})


# ---------------------------------------------------------------------------
# Erasure and export (ADR 0010)
# ---------------------------------------------------------------------------

EVENT_ERASURE_REQUESTED = "account.erasure_requested"
EVENT_EXPORT_REQUESTED = "account.export_requested"

# The bypass. The immutability trigger on activity.events and activity.impressions rejects
# every UPDATE and DELETE unless this session setting is on, and SET LOCAL is what scopes
# it to this transaction: it cannot leak to the next statement on a pooled connection.
_ENABLE_ERASURE = "SET LOCAL groovemap.erasure = 'on'"

_DELETE_EVENTS = "DELETE FROM activity.events WHERE subject_id = %s"
_DELETE_IMPRESSIONS = "DELETE FROM activity.impressions WHERE subject_id = %s"
_DELETE_SUBJECT = "DELETE FROM activity.user_subjects WHERE user_id = %s::uuid"

# `model_versions_before` is the honest part of the record: a model trained before an
# erasure is not retrained by deleting rows. It is empty until a model registry exists to
# name the versions, which is what ADR 0010 defers rather than answers.
_INSERT_ERASURE = """
INSERT INTO activity.erasures (
    subject_id, requested_at, completed_at, events_deleted, impressions_deleted, model_versions_before
) VALUES (%s, %s, NOW(), %s, %s, %s)
RETURNING id
"""

# Every table keyed to the user, in dependency order: observations reference owned copies,
# owned copies reference collection rows, so the leaves go first and no statement is left
# deleting a row another one still points at. `catalog_item_moves` is the native-id merge's
# ledger (ADR 0009's 2026-09-25 amendment): it records which of the user's copies and
# artifacts a merge re-pointed, so it is personal data and joins the closure. It names rows
# rather than referencing them, so its place in the order is free; it goes first as a leaf.
_USER_OWNED_DELETES: tuple[str, ...] = (
    "DELETE FROM catalog_item_moves WHERE user_id = %s::uuid",
    "DELETE FROM observations WHERE user_id = %s::uuid",
    "DELETE FROM collection_snapshots WHERE user_id = %s::uuid",
    "DELETE FROM owned_copies WHERE user_id = %s::uuid",
    "DELETE FROM user_collections WHERE user_id = %s::uuid",
    "DELETE FROM user_wantlists WHERE user_id = %s::uuid",
    "DELETE FROM sync_history WHERE user_id = %s::uuid",
    "DELETE FROM app_tokens WHERE user_id = %s::uuid",
    "DELETE FROM oauth_tokens WHERE user_id = %s::uuid",
)

# The users row is soft-erased in place rather than deleted, and deliberately so: two
# foreign keys to `users` carry no cascade rule, and erasing in place means erasure needs
# no constraint change and cannot orphan an administrative audit trail that exists for a
# different purpose. The email becomes an opaque marker, the password becomes a fresh
# random value nothing can present, and every 2FA column is cleared.
_SOFT_ERASE_USER = """
UPDATE users
SET email = %s,
    hashed_password = %s,
    is_active = FALSE,
    totp_secret = NULL,
    totp_enabled = FALSE,
    totp_recovery_codes = NULL,
    totp_failed_attempts = 0,
    totp_locked_until = NULL,
    updated_at = NOW()
WHERE id = %s::uuid
"""

_SELECT_CREDENTIALS = "SELECT hashed_password, totp_secret, totp_enabled FROM users WHERE id = %s::uuid"

_DETACH_DELETE_USER = "MATCH (u:User {id: $user_id}) DETACH DELETE u"

# The export, in the stable order ADR 0010 names. The activity halves are keyed by the
# subject; everything after them is keyed by the user.
_EXPORT_EVENTS = """
SELECT event_id, event_type, schema_version, session_id, occurred_at, recorded_at,
       producer, consent_purposes, model_version, feature_version, idempotency_key, payload
FROM activity.events
WHERE subject_id = %s
ORDER BY occurred_at, event_id
"""

_EXPORT_IMPRESSIONS = """
SELECT impression_id, surface, policy_id, candidate_set_id, position, item_id,
       score, propensity, request_id, occurred_at, recorded_at, consent_purposes
FROM activity.impressions
WHERE subject_id = %s
ORDER BY occurred_at, impression_id
"""

_EXPORT_COLLECTION = """
SELECT id, release_id, instance_id, folder_id, title, artist, year, label, rating,
       notes, date_added, gm_item_id, owned_copy_id, created_at, updated_at
FROM user_collections
WHERE user_id = %s::uuid
ORDER BY id
"""

_EXPORT_WANTLIST = """
SELECT id, release_id, title, artist, year, format, rating, notes, date_added,
       gm_item_id, created_at, updated_at
FROM user_wantlists
WHERE user_id = %s::uuid
ORDER BY id
"""

_EXPORT_OWNED_COPIES = """
SELECT id, artifact_id, item_id, collection_row_id, acquired_at, created_at, updated_at
FROM owned_copies
WHERE user_id = %s::uuid
ORDER BY id
"""

_EXPORT_OBSERVATIONS = """
SELECT id, owned_copy_id, artifact_id, kind, value, source, confidence, observed_at, created_at
FROM observations
WHERE user_id = %s::uuid
ORDER BY id
"""

# The merge ledger: each row a catalog repair re-pointed from one catalog item to another,
# with the supersession that moved it. Item ids are exported as recorded, like the
# impressions' (ADR 0009's 2026-09-25 amendment, section 4).
_EXPORT_CATALOG_ITEM_MOVES = """
SELECT id, supersession_id, table_name, row_id, from_item_id, to_item_id, moved_at
FROM catalog_item_moves
WHERE user_id = %s::uuid
ORDER BY moved_at, id
"""

# Snapshot ids only. A snapshot's `copy_ids` array repeats copies exported in full one
# line above, so the id and its shape are what the export owes and the array is not.
_EXPORT_SNAPSHOTS = """
SELECT id, taken_at, item_count
FROM collection_snapshots
WHERE user_id = %s::uuid
ORDER BY taken_at, id
"""

_EXPORT_CONSENT = """
SELECT id, purpose, granted_at, revoked_at
FROM activity.consent_grants
WHERE user_id = %s::uuid
ORDER BY granted_at, id
"""

# kind → (statement, keyed-by-subject) in the order the export streams them.
_EXPORT_SECTIONS: tuple[tuple[str, str, bool], ...] = (
    ("event", _EXPORT_EVENTS, True),
    ("impression", _EXPORT_IMPRESSIONS, True),
    ("collection_item", _EXPORT_COLLECTION, False),
    ("wantlist_item", _EXPORT_WANTLIST, False),
    ("owned_copy", _EXPORT_OWNED_COPIES, False),
    ("observation", _EXPORT_OBSERVATIONS, False),
    ("catalog_item_move", _EXPORT_CATALOG_ITEM_MOVES, False),
    ("collection_snapshot", _EXPORT_SNAPSHOTS, False),
    ("consent_grant", _EXPORT_CONSENT, False),
)

_ERASED_EMAIL_DOMAIN = "invalid.groovemap"


def _jsonable(value: Any) -> Any:
    """Render one column value as JSON, preserving null and flattening ids and times."""
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, list | tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, Decimal):
        return float(value)
    if hasattr(value, "isoformat"):
        return str(value.isoformat())
    return str(value)


async def _verify_erasure_credentials(user_id: str, body: ErasureRequest) -> None:
    """Re-authenticate the caller before an irreversible, cross-store deletion.

    The password always, and the TOTP code as well when the account has 2FA enabled, which
    is the same pair `POST /api/auth/2fa/disable` requires for the same reason: a bearer
    token that leaked should not be enough to destroy an account.
    """
    pool = _require_pool()
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await execute_sql(cur, _SELECT_CREDENTIALS, (user_id,))
        user = await cur.fetchone()

    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    if not _verify_password(body.password, user["hashed_password"]):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Incorrect password")

    if not user.get("totp_enabled") or not user.get("totp_secret"):
        return

    if not body.code:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="TOTP code is required while 2FA is enabled")
    totp_key = get_totp_encryption_key(_config.encryption_master_key if _config else None)
    if not totp_key:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Encryption not configured")
    if not verify_totp_code(decrypt_totp_secret(user["totp_secret"], totp_key), body.code):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid TOTP code")


async def _erase_postgres(user_id: str, subject_id: UUID, requested_at: datetime) -> tuple[str, int, int]:
    """Run the whole relational half of the erasure in one transaction.

    One transaction is the point rather than a convenience. The bypass setting is SET LOCAL
    inside it, so it covers exactly these statements and nothing that runs on the
    connection afterwards, and the erasure record is written under the same commit as the
    deletions it counts — a record of an erasure that did not happen would be worse than
    no record.
    """
    pool = _require_pool()
    async with pool.connection() as conn, conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        await execute_sql(cur, _ENABLE_ERASURE, ())

        await execute_sql(cur, _DELETE_EVENTS, (subject_id,))
        events_deleted = cur.rowcount
        await execute_sql(cur, _DELETE_IMPRESSIONS, (subject_id,))
        impressions_deleted = cur.rowcount

        # The pseudonym link goes next, so the subject can never be re-associated with
        # the account even if a row survived somewhere this procedure does not reach.
        await execute_sql(cur, _DELETE_SUBJECT, (user_id,))

        await execute_sql(cur, _INSERT_ERASURE, (subject_id, requested_at, events_deleted, impressions_deleted, []))
        record = await cur.fetchone()

        for statement in _USER_OWNED_DELETES:
            await execute_sql(cur, statement, (user_id,))

        await execute_sql(
            cur,
            _SOFT_ERASE_USER,
            (f"erased+{user_id}@{_ERASED_EMAIL_DOMAIN}", _hash_password(secrets.token_urlsafe(32)), user_id),
        )

    erasure_id = str(record["id"]) if record else ""
    logger.info("🧹 Erasure: PostgreSQL complete", events_deleted=events_deleted, impressions_deleted=impressions_deleted)
    return erasure_id, max(events_deleted, 0), max(impressions_deleted, 0)


async def _erase_neo4j(user_id: str) -> str | None:
    """Remove the user's Neo4j subgraph. Returns a failure description, or None.

    A failure is reported to the caller and logged rather than swallowed: the relational
    half has already committed, and telling somebody their data is gone when one store
    still holds it would be the one lie this endpoint must not tell.
    """
    if _neo4j_driver is None:
        return "Neo4j is not configured; the user node was not removed"
    try:
        async with _neo4j_driver.session() as session:
            await session.run(_DETACH_DELETE_USER, user_id=user_id)
    except Exception as exc:
        logger.error("❌ Erasure: Neo4j step failed", error_type=type(exc).__name__, exc_info=True)
        return f"Neo4j deletion failed: {type(exc).__name__}"
    logger.info("🧹 Erasure: Neo4j complete")
    return None


# The per-user recommendation keys `RecommendCache.invalidate_user` sweeps. They are
# named again here because that helper swallows its own failures — correct for a cache
# invalidation on a request path, and unacceptable for an erasure, which has to be able
# to say whether the keys are actually gone.
_RECOMMENDATION_KEY_PATTERNS: tuple[str, ...] = ("recommend:explore:{user_id}:*", "recommend:enhanced:{user_id}")


async def _surviving_recommendation_keys(redis: Any, user_id: str) -> int:
    """Count the per-user recommendation keys still present after the invalidation."""
    surviving = 0
    for pattern in _RECOMMENDATION_KEY_PATTERNS:
        cursor: str | int = "0"
        while True:
            cursor, keys = await redis.scan(cursor=int(cursor), match=pattern.format(user_id=user_id), count=100)
            surviving += len(keys)
            if str(cursor) == "0":
                break
    return surviving


async def _erase_redis(user_id: str) -> str | None:
    """Delete every per-user Redis key. Returns a failure description, or None.

    ADR 0010 deletes these rather than letting them expire: a 28-day recommendation cache
    outliving an erasure would mean the system still holds something keyed to the user.

    The recommendation sweep goes through ``RecommendCache.invalidate_user``, which is the
    one place that knows those key shapes — but it reports nothing when it fails, because
    a cache invalidation that quietly gives up is the right behaviour on a request path.
    An erasure is not a request path, so the sweep is verified afterwards and a surviving
    key is a reported failure rather than a silent one.
    """
    redis = _redis if _redis is not None else activity.redis_client()
    if redis is None:
        return "Redis is not configured; per-user cache keys were not removed"
    try:
        await RecommendCache(redis=redis).invalidate_user(user_id)
        await redis.delete(
            f"{SnapshotStore._USER_COUNT_KEY_PREFIX}{user_id}",
            f"sync:lock:{user_id}",
            f"sync:cooldown:{user_id}",
        )
        surviving = await _surviving_recommendation_keys(redis, user_id)
    except Exception as exc:
        logger.error("❌ Erasure: Redis step failed", error_type=type(exc).__name__, exc_info=True)
        return f"Redis deletion failed: {type(exc).__name__}"

    if surviving:
        logger.error("❌ Erasure: Redis keys survived the invalidation", surviving=surviving)
        return f"Redis deletion incomplete: {surviving} recommendation key(s) survived"
    logger.info("🧹 Erasure: Redis complete")
    return None


async def _revoke_caller_token(current_user: dict[str, Any]) -> None:
    """Blacklist the caller's own token, the way logout does.

    The account is inactive after the soft-erase, so the token is already worthless at
    every site that re-reads the user; revoking it closes the window before that read.
    """
    redis = _redis if _redis is not None else activity.redis_client()
    jti = current_user.get("jti")
    if redis is None or not jti:
        return
    expires_at = current_user.get("exp")
    now = int(datetime.now(UTC).timestamp())
    ttl = max(expires_at - now, 60) if expires_at else 3600
    with contextlib.suppress(Exception):
        await redis.setex(f"revoked:jti:{jti}", ttl, "1")


@router.post("/api/user/erasure", status_code=status.HTTP_202_ACCEPTED)
async def request_erasure(
    body: ErasureRequest,
    current_user: Annotated[dict[str, Any], Depends(require_user)],
) -> JSONResponse:
    """Erase everything keyed to the caller, across every store.

    The procedure runs in the order ADR 0010 sets out, each step logged with its counts:
    one PostgreSQL transaction under the immutability bypass covering the activity rows,
    the subject link, the erasure record, every user-owned table, and the soft-erase of
    the users row; then the Neo4j subgraph; then the per-user Redis keys; then the
    caller's own token.

    `account.erasure_requested` is emitted *before* the procedure runs, and the procedure
    then deletes it along with every other event for the subject. That is expected and
    intended: the event exists so a concurrent reader sees the request in the stream, and
    the durable record of the erasure is the `activity.erasures` row, which survives.

    Consent does not have to be revoked first; erasure implies it.

    `require_user`, not `require_user_or_app_token`, and no scope exists that would reach
    here: erasure is an account-level right, and a delegated token that could erase the
    account would be a credential the user handed out without meaning to hand that over.
    An app token presented here is not a first-party token and is rejected as a 401.

    Returns 202 with the erasure id. A failed Neo4j or Redis step is reported in
    `incomplete` rather than hidden, because the relational half has already committed.
    """
    user_id = _caller_id(current_user)
    await _verify_erasure_credentials(user_id, body)

    subject_id = await activity.subject_for(user_id)
    if subject_id is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Could not resolve the activity subject")

    requested_at = datetime.now(UTC)
    await activity.record_event(
        user_id,
        EVENT_ERASURE_REQUESTED,
        {"erasure_id": str(new_id())},
        idempotency_key=f"{EVENT_ERASURE_REQUESTED}:{user_id}:{requested_at.isoformat()}",
    )

    erasure_id, events_deleted, impressions_deleted = await _erase_postgres(user_id, subject_id, requested_at)
    # The pseudonym this process cached now names rows that no longer exist.
    activity.forget_subject(user_id)

    incomplete = [failure for failure in (await _erase_neo4j(user_id), await _erase_redis(user_id)) if failure is not None]
    await _revoke_caller_token(current_user)

    logger.info("🧹 Erasure complete", erasure_id=erasure_id, incomplete=len(incomplete))
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={
            "erasure_id": erasure_id,
            "events_deleted": events_deleted,
            "impressions_deleted": impressions_deleted,
            "incomplete": incomplete,
        },
    )


async def _export_lines(user_id: str, subject_id: UUID | None) -> AsyncIterator[str]:
    """Yield one NDJSON line per exported row, in the order ADR 0010 names.

    JSON Lines because the natural unit is a row: the result streams without being
    materialised, and the same file is readable by a person and by a tool. One connection
    spans the whole export so the nine sections are read from one consistent point.
    """
    pool = _require_pool()
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        for kind, statement, by_subject in _EXPORT_SECTIONS:
            if by_subject and subject_id is None:
                continue
            await execute_sql(cur, statement, (subject_id if by_subject else user_id,))
            for row in await cur.fetchall():
                yield json.dumps({"kind": kind, "record": {key: _jsonable(value) for key, value in row.items()}}) + "\n"


@router.get("/api/user/export")
async def export_account(current_user: Annotated[dict[str, Any], Depends(require_user)]) -> StreamingResponse:
    """Stream everything keyed to the caller as application/x-ndjson.

    Sections come in a stable order — events, impressions, collection rows, wantlist rows,
    owned copies, observations, catalog-item moves, snapshot ids, consent grants — so two
    exports of unchanged data are the same file. Snapshots carry their ids and shape only,
    because a snapshot's copy id array repeats copies an earlier section exported in full.
    Catalog-item moves are the native-id merge's ledger rows for the caller's copies and
    artifacts, with item ids as recorded.

    JWT-only for the same reason erasure is: an export is the whole account in one file,
    which is the account holder's right to take and not a delegate's to read. An app token
    presented here is rejected as a 401.
    """
    user_id = _caller_id(current_user)
    _require_pool()

    subject_id = await activity.subject_for(user_id)
    await activity.record_event(
        user_id,
        EVENT_EXPORT_REQUESTED,
        {"export_id": str(new_id()), "format": "jsonl"},
        idempotency_key=f"{EVENT_EXPORT_REQUESTED}:{user_id}:{datetime.now(UTC).isoformat()}",
    )

    return StreamingResponse(
        _export_lines(user_id, subject_id),
        media_type="application/x-ndjson",
        headers={"Content-Disposition": 'attachment; filename="groovemap-export.ndjson"'},
    )
