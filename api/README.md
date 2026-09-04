# GrooveMap catalog API service

Repository-wide configuration, operations, and design guidance is indexed in
[`docs/README.md`](../docs/README.md).

Provides user account management, JWT authentication, and Discogs OAuth 1.0a integration for GrooveMap.

## Overview

The API service:

- Handles user registration and password-based login
- Issues and validates HS256 JWT access tokens
- Manages the Discogs OAuth 1.0a OOB flow for users
- Stores Discogs OAuth access tokens in PostgreSQL
- Reads Discogs app credentials from the `app_config` table (set via `discogs-setup` CLI)

## Architecture

- **Language**: Python 3.14 (managed runtime: 3.14.5)
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

### Discogs OAuth Flow

The API implements Discogs OAuth 1.0a OOB (out-of-band) flow:

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

## API Endpoints

### Authentication

| Method | Path                 | Auth Required | Rate Limit | Description                      |
| ------ | -------------------- | ------------- | ---------- | -------------------------------- |
| POST   | `/api/auth/register` | No            | 3/min      | Register a new user account      |
| POST   | `/api/auth/login`    | No            | 5/min      | Login and receive JWT token      |
| POST   | `/api/auth/logout`   | Yes           | —          | Revoke JWT token (JTI blacklist) |
| GET    | `/api/auth/me`       | Yes           | —          | Get current user details         |

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

### App Tokens

Manage third-party app tokens for the authenticated user. The plaintext token is returned exactly once, at creation; only its SHA-256 hash is persisted thereafter.

| Method | Path                          | Auth Required | Description                                     |
| ------ | ----------------------------- | ------------- | ------------------------------------------------ |
| POST   | `/api/user/app-tokens`        | Yes           | Mint a new app token (plaintext returned once)  |
| GET    | `/api/user/app-tokens`        | Yes           | List active and revoked tokens for the user     |
| DELETE | `/api/user/app-tokens/{id}`   | Yes           | Revoke (tombstone) a token                      |

**Allowed scopes:** `collection:read`

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
- `year_min` — Minimum release year (1000–9999)
- `year_max` — Maximum release year (1000–9999)
- `limit` — Results per page (1–100, default: 20)
- `offset` — Pagination offset (default: 0)

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

**Query parameters for `/api/recommend/similar/artist/{artist_id}`:**

- `limit` — Number of similar artists to return (1–50, default: 20)

**Query parameters for `/api/recommend/explore/{entity_type}/{id}`:**

- `entity_type` — One of `artist`, `label`, `genre`, `style`
- `hops` — Number of hops to traverse (1–3, default: 2)
- `limit` — Maximum discoveries to return (1–50, default: 10)

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
uv sync --all-extras

# Run the API service
uv run python -m api.api
```

### Running Tests

```bash
# Run API tests
uv run pytest tests/api/ -v

# Run with coverage
just test-api
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
