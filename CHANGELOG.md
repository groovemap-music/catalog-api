# Changelog

All notable changes to this project will be documented here by Commitizen.

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
