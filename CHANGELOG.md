# Changelog

All notable changes to this project will be documented here by Commitizen.

## Unreleased

### Feat

- **label**: label details (`GET /api/node/{id}?type=label`) add `longevity_rank` and `longevity_rank_of` (int or null), read from discogs-graph-enricher v0.9.0's `Label.longevity_rank` and the newest ExtractionCompletion's `label_longevity_rank_of` in the same query as the stats_version gate. Both are null unless the newest node is maintained with `stats_version >= 4` (no live fallback); a label with no stored rank has `longevity_rank` null and `longevity_rank_of` set. Published in the graph-explorer v1 contract (`node_label`).

## v0.13.0 (2026-11-09)

### Release notes

First release cut from main since v0.11.1. It carries everything in v0.12.0 plus:

- **Fix: label details for a fresh install** (gm-catalog-api-adq2). The stored-longevity gate in label details, and the same gate in genre/style trends, used a non-optional `MATCH` on `ExtractionCompletion`, so a database with no completion node returned no label (and no trends). Both gates now always yield one row and fall back to the live traversal. v0.12.0 has the bug; do not deploy v0.12.0.
- **Missing previews and owned-copy listing** (gm-catalog-api-t3e9 / 8t16, operator-authorized for release 2026-11-09): bounded, session-owned scope previews (GM06) and an owner-scoped copy listing (GM10), published in the graph-explorer producer contract. graph-explorer's Missing and copy-selection UI needs this release.
- **Prototype cores** (gm-catalog-api-ifqv): offline/bounded evaluation modules (runout interpreter, NLQ baseline evaluator, discovery policies, pursuit preview, crate coverage, pressing comparison, authority-separated evidence readers, copy selection reader), with baselines in docs/prototypes/README.md. They add no new public behaviour beyond the routes above.

No schema change; runs on database-schema 0.12.0 or later (lux runs 0.13.0).

### Feat

- **label**: add is_placeholder and longevity fields to label details
- **admin**: add POST /api/admin/dlq/replay/{queue}
- **contract**: publish Missing and copy routes
- **missing**: serve bounded session-owned scope previews
- **observations**: expose owner-scoped copy listing
- **evidence**: add authority-separated readers
- **pressing**: compare known release candidates
- **crate**: compare bounded joint coverage against fit and popularity
- **observations**: add bounded copy selection reader
- **evaluation**: add replayable discovery policies
- **scope**: add transient pursuit preview core
- **nlq**: add offline baseline evaluator
- **runout**: add pure interpreter and fixture evaluator

### Fix

- **label**: lint fixup
- **label**: always return the label row when no ExtractionCompletion exists
- **admin**: return partial DLQ replay count as fields on 502

### Refactor

- **search**: split decade facet into masters and releases fragments

### Perf

- **trends**: read the stored genre/style release series, live traversal as fallback
- **search**: skip release facets for searches that exclude releases
- **search**: count every match in the release facets
- **search**: serve facets and filtered release branches from release_search
- **search**: read the decade facet's masters half from public.master_search
- **search**: rank master matches from public.master_search

## v0.12.0 (2026-11-09)

### Release notes

Three additions, cut on the release branch `release/v0.11.x-search` (v0.11.4 plus gm-catalog-api-axbs, gm-catalog-api-vflb and gm-catalog-api-3uvj only), so it still carries none of the unreleased prototype or Missing/copy serving source on main.

- **Dead-letter replay** (`POST /api/admin/dlq/replay/{queue}?limit=N`, 1..5000): the same admin auth and queue allow-list as purge. Each message is fetched unacked, republished to its work queue with a publisher confirm and `mandatory`, and acked only after the confirm; an unroutable return or a failed confirm leaves it in the DLQ and stops the replay; `x-*` headers are dropped. It returns `{queue, replayed, remaining}` and writes a `dlq.replay` audit row. On a partial failure the 502 body carries `{detail, replayed, remaining}`, where `remaining` is null when the depth is unknown and 0 when the queue is empty. Published in the operations-console v1 contract (`admin_dlq_replay`, with `error_fields`).
- **Label longevity on label details** (`GET /api/node/{id}?type=label`): `is_placeholder`, `first_year`, `last_year`, `years_active`, `total_releases` and `peak_decade`, computed by label id and matching `/api/insights/label-longevity`. They are read from the fields discogs-graph-enricher v0.8.0 stores on Label nodes when the newest ExtractionCompletion has `stats_version >= 3`, and computed live otherwise. Published in the graph-explorer v1 contract (`node_label`).

