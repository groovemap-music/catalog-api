"""Discogs collection and wantlist sync logic.

Handles paginated fetching from the Discogs API and upserting data into
PostgreSQL (user_collections, user_wantlists) and Neo4j (COLLECTED, WANTS
relationships on existing Release nodes).

Key Discogs API gotchas:
- Collection: response key is 'releases', release ID at item['basic_information']['id']
- Wantlist:   response key is 'wants',    release ID at item['id']
"""

import asyncio
import hashlib
import json
import os
import time
import urllib.parse
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import Any, Final
from uuid import UUID

import httpx
import structlog
from common import AsyncPostgreSQLPool, AsyncResilientNeo4jDriver
from common.identity import AliasRef, resolve_aliases
from common.media import map_discogs_formats
from common.query_debug import execute_sql, log_cypher_query
from psycopg.rows import dict_row

from api.auth import decrypt_oauth_token
from api.cache import RecommendCache
from api.oauth import _build_oauth_header
from api.oauth import _hmac_sha1_signature as _hmac_sha1
from api.telemetry import SPAN_SYNC, api_span, record_sync_duration, timer


logger = structlog.get_logger(__name__)

DISCOGS_API_BASE = "https://api.discogs.com"
SYNC_DELAY_SECONDS = 1.0  # 1 req/sec to stay under 60 req/min (groovemap-fnhk)
PAGE_SIZE = 100
MAX_RATE_LIMIT_RETRIES = 5


class DiscogsSyncError(Exception):
    """Raised when a Discogs collection/wantlist sync fails part-way through.

    Non-200 responses and exhausted rate-limit retries must raise (not just
    break the pagination loop) so run_full_sync's except-block records the
    sync as 'failed' with a populated error_message instead of silently
    reporting a partial/zero-item sync as 'completed'.
    """


# ---------------------------------------------------------------------------
# Native identity, owned copies, snapshots, and change events (ADR 0009/0010)
# ---------------------------------------------------------------------------
#
# The sync is the only writer of user_collections and user_wantlists, so it is
# the only place a native catalog item, an owned copy, a collection snapshot,
# or a collection/wantlist change event can originate.

# The recorder api/activity.py wires in at startup. It stays a narrow callable
# rather than a direct import so this module keeps no dependency on the
# activity plumbing, and so a test can observe exactly which events a sync
# emits. The default drops events, which is what keeps every existing caller
# (and every test that never wires one) working unchanged.
EventRecorder = Callable[[str, str, dict[str, Any]], Awaitable[None]]


async def _discard_event(user_id: str, event_type: str, payload: dict[str, Any]) -> None:
    """Default recorder: accept the event and drop it."""


_event_recorder: EventRecorder = _discard_event


def configure(event_recorder: EventRecorder | None = None) -> None:
    """Wire the activity event recorder the sync emits change events through.

    Passing ``None`` restores the no-op default, so a test that configures a
    recorder can put the module back the way it found it.
    """
    global _event_recorder
    _event_recorder = _discard_event if event_recorder is None else event_recorder


# The provider-facing fields a re-sync can legitimately see change on an
# instance the user already holds. Provider metadata corrections (title,
# artist, year, label, media) are not user changes, so they do not raise
# collection.item_updated — the event type means "a user changed what they
# record about an item they hold".
COLLECTION_TRACKED_FIELDS: Final[tuple[str, ...]] = ("folder_id", "rating", "date_added")

# The two page upserts. `COALESCE(EXCLUDED.gm_item_id, ...)` on both so a page
# whose resolve came back short can never blank an id a previous run wrote.
_UPSERT_COLLECTION: Final = """
    INSERT INTO user_collections (
        user_id, release_id, instance_id, folder_id,
        title, artist, year, formats, label,
        rating, date_added, metadata, media, gm_item_id, updated_at
    ) VALUES (
        %s::uuid, %s, %s, %s,
        %s, %s, %s, %s::jsonb, %s,
        %s, %s, %s::jsonb, %s::jsonb, %s::uuid, %s
    )
    ON CONFLICT (user_id, release_id, instance_id) DO UPDATE SET
        folder_id = EXCLUDED.folder_id,
        title = EXCLUDED.title,
        artist = EXCLUDED.artist,
        year = EXCLUDED.year,
        formats = COALESCE(EXCLUDED.formats, user_collections.formats),
        label = EXCLUDED.label,
        rating = EXCLUDED.rating,
        date_added = EXCLUDED.date_added,
        metadata = COALESCE(EXCLUDED.metadata, user_collections.metadata),
        media = EXCLUDED.media,
        gm_item_id = COALESCE(EXCLUDED.gm_item_id, user_collections.gm_item_id),
        updated_at = EXCLUDED.updated_at
"""

_UPSERT_WANTLIST: Final = """
    INSERT INTO user_wantlists (
        user_id, release_id,
        title, artist, year, format,
        rating, notes, date_added, media, gm_item_id, updated_at
    ) VALUES (
        %s::uuid, %s,
        %s, %s, %s, %s,
        %s, %s, %s, %s::jsonb, %s::uuid, %s
    )
    ON CONFLICT (user_id, release_id) DO UPDATE SET
        title = EXCLUDED.title,
        artist = EXCLUDED.artist,
        year = EXCLUDED.year,
        format = EXCLUDED.format,
        rating = EXCLUDED.rating,
        notes = EXCLUDED.notes,
        date_added = EXCLUDED.date_added,
        media = EXCLUDED.media,
        gm_item_id = COALESCE(EXCLUDED.gm_item_id, user_wantlists.gm_item_id),
        updated_at = EXCLUDED.updated_at
"""

