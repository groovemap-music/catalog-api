# GrooveMap catalog API service

Repository-wide configuration, operations, and design guidance is indexed in
[`docs/README.md`](../docs/README.md).

Provides user account management, JWT authentication, and Discogs OAuth 1.0a integration for GrooveMap.

## Overview

The API service:

- Handles user registration and password-based login
- Issues and validates HS256 JWT access tokens
- Manages the Discogs OAuth 1.0a flow for users (OOB by default, registered callback when configured)
- Stores Discogs OAuth access tokens in PostgreSQL
- Reads Discogs app credentials from the `app_config` table (set via `discogs-setup` CLI)

Catalog data arrives through independently owned
[`discogs-ingestion`](https://github.com/groovemap-music/discogs-ingestion) and
[`musicbrainz-ingestion`](https://github.com/groovemap-music/musicbrainz-ingestion) event
contracts. Their source-matching graph enrichers and SQL loaders populate the stores this API
reads; Catalog API does not schedule or coordinate those producers.

## Architecture

- **Language**: Python 3.14 (managed runtime: 3.14.7)
- **Framework**: FastAPI with async PostgreSQL (`psycopg3`)
- **Cache**: Redis (OAuth state, graph snapshot persistence, JWT revocation blacklist)
- **Database**: PostgreSQL 18
- **Auth**: HS256 JWT with PBKDF2-SHA256 password hashing
- **Service Port**: 8004
- **Health Port**: 8005

## Configuration

Environment variables:

```bash
# PostgreSQL connection
POSTGRES_HOST=postgres
POSTGRES_USERNAME=groovemap
POSTGRES_PASSWORD=groovemap
POSTGRES_DATABASE=groovemap

# Neo4j connection (required — used by graph queries, sync, and recommendations)
NEO4J_HOST=neo4j
NEO4J_USERNAME=neo4j
NEO4J_PASSWORD=groovemap

# Redis (OAuth state + JTI blacklist storage)
REDIS_HOST=redis

# JWT signing secret
JWT_SECRET_KEY=your-secret-key-here

# Discogs API
DISCOGS_USER_AGENT="GrooveMap-catalog-api/1.0 +https://github.com/groovemap-music/catalog-api"

# HKDF master encryption key (derives OAuth + TOTP keys; generate with:
# python -c 'import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())')
# Required for TOTP 2FA. Without it, OAuth tokens are stored unencrypted and 2FA is disabled.
ENCRYPTION_MASTER_KEY=your-base64-master-key-here

# Optional — Resend email for password reset notifications (when not set, reset links are logged)
# RESEND_API_KEY=your-resend-api-key
# RESEND_SENDER_EMAIL=noreply@yourdomain.com
# RESEND_SENDER_NAME=GrooveMap

# Optional — CORS
CORS_ORIGINS="http://localhost:8003,http://localhost:8006"  # Comma-separated allowed origins

# Optional — Snapshot settings
SNAPSHOT_TTL_DAYS=28     # Default: 28 days
SNAPSHOT_MAX_NODES=100   # Default: 100 nodes per snapshot

# Optional
JWT_EXPIRE_MINUTES=30     # Default: 30 minutes
LOG_LEVEL=INFO
```

### JWT Authentication

All tokens are HS256 JWTs containing:

- `sub`: User UUID (PostgreSQL `users.id`)
- `email`: User email address
- `iat`: Issued-at timestamp
- `exp`: Expiry timestamp
- `jti`: Unique token ID, used for logout revocation (blacklisted in Redis)

The API handles JWT validation locally; `JWT_SECRET_KEY` remains inside the catalog-api boundary.
All JWT consumers delegate token-purpose allowlisting and revocation policy to
`api.dependencies.validate_token`; new routers must import that shared boundary instead of
decoding tokens locally.

### Discogs OAuth Flow

The API implements a Discogs OAuth 1.0a flow. With `DISCOGS_OAUTH_CALLBACK_URL` unset it uses
the OOB (out-of-band) verifier flow; when set it sends users through the registered callback:

1. **Start**: `GET /api/oauth/authorize/discogs` — requests a token from Discogs and returns an authorization URL and state token. State is stored in Redis with a TTL.
1. **Authorize**: User visits the Discogs URL and approves access, receiving a PIN verifier code.
1. **Complete**: `POST /api/oauth/verify/discogs` — exchanges the verifier for a permanent access token, which is stored in the `oauth_tokens` table.

After the flow, the API uses these tokens to synchronize the user's Discogs collection and
wantlist directly. Both sync paths compute the ADR 0007 canonical `media` block from the raw
Discogs API format objects (via `common.media.map_discogs_formats`) and store it in the
`media` JSONB column on `user_collections` / `user_wantlists`, alongside the existing
(deprecated) `formats` / `format` columns, which are unchanged.

## Operator Setup

Before users can connect their Discogs accounts, an operator must configure the Discogs app credentials.

### 1. Register a Discogs Developer App

Go to <https://www.discogs.com/settings/developers> and create a new application to obtain a **Consumer Key** and **Consumer Secret**.

### 2. Store Credentials via the CLI

The `discogs-setup` CLI is included in the API container:

```bash
# Set credentials
docker exec <api-container> discogs-setup \
  --consumer-key YOUR_CONSUMER_KEY \
  --consumer-secret YOUR_CONSUMER_SECRET

# Verify (values are masked)
docker exec <api-container> discogs-setup --show
```

The CLI upserts the values into the `app_config` table using the container's existing database connection environment variables. No service restart is required.

### 3. Verify

After running `--show`, output should resemble:

```
discogs_consumer_key:    ab********************cd
discogs_consumer_secret: ef********************gh
```

### Error Without Credentials

If a user attempts to start the Discogs OAuth flow before credentials are configured, the API returns:

```json
{
  "detail": "Discogs app credentials not configured. Ask an admin to run discogs-setup on the API container."
}
```

### Backfilling `media` on Existing Sync Data

Rows synced before the `media` column existed have `media IS NULL`. The `catalog-media-backfill`
CLI is a one-shot tool, included in the API container, that fills them in:

```bash
# Backfill both user_collections and user_wantlists
docker exec <api-container> catalog-media-backfill

# Only one table, or a smaller/larger batch size
docker exec <api-container> catalog-media-backfill --collection-only
docker exec <api-container> catalog-media-backfill --wantlist-only
docker exec <api-container> catalog-media-backfill --batch-size 200
```

It reads a batch of rows with `media IS NULL`, computes the media block, and writes the batch
back, repeating until none remain. `user_collections` rows are mapped from the raw Discogs API
`formats` column via `map_discogs_formats`; `user_wantlists` rows only ever kept the first
format's name (the deprecated `format` column), so they're mapped via the best-effort
`legacy_format_names_to_media` helper instead. Because only `media IS NULL` rows are ever
selected, the command is idempotent — safe to re-run, and safe to run alongside new syncs
(which always write `media` themselves).

### Projecting `gm_id` onto Neo4j Nodes

[ADR 0009](https://github.com/groovemap-music/design/blob/main/docs/adr/0009-native-identity-and-provider-aliases.md)
demotes provider identifiers to evidence: PostgreSQL's `provider_aliases` table is the
authority for native identity, and Neo4j nodes carry an additive `gm_id` property that is
only ever a projection of it. `run_gm_id_projection` (`api/projection.py`) keeps that
projection current — for each catalog kind (artist, label, master, release) it pages the
currently-valid Discogs aliases with a keyset cursor on `external_id` and, per page, runs
one `UNWIND` Cypher statement that sets `gm_id` on the matching nodes. The job's scope is
that one property: it creates no node, writes no relationship, and touches no other
property, so a failed or lagging run leaves a stale property rather than a mutated graph.

Two ways to run it:

- **Admin API**: `POST /api/admin/identity/project` (admin JWT required) starts the job as
  a tracked background task and returns `202` immediately with a job id:

  ```bash
  curl -X POST -H "Authorization: Bearer <admin-jwt>" \
    https://api.groovemap.music/api/admin/identity/project
  # {"id": "...", "status": "running"}
  ```

- **CLI**: `catalog-identity-projection` is a one-shot tool, included in the API container,
  that runs the job once and prints the per-label counts:

  ```bash
  docker exec <api-container> catalog-identity-projection
  docker exec <api-container> catalog-identity-projection --batch-size 500
  ```

Both entry points call the same `run_gm_id_projection`, so the API trigger and the CLI stay
behaviorally identical. Re-running is safe: nodes without a currently-valid alias are simply
not matched, and a node whose `gm_id` is already correct is set to the same value again.

### Re-attaching Load-Order-Split Catalog Items

[ADR 0014 section 8](https://github.com/groovemap-music/design/blob/main/docs/adr/0014-cross-catalog-edition-candidates.md)
repairs a split the loaders cannot heal. When a MusicBrainz release, release group, artist, or
label loads before the Discogs row its `discogs_*_id` names, it mints its own native id; the
Discogs row later mints a second one, and `attach_aliases` never overwrites. `api/reattach.py`
finds every such row — its Discogs alias resolves to a different native id than its
`musicbrainz` alias — and, per item in one transaction, closes every current alias on the split
native id, re-inserts the same aliases against the Discogs native id as `source = 'catalog'`,
and sets that MusicBrainz row's `gm_item_id`.

The same transaction then merges the split item into the Discogs item, per
[ADR 0009's 2026-09-25 amendment](https://github.com/groovemap-music/design/blob/main/docs/adr/0009-native-identity-and-provider-aliases.md#2026-09-25-superseded-catalog-items-and-native-id-merge)
(`api/catalog_merge.py`):

1. It locks both `catalog_items` rows, in id order. The split item is locked `FOR UPDATE`, so
   no copy or artifact can be created against it mid-move. The Discogs item is locked
   `FOR NO KEY UPDATE`.
2. It opens a `catalog_item_supersessions` row (`cause = 'catalog_reattachment'`, and
   `decision_ref` is the run's `admin_audit_log` entry). It also compresses chains, so
   resolution stays one hop.
3. It re-points `artifacts.item_id` and `owned_copies.item_id` from the split item to the
   survivor, and writes every moved row to `catalog_item_moves` with its owner.
4. It recomputes the `gm_item_id` caches (the entity tables, `user_collections`, and
   `user_wantlists`) from the moved aliases.

The former native id is kept, never deleted, and `public.resolve_catalog_item` resolves it to
the survivor. A merge changes only a user-owned row's item reference: no user-authored value,
id, or owner. It emits no event. Observations and snapshots are not touched, because copies
and artifacts keep their ids. `revert_supersession` reverses a merge exactly, for the future
promotion revert: it moves back only the ledgered rows still on the survivor.

The dependents guard is gone: it was removed in the same change that added the merge. A split
item with dependents is now merged, not skipped. A skip wrote nothing, so the first run after
this change merges every item earlier runs skipped for dependents. Compare its
`merged_with_dependents` count against those runs' `guard_reasons.dependents`.

It still skips and reports, without modifying, any split native id that holds a `discogs` or
another row's alias (a real item, not an orphan), or that holds an alias whose source is not
`catalog`. A merge that cannot happen rolls the whole item back and is counted as failed: the
kinds differ, the Discogs item is itself superseded, or the split item already resolves
elsewhere. A barcode or catalogue
number the MusicBrainz side won moves to the Discogs item; if the Discogs item already holds a
current alias for the same value it is not duplicated, and the split row stays closed. The
module docstring documents the lock order and why a concurrent loader attach converges.

**Dry run is the default.** A dry run only runs the read-only census. Per kind, it reports:

- split items;
- guarded items, by reason;
- items with dependents, by table;
- `will_move`: among eligible items, how many have dependents and how many artifact and
  owned-copy rows the merge will re-point;
- identifier aliases the split items hold, and how many of those the Discogs record also
  carries;
- Discogs ids that resolve to nothing yet.

Writing needs an explicit flag. Every applying run writes one `admin_audit_log` entry under
its job id, the id its supersessions name. The entry is written first, as
`identity.reattach.started`, before any identity write; if it cannot be written, the run does
not start and changes nothing. When the run finishes, the entry becomes
`identity.reattach.apply` with per-kind outcomes: supersessions opened, chains compressed, rows
moved and caches recomputed per table, and `merged_with_dependents`. It holds counts only,
never a user id or a user-owned row id, because that table outlives erasure. A run that fails
becomes `identity.reattach.failed`. If that last update itself fails, the entry stays
`identity.reattach.started` and an error is logged, so a supersession's `decision_ref` always
names a real entry. Re-running is safe: a repaired item is no longer split, and a
guarded item is skipped again.

- **Admin API**: `POST /api/admin/identity/reattach` (admin JWT required) returns `202` with a
  job id; add `?apply=true` to write. The census and per-item outcomes are logged.

  ```bash
  curl -X POST -H "Authorization: Bearer <admin-jwt>" \
    "https://api.groovemap.music/api/admin/identity/reattach?apply=true"
  # {"id": "...", "status": "running", "apply": true}
  ```

- **CLI**: `catalog-identity-reattach` prints the census; `--apply --admin-id <uuid>` writes,
  auditing the run against that admin:

  ```bash
  docker exec <api-container> catalog-identity-reattach
  docker exec <api-container> catalog-identity-reattach --apply --admin-id <admin-uuid>
  ```

**After an applying run, trigger the `gm_id` projection** (`POST /api/admin/identity/project` or
`catalog-identity-projection`, above) so the graph follows the alias table.

### Running Both Automatically After Each Discogs Import

Set `IDENTITY_AUTO_REATTACH_ENABLED=true` and the API runs the re-attachment (apply) and then
the `gm_id` projection by itself, once per completed Discogs extraction. It is off by default;
`IDENTITY_AUTO_REATTACH_INTERVAL` (seconds, default `300`) sets how often it polls.
`api/reattach_trigger.py`'s module docstring has the full design.

- **Trigger.** It reads `public.loader_extraction_latch` (declared by `database-schema`,
  written by `discogs-sql-loader`) and writes nothing there. An extraction is complete when
  its `loader = 'discogs'` row's `signals` holds `artists`, `labels`, `masters`, and
  `releases`. Only the newest complete extraction is a candidate: both jobs cover the whole
  catalog, so one run after the latest import covers earlier ones too, and enabling the
  watcher on an already-imported deployment runs it once. Until the relation exists the
  watcher idles.
- **Exactly once, across restarts and replicas.** Each poll takes a PostgreSQL advisory lock
  (`pg_try_advisory_lock`); a replica that cannot take it skips the poll. The holder checks
  the handled marker again under the lock, writes the run's entry, runs both jobs, turns the
  entry into the marker, and unlocks. A failure, or a process that dies mid-run, leaves no
  marker, so the next poll by any
  replica retries it. Both jobs are safe to re-run.
- **Handled marker and audit.** Each run writes one `admin_audit_log` entry with
  `target = 'discogs:<version>'`. Its row id is the run's job id, which the run's
  supersessions name as `decision_ref`, so it is written before the re-attachment as
  `identity.reattach.auto.started`; if it cannot be written, the run does not start. A
  finished run updates it to `action = 'identity.reattach.auto'`, with the per-kind
  re-attachment outcomes and projection counts in `details`. That entry is the durable
  handled marker, so no new table is needed. A failed run updates it to
  `identity.reattach.auto.failed`. Neither the started nor the failed entry is the marker, so
  such a run is retried, under a new job id and entry; the earlier entry stays, and its
  supersessions' `decision_ref` still resolves.
- **System actor.** The CLI requires `--admin-id` and the endpoint uses the caller's JWT
  because there a person decides to write. Here nobody does, so automatic runs are recorded
  against a reserved system user, `identity-maintenance@system.groovemap.invalid`, that the
  watcher creates on first use with a fixed id. It is inactive, not an admin, and has a
  password hash that matches no password, so it cannot log in or call admin routes. It is
  also counted in the admin dashboard's `total_users`.

## API Endpoints

### Authentication

| Method | Path                 | Auth Required | Rate Limit | Description                      |
| ------ | -------------------- | ------------- | ---------- | -------------------------------- |
| POST   | `/api/auth/register` | No            | 3/min      | Register a new user account      |
| POST   | `/api/auth/login`    | No            | 5/min      | Login and receive JWT token      |
| POST   | `/api/auth/logout`   | Yes           | —          | Revoke JWT token (JTI blacklist) |
| GET    | `/api/auth/me`       | Yes           | —          | Get current user details         |
| POST   | `/api/auth/change-password` | Yes     | —          | Change password and revoke older sessions |

### Password Reset

| Method | Path                      | Auth Required | Description                         |
| ------ | ------------------------- | ------------- | ----------------------------------- |
| POST   | `/api/auth/reset-request` | No            | Request a password reset email/link |
| POST   | `/api/auth/reset-confirm` | No            | Confirm password reset with token   |

### Two-Factor Authentication (TOTP 2FA)

Requires `ENCRYPTION_MASTER_KEY` to be configured. All 2FA endpoints require JWT authentication.

| Method | Path                     | Auth Required | Description                          |
| ------ | ------------------------ | ------------- | ------------------------------------ |
| POST   | `/api/auth/2fa/setup`    | Yes           | Generate TOTP secret and QR code URI |
| POST   | `/api/auth/2fa/confirm`  | Yes           | Confirm 2FA setup with TOTP code     |
| POST   | `/api/auth/2fa/verify`   | Yes           | Verify TOTP code during login        |
| POST   | `/api/auth/2fa/recovery` | Yes           | Use a recovery code to bypass 2FA    |
| POST   | `/api/auth/2fa/disable`  | Yes           | Disable 2FA for the account          |

### Discogs OAuth

| Method | Path                           | Auth Required | Description                           |
| ------ | ------------------------------ | ------------- | ------------------------------------- |
| GET    | `/api/oauth/authorize/discogs` | Yes           | Start Discogs OAuth flow              |
| POST   | `/api/oauth/verify/discogs`    | Yes           | Complete OAuth with verifier code     |
| GET    | `/api/oauth/status/discogs`    | Yes           | Check if Discogs account is connected |
| DELETE | `/api/oauth/revoke/discogs`    | Yes           | Disconnect Discogs account            |

### Graph Queries

All graph query endpoints are served by the API and consumed by
[`graph-explorer`](https://github.com/groovemap-music/graph-explorer).

| Method | Path                  | Auth Required | Rate Limit | Description                          |
| ------ | --------------------- | ------------- | ---------- | ------------------------------------ |
| GET    | `/api/autocomplete`   | No            | 30/min     | Search entities with autocomplete    |
| GET    | `/api/explore`        | No            | —          | Get center node with category counts |
| GET    | `/api/expand`         | No            | —          | Expand a category node (paginated)   |
| GET    | `/api/node/{node_id}` | No            | —          | Get full details for a node          |
| GET    | `/api/trends`         | No            | —          | Get time-series release counts       |

**Media (`GET /api/node/{node_id}?type=release`):** a release node response always carries a
`media` field — the ADR 0007 canonical media block. It is read from `releases.media` in
PostgreSQL (the block the Discogs SQL loader computes at load time), keyed by the same id as
the Neo4j `Release.id`. When that row does not exist yet, or its `media` column is NULL, the
API derives a best-effort block from the release's raw `formats` name list through
`common.media.legacy_format_names_to_media`, so `media` is never omitted for a release — an
unrecoverable release with no formats data gets an empty-but-valid block. The deprecated raw
`formats` list (see [Deprecations](#deprecations)) is still returned alongside it. Non-release
node responses (`artist`, `genre`, `label`, `style`) are unaffected and carry no `media` key.

### Collection Sync

| Method | Path               | Auth Required | Rate Limit | Description                     |
| ------ | ------------------ | ------------- | ---------- | ------------------------------- |
| POST   | `/api/sync`        | Yes           | 10/min     | Trigger a full Discogs sync     |
| GET    | `/api/sync/status` | Yes           | —          | Get sync history (last 10 jobs) |

A per-user Redis cooldown additionally blocks re-triggering a sync for 60 seconds after the previous one starts.

### User Collection

Personalized endpoints that return data from the user's synced Discogs collection.

| Method | Path                         | Auth Required | Description                              |
| ------ | ---------------------------- | ------------- | ---------------------------------------- |
| GET    | `/api/user/collection`       | Yes           | List user's collected releases           |
| GET    | `/api/user/wantlist`         | Yes           | List user's wantlist releases            |
| GET    | `/api/user/recommendations`  | Yes           | Get recommended releases                 |
| GET    | `/api/user/collection/stats` | Yes           | Collection statistics summary            |
| GET    | `/api/user/status`           | Optional      | Check collection/wantlist status for IDs |

**Native identity ([ADR 0009](https://github.com/groovemap-music/design/blob/main/docs/adr/0009-native-identity-and-provider-aliases.md)):**
collection, wantlist, and collection-gap items carry `gm_item_id` — the native id of the
release, read from the sync-written column — and collection items additionally carry
`owned_copy_id`, the native id of the physical copy the sync minted for that row. Both are
`null` when the sync has not resolved a native id for that row yet. Search hits and every
recommendation shape (`SimilarArtist`, explore's `EntityRef`/`DiscoveryNode`, and
`EnhancedRecommendation`) carry the equivalent `gm_id` field for the same reason. See
[Native identity and first-party activity](../docs/identity-and-activity.md) for the full
resolution model, owned copies, and observations.

### Observations

User-captured evidence about a copy the caller holds — a matrix inscription, a grading, a
purchase price. Owner-scoped: a copy belonging to someone else reads as `404`, the same as an
unknown one.

| Method | Path                                      | Auth Required                     | Description                                |
| ------ | ----------------------------------------- | --------------------------------- | ------------------------------------------ |
| POST   | `/api/user/copies/{copy_id}/observations` | JWT or `observations:write` token | Record one observation about an owned copy |
| GET    | `/api/user/copies/{copy_id}/observations` | JWT or `observations:read` token  | List observations for an owned copy        |

Both accept a first-party JWT or an app token carrying the matching scope; the owner the
copy is scoped to is the session's user or the token's owner, so the `404` on someone
else's copy is the same either way.

### Activity, Consent, Erasure, and Export

First-party behavioural events, recommendation impression tracking, consent, and GDPR-style
erasure and export, per [ADR 0010](https://github.com/groovemap-music/design/blob/main/docs/adr/0010-first-party-events-consent-and-deletion.md).
See [Native identity and first-party activity](../docs/identity-and-activity.md) for the
recorder's emission points (search, recommendations, sync), the erasure procedure and its
cross-store failure reporting, and the export's NDJSON section order.

| Method | Path                          | Auth Required                  | Description                                                      |
| ------ | ----------------------------- | ------------------------------ | ---------------------------------------------------------------- |
| POST   | `/api/activity/events`        | JWT or `activity:write` token  | Report a client-side outcome against a recommendation impression |
| GET    | `/api/user/consent`           | JWT or `consent:read` token    | Both consent purposes with their current grant/revocation state  |
| PUT    | `/api/user/consent/{purpose}` | JWT or `consent:write` token   | Grant or revoke consent for one purpose                          |
| POST   | `/api/user/erasure`           | JWT only                       | Erase everything keyed to the caller, across every store         |
| GET    | `/api/user/export`            | JWT only                       | Stream everything keyed to the caller as NDJSON                  |

A delegated agent reports outcomes and reads consent with a scoped app token instead of a
session, and the recorder sees the token owner's id — so the row is identical to one the
owner wrote themselves. Erasure and export stay JWT-only on purpose: they are
account-level rights no scope reaches, and an app token presented there is a `401`.

### App Tokens

Manage third-party app tokens for the authenticated user. The plaintext token is returned exactly once, at creation; only its SHA-256 hash is persisted thereafter.

| Method | Path                          | Auth Required | Description                                     |
| ------ | ----------------------------- | ------------- | ------------------------------------------------ |
| POST   | `/api/user/app-tokens`        | Yes           | Mint a new app token (plaintext returned once)  |
| GET    | `/api/user/app-tokens`        | Yes           | List active and revoked tokens for the user     |
| DELETE | `/api/user/app-tokens/{id}`   | Yes           | Revoke (tombstone) a token                      |

**Allowed scopes:** every scope below, and only these — an unknown scope is rejected at
mint time with a `400`.

| Scope                | Grants                                             |
| -------------------- | -------------------------------------------------- |
| `collection:read`    | `GET /api/user/collection`, `/collection/stats`, `/collection/timeline` |
| `activity:write`     | `POST /api/activity/events`                        |
| `consent:read`       | `GET /api/user/consent`                            |
| `consent:write`      | `PUT /api/user/consent/{purpose}`                  |
| `observations:read`  | `GET /api/user/copies/{copy_id}/observations`      |
| `observations:write` | `POST /api/user/copies/{copy_id}/observations`     |

No scope reaches `POST /api/user/erasure` or `GET /api/user/export`.

### Collection Gap Analysis

"Complete My Collection" endpoints that find releases the user does not own.

| Method | Path                                      | Auth Required | Description                                          |
| ------ | ----------------------------------------- | ------------- | ----------------------------------------------------- |
| GET    | `/api/collection/formats`                 | Yes           | Deprecated: distinct raw format names in collection  |
| GET    | `/api/collection/media`                   | Yes           | Canonical media families/mediums in user's collection |
| GET    | `/api/collection/gaps/label/{label_id}`   | Yes           | Missing releases on a label                          |
| GET    | `/api/collection/gaps/artist/{artist_id}` | Yes           | Missing releases by an artist                        |
| GET    | `/api/collection/gaps/master/{master_id}` | Yes           | Missing editions of a master release                 |

**Media filter (ADR 0007):** each gap endpoint accepts a repeatable `media` query
parameter of canonical family or medium ids (see `GET /api/collection/media` for the
ids present in the user's own collection), validated against the taxonomy — an
unknown id returns `400`. The deprecated `formats` parameter (raw Discogs format
names) is still accepted and mapped onto the same canonical ids through the shared
`legacy_format_names_to_media` helper; both filters combine, and the response's
`filters` object echoes back whatever was requested under both `media` and `formats`.

### Snapshots

Save and restore graph exploration states as shareable URLs.

| Method | Path                    | Auth Required | Description                 |
| ------ | ----------------------- | ------------- | --------------------------- |
| POST   | `/api/snapshot`         | Yes           | Save current graph snapshot |
| GET    | `/api/snapshot/{token}` | No            | Restore a saved snapshot    |

### Unified Search

Full-text search across all entity types using PostgreSQL, with facet counts and result highlighting. Results are cached in Redis for 5 minutes. The response's `facets` object carries `type`, `genre`, `decade`, and `media` — each a `{value: count}` mapping (`media` keyed by ADR 0007 family id), counted from matching releases.

| Method | Path          | Auth Required | Rate Limit | Description                                   |
| ------ | ------------- | ------------- | ---------- | --------------------------------------------- |
| GET    | `/api/search` | No            | 30/min     | Search artists, labels, masters, and releases |

**Query parameters:**

- `q` (required) — Search query (minimum 3 characters)
- `types` — Comma-separated entity types to search (default: `artist,label,master,release`)
- `genres` — Comma-separated genre filter
- `media` — Repeated media family or medium id to filter release results (e.g. `?media=vinyl&media=optical_cd`). Ids come from the ADR 0007 canonical media taxonomy vendored in `common.media` (`family_ids()` / `medium_ids()`); an unrecognised id returns `400` listing the unknown id(s). Family and medium ids are OR-combined with each other and AND-combined with `genres`/`year_min`/`year_max`. Only release results carry media — the filter is a no-op for artist/label/master results.

A signed-in caller's search is recorded as first-party activity (ADR 0010): one `search.query`
event plus one `search.result_impression` event per hit shown. See
[Native identity and first-party activity](../docs/identity-and-activity.md) for the emission
detail, including how the `types` filter is represented in the recorded payload.
- `year_min` — Minimum release year (1000–9999)
- `year_max` — Maximum release year (1000–9999)
- `limit` — Results per page (1–100, default: 20)
- `offset` — Pagination offset (default: 0)

### Identifier Lookup

Resolve one catalogue identifier — printed on the record itself — to the release or releases
that carry it ([ADR 0011](https://github.com/groovemap-music/design/blob/main/docs/adr/0011-catalog-identifiers-and-manufacturing-credits.md)).
Public and rate limited like search, since the caller is standing in a shop with the record
in hand rather than signed in.

| Method | Path                             | Auth Required | Rate Limit | Description                                    |
| ------ | -------------------------------- | ------------- | ---------- | ----------------------------------------------- |
| GET    | `/api/lookup/{provider}/{value}` | No            | 30/min     | Resolve a barcode, catalogue number, or matrix  |

`provider` is one of `barcode`, `catalog_number`, or `matrix` — the alias namespaces ADR 0011
mints. `value` is normalized with that namespace's declared rule before it is looked up (a
barcode's grouping spaces or dashes, for instance, don't matter). An unminted namespace is
`400`; a value no alias carries, or whose alias points at no loaded release row, is `404`.

**Barcode equivalence ([ADR 0011's "UPC-A and EAN-13 are one GTIN at lookup" amendment](https://github.com/groovemap-music/design/blob/main/docs/adr/0011-catalog-identifiers-and-manufacturing-credits.md#2026-09-25-upc-a-and-ean-13-are-one-gtin-at-lookup-no-alias-is-re-keyed)):**
GS1 treats GTIN-12, GTIN-13, and GTIN-14 as one number space — a shorter GTIN is the same GTIN
zero-padded to 14 digits — and this surface applies that at lookup, without re-keying any
stored alias:

- A 12-digit value `D` is equivalent to `0D` and `00D`.
- A 13-digit value is equivalent to its own 14-digit zero-padded form, and additionally to the
  bare 12-digit form when it itself already starts with `0`.
- A 14-digit value starting with `0` is equivalent to the same value with one or two leading
  zeros removed, as far as that stays 12 or 13 digits.
- A 14-digit value starting with a nonzero indicator digit (`1`-`9`, GS1's marker for a
  different trade item such as a case), an 8-digit EAN-8/UPC-E, and every other length are
  equivalent only to themselves. The check digit is never validated.

A request for any one form probes every equivalent form in the same batched query, so a
sleeve read as `036000291452` also finds an item minted as `0036000291452` or `00036000291452`.
When the resolved rows name two or more native items, the lookup returns every one of them —
it does not pick a winner, merge them, or treat it as a split. The additive `matches` array
carries one entry per resolved row (a native id reached through two forms is not deduplicated
into one entry), each with its own `gm_id`, the stored `external_id` that resolved it, and its
`releases`; entries are ordered by an exact match to the typed value first, then by the stored
value's own length (shortest first), then by native id. The top-level `gm_id`/`releases` name
the first entry, so a single-item client keeps working unchanged. `matches` is empty for the
ordinary case where every resolved row names the same item.

### Path Finder

Find the shortest path between any two entities in the knowledge graph.

| Method | Path        | Auth Required | Description                              |
| ------ | ----------- | ------------- | ---------------------------------------- |
| GET    | `/api/path` | No            | Shortest path between two named entities |

**Query parameters:**

- `from_name` (required) — Source entity name
- `from_type` — Source entity type (default: `artist`)
- `to_name` (required) — Target entity name
- `to_type` — Target entity type (default: `artist`)
- `max_depth` — Maximum path depth (1–15, default: 10)

### Collaborators

Find artists who share releases with a given artist, with temporal collaboration data (yearly counts, first/last year).

| Method | Path                             | Auth Required | Rate Limit | Description                                          |
| ------ | -------------------------------- | ------------- | ---------- | ---------------------------------------------------- |
| GET    | `/api/collaborators/{artist_id}` | No            | 30/min     | Get collaborating artists with release overlap stats |

**Query parameters:**

- `limit` — Maximum collaborators to return (1–100, default: 20)

### Collaboration Network

Multi-hop collaborator traversal, centrality scoring, and community detection via the knowledge graph. Centrality and cluster results are cached in Redis (1h TTL). Rate limited to 30 requests/minute.

| Method | Path                                     | Auth Required | Rate Limit | Description                                               |
| ------ | ---------------------------------------- | ------------- | ---------- | --------------------------------------------------------- |
| GET    | `/api/network/artist/{id}/collaborators` | No            | 30/min     | Multi-hop collaborators via shared releases (depth 1–3)   |
| GET    | `/api/network/artist/{id}/centrality`    | No            | 30/min     | Degree centrality, collaborator count, group/alias counts |
| GET    | `/api/network/cluster/{id}`              | No            | 30/min     | Community detection via genre-based clustering            |

**Query parameters for `/api/network/artist/{id}/collaborators`:**

- `depth` — Number of hops to traverse (1–3, default: 2)
- `limit` — Maximum collaborators to return (1–200, default: 50)

**Query parameters for `/api/network/cluster/{id}`:**

- `limit` — Maximum cluster members to return (1–200, default: 50)

### Recommendations

Artist similarity and personalized graph-traversal discovery, ranked by multi-dimensional profile matching or the authenticated user's taste. Results are cached in Redis.

| Method | Path                                          | Auth Required | Rate Limit | Description                                                    |
| ------ | ---------------------------------------------- | ------------- | ---------- | ---------------------------------------------------------------- |
| GET    | `/api/recommend/similar/artist/{artist_id}`    | No            | 30/min     | Artists with the closest multi-dimensional similarity           |
| GET    | `/api/recommend/explore/{entity_type}/{id}`    | Yes           | 30/min     | Personalized multi-hop traversal from an entity, ranked by taste |
| GET    | `/api/fit/release/{release_id}`                | JWT or `fit:read` token | 30/min | CrateFit: the decomposed fit of one candidate release for the caller |

**Query parameters for `/api/recommend/similar/artist/{artist_id}`:**

- `limit` — Number of similar artists to return (1–50, default: 20)

**Query parameters for `/api/recommend/explore/{entity_type}/{id}`:**

- `entity_type` — One of `artist`, `label`, `genre`, `style`
- `hops` — Number of hops to traverse (1–3, default: 2)
- `limit` — Maximum discoveries to return (1–50, default: 10)

**Impression tracking ([ADR 0010](https://github.com/groovemap-music/design/blob/main/docs/adr/0010-first-party-events-consent-and-deletion.md)):**
every served item on a ranked recommendation surface carries an `impression_id`, minted after
the response body is filled so a cached response never reuses an id across viewers; it is
`null` when the candidate had no native id or the write failed. Report an outcome against it
with `POST /api/activity/events` (see [Activity, Consent, Erasure, and Export](#activity-consent-erasure-and-export)).
Each surface writes under its own policy id: `similar_artist_weighted_cosine_v1`,
`explore_personalized_v1`, `user_recommendations_artist_v1` (`strategy=artist`, the default),
and `user_recommendations_multi_v1` (`strategy=multi`).

**CrateFit** (`/api/fit/release/{release_id}`) is a different shape from the ranked
surfaces above: one candidate, five named components with evidence, and an identity
confidence reported beside the fit rather than folded into it. It writes one impression
per request served under the `cratefit_v0` policy. See
[the CrateFit guide](../docs/cratefit.md) for the components, the v0 heuristics, and the
limits of version 0.

### Genre Tree

Full genre/style hierarchy derived from release co-occurrence in the knowledge graph.

| Method | Path              | Auth Required | Rate Limit | Description                                   |
| ------ | ----------------- | ------------- | ---------- | --------------------------------------------- |
| GET    | `/api/genre-tree` | No            | 30/min     | Genre hierarchy with nested styles and counts |

The genre tree is cached in-memory for 5 minutes since the hierarchy changes only on data import.

### Graph Statistics

Aggregate node counts across the knowledge graph.

| Method | Path               | Auth Required | Description                                                              |
| ------ | ------------------ | ------------- | ------------------------------------------------------------------------ |
| GET    | `/api/graph/stats` | No            | Total entity counts (artists, labels, releases, masters, genres, styles) |

### Time travel

Time-travel through the knowledge graph with year-range and genre-emergence queries.

| Method | Path                           | Auth Required | Description                                 |
| ------ | ------------------------------ | ------------- | ------------------------------------------- |
| GET    | `/api/explore/year-range`      | No            | Get min/max release years in the graph      |
| GET    | `/api/explore/genre-emergence` | No            | Get genres that emerged before a given year |

**Query parameters for `/api/explore/genre-emergence`:**

- `before_year` (required) — Year cutoff (1900–2030)

### Analytics results

These endpoints proxy precomputed music trends from
[`analytics-engine`](https://github.com/groovemap-music/analytics-engine). They return 503 when
that service is unavailable. The catalog API also owns the authenticated
`/api/internal/insights/*` wire contract used by `analytics-engine` to fetch raw Neo4j and
PostgreSQL query results.

| Method | Path                              | Auth Required | Description                         |
| ------ | --------------------------------- | ------------- | ----------------------------------- |
| GET    | `/api/insights/top-artists`       | No            | Top artists by release count        |
| GET    | `/api/insights/genre-trends`      | No            | Genre popularity trends over time   |
| GET    | `/api/insights/label-longevity`   | No            | Label longevity rankings            |
| GET    | `/api/insights/this-month`        | No            | Releases and trends for this month  |
| GET    | `/api/insights/data-completeness` | No            | Data quality and completeness stats |
| GET    | `/api/insights/status`            | No            | Computation status of analytics data |

### Natural Language Queries (NLQ)

Natural language query interface for the knowledge graph. Translates plain English questions into graph queries.

| Method | Path                    | Auth Required | Rate Limit | Description                                   |
| ------ | ----------------------- | ------------- | ---------- | ---------------------------------------------- |
| GET    | `/api/nlq/suggestions`  | No            | 100/min    | Dynamic suggested queries for the Ask pill    |
| GET    | `/api/nlq/status`       | No            | —          | Check NLQ service availability                |
| POST   | `/api/nlq/query`        | Optional      | 10/min     | Execute a natural language query (supports SSE streaming; personalized when a Bearer token is supplied) |

**Media as a filter dimension.** Per [ADR 0007](https://github.com/groovemap-music/design/blob/main/docs/adr/0007-canonical-media-taxonomy.md), the model can narrow a graph filter (`ui_filter_graph` with `by: "media"`) or the `get_collection_gaps` tool to a canonical media family or medium id — e.g. "cassette-only labels" or "what am I missing on CD". The system prompt lists the valid family ids and explains how a spoken format ("cassette", "CD") maps to a medium id (`tape_cassette`, `optical_cd`) or its family (`tape`, `optical`); the `get_collection_gaps` tool's `media` parameter is validated against the same taxonomy as the REST gap endpoints (`api/queries/media_filters.py`), and an unrecognised id comes back as a tool-level error the model can read and retry. The Ask pill's suggestion set includes one media example ("Which labels released the most on cassette?").

### Release Rarity Scoring

Rarity analysis for releases based on market scarcity, media, and collector demand.

| Method | Path                             | Auth Required | Rate Limit | Description                            |
| ------ | -------------------------------- | ------------- | ---------- | --------------------------------------- |
| GET    | `/api/rarity/leaderboard`        | No            | 30/min     | Top rarest releases overall            |
| GET    | `/api/rarity/hidden-gems`        | No            | 30/min     | Underappreciated rare releases         |
| GET    | `/api/rarity/artist/{artist_id}` | No            | 30/min     | Rarity scores for an artist's releases |
| GET    | `/api/rarity/label/{label_id}`   | No            | 30/min     | Rarity scores for a label's releases   |
| GET    | `/api/rarity/{release_id}`       | No            | 30/min     | Rarity score for a specific release    |

#### Media-neutral core and per-family extensions

Per [ADR 0007](https://github.com/groovemap-music/design/blob/main/docs/adr/0007-canonical-media-taxonomy.md), scoring is split into a core that reasons about every medium the same way, plus extension modules keyed by canonical media family. The code lives in `api/rarity/`; `api/queries/rarity_queries.py` owns only the graph and PostgreSQL access around it.

**Core signals** (`api/rarity/core.py`) apply to every release:

| Signal                  | Weight | Meaning                                                        |
| ----------------------- | ------ | -------------------------------------------------------------- |
| `label_catalog`         | 0.10   | Label catalog size; a smaller catalog is rarer                 |
| `medium_rarity`         | 0.10   | The rarest canonical medium the release was issued on          |
| `temporal_scarcity`     | 0.20   | Age, discounted when a recent reissue exists                   |
| `graph_isolation`       | 0.15   | Graph degree; fewer connections is rarer                       |
| `collection_prevalence` | 0.20   | Inverse community ownership, with a want-over-have bonus       |

**Family extensions** (`api/rarity/families/`) contribute only where their media justify it. Today there is one:

| Module    | Families                          | Signal              | Weight |
| --------- | --------------------------------- | ------------------- | ------ |
| `grooved` | `vinyl`, `shellac`, `grooved_other` | `pressing_scarcity` | 0.25   |

Pressing scarcity counts sibling pressings of a master, which is a property of a physical grooved pressing rather than of a release. A CD, a download card, or a VHS tape has no pressings to count, so no such signal is produced for one. This is the seam a future vinyl-specific service would own: pressing plant, matrix and runout, lacquer and stamper lineage, and colour evidence all belong in this module when they arrive.

The core weights deliberately sum to 0.75, not 1.0. `compose` renormalises over the signals a release actually has, so both a lone CD and a lone LP score on a full 0-100 scale and the tier thresholds mean the same thing for both. A grooved release scores under weights identical to the pre-split table.

**Medium rarity** is a table keyed by canonical medium id (`MEDIUM_RARITY_SCORES`), with a documented default per family (`FAMILY_DEFAULT_MEDIUM_RARITY`) for a medium a later taxonomy version adds. It reads the release's media from `(:Release)-[:ISSUED_ON]->(:Medium)` edges, falling back to the `media_families` node property and then to the deprecated raw `formats` list through the shared mapper.

**Deprecated for one minor version:** `format_rarity`, which keyed on raw Discogs format names and so mixed media with descriptors. It is still computed and still appears in the breakdown, with weight `0.0`, and no longer moves the score.

#### Adding a family module

1. Write `api/rarity/families/<family>.py` with a class satisfying the `FamilySignals` protocol: `module_id`, `weights`, `queries`, `applies_to(families)`, and `signals(release_ctx)`.
2. Choose absolute weights on the same scale as the core's. There is no total to keep balanced; they are renormalised at compose time.
3. Declare any Cypher the core does not already fetch. Each query takes an `$ids` page and returns a `release_id` column, per the chunking contract in `api/queries/rarity_queries.py`. The fact name keys the row into `ReleaseContext.facts`.
4. Register it in `api/rarity/families/__init__.py` against the taxonomy family ids it serves.

Nothing in the core changes. The orchestrator discovers the module's queries, runs them per page, and folds its signals into the composite.

**Breakdown response.** `GET /api/rarity/{release_id}` returns `media_families` (the canonical families the release covers) and `family_signals` (which modules contributed and what they scored) alongside `breakdown`. Each `breakdown` entry's `weight` is the effective, renormalised weight for that release.

### Label DNA

Fingerprint and compare record labels based on their genre, style, media, and decade profiles. Rate limited to 30 requests/minute.

| Method | Path                            | Auth Required | Rate Limit | Description                                    |
| ------ | ------------------------------- | ------------- | ---------- | ---------------------------------------------- |
| GET    | `/api/label/{label_id}/dna`     | No            | 30/min     | Full DNA fingerprint for a label               |
| GET    | `/api/label/{label_id}/similar` | No            | 30/min     | Find labels with closest DNA fingerprint       |
| GET    | `/api/label/dna/compare`        | No            | 30/min     | Side-by-side DNA comparison of multiple labels (family-level media profiles) |

**Query parameters for `/api/label/{label_id}/similar`:**

- `limit` — Number of similar labels to return (1–50, default: 10)

**Query parameters for `/api/label/dna/compare`:**

- `ids` (required) — Comma-separated label IDs (2–5 labels)

**Media profile (`media`):** each DNA fingerprint carries a `media` list, grouped by canonical
media family (`vinyl`, `shellac`, `grooved_other`, `tape`, `optical`, `digital`, `video`,
`other`) with per-medium detail nested inside — e.g. a `vinyl` family entry lists its
`vinyl_12` and `vinyl_7` mediums separately, and a `tape` family entry lists its
`tape_cassette` medium. A family's `percentage` is its share of the label's total media-tagged releases; a medium's
`percentage` is its share within its own family. Counts come from `ISSUED_ON` edges to `Medium`
nodes and count each `(release, medium)` once even when both the Discogs and MusicBrainz
enrichers have asserted an edge to the same medium. A label whose releases predate the media
taxonomy cutover (no `ISSUED_ON` edges yet) falls back to the `Release.media_families` property,
which yields family-level counts only — `mediums` is empty for those families. The deprecated
`formats` list (raw Discogs format names, unweighted by family) is kept for one minor version;
new consumers should read `media` instead.

### Taste Fingerprint

Personalized taste analysis endpoints based on the authenticated user's synced collection. Requires a minimum of 10 collection items.

| Method | Path                          | Auth Required | Description                                                               |
| ------ | ----------------------------- | ------------- | ------------------------------------------------------------------------- |
| GET    | `/api/user/taste/heatmap`     | Yes           | Genre x decade heatmap of user's collection                               |
| GET    | `/api/user/taste/fingerprint` | Yes           | Full taste fingerprint (heatmap, obscurity, drift, blind spots)           |
| GET    | `/api/user/taste/blindspots`  | Yes           | Genres the user's favourite artists release in but they haven't collected |
| GET    | `/api/user/taste/card`        | Yes           | SVG taste card image (returns `image/svg+xml`)                            |

**Query parameters for `/api/user/taste/blindspots`:**

- `limit` — Number of blind spots to return (1–20, default: 5)

### Collection Timeline

Temporal analysis of the authenticated user's collection, showing how their taste has evolved over time.

| Method | Path                             | Auth Required | Description                                     |
| ------ | -------------------------------- | ------------- | ----------------------------------------------- |
| GET    | `/api/user/collection/timeline`  | Yes           | Release count distribution by year or decade    |
| GET    | `/api/user/collection/evolution` | Yes           | How genre, style, or label mix shifts over time |

**Query parameters for `/api/user/collection/timeline`:**

- `bucket` — Grouping bucket: `year` or `decade` (default: `year`)

**Query parameters for `/api/user/collection/evolution`:**

- `metric` — Evolution metric: `genre`, `style`, or `label` (default: `genre`)

### Credits & Provenance

Query the credited personnel (producers, engineers, mastering engineers, session musicians,
designers) behind releases. The graph data is produced by
[`discogs-graph-enricher`](https://github.com/groovemap-music/discogs-graph-enricher) from Discogs
`extraartists` records.

| Method | Path                                  | Auth Required | Rate Limit | Description                                           |
| ------ | ------------------------------------- | ------------- | ---------- | ----------------------------------------------------- |
| GET    | `/api/credits/person/{name}`          | No            | 60/min     | All releases a person is credited on, grouped by role |
| GET    | `/api/credits/person/{name}/timeline` | No            | 60/min     | Year-by-year credit activity for a person             |
| GET    | `/api/credits/person/{name}/profile`  | No            | 60/min     | Summary profile with role breakdown                   |
| GET    | `/api/credits/release/{release_id}`   | No            | 60/min     | Full credits breakdown for a release                  |
| GET    | `/api/credits/role/{role}/top`        | No            | 30/min     | Most prolific people in a given role category         |
| GET    | `/api/credits/shared`                 | No            | 30/min     | Releases where two people are both credited           |
| GET    | `/api/credits/connections/{name}`     | No            | 30/min     | People connected through shared releases              |
| GET    | `/api/credits/autocomplete`           | No            | 120/min    | Search credits by person name (fulltext, min 2 chars) |

**Role categories:** `production`, `engineering`, `mastering`, `session`, `design`, `management`, `other`

**Query parameters for `/api/credits/role/{role}/top`:**

- `limit` — Number of entries (1–100, default: 20)

**Query parameters for `/api/credits/shared`:**

- `person1` (required) — First person name
- `person2` (required) — Second person name

**Query parameters for `/api/credits/connections/{name}`:**

- `depth` — Connection depth (1–3, default: 2)
- `limit` — Maximum connections (1–200, default: 50)

**Query parameters for `/api/credits/autocomplete`:**

- `q` (required) — Search query (minimum 2 characters)
- `limit` — Results to return (1–50, default: 10)

### MusicBrainz Enrichment

Endpoints exposing MusicBrainz enrichment data linked to Discogs entities. Neo4j enrichment is
owned by
[`musicbrainz-graph-enricher`](https://github.com/groovemap-music/musicbrainz-graph-enricher),
and PostgreSQL enrichment is owned by
[`musicbrainz-sql-loader`](https://github.com/groovemap-music/musicbrainz-sql-loader).

| Method | Path                                     | Auth Required | Rate Limit | Description                                                                   |
| ------ | ---------------------------------------- | ------------- | ---------- | ----------------------------------------------------------------------------- |
| GET    | `/api/artist/{artist_id}/musicbrainz`    | No            | 30/min     | MusicBrainz metadata (type, gender, dates, area, disambiguation)              |
| GET    | `/api/artist/{artist_id}/relationships`  | No            | 30/min     | MusicBrainz-sourced relationship edges (collaborations, memberships)          |
| GET    | `/api/artist/{artist_id}/external-links` | No            | 30/min     | External links (Wikipedia, Wikidata, AllMusic, Last.fm)                       |
| GET    | `/api/enrichment/status`                 | No            | 10/min     | Enrichment coverage statistics (MB entities, Discogs matches, Neo4j enriched) |

**Data sources:**

- `/musicbrainz` and `/relationships` — Neo4j, populated by `musicbrainz-graph-enricher`
- `/external-links` — PostgreSQL `musicbrainz.external_links`, populated by `musicbrainz-sql-loader`
- `/enrichment/status` — Both Neo4j and PostgreSQL

### Media mapping coverage

Admin-only observability for the [ADR 0007](https://github.com/groovemap-music/design/blob/main/docs/adr/0007-canonical-media-taxonomy.md)
canonical media taxonomy. Whenever a loader meets a provider format name the taxonomy does not
recognise, it keeps the raw name in the release's `media` block under `unmapped` — split into
`formats` (the provider's own format names) and `descriptions` (their qualifiers). This endpoint
ranks those names across a provider's release table, so mapping coverage is read from the stored
data rather than inferred.

| Method | Path                         | Auth Required | Description                                                    |
| ------ | ---------------------------- | ------------- | -------------------------------------------------------------- |
| GET    | `/api/admin/media/unmapped`  | Admin JWT     | Top unmapped raw media names for one provider, with coverage counts |

**Query parameters:**

| Name       | Required | Default | Description                                                              |
| ---------- | -------- | ------- | ------------------------------------------------------------------------ |
| `provider` | Yes      | —       | `discogs` (the `releases` table) or `musicbrainz` (`musicbrainz.releases`) |
| `limit`    | No       | `20`    | How many top names to return, `1`–`200`                                   |

An unrecognised `provider` returns **422**, as does a `limit` outside its range or a missing
`provider`. A request without a valid admin JWT returns **401**.

Names are ranked by how many media-tagged releases carry them, descending; `kind` distinguishes a
provider format name from a description. Because the taxonomy de-duplicates each `unmapped` list
per release, `releases` is a release count, not an occurrence count.

```console
$ curl -H "Authorization: Bearer $ADMIN_JWT" \
    "http://localhost:8004/api/admin/media/unmapped?provider=discogs&limit=3"
```

```json
{
  "provider": "discogs",
  "media_tagged_releases": 128034,
  "releases_with_unmapped": 4127,
  "unmapped_rate": 0.0322,
  "limit": 3,
  "top_unmapped": [
    { "kind": "format", "name": "Lathe Cut", "releases": 812 },
    { "kind": "description", "name": "Hand-Numbered", "releases": 640 },
    { "kind": "format", "name": "Shellac", "releases": 415 }
  ]
}
```

- `media_tagged_releases` — releases whose `media` column is populated (the denominator)
- `releases_with_unmapped` — how many of those carry at least one unmapped name
- `unmapped_rate` — `releases_with_unmapped / media_tagged_releases`, rounded to 4 places, `0.0`
  when nothing is tagged yet

### Internal analytics computation

Internal endpoints called by `analytics-engine` over HTTP to fetch raw query results. These wire
paths retain `/insights/` for API compatibility and are not intended for direct external use.

| Method | Path                                       | Auth Required | Description                           |
| ------ | ------------------------------------------ | ------------- | ------------------------------------- |
| GET    | `/api/internal/insights/artist-centrality` | No            | Artist centrality data from Neo4j     |
| GET    | `/api/internal/insights/genre-trends`      | No            | Genre trend data from Neo4j           |
| GET    | `/api/internal/insights/label-longevity`   | No            | Label longevity data from Neo4j       |
| GET    | `/api/internal/insights/anniversaries`     | No            | Anniversary data from PostgreSQL      |
| GET    | `/api/internal/insights/data-completeness` | No            | Data completeness from both databases |
| GET    | `/api/internal/insights/rarity-scores`     | No            | Rarity score data from PostgreSQL     |

### Health

| Method | Path      | Port | Description                                 |
| ------ | --------- | ---- | ------------------------------------------- |
| GET    | `/health` | 8004 | Health check on the main API server         |
| GET    | `/health` | 8005 | Health check on the dedicated health server |

## Development

### Running Locally

```bash
# Install dependencies
just setup

# Run the API service
uv run python -m api.api
```

### Running Tests

```bash
# Run the repository test suite once
just test

# Run the same suite and retain coverage.xml
just coverage

# Run the complete pre-merge gate
just check
```

## Container image

Build the repository-owned image locally:

```bash
just image
```

Runtime topology, databases, networks, and container startup are owned by
[`deployment`](https://github.com/groovemap-music/deployment).

## Database Schema

The API service uses the following tables. Their DDL and initialization image are owned by
[`database-schema`](https://github.com/groovemap-music/database-schema):

- `users` — user accounts (`id`, `email`, `hashed_password`, `is_active`, `created_at`)
- `oauth_tokens` — Discogs OAuth tokens (`user_id`, `provider`, `access_token`, `access_secret`, `provider_username`, `provider_user_id`, `updated_at`)
- `app_config` — admin key-value configuration (`key`, `value`, `updated_at`)
- `app_tokens` — revocable third-party app tokens (`id`, `user_id`, `name`, `scope`, `token_hash`, `created_at`, `last_used_at`, `revoked_at`)
- `provider_aliases` — native id mapping (`provider`, `entity_kind`, `external_id`, `native_id`, `valid_to`) — see [ADR 0009](https://github.com/groovemap-music/design/blob/main/docs/adr/0009-native-identity-and-provider-aliases.md)
- `owned_copies`, `observations`, `collection_snapshots` — the physical-copy and evidence tables ADR 0009 adds; see [Native identity and first-party activity](../docs/identity-and-activity.md)
- `activity.events`, `activity.impressions`, `activity.user_subjects`, `activity.consent_grants`, `activity.erasures` — the month-partitioned behavioural record and its consent/erasure bookkeeping; see [ADR 0010](https://github.com/groovemap-music/design/blob/main/docs/adr/0010-first-party-events-consent-and-deletion.md)

## Deprecations

Per [ADR 0007](https://github.com/groovemap-music/design/blob/main/docs/adr/0007-canonical-media-taxonomy.md),
raw Discogs format names are being superseded by the canonical `media` taxonomy (family and
medium ids). The following are kept for one minor version and will be removed afterward:

| Deprecated                                     | Replacement                                                                 |
| ----------------------------------------------- | ---------------------------------------------------------------------------- |
| `formats` query parameter (gap endpoints)        | `media` query parameter — see [Collection Gap Analysis](#collection-gap-analysis) |
| `GET /api/collection/formats`                    | `GET /api/collection/media`                                                  |
| `formats` field in label DNA responses           | `media` field (`MediaFamilyWeight`, family-grouped with nested mediums) — see [Label DNA](#label-dna) |
| `format_rarity` in the rarity breakdown           | `medium_rarity` (still present at weight `0.0`) — see [Release Rarity Scoring](#release-rarity-scoring) |
| `Release.formats` reads (raw Discogs format list) | `Release` media edges / `media_families` — see [Backfilling `media` on Existing Sync Data](#backfilling-media-on-existing-sync-data) |
| `formats` field in `GET /api/node/{id}?type=release` | `media` field (canonical block) — see [Graph Queries](#graph-queries) |

No endpoint or field is removed yet; all of the above remain readable and are still populated.

## Security

- **Passwords**: PBKDF2-SHA256 (100,000 iterations, random 32-byte salt)
- **Constant-time auth**: Login and registration use constant-time comparison to prevent user enumeration via timing attacks
- **Blind registration**: Duplicate email registration returns the same 201 response to prevent enumeration
- **JWT revocation**: Logout blacklists the JWT's `jti` claim in Redis with TTL matching the token expiry
- **OAuth tokens encrypted at rest**: Discogs OAuth access tokens are encrypted with Fernet symmetric encryption using an HKDF-derived key from `ENCRYPTION_MASTER_KEY`
- **TOTP 2FA**: Optional time-based one-time password with `pyotp`, Fernet-encrypted secrets, SHA-256 hashed recovery codes, brute-force lockout
- **Password reset**: Redis-backed tokens (15min TTL), anti-enumeration responses, session revocation on password change
- **Rate limiting**: register (3/min), login (5/min), sync (10/min), autocomplete (30/min) via slowapi; per-user sync cooldown (60s) in Redis
- **Security response headers**: `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`, `Permissions-Policy`
- **CORS**: Configurable via `CORS_ORIGINS` env var (disabled by default)
- **Snapshots require auth**: `POST /api/snapshot` requires a valid JWT
- **Container**: All endpoints run as non-root container user (UID 1000)

## Monitoring

- Health endpoint at `http://localhost:8005/health`
- Structured logging with visual emoji prefixes
- Health response includes `service`, `status`, and `timestamp` fields