No schema change; runs on database-schema 0.12.0 or later, like v0.11.2.

### Feat

- **label**: add is_placeholder and longevity fields to label details
- **admin**: add POST /api/admin/dlq/replay/{queue}

### Fix

- **admin**: return partial DLQ replay count as fields on 502

## v0.11.4 (2026-11-09)

### Release notes

Genre and style trends read the per-year release series that discogs-graph-enricher v0.7.0 stores on each Genre and Style node (`release_years`, `release_year_counts`) instead of traversing every release on request: one point read, about 9 database hits instead of about 20.7M for Rock, with results equal to the live traversal row for row (checked for Rock and Trance on lux). The stored series is used only when the newest ExtractionCompletion has `maintained_at` and `stats_version >= 2`; otherwise, or when a node has no series, the unchanged live traversal answers, so it is safe on an older enricher. Cut on the release branch `release/v0.11.x-search` (gm-catalog-api-2hrn only), so it still carries none of the unreleased prototype or Missing/copy source on main.

### Perf

- **trends**: read the stored genre/style release series, live traversal as fallback

## v0.11.3 (2026-11-09)

### Release notes

A search restricted to types that exclude releases (for example `types=master`) no longer computes the genre and media facets, or the releases half of the decade facet, over every matching release. Those queries ran concurrently and set a ~0.6 s floor on master search (584 ms for "love" on lux after v0.11.2) although the master query and count take ~40 ms and ~15 ms. For such searches `facets.genre` and `facets.media` are now empty, and the decade facet counts only the requested types (it previously also counted releases the search did not return). Mixed-type searches, results, totals and pagination are unchanged. Same requirement as v0.11.2: database-schema 0.12.0 or later. Cut on the release branch `release/v0.11.x-search` (gm-catalog-api-zi2r only), so it still carries none of the unreleased prototype or Missing/copy source on main.

### Perf

- **search**: skip release facets for searches that exclude releases

## v0.11.2 (2026-11-09)

### Release notes

Search reads the side tables database-schema v0.12.0 maintains instead of the full documents. The genre, decade and media facets and every filtered release branch (year, genre, media, country) read `public.release_search`, and master search, its count, its year filter and the masters half of the decade facet read `public.master_search`. The release facets now count every match instead of the first 10,000 (exact counts; about 0.5-0.8 s per facet for the most common terms on lux). Results are otherwise unchanged. On lux: the "love" genre facet went from 365 ms to 112 ms capped (580 ms uncapped and exact), decade 686 to 209 ms, media 192 to 103 ms, the "love" + Jazz + 1970 + vinyl filtered results 10.4 s to 0.67 s, and ranked master search 140 to 39 ms. Requires database-schema 0.12.0 with `release_search_drift()` and `master_search_drift()` both `(0,0,0)`; on an older schema these queries fail. Cut from v0.11.1 on the release branch `release/v0.11.x-search` with only the search commits (gm-catalog-api-2jka, gm-catalog-api-4asm), so it carries none of the unreleased prototype or Missing/copy serving source on main.

### Refactor

- **search**: split decade facet into masters and releases fragments

### Perf

- **search**: count every match in the release facets
- **search**: serve facets and filtered release branches from release_search
- **search**: read the decade facet's masters half from public.master_search
- **search**: rank master matches from public.master_search

## v0.11.1 (2026-11-08)

### Release notes

The ledger works read with a time window now reads the unwindowed per-work summary when the window holds every event of the filter, which gives an identical result: 2.73 s down to 0.28 s on lux for 2026-11. Otherwise it reads full days from `public.identity_ledger_work_summary_day` and only the edge days from raw events. No new schema floor: this release runs on database-schema v0.11.0, and the rollup path needs a filled `work_summary_day`, which database-schema v0.12.0 requests as a rebuild. Results are unchanged.

### Perf

- **ledger**: read windowed works from the per-day rollup

## v0.11.0 (2026-11-07)

### Release notes

Deploy after database-schema v0.11.0 is applied and `public.release_search` is backfilled: `SELECT * FROM public.release_search_drift()` must return `(0,0,0)`. Unfiltered release search now ranks from that table, so on lux the first `love` search fell from 16.7 s to 0.58 s with identical results. The graph-explorer contract gains `insights_label_longevity` with `include_placeholders` and `limit` (1-200). Label DNA and ledger routes do less work per request. No API shape changes for existing callers.

### Feat

- **insights**: declare label-longevity parameters in the graph-explorer contract