# Both the before-state and the after-state of a page are read with this one
# statement, so the diff compares values of identical types straight from
# PostgreSQL rather than a datetime against the ISO string Discogs sent.
_SELECT_COLLECTION_PAGE: Final = """
    SELECT id, release_id, instance_id, gm_item_id, owned_copy_id, folder_id, rating, date_added
    FROM user_collections
    WHERE user_id = %s::uuid AND release_id = ANY(%s::bigint[])
"""

_SELECT_WANTLIST_PAGE: Final = """
    SELECT release_id
    FROM user_wantlists
    WHERE user_id = %s::uuid AND release_id = ANY(%s::bigint[])
"""

# One owned copy per collection row, enforced by the partial unique index on
# `collection_row_id`; ON CONFLICT names that index by repeating its predicate.
# The DO UPDATE's WHERE clause is what makes a re-sync free: a page whose copies
# are already correct writes no rows at all, so re-syncing is idempotent rather
# than merely convergent.
_MINT_OWNED_COPIES: Final = """
    INSERT INTO owned_copies (user_id, item_id, collection_row_id, acquired_at)
    SELECT %s::uuid, minted.item_id, minted.row_id, minted.acquired_at
    FROM unnest(%s::uuid[], %s::uuid[], %s::timestamptz[]) AS minted(item_id, row_id, acquired_at)
    ON CONFLICT (collection_row_id) WHERE collection_row_id IS NOT NULL
    DO UPDATE SET item_id = EXCLUDED.item_id, acquired_at = EXCLUDED.acquired_at, updated_at = NOW()
    WHERE owned_copies.item_id IS DISTINCT FROM EXCLUDED.item_id
       OR owned_copies.acquired_at IS DISTINCT FROM EXCLUDED.acquired_at
"""

# The back-link. `IS DISTINCT FROM` keeps an unchanged row untouched, and the
# RETURNING rows are exactly the links this statement established — the ones it
# skipped were already correct in the page read above.
_LINK_OWNED_COPIES: Final = """
    UPDATE user_collections AS uc
    SET owned_copy_id = oc.id
    FROM owned_copies AS oc
    WHERE oc.collection_row_id = uc.id
      AND uc.id = ANY(%s::uuid[])
      AND uc.owned_copy_id IS DISTINCT FROM oc.id
    RETURNING uc.id, uc.owned_copy_id
"""

# A copy whose collection row was removed keeps existing (the FK nulls
# `collection_row_id` rather than cascading), and drops out of the snapshot
# here — the snapshot is the collection as it stands, not every copy ever held.
# The sweeps return what they removed so the diff that emits
# collection.item_removed / wantlist.item_removed is the delete itself rather
# than a second read of rows that are already gone.
_DELETE_STALE_COLLECTION: Final = """
    DELETE FROM user_collections WHERE user_id = %s::uuid AND updated_at < %s
    RETURNING gm_item_id, owned_copy_id
"""

_DELETE_STALE_WANTLIST: Final = """
    DELETE FROM user_wantlists WHERE user_id = %s::uuid AND updated_at < %s
    RETURNING gm_item_id
"""

_SELECT_SNAPSHOT_COPY_IDS: Final = """
    SELECT id FROM owned_copies
    WHERE user_id = %s::uuid AND collection_row_id IS NOT NULL
    ORDER BY id
"""

_INSERT_COLLECTION_SNAPSHOT: Final = """
    INSERT INTO collection_snapshots (user_id, taken_at, item_count, copy_ids, content_hash)
    VALUES (%s::uuid, %s, %s, %s::uuid[], %s)
"""


def _content_hash(copy_ids: Sequence[UUID]) -> bytes:
    """Hash a snapshot's copy ids: sha256 over the sorted ids, newline separated.

    Newline separated rather than concatenated so the digest cannot collide
    across two different id lists that share a character sequence, and over the
    canonical lower-case hyphenated text of each id so the digest does not
    depend on how psycopg happened to return them.
    """
    return hashlib.sha256("\n".join(str(copy_id) for copy_id in copy_ids).encode("utf-8")).digest()


def _as_text(value: UUID | None) -> str | None:
    """Render a native id for a JSONB event payload."""
    return None if value is None else str(value)


async def _emit_events(user_uuid: UUID, events: Sequence[tuple[str, dict[str, Any]]]) -> None:
    """Hand each change event to the configured recorder.

    Never raises: a sync that wrote its rows has done its job, and losing an
    analytics event must not turn a completed sync into a failed one.
    """
    for event_type, payload in events:
        try:
            await _event_recorder(str(user_uuid), event_type, payload)
        except Exception:
            logger.warning("⚠️ Failed to record sync event", event_type=event_type, exc_info=True)


def _alias_refs(release_ids: Sequence[int]) -> list[AliasRef]:
    """Build the Discogs release refs for one page."""
    return [AliasRef("discogs", "release", str(release_id)) for release_id in release_ids]


def _native_ids_by_release(resolved: dict[AliasRef, UUID]) -> dict[int, UUID]:
    """Key a resolve result back onto the Discogs release ids that produced it."""
    return {int(ref.external_id): native_id for ref, native_id in resolved.items()}


async def _ensure_owned_copies(
    cur: Any,
    user_uuid: UUID,
    page_rows: list[dict[str, Any]],
) -> dict[UUID, UUID]:
    """Ensure one owned copy per collection row on this page, and link it back.

    Returns the owned copy id of every row on the page, whether this call
    created the copy, corrected it, or found it already right.

    A row that resolved to no native id is skipped: `owned_copies.item_id` is
    NOT NULL, so there is no copy to mint for an item with no identity.
    """
    identified = [row for row in page_rows if row["gm_item_id"] is not None]
    if not identified:
        return {}

    await execute_sql(
        cur,
        _MINT_OWNED_COPIES,
        (
            str(user_uuid),
            [row["gm_item_id"] for row in identified],
            [row["id"] for row in identified],
            [row["date_added"] for row in identified],
        ),
    )
    copy_ids: dict[UUID, UUID] = {row["id"]: row["owned_copy_id"] for row in identified if row["owned_copy_id"] is not None}
    await execute_sql(cur, _LINK_OWNED_COPIES, ([row["id"] for row in identified],))
    for linked in await cur.fetchall():
        copy_ids[linked["id"]] = linked["owned_copy_id"]
    return copy_ids


