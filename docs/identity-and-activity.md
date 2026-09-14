# Native identity and first-party activity

This guide covers the native-identity and first-party-events program:
[ADR 0009](https://github.com/groovemap-music/design/blob/main/docs/adr/0009-native-identity-and-provider-aliases.md)
(native identity, owned copies, observations, and the `gm_id` graph projection) and
[ADR 0010](https://github.com/groovemap-music/design/blob/main/docs/adr/0010-first-party-activity-events.md)
(the activity recorder, consent, erasure, and export). Endpoint auth and rate limits for
everything below live with the rest of the API in [`api/README.md`](../api/README.md#api-endpoints);
this page is the "what it does and why" companion.

## Native identity resolution

Every catalog entity — artist, label, master, release — is keyed by a native UUID version 7
that `provider_aliases` maps a provider's id onto. `common.identity.resolve_aliases` is the
only thing that mints one, and it runs on the write side only: the Discogs collection sync
(`api/syncer.py`) calls it once per page, inside the sync's own transaction, for every item it
writes.

Read paths never mint. `api/identity.py` offers the lookup half alone:

- `lookup_native_ids(conn, refs)` — one `SELECT` over the currently-valid alias rows for a
  batch of `AliasRef`s, joined through `unnest`. A ref with no valid alias is simply absent
  from the result.
- `resolve_native_ids` / `native_ids_for` / `native_ids_for_pairs` — the same lookup through
  the configured pool, degrading to `{}` (and logging a warning) on any failure so a native id
  is never the reason a response fails.
- `NativeIdCache` — memoizes both hits and misses for the life of one request, so a response
  that names the same entity more than once (a recommendation set and the gap rows behind it,
  for example) resolves it once.
- `lookup_owned_copy_ids(user_id, release_ids)` — maps a user's Discogs release ids onto the
  owned copy they hold, read from `user_collections.owned_copy_id` rather than the alias table,
  since an owned copy identifies one user's physical object and no provider ever named it.

Provider ids are never dropped from a response; the native id is additive.

**Response fields:**

| Field | Added to | Meaning |
| --- | --- | --- |
| `gm_id` | Search hits, `SimilarArtist`, `EntityRef`/`DiscoveryNode` (explore), `EnhancedRecommendation` | The catalog entity's native id, or `null` when no alias currently resolves it |
| `gm_item_id` | Collection, wantlist, and gap items | The native id of the release the row is about |
| `owned_copy_id` | Collection items only | The native id of the physical copy the user owns for that row, when one has been minted |

### Owned copies and observations

The sync mints one `owned_copies` row per `user_collections` row (`item_id` = the release's
native id, `collection_row_id` = the collection row, `acquired_at` = the Discogs `date_added`)
and links it back through `user_collections.owned_copy_id`. Removing a collection row leaves the
copy in place; `collection_row_id` becomes `NULL` through the foreign key rather than the copy
being deleted, because the copy is evidence the user once owned it.

After a successful sync, one `collection_snapshots` row is written: `taken_at`, `item_count`,
and a sorted `copy_ids` array whose `content_hash` is the SHA-256 of that sorted list.

An observation is user-captured evidence about a copy the caller holds — a matrix inscription,
a grading, a purchase price — asserted by the user rather than derived from a provider listing:

| Method | Path | Auth Required | Description |
| --- | --- | --- | --- |
| POST | `/api/user/copies/{copy_id}/observations` | Yes | Record one observation about a copy the caller owns |
| GET | `/api/user/copies/{copy_id}/observations` | Yes | List observations for a copy the caller owns, newest first |

Both endpoints are owner-scoped: the ownership check travels with the query rather than
preceding it, so a copy that belongs to someone else reads exactly like an unknown one — `404`,
with no hint that the id names a real row. `kind` and `value` are free text; `source` is
validated against the identity vocabulary's alias sources (who or what asserted the fact);
`confidence` is an optional `0.0`–`1.0` float.

### The `gm_id` graph projection job

PostgreSQL's `provider_aliases` table is the authority; Neo4j only ever holds a projection of
it. See [Projecting `gm_id` onto Neo4j Nodes`](../api/README.md#projecting-gm_id-onto-neo4j-nodes)
in the main README for the job itself, its admin trigger (`POST /api/admin/identity/project`),
and its CLI entry point (`catalog-identity-projection`).

## The activity recorder (ADR 0010)

`api/activity.py` is the one writer for every first-party event and recommendation impression,
following the same never-raise-into-the-request shape as `api/audit_log.py`. A caller passes a
user id, an event type, and a payload; it never sees a subject id, a partition, or an exception.

Each write:

1. Resolves the user to a pseudonymous **subject** (`activity.user_subjects`, get-or-create,
   cached per process) — events and impressions reference the subject only, never the account,
   which is what lets the behavioural tables be read without carrying identity and lets erasure
   make the pseudonym unre-associable by deleting one row.
2. Snapshots the **consent purposes** active at that moment (`product_analytics`,
   `model_training`) onto the row, so an old row stays interpretable without reconstructing the
   grant history.
3. Validates the payload against the published, closed vocabulary — every payload schema
   declares `additionalProperties: false`, so an unnamed key is a rejected write, not an ignored
   extra — and builds the envelope through `common.events`.
4. Ensures the month partition the row lands in (`activity.ensure_month_partition`, cached per
   process per month; also run for the current and next month at startup).
5. Inserts, and counts the result on one of two OTEL counters: `groovemap.api.activity_events`
   (attribute `event_type`) for what was written, and a failure counter (attribute `outcome`)
   for what was not.

### Where the vocabulary the implementation resolved differs from a casual reading

The published schemas are narrower than the ADR's prose reads at a glance, and the
implementation follows the schema:

- **Propensity is `1.0` for every policy in production today.** The impression schema requires
  `propensity` and bounds it to `(0, 1]`, so it can never be `null`. Every ranking policy this
  service runs is deterministic — it scores candidates and returns the top slice — so the
  probability the policy assigned to showing an item is `DETERMINISTIC_PROPENSITY = 1.0`, a
  true statement about these policies rather than a placeholder. It stops being true, and a call
  site should pass its own, the moment a sampled policy ships.
- **`search.query`'s `types` filter travels inside `filters`.** The published payload is closed
  over `query`, `filters`, `result_count`, and `request_id`; the entity-type restriction has
  nowhere of its own to go, so it is folded into `filters` under a `type:` prefix beside the
  genre, media, and year-bound entries (e.g. `type:release`, `genre:techno`, `year_min:1990`).
- **`search.result_impression` is one event per result, not one event with an ordered list.**
  The published payload is closed over `impression_id`, `item_id`, `position`, and `request_id`
  and describes a single result at a single position, so a twenty-hit page emits twenty events
  (batched into one round trip via `record_events`).
- **`recommendation.shown` is never emitted.** The epic design considered logging a candidate
  with no native id as a `recommendation.shown` event carrying the provider id, but that
  payload's closed key set (`impression_id`, `candidate_set_id`, `policy_id`, `item_count`) has
  nowhere to put a provider id. Such a candidate is skipped and counted on the
  `ACTIVITY_NO_NATIVE_ID` failure outcome instead, which keeps the gap visible without inventing
  a key the vocabulary doesn't carry.
- **The outcome endpoint requires `item_id`.** The published `impression_outcome` payload
  requires both `impression_id` and `item_id` and is closed over them; `POST
  /api/activity/events` therefore takes both in its request body rather than looking the item
  up from the stored impression (which is partitioned by occurrence time, not keyed for that
  lookup).
- **`account.export_requested` and `account.erasure_requested` carry `export_id`/`format` and
  `erasure_id`.** Each is minted fresh at request time and put in the event payload, ahead of
  (and independent from) the ids the endpoints return in their HTTP response.

### Emission points

| Surface | Event type(s) | Where |
| --- | --- | --- |
| Search | `search.query`, `search.result_impression` | `GET /api/search` — one query event plus one impression event per hit shown |
| Recommendations | impressions under `SURFACE_RECOMMENDATION` | The four ranked endpoints below |
| Recommendation outcome | `recommendation.opened`, `recommendation.saved`, `recommendation.dismissed`, `recommendation.hidden` | `POST /api/activity/events`, client-reported |
| Collection sync | `collection.item_added`, `collection.item_removed`, `collection.item_updated`, `wantlist.item_added`, `wantlist.item_removed` | The sync diff, from `api/syncer.py` |
| Consent | `consent.granted`, `consent.revoked` | `PUT /api/user/consent/{purpose}` |
| Erasure / export | `account.erasure_requested`, `account.export_requested` | `POST /api/user/erasure`, `GET /api/user/export` |

**Recommendation impressions.** Every served candidate on a ranked surface is recorded as one
impression via `stamp_recommendation_impressions`, which mints impression ids *after* the
response body is filled (two of the three surfaces cache their body in Redis, and an impression
records a list having been *shown* — reusing ids from the request that populated the cache would
misattribute every later cache hit to the request that built it). Every served item gets an
`impression_id` key: `null` when the candidate had no native id (counted, never recorded) or
when the write itself failed, so a client can never report an outcome against a row that does
not exist.

| Policy id | Endpoint |
| --- | --- |
| `similar_artist_weighted_cosine_v1` | `GET /api/recommend/similar/artist/{artist_id}` |
| `explore_personalized_v1` | `GET /api/recommend/explore/{entity_type}/{id}` |
| `user_recommendations_artist_v1` | `GET /api/user/recommendations?strategy=artist` (default) |
| `user_recommendations_multi_v1` | `GET /api/user/recommendations?strategy=multi` |

**Reporting an outcome:**

| Method | Path | Auth Required | Description |
| --- | --- | --- | --- |
| POST | `/api/activity/events` | Yes | Record one client-reported outcome against an impression |

The body carries `event_type` (one of the four outcomes above), `impression_id`, and `item_id`;
any other `event_type` is rejected as a `422` by the request model before it reaches the
recorder. The write is idempotent on `{event_type}:{impression_id}`, so a retried report is a
no-op. Returns `202` — the row is written before the response, but the caller is told the
outcome was accepted, not that an analysis has consumed it.

## Consent

Two purposes, `product_analytics` and `model_training`, each independently grantable and
revocable:

| Method | Path | Auth Required | Description |
| --- | --- | --- | --- |
| GET | `/api/user/consent` | Yes | Both purposes with their current grant/revocation state |
| PUT | `/api/user/consent/{purpose}` | Yes | Grant or revoke consent for one purpose |

`GET` always reports both purposes, granted or not, so a client renders the same two controls
before and after the first decision. `PUT` is idempotent in both directions — granting what is
already granted, or revoking what is already revoked, both succeed and change nothing — and
emits `consent.granted` / `consent.revoked` only when the state actually changed, since a
repeated request is not a second decision.

## Erasure

`POST /api/user/erasure` (require current password; a TOTP code as well when the account has
2FA enabled — the same re-authentication `POST /api/auth/2fa/disable` requires, because a
leaked bearer token should not be enough to destroy an account) erases everything keyed to the
caller, across every store, in this order:

1. **PostgreSQL, one transaction**, under `SET LOCAL groovemap.erasure = 'on'` (the bypass the
   immutability trigger on `activity.events`/`activity.impressions` requires, scoped to this
   transaction only so it cannot leak to the next statement on a pooled connection):
   - Delete `activity.events` and `activity.impressions` by subject id, then
     `activity.user_subjects` — so the pseudonym can never be re-associated with the account
     even if a row survived somewhere else.
   - Insert one `activity.erasures` row (`model_versions_before = []` until a model registry
     exists — deleting rows does not retrain a model already trained on them).
   - Delete `observations`, `collection_snapshots`, `owned_copies`, `user_collections`,
     `user_wantlists`, `sync_history`, `app_tokens`, and OAuth tokens, in that dependency order
     (leaves before the rows they reference).
   - Soft-erase the `users` row in place: `email` becomes `erased+<id>@invalid.groovemap`,
     `hashed_password` a fresh unusable random hash, `is_active = false`, every TOTP column
     cleared. The row is not deleted — two foreign keys to `users` carry no cascade rule, and
     erasing in place needs no constraint change and cannot orphan an unrelated audit trail.
2. **Neo4j**: `DETACH DELETE` the user's node.
3. **Redis**: `RecommendCache.invalidate_user`, plus the snapshot user-count key and any sync
   lock/cooldown keys, then a verification scan for surviving per-user recommendation keys — an
   erasure is not a request path, so a survivor is reported, not silently retried.
4. **Revoke the caller's own token.**

Consent revocation is not a prerequisite; erasure implies it. `account.erasure_requested` is
emitted *before* the procedure runs (so a concurrent reader sees the request in the stream) and
is then itself deleted along with every other event for the subject — the durable record of the
erasure is the `activity.erasures` row, which survives.

**Partial cross-store failure.** The relational transaction commits first; a failure in Neo4j or
Redis after that point cannot be rolled back, so each of those two steps is verified
independently and reported rather than hidden. The response is `202`:

```json
{
  "erasure_id": "...",
  "events_deleted": 42,
  "impressions_deleted": 17,
  "incomplete": []
}
```

`incomplete` is a list of human-readable failure descriptions — one entry per store step that
did not complete cleanly (e.g. `"Neo4j deletion failed: ServiceUnavailable"`, or `"Redis
deletion incomplete: 2 recommendation key(s) survived"`) — empty when every step succeeded. The
PostgreSQL half is never reported as incomplete: if it failed, the transaction rolled back and
the whole request raised instead of returning `202`.

## Export

`GET /api/user/export` streams everything keyed to the caller as `application/x-ndjson`, one
JSON object per line, each shaped `{"kind": "...", "record": {...}}`. Sections come in a stable
order over one PostgreSQL connection, so two exports of unchanged data are byte-identical:

1. `event` — every `activity.events` row for the subject
2. `impression` — every `activity.impressions` row for the subject
3. `collection_item` — `user_collections` rows
4. `wantlist_item` — `user_wantlists` rows
5. `owned_copy` — `owned_copies` rows
6. `observation` — `observations` rows
7. `collection_snapshot` — snapshot ids and shape only (`id`, `taken_at`, `item_count`) — the
   `copy_ids` array is not repeated here since the owned copies it names are already exported in
   full in section 5
8. `consent_grant` — `activity.consent_grants` rows

`account.export_requested` is emitted before streaming begins. The response carries
`Content-Disposition: attachment; filename="groovemap-export.ndjson"`.

## See also

- [`api/README.md`](../api/README.md) — auth requirements, rate limits, and the `gm_id`
  projection job's admin trigger and CLI.
- [Architecture decisions](architecture-decisions.md) — the accepted-decisions summary.