### Perf

- **search**: rank release matches from public.release_search
- **label-dna**: read media family and medium counts in one pass
- **ledger**: compute the windowed works aggregation once

## v0.10.0 (2026-11-07)

This release was first prepared as v1.0.0 because Commitizen took the breaking change past 0.x without major_version_zero. Going to 1.0 is a product decision, so it ships as v0.10.0 and 0.x bumps now stay below 1.0.

### Release notes

No schema change. Breaking for internal callers only: GET /api/internal/insights/rarity-scores is removed, and internal-insights v2 is published while v1 stays frozen for analytics-engine, which no longer calls the route. The rarity membership check runs once per completed run, in the background. The rarity integration tests no longer interfere, so CI is green.

### Feat

- **contracts**: remove the whole-catalog rarity-scores route in internal-insights v2

## v0.9.1 (2026-11-07)

### Release notes

No schema change. The rarity membership check runs once per completed rarity run, in the background, instead of a 4 to 6 s table scan on one request every 15 minutes.

### Perf

- **rarity**: take the membership table scan off the request path

## v0.9.0 (2026-11-06)

### Release notes

Deploy after analytics-engine v0.6.2 and graph-explorer v0.3.3: artists and labels now return a null completeness_pct. Rarity by label and artist runs inside PostgreSQL, about 0.7 s instead of 15.7 s for the largest label. Autocomplete ranks established artists first among equal matches. No schema change.

### Feat

- **explore**: rank established artists first among equal autocomplete matches

### Fix

- **insights**: report null completeness for artists and labels

### Perf

- **rarity**: filter by label and artist inside PostgreSQL

## v0.8.0 (2026-11-06)

### Release notes

No schema change and no required deploy order; deploy catalog-api alone.

- The internal-insights contract is 1.10.0: 1.8.0 gives genre-trends a published 300 s processing budget, 1.9.0 does the same for label-longevity, and 1.10.0 adds `include_placeholders` to label-longevity. analytics-engine's default 900 s read timeout is above every one of these budgets, so no analytics-engine release is needed.
- style-landmarks, style-cotags and label-longevity now complete on the full catalog: each counts one group at a time instead of in one pass. Label-longevity runs under a 300 s budget, and a Neo4j timeout or memory failure on any of the three is a retryable 503, never empty data. Genre-trends does the same.
- The label-longevity route leaves Discogs placeholder labels (such as Not On Label) out by default; `include_placeholders=true` brings them back and every row carries `is_placeholder`.
- `CATALOG_CACHE_VALKEY_HOST` is optional. Unset, the single Valkey client is used exactly as before; set, catalog caches go to a second allkeys-lru instance.

### Feat

- **insights**: leave placeholder labels out of label longevity by default
- **contracts**: pin the MusicBrainz relationship types through the enricher's contract
- **cache**: send catalog caches to a dedicated evicting Valkey

### Fix

- **insights**: count landmarks, co-tags and label longevity one group at a time
- **insights**: run genre trends under a long read budget and fail retryably on timeout
- **admin**: record a finished extraction as completed, not waiting
- **extraction-analysis**: list extraction versions that have no violations
- **cache**: fail an erasure or an invalidation when the cache instance is down

### Perf

- **explore**: label the target of the hub witness query so the planner starts from the indexed node

## v0.7.0 (2026-11-05)

### Release notes

Deploy order: **catalog-api v0.7.0 first, then database-schema v0.7.0 on lux, then analytics-engine v0.6.0.**

- This release pins database-schema v0.7.0 and reads the Identity Ledger by relation name, whatever its kind, so it runs on the v0.6.x materialized views and on the v0.7.0 tables. A catalog-api older than this one answers 503 on the ledger routes once the schema is applied. Applying the schema first is therefore wrong; the first ledger refresh after the schema upgrade rebuilds the tables.
- analytics-engine v0.6.0 writes `label_ids` and `artist_ids` into `insights.release_rarity` and needs contract 1.7.0 (this release) and schema v0.7.0. Run before the schema is live and every rarity run fails with `UndefinedColumn`.
- The rarity table stays empty, and the rarity routes return no rows, until one complete analytics-engine rarity run finishes after both upgrades. The previous table is replaced in one transaction, so nothing partial is ever visible.

### Feat