# Payloads are exactly what the vendored event vocabulary declares —
# `collection_item_change` and `collection_item_updated` are
# additionalProperties: false, so the native `item_id` is the only identifier a
# payload carries. A consumer that needs the Discogs release id resolves it
# back through `provider_aliases`; ADR 0009 demotes the provider id to evidence
# precisely so it does not travel on every record that mentions an item.
def _collection_page_events(
    page_keys: Sequence[tuple[int, int | None]],
    before: dict[tuple[int, int | None], dict[str, Any]],
    after: dict[tuple[int, int | None], dict[str, Any]],
    copy_ids: dict[UUID, UUID],
) -> list[tuple[str, dict[str, Any]]]:
    """Diff one page's before-state against its after-state into change events.

    A key absent from the before-state is an addition; a key present in both
    whose tracked fields moved is an update. Removals come from the
    reconciliation sweep, not from here — a page only carries what Discogs
    still holds.
    """
    events: list[tuple[str, dict[str, Any]]] = []
    for key in dict.fromkeys(page_keys):
        row = after.get(key)
        if row is None or row["gm_item_id"] is None:
            continue
        payload: dict[str, Any] = {
            "item_id": _as_text(row["gm_item_id"]),
            "artifact_id": None,
            "owned_copy_id": _as_text(copy_ids.get(row["id"])),
        }
        previous = before.get(key)
        if previous is None:
            events.append(("collection.item_added", payload))
            continue
        changed = [field for field in COLLECTION_TRACKED_FIELDS if previous[field] != row[field]]
        if changed:
            events.append(("collection.item_updated", {**payload, "changed_fields": changed}))
    return events


async def _persist_collection_page(
    user_uuid: UUID,
    pg_pool: AsyncPostgreSQLPool,
    batch_params: list[tuple[Any, ...]],
) -> list[tuple[str, dict[str, Any]]]:
    """Write one fetched collection page and return the change events it caused.

    One transaction per page, on one connection. The pool hands out autocommit
    connections, so the explicit transaction is what makes the page atomic and
    what lets `resolve_aliases` mint inside the same unit of work as the rows
    that reference what it minted — it opens no SAVEPOINT of its own, by
    design, so its failure is this transaction's failure.

    The before-state is read before the upsert and the after-state after it,
    with the same statement, because the upsert is an `executemany` whose
    `RETURNING` rows have no defined correspondence to the input tuples.
    """
    release_ids = [params[1] for params in batch_params]
    page_keys = [(params[1], params[2]) for params in batch_params]

    async with pg_pool.connection() as conn, conn.transaction():
        # One resolve for the whole page: three round trips for a hundred
        # releases rather than a hundred lookups.
        native_ids = _native_ids_by_release(await resolve_aliases(conn, _alias_refs(release_ids)))

        async with conn.cursor(row_factory=dict_row) as cur:
            await execute_sql(cur, _SELECT_COLLECTION_PAGE, (str(user_uuid), release_ids))
            before = {(row["release_id"], row["instance_id"]): row for row in await cur.fetchall()}

            # gm_item_id goes second-to-last so `updated_at` stays the last
            # tuple element the reconciliation cutoff is read from.
            await cur.executemany(
                _UPSERT_COLLECTION,
                [(*params[:-1], native_ids.get(params[1]), params[-1]) for params in batch_params],
            )

            await execute_sql(cur, _SELECT_COLLECTION_PAGE, (str(user_uuid), release_ids))
            after = {(row["release_id"], row["instance_id"]): row for row in await cur.fetchall()}
            copy_ids = await _ensure_owned_copies(cur, user_uuid, [after[key] for key in page_keys if key in after])

    return _collection_page_events(page_keys, before, after, copy_ids)


async def _persist_wantlist_page(
    user_uuid: UUID,
    pg_pool: AsyncPostgreSQLPool,
    batch_params: list[tuple[Any, ...]],
) -> list[tuple[str, dict[str, Any]]]:
    """Write one fetched wantlist page and return the change events it caused.

    Same one-transaction-per-page shape as the collection half, minus the
    owned copy: a wantlist entry is an intention, not a thing the user holds,
    so it has a native item and no copy. Only the before-state is read — the
    wantlist row's identity is the resolve result, not a generated key.
    """
    release_ids = [params[1] for params in batch_params]

    async with pg_pool.connection() as conn, conn.transaction():
        native_ids = _native_ids_by_release(await resolve_aliases(conn, _alias_refs(release_ids)))

        async with conn.cursor(row_factory=dict_row) as cur:
            await execute_sql(cur, _SELECT_WANTLIST_PAGE, (str(user_uuid), release_ids))
            before = {row["release_id"] for row in await cur.fetchall()}

            await cur.executemany(
                _UPSERT_WANTLIST,
                [(*params[:-1], native_ids.get(params[1]), params[-1]) for params in batch_params],
            )

    events: list[tuple[str, dict[str, Any]]] = []
    for release_id in dict.fromkeys(release_ids):
        native_id = native_ids.get(release_id)
        if release_id in before or native_id is None:
            continue
        events.append(("wantlist.item_added", {"item_id": str(native_id)}))
    return events


