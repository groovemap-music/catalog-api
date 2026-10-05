# Changelog

All notable changes to this project will be documented here by Commitizen.

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