- **schema**: pin database-schema v0.7.0 and treat the ledger state row as "refreshed"
- **rarity**: emit label_ids and artist_ids in the rarity rows
- **rarity**: page a tier on the leaderboard by an opaque server-made keyset cursor
- **cache**: let a costlier entry evict the lightest one when a cache budget is full
- **insights**: serve the rarity walk as stateless pages, contract 1.6.0
- **import-cache**: alert when a version waits hours on the enricher's maintenance pass
- **telemetry**: time each Cypher statement by its query function name
- **search**: bound search slots and statement time so cold searches cannot starve logins

### Fix

- **rarity**: bind the leaderboard cursor to its tier and reject values Postgres would refuse
- **rarity**: page a tier by keyset with the score bound as REAL
- **autocomplete**: keep a prefix on every token so plain input matches what it did before
- **autocomplete**: lower-case tokens so a typed AND, OR or NOT is a word, not an operator
- **autocomplete**: cut input into analyzer tokens and require all of them
- **cache**: admit on the entity's release count, not on measured latency
- **cache**: keep cheap results from taking cache budget slots
- **recommend**: start label hub paths at the label name and release hub locks by token
- **recommend**: masters in hub scans, placeholder demotion, resumable hub build, lock, one row per neighbour
- **collection**: break gap-page ties on release id
- **credits**: page over distinct credits so every credit appears exactly once; record limit/offset in the contract
- **import-cache**: reset budget indexes, wait for enricher maintenance, make a watcher that cannot start loud
- **insights**: leave the always-zero image field out of the completeness percentage
- **cache**: bound the budgeted caches in bytes as well as keys
- **cache**: apply a key budget to the Explore, trends and strata caches
- **ledger**: find the identity ledger views by name, whatever their relkind
- **telemetry**: name a statement by its first public caller and tag its operation
- **expand**: import time for the cache budget after the genre tree cache left explore.py
- **insights**: stream rarity-scores instead of holding a row per release
- **genre-tree**: retry the first build on a short backoff while no tree exists
- **expand**: cap the number of live expand cache keys globally
- **expand**: write only bounded, expensive expand pages to the shared Valkey
- **explore**: invalidate only once the graph's own extraction latch is complete too
- **explore**: drop Explore and trends caches when a Discogs import completes
- **neo4j**: give every request-path read a 30 s server timeout and map timeouts to 504
- **network**: bound the direct expansion of a hub root and flag the result truncated

### Perf

- **autocomplete**: boost whole-name matches in the Lucene query and pass the limit to Lucene
- **label-dna**: stored identity counts, one year read, one genre and style pass, HAS_STYLE for similar labels behind a completeness marker
- **label-dna**: stored identity counts, one year read, one genre and style pass, HAS_STYLE for similar labels
- **recommend**: exact genres and styles and a stable nearest-100 of artists and labels in explore
- **musicbrainz**: count enrichment status one relationship type at a time, and cache it
- **collection**: cut the page before enriching it in collection, wantlist and gap queries
- **credits**: paginate /api/credits/person with a total and rank second hops before the slice
- **explore**: read an artist's release and label counts from stored stats
- **rarity**: answer by-label and by-artist from an empty rarity table without reading ids from Neo4j
- **network**: skip the live genre read for artists the enricher already swept
- **network**: rank cluster neighbours first, then read primary genre
- **insights**: read each document once in the data-completeness counts
- **explore**: read the enricher's stored counts for node details and expand totals
- **strata**: cache shared-personnel edges by the drawn masters and limit
- **graph**: count each label with a bare aggregation so get_graph_stats uses the count store
- **musicbrainz**: type the artist relationships query on the enricher's types
- **genre-tree**: serve /api/genre-tree from a stored tree rebuilt in the background
- **expand**: order before_year pages by index and cache expand pages in Valkey
- **network**: collect the collaborator set once and return page and total from one statement
- **search**: count totals without ranking or sorting
- **search**: rank before reading documents so common terms stop taking seconds

## v0.6.0 (2026-10-30)

### Feat