async def _write_collection_snapshot(
    user_uuid: UUID,
    pg_pool: AsyncPostgreSQLPool,
    taken_at: datetime,
) -> None:
    """Record the collection as this run left it: one row per successful sync.

    Written after reconciliation, so a copy whose collection row this run
    removed is already unlinked and out of the snapshot. `taken_at` is the
    run's own `sync_started` clock rather than a fresh reading, so the snapshot
    is attributable to the run whose rows it describes.
    """
    async with pg_pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
        await execute_sql(cur, _SELECT_SNAPSHOT_COPY_IDS, (str(user_uuid),))
        copy_ids = sorted(row[0] for row in await cur.fetchall())
        await execute_sql(
            cur,
            _INSERT_COLLECTION_SNAPSHOT,
            (str(user_uuid), taken_at, len(copy_ids), copy_ids, _content_hash(copy_ids)),
        )
    logger.info("📸 Collection snapshot recorded", user_id=str(user_uuid), item_count=len(copy_ids))


def _auth_header(
    method: str,
    url: str,
    consumer_key: str,
    consumer_secret: str,
    access_token: str,
    token_secret: str,
    query_params: dict[str, str] | None = None,
) -> str:
    """Build a complete OAuth 1.0a Authorization header for a request.

    Per RFC 5849 section 3.4.1, query parameters present in the request URI
    must be included in the signature base string.
    """
    nonce = os.urandom(16).hex()
    timestamp = str(int(time.time()))

    params = {
        "oauth_consumer_key": consumer_key,
        "oauth_nonce": nonce,
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": timestamp,
        "oauth_token": access_token,
        "oauth_version": "1.0",
    }
    # Merge query params into signature base per RFC 5849
    sig_params = {**params, **(query_params or {})}
    sig = _hmac_sha1(method, url, sig_params, consumer_secret, token_secret)
    params["oauth_signature"] = sig
    return _build_oauth_header(params)


