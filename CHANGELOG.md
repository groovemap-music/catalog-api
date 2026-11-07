# Changelog

All notable changes to this project will be documented here by Commitizen.

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