- **valkey**: adopt shared Valkey TLS settings from python-libraries
- **valkey**: namespace every key and keep pre-namespace security keys working across the upgrade
- **admin**: refresh the precomputed Identity Ledger after identity runs and loads
- **admin**: read the Identity Ledger list and density from the precomputed views
- **admin**: purge dead-letter queues over AMQP instead of the management API
- **admin**: serve the Identity Ledger as three read-only admin_identity_ledger_* routes
- **identity**: backfill the Identity Ledger's link history from the MusicBrainz full export
- **admin**: persist the extraction source on extraction_history
- **insights**: publish style co-tag pairs and proxy the per-style read, contract 1.5.0
- **insights**: publish landmark masters per style, contract 1.4.0
- **metrics**: read queue history from RabbitMQ /metrics/detailed
- **admin**: add GET /api/admin/auth/session and publish it as admin_session
- **strata**: accept pressing focus cursors and add owned copy ids to signed-in release nodes
- **fit**: add session-only evidence_entries with typed entity ids
- **taste**: add GET /api/user/taste-projection for Taste Strata
- **insights**: add genre_years to style-year-counts, contract 1.3.0
- **insights**: proxy GET /api/insights/style-atlas and add it to the graph-explorer contract
- **insights**: add internal style-year-counts endpoint, contract 1.2.0
- **explore**: cache strata catalogs, refuse roots above 134,051 releases, bound every read
- **explore**: add the strata builder, reads, and GET /api/explore/strata
- **recommend**: serve similar artists from precomputed lists behind a switch
- **telemetry**: make Valkey cache-write failures visible

### Fix