async def sync_collection(
    user_uuid: UUID,
    discogs_username: str,
    consumer_key: str,
    consumer_secret: str,
    access_token: str,
    token_secret: str,
    user_agent: str,
    pg_pool: AsyncPostgreSQLPool,
    neo4j_driver: AsyncResilientNeo4jDriver,
) -> int:
    """Sync user's Discogs collection to PostgreSQL and Neo4j.

    Collection API: GET /users/{username}/collection/folders/0/releases
    Response key: 'releases'
    Release ID: item['basic_information']['id']

    Returns:
        Total number of items synced
    """
    total_synced = 0
    page = 1
    rate_limit_retries = 0
    # Captured once, before the loop — used both as the Neo4j synced_at stamp
    # for every page of this run and as the reconciliation cutoff below, so
    # rows/edges untouched by this run (removed on Discogs, or superseded by
    # a duplicate instance_id) can be identified and deleted.
    sync_started = datetime.now(UTC)

    logger.info("📋 Starting collection sync", user=discogs_username)

    async with httpx.AsyncClient(timeout=30.0) as client:
        while True:
            url = f"{DISCOGS_API_BASE}/users/{discogs_username}/collection/folders/0/releases"
            params = {"page": str(page), "per_page": str(PAGE_SIZE), "sort": "added", "sort_order": "desc"}
            full_url = f"{url}?{urllib.parse.urlencode(params)}"

            auth = _auth_header("GET", url, consumer_key, consumer_secret, access_token, token_secret, query_params=params)
            headers = {
                "Authorization": auth,
                "User-Agent": user_agent,
                "Accept": "application/json",
            }

            response = await client.get(full_url, headers=headers)

            if response.status_code == 429:
                rate_limit_retries += 1
                if rate_limit_retries > MAX_RATE_LIMIT_RETRIES:
                    logger.error("❌ Rate limit retries exhausted for collection sync", user=discogs_username, retries=rate_limit_retries)
                    raise DiscogsSyncError(f"Discogs rate limit retries exhausted for collection sync (user={discogs_username})")
                logger.warning("⚠️ Rate limited by Discogs, waiting 60s...", retry=rate_limit_retries, max_retries=MAX_RATE_LIMIT_RETRIES)
                await asyncio.sleep(60)
                continue
            rate_limit_retries = 0

            if response.status_code != 200:
                logger.error(
                    "❌ Collection API error",
                    status=response.status_code,
                    page=page,
                )
                raise DiscogsSyncError(f"Discogs collection API error: status={response.status_code} page={page} user={discogs_username}")

            data = response.json()
            releases = data.get("releases", [])

            if not releases:
                break

            # Build batch params — skip items without a release_id
            batch_params = []
            for item in releases:
                basic = item.get("basic_information", {})
                release_id = basic.get("id")
                if not release_id:
                    continue

                artists = basic.get("artists", [])
                artist_name = artists[0]["name"] if artists else None
                labels = basic.get("labels", [])
                label_name = labels[0]["name"] if labels else None
                # Per-release metadata bag — only keys with non-null values are
                # included so a missing field never wipes an existing value
                # (in Neo4j, SET k = null deletes the property). Same shape is
                # reused for both `user_collections.metadata` JSONB and the
                # cypher `SET r += rel.metadata` write. Future fields just add
                # a key here.
                #
                # When this run's fetch has no catalog_number (or no formats),
                # metadata_json/formats_json is None — the PostgreSQL upsert below
                # uses COALESCE(EXCLUDED.x, user_collections.x) so a NULL from this
                # run can never overwrite a previously-synced value; Neo4j's `+=`
                # merge already had that property (groovemap-z7d3).
                release_metadata: dict[str, Any] = {}
                if labels and labels[0].get("catno"):
                    release_metadata["catalog_number"] = labels[0]["catno"]
                metadata_json = json.dumps(release_metadata) if release_metadata else None
                formats_raw = basic.get("formats", [])
                formats_json = json.dumps(formats_raw) if formats_raw else None
                # ADR 0007: the canonical media block, computed straight from the raw
                # Discogs API format objects (map_discogs_formats accepts that shape
                # directly — this live sync never passes through a catalog-events
                # producer, so the API is the only place this mapping can happen).
                # map_discogs_formats always returns a full block (never None), so the
                # upsert below can overwrite unconditionally rather than COALESCE.
                media_json = json.dumps(map_discogs_formats(formats_raw))

                batch_params.append(
                    (
                        str(user_uuid),
                        release_id,
                        item.get("instance_id"),
                        item.get("folder_id"),
                        basic.get("title"),
                        artist_name,
                        basic.get("year"),
                        formats_json,
                        label_name,
                        item.get("rating", 0),
                        item.get("date_added"),
                        metadata_json,
                        media_json,
                        # Stamp with the app-host sync_started clock (not PG's NOW()) so
                        # this write and _reconcile_stale_collection's DELETE cutoff share
                        # ONE clock source — mirroring the Neo4j half of this same sync,
                        # which already stamps synced_at=sync_started. A DB-host clock
                        # lagging the API host would otherwise make NOW() land before the
                        # cutoff and _reconcile_stale_collection would delete the row this
                        # sync just wrote (groovemap-vqr0). Kept as the LAST tuple element
                        # so existing `batch_params[i][-1]` assertions stay valid.
                        sync_started,
                    )
                )

            # Upsert to PostgreSQL — one executemany per page instead of N
            # round-trips, inside the page transaction that also resolves the
            # native ids and mints the owned copies (ADR 0009).
            if batch_params:
                page_events = await _persist_collection_page(user_uuid, pg_pool, batch_params)
                total_synced += len(batch_params)
                await _emit_events(user_uuid, page_events)

            # Upsert to Neo4j — ensure User node and COLLECTED relationships.
            # `SET r += rel.metadata` merges only the keys present in the bag —
            # absent keys are untouched, so a sync without catalog_number never
            # wipes a value that the bulk graphinator pipeline may have written.
            cypher = """
            MERGE (u:User {id: $user_id})
            ON CREATE SET u.discogs_username = $discogs_username
            ON MATCH SET u.discogs_username = $discogs_username
            WITH u
            UNWIND $releases AS rel
            MATCH (r:Release {id: toString(rel.release_id)})
            MERGE (u)-[c:COLLECTED {instance_id: rel.instance_id}]->(r)
            SET c.rating = rel.rating,
                c.folder_id = rel.folder_id,
                c.date_added = rel.date_added,
                c.synced_at = $synced_at,
                r += rel.metadata
            """

            def _release_metadata(item: dict[str, Any]) -> dict[str, Any]:
                labels_for_item = item.get("basic_information", {}).get("labels") or []
                first = labels_for_item[0] if labels_for_item else {}
                catno_value = first.get("catno") if isinstance(first, dict) else None
                bag: dict[str, Any] = {}
                if catno_value:
                    bag["catalog_number"] = catno_value
                return bag

            neo4j_releases = [
                {
                    "release_id": item.get("basic_information", {}).get("id"),
                    "instance_id": str(item["instance_id"]) if item.get("instance_id") else None,
                    "rating": item.get("rating", 0),
                    "folder_id": item.get("folder_id"),
                    "date_added": item.get("date_added"),
                    "metadata": _release_metadata(item),
                }
                for item in releases
                if item.get("basic_information", {}).get("id")
            ]

            if neo4j_releases:
                cypher_params: dict[str, Any] = {
                    "user_id": str(user_uuid),
                    "discogs_username": discogs_username,
                    "releases": neo4j_releases,
                    "synced_at": sync_started.isoformat(),
                }
                log_cypher_query(
                    cypher,
                    {
                        "user_id": str(user_uuid),
                        "discogs_username": discogs_username,
                        "releases": f"[{len(neo4j_releases)} items]",
                        "synced_at": "...",
                    },
                )
                async with neo4j_driver.session() as session:
                    result = await session.run(cypher, cypher_params)
                    await result.consume()

            # Check if there are more pages
            pagination = data.get("pagination", {})
            if page >= pagination.get("pages", 1):
                break

            page += 1
            await asyncio.sleep(SYNC_DELAY_SECONDS)

    # Reached only when the loop completed normally (no DiscogsSyncError raised
    # above) — reconcile away rows/edges this run never touched: items removed
    # from Discogs since the last sync, and stale duplicate instance_id rows
    # left behind when an item was removed and re-added.
    removed = await _reconcile_stale_collection(user_uuid, pg_pool, neo4j_driver, sync_started)
    await _emit_events(user_uuid, removed)

    # After reconciliation, so the snapshot is the collection as this run left
    # it rather than as it stood mid-sweep (ADR 0009).
    await _write_collection_snapshot(user_uuid, pg_pool, sync_started)

    logger.info("✅ Collection sync complete", user=discogs_username, total=total_synced)
    return total_synced


async def _reconcile_stale_collection(
    user_uuid: UUID,
    pg_pool: AsyncPostgreSQLPool,
    neo4j_driver: AsyncResilientNeo4jDriver,
    sync_started: datetime,
) -> list[tuple[str, dict[str, Any]]]:
    """Delete collection rows/edges for this user untouched by the current sync run.

    Every row/edge touched by sync_collection gets updated_at=NOW() (PG) /
    synced_at=sync_started (Neo4j). Anything still stamped from before
    sync_started was NOT present in the freshly-fetched Discogs data — either
    the item was removed from the collection, or it was removed-and-re-added
    under a new instance_id (leaving the old instance_id row/edge orphaned).
    Only called after a fully successful pagination run (see sync_collection).

    Returns the collection.item_removed events the sweep earned. The deleted
    row's owned copy is NOT deleted with it — the FK nulls `collection_row_id`
    instead, so the copy and everything observed about it survive a removal —
    which is why the event can still name the copy it left behind.
    """
    async with pg_pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await execute_sql(
            cur,
            _DELETE_STALE_COLLECTION,
            (str(user_uuid), sync_started),
        )
        removed = await cur.fetchall()

    cypher = """
    MATCH (u:User {id: $user_id})-[c:COLLECTED]->()
    WHERE c.synced_at < $sync_started
    DELETE c
    """
    async with neo4j_driver.session() as session:
        result = await session.run(cypher, {"user_id": str(user_uuid), "sync_started": sync_started.isoformat()})
        await result.consume()

    return [
        (
            "collection.item_removed",
            {
                "item_id": _as_text(row["gm_item_id"]),
                "artifact_id": None,
                "owned_copy_id": _as_text(row["owned_copy_id"]),
            },
        )
        for row in removed
        if row["gm_item_id"] is not None
    ]


async def sync_wantlist(
    user_uuid: UUID,
    discogs_username: str,
    consumer_key: str,
    consumer_secret: str,
    access_token: str,
    token_secret: str,
    user_agent: str,
    pg_pool: AsyncPostgreSQLPool,
    neo4j_driver: AsyncResilientNeo4jDriver,
) -> int:
    """Sync user's Discogs wantlist to PostgreSQL and Neo4j.

    Wantlist API: GET /users/{username}/wants
    Response key: 'wants'
    Release ID: item['id']  ← NOTE: top-level, NOT nested in basic_information

    Returns:
        Total number of items synced
    """
    total_synced = 0
    page = 1
    rate_limit_retries = 0
    # Captured once, before the loop — see sync_collection for why.
    sync_started = datetime.now(UTC)

    logger.info("📋 Starting wantlist sync", user=discogs_username)

    async with httpx.AsyncClient(timeout=30.0) as client:
        while True:
            url = f"{DISCOGS_API_BASE}/users/{discogs_username}/wants"
            params = {"page": str(page), "per_page": str(PAGE_SIZE)}
            full_url = f"{url}?{urllib.parse.urlencode(params)}"

            auth = _auth_header("GET", url, consumer_key, consumer_secret, access_token, token_secret, query_params=params)
            headers = {
                "Authorization": auth,
                "User-Agent": user_agent,
                "Accept": "application/json",
            }

            response = await client.get(full_url, headers=headers)

            if response.status_code == 429:
                rate_limit_retries += 1
                if rate_limit_retries > MAX_RATE_LIMIT_RETRIES:
                    logger.error("❌ Rate limit retries exhausted for wantlist sync", user=discogs_username, retries=rate_limit_retries)
                    raise DiscogsSyncError(f"Discogs rate limit retries exhausted for wantlist sync (user={discogs_username})")
                logger.warning("⚠️ Rate limited by Discogs, waiting 60s...", retry=rate_limit_retries, max_retries=MAX_RATE_LIMIT_RETRIES)
                await asyncio.sleep(60)
                continue
            rate_limit_retries = 0

            if response.status_code != 200:
                logger.error(
                    "❌ Wantlist API error",
                    status=response.status_code,
                    page=page,
                )
                raise DiscogsSyncError(f"Discogs wantlist API error: status={response.status_code} page={page} user={discogs_username}")

            data = response.json()
            wants = data.get("wants", [])

            if not wants:
                break

            # Build batch params — CRITICAL: wantlist ID is at item['id'] (top-level),
            # unlike collection where it's at item['basic_information']['id']
            batch_params = []
            for item in wants:
                release_id = item.get("id")
                if not release_id:
                    continue

                basic = item.get("basic_information", {})
                artists = basic.get("artists", [])
                artist_name = artists[0]["name"] if artists else None
                formats = basic.get("formats", [])
                fmt_name = formats[0]["name"] if formats else None
                # ADR 0007: same canonical media block as sync_collection, computed
                # from the same raw Discogs API format objects — the wantlist path
                # previously kept only formats[0]["name"] (fmt_name above, retained
                # for the deprecated `format` column) and lost every other format
                # entry's descriptions.
                media_json = json.dumps(map_discogs_formats(formats))

                batch_params.append(
                    (
                        str(user_uuid),
                        release_id,
                        basic.get("title"),
                        artist_name,
                        basic.get("year"),
                        fmt_name,
                        item.get("rating", 0),
                        item.get("notes"),
                        item.get("date_added"),
                        media_json,
                        # Same single-clock fix as sync_collection: stamp with the
                        # app-host sync_started clock, not PG's NOW() (groovemap-vqr0).
                        # Kept as the LAST tuple element so existing
                        # `batch_params[i][-1]` assertions stay valid.
                        sync_started,
                    )
                )

            # Upsert to PostgreSQL — one executemany per page instead of N
            # round-trips, inside the page transaction that also resolves the
            # native ids (ADR 0009).
            if batch_params:
                page_events = await _persist_wantlist_page(user_uuid, pg_pool, batch_params)
                total_synced += len(batch_params)
                await _emit_events(user_uuid, page_events)

            # Upsert to Neo4j — ensure User node and WANTS relationships.
            # Same metadata-bag pattern as the collection sync: `SET r += w.metadata`
            # merges only the keys present, so missing fields don't wipe values.
            cypher = """
            MERGE (u:User {id: $user_id})
            ON CREATE SET u.discogs_username = $discogs_username
            ON MATCH SET u.discogs_username = $discogs_username
            WITH u
            UNWIND $wants AS w
            MATCH (r:Release {id: toString(w.release_id)})
            MERGE (u)-[wnt:WANTS]->(r)
            SET wnt.rating = w.rating,
                wnt.date_added = w.date_added,
                wnt.synced_at = $synced_at,
                r += w.metadata
            """

            def _want_metadata(item: dict[str, Any]) -> dict[str, Any]:
                labels_for_item = item.get("basic_information", {}).get("labels") or []
                first = labels_for_item[0] if labels_for_item else {}
                catno_value = first.get("catno") if isinstance(first, dict) else None
                bag: dict[str, Any] = {}
                if catno_value:
                    bag["catalog_number"] = catno_value
                return bag

            neo4j_wants = [
                {
                    "release_id": item.get("id"),
                    "rating": item.get("rating", 0),
                    "date_added": item.get("date_added"),
                    "metadata": _want_metadata(item),
                }
                for item in wants
                if item.get("id")
            ]

            if neo4j_wants:
                cypher_params: dict[str, Any] = {
                    "user_id": str(user_uuid),
                    "discogs_username": discogs_username,
                    "wants": neo4j_wants,
                    "synced_at": sync_started.isoformat(),
                }
                log_cypher_query(
                    cypher,
                    {
                        "user_id": str(user_uuid),
                        "discogs_username": discogs_username,
                        "wants": f"[{len(neo4j_wants)} items]",
                        "synced_at": "...",
                    },
                )
                async with neo4j_driver.session() as session:
                    result = await session.run(cypher, cypher_params)
                    await result.consume()

            pagination = data.get("pagination", {})
            if page >= pagination.get("pages", 1):
                break

            page += 1
            await asyncio.sleep(SYNC_DELAY_SECONDS)

    # Reached only when the loop completed normally — reconcile away rows/edges
    # this run never touched (items removed from the wantlist since the last
    # sync). See sync_collection's _reconcile_stale_collection for the same
    # pattern.
    removed = await _reconcile_stale_wantlist(user_uuid, pg_pool, neo4j_driver, sync_started)
    await _emit_events(user_uuid, removed)

    logger.info("✅ Wantlist sync complete", user=discogs_username, total=total_synced)
    return total_synced