- **valkey**: size the legacy deny window to token lifetimes and run migration once
- **rarity**: exclude zero-master hubs before sibling expansion (#37)
- **rarity**: exclude zero-master sentinel siblings
- **deps**: upgrade msgpack past CVE-2026-114613
- **rarity**: exclude zero-master hubs before sibling expansion
- **activity**: key erasure and export request events on the request, not the account
- **erasure**: record how many idempotency keys an erasure removed
- **activity**: count a retry the schema skipped as a duplicate, not a write
- **insights**: make the analytics-engine client URL configurable
- **performance**: send /api/expand the entity name and flag empty results
- **deps**: regenerate the dependency license inventory after the uv-version bump (#29)
- **admin**: record the extraction run's own source entity types
- **admin**: require explicit extraction source, reject unknown in _extractor_base_url
- **admin**: dispatch extraction triggers to the requested source's extractor
- **deps**: upgrade filelock to 4.2.1 past CVE-2026-112940
- **deps**: upgrade certifi and opentelemetry past PYSEC-2026-4320 and CVE-2026-114603
- **syncer**: keep stored date_added when a synced item omits it
- **valkey**: return 503 and keep state consistent when Valkey rejects writes
- **auth**: write password_changed marker before committing password changes
- **licenses**: synchronize locked dependency notice versions

### Refactor

- **identity**: expose the identity-maintenance system actor helper
- **admin**: retire the PostgreSQL metrics collector and history routes

### Perf

- **credits**: serve the role leaderboard from precomputed credit counts
- **admin**: add a deterministic Identity Ledger scale generator and harness

## v0.5.0 (2026-10-05)

### Feat

- **valkey**: migrate catalog client preserving security and compatibility (#20)
- **valkey**: migrate catalog client and operator names preserving security contracts
- **lookup**: resolve UPC-A, EAN-13, and zero-led GTIN-14 barcodes as one GTIN (#15)
- **lookup**: resolve barcode UPC-A and EAN-13 forms as one GTIN
- **lookup**: add LookupMatch and an additive matches field
- **lookup**: add batched multi-form provider_alias resolution
- **recommend**: score every shared-signal artist for similar-artist candidates
- **identity**: merge re-attached items and remove the dependents guard
- **identity**: add the native-id merge steps and their exact revert
- **privacy**: erase and export the catalog-item merge ledger
- **identity**: re-attach and project gm_id on a completed Discogs extraction latch
- **identity**: re-attach load-order-split catalog items to their Discogs native id
- **identity**: add a read-only census of load-order-split catalog items
- **explore**: add PostgreSQL exploration query family
- **recommendations**: move fit and recommendation reads to postgres
- **collection**: move read families to postgres
- **label-dna**: add postgres backend family
- **credits**: route the credits router through the graph-backend seam
- **graph-backend**: register the credits family behind a protocol
- **credits**: answer the credits family from postgres
- **paths**: serve bounded traversals from postgres
- **musicbrainz**: serve enrichment reads from postgres
- **insights**: serve graph computations from postgres
- **graph**: register the rarity family in the backend seam
- **rarity**: answer the rarity signal batch from postgres
- **graph-backend**: register the vertex-lookup and store-statistics families
- **admin**: add the postgres storage-panel query, replacing apoc.meta.stats
- **catalog**: add postgres year-range and graph-stats lookups
- **gap**: add postgres label/artist/master metadata lookups
- **collaborators**: add the postgres artist-identity lookup for the Explore endpoint
- **explore**: apply the backend-neutral error mapping to one_hop_collaborators
- **api**: serve autocomplete through the graph-backend seam
- **autocomplete**: answer the name searches from the trigram indexes
- **nlq**: route the get_collaborators tool through the graph-backend seam
- **explore**: route get_collaborators/count through the graph-backend seam
- **graph**: register the one_hop_collaborators family on the seam
- **collaborators**: add the one-hop GRAPH_TABLE backend over graph.catalog
- **graph**: map PostgreSQL statement timeouts to the same 504 as Neo4j
- **network**: serve the collaborators family from graph.catalog with GRAPH_TABLE
- **graph**: introduce GRAPH_BACKEND setting and a per-family backend selector

### Fix

- **parity**: reinstate the wpku family registrations dropped by the main refresh
- **identity**: write the re-attachment run's audit entry before its identity writes (#14)
- **identity**: write the re-attachment run's audit entry before its identity writes
- **build**: merge the duplicated ruff per-file-ignores table
- **recommend**: restore the min-releases floor and measure realistic latency
- **explore**: follow alias-to-primary edge direction
- **network**: serve artist centrality through selected backend
- **explore**: route REST and NLQ through selected backend
- **tests**: remove catalog integration volumes
- **credits**: cover the backend error mapping, and stop it colliding
- **rarity**: allow postgres mode without neo4j
- **rarity**: call every family function with the family handle
- **explore**: patch the identity lookup's actual backend after 91a.2
- **graph**: separate backend unavailability from query timeouts

### Refactor

- **graph**: remove the SQL PGQ backend
- **rarity**: drive the signal batch off a backend-neutral walk

### Perf

- **recommend**: cap and parallelize similar-artist candidate profiling

## v0.4.0 (2026-09-15)

### Feat

- **activity**: record fit impressions against the literal fit surface
- **fit**: add structured evidence_items beside each component's sentences

## v0.3.0 (2026-09-15)

### Feat

- **search**: add the country facet and identifiers, companies, and country on release detail
- **lookup**: resolve barcodes, catalogue numbers, and matrix inscriptions through provider_aliases
- **fit**: serve GET /api/fit/release/{release_id} with caching and impressions
- **fit**: implement the CrateFit v0 scoring with evidence and confidence
- **fit**: add the collection id-set and release-context queries
- **evaluation**: add the time split, metrics, report recipe, and snapshot
- **evaluation**: freeze the heuristic weights as a versioned baseline
- **evaluation**: answer the query shapes from an in-memory golden graph
- **evaluation**: add the format-balanced synthetic golden set

## v0.2.0 (2026-09-14)

### Feat

- **auth**: accept scoped app tokens on the activity, consent, and observation routes
- **contracts**: publish the identity and activity consumer contracts
- **api**: add the erasure and export endpoints with cross-store closure
- **api**: add the consent endpoints
- **api**: log search events, recommendation impressions, and outcomes
- **activity**: add the in-process activity recorder
- **sync**: resolve native ids, mint owned copies and snapshots, and emit change events
- **api**: expose native ids on responses and add copy observations
- **identity**: add the gm_id projection job with admin trigger and CLI
- **telemetry**: open the api.sync and api.nlq spans and sample event-loop lag
- **contracts**: declare the unmapped media route for the console
- **admin**: aggregate unmapped media names per provider
- **node**: attach the canonical media block to release node responses
- **contracts**: publish media-aware consumer contracts and media-neutral docs
- **nlq**: expose media as a filter dimension and gap-tool parameter
- **collection**: add media filters and the collection media endpoint
- **rarity**: split scoring into a media-neutral core and per-family extensions
- **search**: add a media facet and filter to search
- **sync**: store canonical media on collection and wantlist sync
- **label-dna**: report media profiles by family and medium
- **telemetry**: export OpenTelemetry metrics from catalog-api

### Fix

- **sync**: conform change event payloads to the published schemas
- **contracts**: correct audit provenance and docs
- **ci**: accept commitizen's no-eligible-commits bump-preview state
- **build**: use uv run python for check-contracts in source-check
- **ci**: use public python libraries

### Refactor

- **auth**: centralize JWT validation policy
- **ci**: normalize validation recipes
- **api**: clarify composition lifecycle
- **extraction**: separate routing policy
- **queries**: consolidate neo4j templates

## v0.1.1 (2026-08-31)

### Fix

- **ci**: accept release-boundary bump states

## v0.1.0 (2026-08-31)

The `v0.1.0` workflow failed before publishing artifacts or images. The tag is retained as an immutable record of that release attempt.