async def _reconcile_stale_wantlist(
    user_uuid: UUID,
    pg_pool: AsyncPostgreSQLPool,
    neo4j_driver: AsyncResilientNeo4jDriver,
    sync_started: datetime,
) -> list[tuple[str, dict[str, Any]]]:
    """Delete wantlist rows/edges for this user untouched by the current sync run.

    Same reconciliation pattern as _reconcile_stale_collection: anything still
    stamped from before sync_started was not present in the freshly-fetched
    Discogs wantlist (removed from the wantlist, typically after purchase).
    Only called after a fully successful pagination run (see sync_wantlist).

    Returns the wantlist.item_removed events the sweep earned.
    """
    async with pg_pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await execute_sql(
            cur,
            _DELETE_STALE_WANTLIST,
            (str(user_uuid), sync_started),
        )
        removed = await cur.fetchall()

    cypher = """
    MATCH (u:User {id: $user_id})-[wnt:WANTS]->()
    WHERE wnt.synced_at < $sync_started
    DELETE wnt
    """
    async with neo4j_driver.session() as session:
        result = await session.run(cypher, {"user_id": str(user_uuid), "sync_started": sync_started.isoformat()})
        await result.consume()

    return [("wantlist.item_removed", {"item_id": _as_text(row["gm_item_id"])}) for row in removed if row["gm_item_id"] is not None]


async def run_full_sync(
    user_uuid: UUID,
    sync_id: str,
    pg_pool: AsyncPostgreSQLPool,
    neo4j_driver: AsyncResilientNeo4jDriver,
    discogs_user_agent: str,
    oauth_encryption_key: str | None = None,
    redis_client: Any | None = None,
) -> dict[str, Any]:
    """Run a full collection + wantlist sync for a user, inside the `api.sync` span.

    A sync runs as a detached background task, so this span is the root of its own trace:
    the Discogs calls, the PostgreSQL writes, and the Neo4j writes underneath it all hang
    off it. `record_sync_duration` stamps the terminal outcome on it from the `finally`
    block below, so the span and `groovemap.api.sync.duration` always agree.

    Fetches OAuth credentials from PostgreSQL and app config,
    then syncs collection and wantlist.

    Returns:
        dict with sync results (items_synced, pages_fetched, error)
    """
    with api_span(SPAN_SYNC):
        return await _run_full_sync(user_uuid, sync_id, pg_pool, neo4j_driver, discogs_user_agent, oauth_encryption_key, redis_client)


async def _run_full_sync(
    user_uuid: UUID,
    sync_id: str,
    pg_pool: AsyncPostgreSQLPool,
    neo4j_driver: AsyncResilientNeo4jDriver,
    discogs_user_agent: str,
    oauth_encryption_key: str | None,
    redis_client: Any | None,
) -> dict[str, Any]:
    """Do the sync itself. Split out only so the span above wraps every exit path."""
    error_message = None
    collection_count = 0
    wantlist_count = 0
    cancelled = False
    elapsed = timer()

    try:
        # Fetch OAuth tokens for the user
        async with pg_pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await execute_sql(
                cur,
                """
                    SELECT ot.access_token, ot.access_secret, ot.provider_username
                    FROM oauth_tokens ot
                    WHERE ot.user_id = %s::uuid AND ot.provider = 'discogs'
                    """,
                (str(user_uuid),),
            )
            token = await cur.fetchone()

            if not token:
                raise ValueError("No Discogs OAuth token found for user. Please connect Discogs first.")

            # Decrypt OAuth tokens if encryption key is configured
            access_token_value = decrypt_oauth_token(token["access_token"], oauth_encryption_key)
            access_secret_value = decrypt_oauth_token(token["access_secret"], oauth_encryption_key)

            # Fetch app credentials
            await execute_sql(cur, "SELECT key, value FROM app_config WHERE key IN ('discogs_consumer_key', 'discogs_consumer_secret')")
            config_rows = await cur.fetchall()
            app_config = {row["key"]: row["value"] for row in config_rows}
            for cred_key in ("discogs_consumer_key", "discogs_consumer_secret"):
                if cred_key in app_config:
                    app_config[cred_key] = decrypt_oauth_token(app_config[cred_key], oauth_encryption_key)

        if "discogs_consumer_key" not in app_config or "discogs_consumer_secret" not in app_config:
            raise ValueError("Discogs app credentials not configured in app_config table")

        discogs_username = token["provider_username"]

        # Run collection sync
        collection_count = await sync_collection(
            user_uuid=user_uuid,
            discogs_username=discogs_username,
            consumer_key=app_config["discogs_consumer_key"],
            consumer_secret=app_config["discogs_consumer_secret"],
            access_token=access_token_value,
            token_secret=access_secret_value,
            user_agent=discogs_user_agent,
            pg_pool=pg_pool,
            neo4j_driver=neo4j_driver,
        )

        # Run wantlist sync
        wantlist_count = await sync_wantlist(
            user_uuid=user_uuid,
            discogs_username=discogs_username,
            consumer_key=app_config["discogs_consumer_key"],
            consumer_secret=app_config["discogs_consumer_secret"],
            access_token=access_token_value,
            token_secret=access_secret_value,
            user_agent=discogs_user_agent,
            pg_pool=pg_pool,
            neo4j_driver=neo4j_driver,
        )

    except asyncio.CancelledError:
        # CancelledError is a BaseException, not an Exception — the `except
        # Exception` below never sees it. Shutdown/restart cancels this task
        # via api/api.py's lifespan (task.cancel() + gather(...,
        # return_exceptions=True)), so this path is routinely reachable, not
        # exceptional. Record a terminal status in `finally` below and
        # re-raise so cancellation still propagates normally
        # (groovemap-pxqw).
        cancelled = True
        error_message = "Sync cancelled (service shutdown or restart)"
        raise
    except Exception as exc:
        error_message = str(exc)
        logger.error("❌ Sync failed", user_id=str(user_uuid), error=error_message)
    finally:
        # Invalidate the user-scoped recommendation cache whenever a sync was
        # attempted — not only on full success. sync_collection/sync_wantlist
        # commit each page durably (autocommit executemany + immediate Neo4j
        # writes) as they go, so a failure partway through (e.g. Neo4j hiccups
        # on a later page) can leave the collection/wantlist durably changed
        # even though run_full_sync takes the exception path above. Without
        # this, stale personalized recommendations would keep serving for up
        # to the cache's TTL. invalidate_user() already swallows Redis errors
        # internally, so running it unconditionally here is safe.
        if redis_client is not None:
            rec_cache = RecommendCache(redis=redis_client)
            await rec_cache.invalidate_user(str(user_uuid))
            logger.info("🔄 Recommendation cache invalidated", user_id=str(user_uuid))

        # Update sync_history record — moved INTO `finally` so it runs even
        # when CancelledError is propagating (the previous plain-code
        # placement after the try/except/finally never executed on
        # cancellation, leaving the row stuck at status='running' forever —
        # groovemap-pxqw).
        final_status = "cancelled" if cancelled else ("failed" if error_message else "completed")
        # Recorded before the sync_history write so a failing UPDATE cannot cost the
        # measurement, and inside `finally` so a cancelled run is still counted.
        record_sync_duration(elapsed(), final_status)
        try:
            async with pg_pool.connection() as conn, conn.cursor() as cur:
                await execute_sql(
                    cur,
                    """
                        UPDATE sync_history
                        SET status = %s,
                            items_synced = %s,
                            error_message = %s,
                            completed_at = NOW()
                        WHERE id = %s::uuid
                        """,
                    (final_status, collection_count + wantlist_count, error_message, sync_id),
                )
        except Exception as update_exc:
            logger.error("❌ Failed to update sync_history", sync_id=sync_id, error=str(update_exc))

    return {
        "sync_id": sync_id,
        "status": final_status,
        "collection_count": collection_count,
        "wantlist_count": wantlist_count,
        "error": error_message,
    }


async def reconcile_stale_sync_history(pg_pool: AsyncPostgreSQLPool) -> None:
    """Flip any sync_history row stuck at status='running' to 'failed' at startup.

    run_full_sync's own CancelledError/Exception handling (see above) covers
    an in-process cancellation or crash, but it can do nothing about a hard
    process death (SIGKILL/OOM) between the INSERT in
    api/routers/sync.py:trigger_sync and run_full_sync's terminal UPDATE —
    the row is simply abandoned with no in-process handler left to run.
    Call this once at service startup, before traffic is accepted, so a
    restart after a crash mid-sync doesn't leave GET /api/sync/status
    reporting a phantom running sync forever (groovemap-pxqw).
    """
    async with pg_pool.connection() as conn, conn.cursor() as cur:
        await execute_sql(
            cur,
            """
                UPDATE sync_history
                SET status = 'failed', error_message = 'Interrupted by service restart', completed_at = NOW()
                WHERE status = 'running'
                """,
        )
        reconciled = cur.rowcount
    if reconciled:
        logger.warning("⚠️ Reconciled stale sync_history rows stuck at 'running'", count=reconciled)
