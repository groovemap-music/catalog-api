# SQL performance audit, 2026-11-04

Audit of every PostgreSQL statement in `catalog-api` (bead `gm-catalog-api-6zq4`). Read-only: no
production code changed, and nothing on lux was written. Cypher is out of scope and belongs to
`gm-catalog-api-0n2z`. Ledger route latency stays with `gm-catalog-api-kmlo`.

## Summary

Point lookups are healthy. The real cost sits in a few statements that repeatedly read large
documents or recompute unchanged history, and it grows with the catalog.

| Rank | Hot spot | Now (3.97M releases) | 19.6M (extrapolated) | Best measured fix |
| ---: | --- | ---: | ---: | --- |
| H1 | Search results, heavy term | 8.79 s | about 43 s | 0.98 s with a statement rewrite, no schema change |
| H2 | Search total, heavy term | 3.10 s | about 15 s | 0.11 s by not sorting to count |
| H3 | Pool exhaustion under cold searches | No failure at 4 concurrent | 6 of 8 searches fail at 8 concurrent | Shrinking H1 and H2, then a concurrency gate |
| H4 | `data-completeness` scan | About 8 min serial | About 41 min serial | 2.3x with identical results |
| H5 | `identity_ledger_refresh` | 45 s for 4 rows | 3 to 6 min | Incremental refresh; components measured |
| H6 | Rarity leaderboard by tier | Table empty on lux | About 10 s (from 4M rows) | 0.19 ms with a composite index, synthetic |

Three findings change how earlier numbers should be read:

- **`pg_stat_statements` is not usable on lux.** The library is preloaded, but the extension was
  never created, so no cumulative totals exist. Nothing here ranks by real traffic, and lux has one
  user and no syncs. Ranking is by per-call cost and exposure.
- **Lux is not on v0.6.1 yet.** `graph.credited_on` still has its btree primary key and
  `idx_releases_title` is still a btree. The hash-key cost could not be observed there. See
  [The v0.6.1 hash keys](#the-v061-hash-keys).
- **`scripts/ledger_scale` omits identifier aliases.** Lux holds about 2.1 per release, so the
  48.6M full-scale alias figure is too low.

**Open against the bead:** every follow-up below is proposed, not filed, because filing was refused
by the harness's permission check. See [Proposed follow-up beads](#proposed-follow-up-beads).

## How this was measured

Every figure names one of these datasets.

| Tag | Dataset | Notes |
| --- | --- | --- |
| **lux** | Live PostgreSQL 18.6, read-only, 2026-11-04 20:00 to 20:30 UTC, operator approved | 56 GB. `releases` 3.97M of 19.6M, `artists` 7.93M of 10.3M, `labels` and `masters` complete. `provider_aliases` 25.4M, `catalog_items` 16.9M. MusicBrainz, supersessions, link events, rarity, and user data are empty, bar one user. Load stalled, so the server was near idle; another audit session was also reading |
| **seed 0.04, 0.12** | `scripts/ledger_scale/seed.sql` on the v0.6.1 DDL, PostgreSQL 18.6, production's image digest and memory settings | 1.9M and 5.8M aliases, 0.76 and 2.25 GB. Synthetic, with no identifier aliases |
| **rarity seed** | 4M synthetic `insights.release_rarity` rows, tiers from `RARITY_TIERS`, skewed score | Real distribution unknown: the table has never been computed on lux |
| **empty schema** | The v0.6.1 DDL with no rows and `enable_seqscan=off` | Proves an index path exists, not its speed |

All local runs shared a 6-vCPU Colima VM with other developers' containers, so treat local
absolutes as noisy and compare ratios within one run. Lux timings under `EXPLAIN ANALYZE` carry
instrumentation overhead: the density and edition plans sum to 52.5 s against a real 45.1 s refresh.

**Partial data is a lower bound.** Extrapolations multiply by 4.94 (19.6M / 3.97M) where cost
follows matches or rows. That understates cold reads: `releases` will be about 64 GB against 6 GB
of shared buffers, while today's table mostly fits in cache. No extrapolated figure is a
measurement.

Lux settings differ from `deployment/docker-compose.prod.yml`: `shared_buffers` is 6 GB, not 2 GB,
`effective_cache_size` 18 GB, and `max_connections` 300.

## Inventory

The [appendix](#appendix-statement-inventory) lists 223 statements in 36 modules, found by walking
the AST rather than by grep, and maps each to its route or job through a static, name-based call
graph. The bead's "about 360" counted lines.

| Evidence | Statements | Meaning |
| ---: | ---: | --- |
| I | 124 | Index path proven on the empty schema |
| S | 30 | Measured on a synthetic seed |
| P | 27 | No table scan to plan, such as `INSERT ... VALUES` |
| L | 21 | `EXPLAIN (ANALYZE, BUFFERS)` on lux, or on the same statement family |
| Q | 11 | A sequential scan survived; reviewed below |
| R | 7 | Reviewed by hand; composed text the plan check could not render |
| n/a | 3 | Session settings, not plannable |

**This falls short of the design's "EXPLAIN ANALYZE each statement".** The 124 I statements are
mostly user, auth, and identity-write statements whose tables are empty on lux, so they have a
proven index path but no timing. Almost all are keyed reads and writes, so risk is low, but re-check
them once real users exist.

The 11 Q statements are acceptable: singleton state rows, the small `users` table, per-user
`app_tokens`, and one-shot `media_backfill`. The exception is `_SELECT_OBSERVATIONS`, which has no
index on `observations.owned_copy_id` and scans the table (H7).

## Ranked hot spots

### H1. Search results for a heavy term

`_run_results` (`api/queries/search_queries.py:375`), `GET /api/search`, cached for one hour,
limited to 30/min per client. Worst case measured: `q=love`, 124,549 release matches (3.1%).

The GIN index is not the problem. On lux the Bitmap Heap Scan finds every match in 429 ms, and the
Nested Loop above it takes 9,078 ms, all CPU: a warm rerun has no disk reads.

```text
Nested Loop  (actual time=33..9078 rows=124549)
  ->  CTE Scan on q_3
  ->  Bitmap Heap Scan on releases  (actual time=33..429 rows=124549)
Workers launched: 0.  Buffers: shared hit=2,652,370.  Planned rows: 19,896.
```

**Cause.** Two separate defects, and neither is `ts_rank` itself: rank and `to_tsvector` together
add only about 80 ms across all matches.

1. **The shared `q` CTE forces a serial plan.** Four branches reference it, so it materializes, and
   a CTE scan is parallel-restricted. With one reference PostgreSQL inlines it, which is why a
   single branch run alone looks fine and misleads.
2. **Each match detoasts its document about five times.** `name`, `rank`, `year`, `genres`, and
   `country` are each computed before the sort, and each `data` access decompresses the document
   again. Reading the title once costs 3.9 buffers per match; the route spends 21. 60% of a 1%
   sample stores over 2 kB, and `releases` is 3.2 GB of heap and 10.1 GB of TOAST.

| Shape (whole four-branch statement, lux) | Time | Buffers | Workers |
| --- | ---: | ---: | ---: |
| Current route | 8,792 ms | 2,652,370 | 0 |
| Inline tsquery, no CTE | 2,586 ms | 2,654,271 | 11 |
| Inline tsquery, rank first, read documents after the `LIMIT` | **978 ms** | 682,260 | 11 |

A typical term is fine: `miles davis` (423 releases) takes 85 ms, and all six statements 205 ms.

**Fix.** Inline the tsquery and materialize late. Estimated gain 9x. Risk low: ordering must stay
identical. These runs used literals and production binds parameters, so confirm with bound values
and past psycopg's prepare threshold. Under load, workers run out, and the serial floor is what
counts: the release branch alone took 2.39 s with late materialization and no workers.

**Scale.** Linear in matches: about 615,000 for `love`, so 43 s today and 5 s fixed, both before
cold-read cost. This is the first route to exceed a client timeout.

A stored `tsvector` and title column would remove the document read entirely, but needs a rewrite
of `releases`. Reassess only if the rewrite above misses its target.

### H2. Search total

`_run_total` (`api/queries/search_queries.py:425`) reuses the results builder, so it ranks and
sorts up to 10,000 rows per table only to count them.

| Shape (lux, `q=love`) | Time | Buffers |
| --- | ---: | ---: |
| Current route | 3,102 ms | 679,221 |
| No rank, no sort, same cap | **110 ms** | 28,649 |

Without filters it also duplicates `_run_type_counts`, which runs the same probes in 112 ms.
**Fix:** drop the sort, and reuse the type counts when unfiltered. Gain 28x, risk very low, because
a capped count does not depend on which rows it keeps.

### H3. Cold searches and the connection pool

One uncached search opens six connections from a pool of 8 (`postgres_pool_max_size`). There is no
global `statement_timeout`, so only `strata_queries.py` bounds a statement. A cache miss also
reruns all six statements per page, because `cache_key` includes `limit` and `offset`, though facets
and totals do not depend on them.

Measured on the real `AsyncPostgreSQLPool`, max 8, with statements simulated by `pg_sleep` at lux's
durations, and a login-style probe running throughout:

| Cold searches at once | Scale | Searches failed | Probe worst wait |
| ---: | --- | ---: | ---: |
| 2 | 3.97M | 0 of 2 | 0.2 s |
| 4 | 3.97M | 0 of 4 | 2.4 s |
| 8 | 3.97M | 0 of 8 | 4.2 s |
| 16 | 3.97M | 3 of 16 | 14.9 s |
| 2 | 19.6M | 0 of 2 | 0.7 s |
| 4 | 19.6M | 0 of 4 | 16.6 s |
| 8 | 19.6M | **6 of 8** | **probe failed at 17.1 s** |

The pool copes better than the "six of eight" arithmetic suggests, because only two of the six
statements run long. It fails once long holds outlast the checkout budget of about 18.5 s, and then
unrelated routes such as login fail too. The limiter is per client, so this needs eight distinct
cold requests, which is a burst or a crawler rather than normal use.

**Fix.** H1 and H2 are the real fix: they cut hold times about 9x. Add a gate on concurrent cold
searches and a search timeout as a backstop, and split the facet cache from the page cache. Risk
medium: a timed-out search needs a defined response, never an empty success.

### H4. `data-completeness`

`_COMBINED_QUERIES` (`api/queries/insights_pg_queries.py:46`), behind
`GET /api/internal/insights/data-completeness`. Secret-protected, 5/min, cached for 6 hours. It
runs four full scans at once, one connection each.

Each field's filter reads `data->>'key'` three times. Across four fields that is about 12 detoasts
per row, which matches the 39 buffers per row measured. A 2% sample, deliberately not a full run, to avoid pushing 13 GB through lux's buffers:

| Lux, `TABLESAMPLE SYSTEM (2) REPEATABLE (7)`, 78,212 rows | Time | Buffers |
| --- | ---: | ---: |
| Current filters | 9,698 ms | 3,049,161 |
| `data->>'key' NOT IN ('', '[]')` | **4,226 ms** | 1,241,567 |

Both return identical counts. That is 125 µs per row, so about 8 minutes serial at 3.97M rows and
41 at 19.6M. The full plan has 4 workers, so about 1.7 and 8 minutes if they scale ideally, which
was not measured. A sample scan is serial, so these are rates, not route times.

**Fix.** The rewrite gains 2.3x at no risk. A sampled estimate would gain about 50x, but changes an
exact count to an estimate, which is a product decision.

**Wrong data, not only slow.** `with_image` was 0 across the whole sample, so `images` looks absent
from `data`. That metric appears to report no image coverage for any release. Confirm where image
data lives before trusting it.

### H5. `identity_ledger_refresh`

See [The cost of `identity_ledger_refresh`](#the-cost-of-identity_ledger_refresh). It ranks fifth
because no user waits on it, and at 45 s every 6 hours it is about 0.2% of a day. It matters
because it holds one long snapshot during loads and blocks the reattach run that waits on it inline.

### H6. Rarity leaderboard by tier

`get_rarity_leaderboard` (`api/queries/rarity_queries.py:535`), `GET /api/rarity/leaderboard`, public,
30/min, uncached. `insights.release_rarity` is empty on lux, so this is the rarity seed only.

`idx_release_rarity_score` has no `tier`, so a filtered page walks past every higher-scoring row.
The best tier is instant and the commonest is slowest, which is the wrong way round.

| 4M rows | Current | With `(tier, rarity_score DESC, release_id)` |
| --- | ---: | ---: |
| `tier=common`, page 1 | 1,984 ms | **0.19 ms** |
| `tier=ultra-rare`, page 1 | 0.09 ms | 0.12 ms |
| No tier, `OFFSET 1000000` | 2,575 ms | Unchanged, needs keyset paging |
| `count(*)` per request | 138 ms | Unchanged |

**Fix.** Add the index in `database-schema`, and it can replace `idx_release_rarity_tier` because
its leading column is the same. It costs about 155 MB per 4M rows against 25 MB for the one it
replaces, so about 760 MB at 19.6M, or about 640 MB net. Risk low. Move deep pages to keyset paging, or cap `page`.
**Scale:** linear, so about 10 s for `tier=common` at 19.6M. Real scores may differ.

### H7. Smaller items

| Item | Evidence | Scale | Recommendation |
| --- | --- | --- | --- |
| `_SELECT_OBSERVATIONS`, `api/routers/observations.py:53` | Seq Scan survives on the empty schema; no index on `owned_copy_id` | Linear in all users' observations | Add `(owned_copy_id, observed_at DESC)` before the table grows |
| `/api/enrichment/status` | Public, 10/min, uncached, eight `COUNT(*)` per request. MusicBrainz is empty on lux, so unmeasured | Linear | Re-measure after the MusicBrainz load; `reltuples` or a cache would do. Its Neo4j half belongs to `gm-catalog-api-0n2z` |
| `/api/admin/media/unmapped` | lux: 0.73 s + 1.99 s | About 13 s at 19.6M | Admin only, leave it |
| Re-attachment census | seed 0.12: 2.76 s for `release` | About 23 s per large kind | Background job, leave it |
| Year filter, `api/queries/search_queries.py:126` | lux: 7.67 s for `love` with 1990 to 1999 | As H1 | `::int` cannot use `idx_releases_year`; H1 covers most of it |
| Search planner estimate | 19,896 planned, 124,549 actual | Constant ratio | Harmless once H1 lands |
| `run_pending` | Holds one pooled connection for a whole run, for its session advisory lock | One of 8 | Accept; note it when sizing the pool |

## The cost of `identity_ledger_refresh`

### What the 45 seconds buys

On lux the stored run took 45.15 s and produced this:

| View | Plan time (lux) | Rows |
| --- | ---: | ---: |
| `identity_ledger_density` | 40.9 s | **4** |
| `identity_ledger_edition_events` | 11.6 s | **0** |
| `identity_ledger_edition_parents` | 0.3 ms | 0 |
| `identity_ledger_work_summary` | 0.3 ms | 0 |

Lux has no MusicBrainz rows, supersessions, or link events, so 45 s went on proving almost nothing
happened. The four density rows are one month of mints per kind, and a plain scan reproduces them
exactly in **0.93 s**.

**Cause.** Every view recomputes all history on every call, and a refresh with no source change
costs the same as a real one. Density's `_ledger_item_events` CTE runs once per kind: each pass
merge-joins `provider_aliases` to `catalog_items`, computes a window over every item, and stores
the result, which costs 21.4M buffers and 256,290 temp blocks (2.0 GB) per refresh on lux. The
release pass alone handles 12.4M aliases to emit 3.97M mints. `REFRESH ... CONCURRENTLY` then adds a
duplicate check and a diff. No plan node sorted to disk: the temp blocks are stored CTEs, so
`work_mem` is not the lever.

### Why it recurs

The poll is working as designed. The stored watermark, 45,122,430, equals the sum of the live
counters exactly (2,734,842 + 16,955,677 + 25,431,911), so a stalled load refreshes nothing. It
recurs while a load runs, through the 6-hour staleness rule and each quiet gap. Slow-log frequency
was not read, because the wrappers do not reach the log.

### How it scales

Roughly linear in aliases and items: 15.6 s at 1.9M aliases and 52 s at 5.8M on the seed, an
exponent of about 1.1, which fits the 195 s recorded at scale 0.5.

| Input | Lux now | Full, estimated |
| --- | ---: | ---: |
| Discogs entity aliases | 16.9M | About 35M |
| Identifier aliases | 8.45M | Up to about 42M; shared barcodes hold one alias each, so this is a ceiling |
| Density pass | About 35 s | About 2 to 2.5 min |
| Whole refresh, once events exist | 45 s | **3 to 6 min, plus MusicBrainz** |

The upper end assumes density stays about 41% of the total once events exist, as on the seed.

### Can it be incremental? Yes

SQL rewrites alone are not worth it. Prototypes returned identical rows (`EXCEPT` empty both ways
on seed 0.12), but saved only 19 to 20% on lux and 27 to 36% on the seed, because they still touch
every item.

The cost is all in unchanged history, and each component of a dirty-item design measured well:

| Component | Current | Measured | Dataset |
| --- | ---: | ---: | --- |
| Mints per month, from `catalog_items.created_at` | 40.9 s | **0.93 s** | lux |
| Find changed aliases, no new index | n/a | 451 ms | lux |
| Find changed aliases, BRIN on `valid_from` plus partial btree on `valid_to` | 270 ms | **0.47 ms**, 56 kB of index | seed 0.12 |
| Recompute events for 1,000 items | n/a | **6 ms** | seed 0.12 |
| Recompute events for 100,000 items | n/a | **266 ms** | seed 0.12 |

Mint equals `created_at` on all 16,944,439 items on lux, with zero skew, and `valid_from`
correlation is 0.99999, so BRIN fits real data as well as it fit the seed. A monthly delta of about
171,000 aliases would recompute in about half a second.

**This was not built end to end**, so the total is an estimate: about 1 s plus the changed items,
against 3 to 6 minutes.

**Recommendation.** Replace the matviews' full recompute with per-item maintenance in
`database-schema`, with catalog-api keeping the triggers. The design must handle:

- **The dirty unit is the whole item, not the event.** `linked_at_ingest` reads `discogs_at`
  without comparing it to the alias's own time, so a later Discogs alias reclassifies an older
  MusicBrainz event.
- **Dirty sources beyond aliases.** Writes to `graph.derived_from` and `musicbrainz.releases` change
  an edition's parents, and a supersession moves events between items.
- **Density needs stored non-mint events,** because old counts cannot be subtracted from base tables
  after they change.
- **Keep the full rebuild** for first build and for repair when most items changed.

There is no urgency while the ledger is empty, so schedule it before MusicBrainz loads rather than
now. Until then, leave the refresh as it is. Do not tune `work_mem`, and do not drop `CONCURRENTLY`,
which is what keeps readers unblocked.

## The v0.6.1 hash keys

Person-led reads of very heavy names regressed from 18 ms to 382 ms cold in the schema's own
measurement. **No catalog-api route is exposed to this.** The only `graph.*` relation in any
catalog-api SQL is `graph.derived_from`, which is not one of the six re-keyed relations and keeps
its btrees. The person routes run on Neo4j.

`idx_releases_title` and the five other name indexes become hash indexes, but nothing in catalog-api
orders or ranges on those expressions: search uses the GIN indexes, and no statement uses `LIKE`.
`projection.py`'s keyset pager uses `provider_aliases`, whose key stays a btree: 0.57 ms on lux.

This is static proof. It could not be confirmed live, because lux has not migrated. If person reads
move to PostgreSQL, the schema docs' own remedy applies.

## Design checklist

| Looked for | Result |
| --- | --- |
| Sequential scans on large tables | H4, media coverage, enrichment counts, all deliberate aggregates |
| jsonb extraction in filters | H4, and the search year filter. The country filter does use `idx_releases_country` through a BitmapOr, as its docstring says |
| Sort and hash spills | No `Disk:` sort in any lux plan. Refresh writes temp through stored CTEs (H5) |
| Missing or partial indexes | H6 and `observations.owned_copy_id` |
| Expensive functions in hot paths | Repeated detoast (H1, H4). `ts_rank` itself is cheap |
| OFFSET pagination | Rarity (H6). Search `offset` is unbounded but capped by the per-table limit: 9.0 s at offset 5000, about the same as page 1. Ledger pages stay fast |
| N+1 calls | None on request paths. `media_backfill` updates row by row, one-shot. Re-attachment commits per item by design |
| Transactions held across awaits | None across HTTP. `_commit_password_change` awaits Valkey inside one, for its marker and a key cleanup |
| Prepared statements, PgBouncer | Session mode is required and documented. No issue found. Re-check H1 past the prepare threshold |
| Statement timeouts | Only `strata_queries.py`. See H3 |
| Materialized data | H5. Rarity's precomputed table is the right pattern |

Point lookups on lux, all index-backed and warm: alias single 0.14 ms, alias batch of 100 4.7 ms,
release by `gm_item_id` 0.75 ms, catalog blocks 0.27 ms, one work's editions 15 ms (largest work so
far: 221 releases), poll watermark 0.37 ms. Ledger routes on seed 0.12 stayed at or under 189 ms
p95, in line with `api/README.md`, so `gm-catalog-api-kmlo` has no conflicting finding.

Checked and clear, each suspected first: `_persist_*` reuses its transaction's connection rather
than nesting a pool checkout, and `_ensure_partition` is memoised per process.

Stale text found on the way: the `execute_search` docstring says the cache TTL is 300 s, and it is
3600 s. The `_run_total` docstring says `Rock` matches 18.9M releases, which looks like a genre count:
the title term matches 29,543 of 3.97M on lux. `docs/query-performance-optimizations.md` says search limits
each relation "before the final union and ranking". That holds for the final ranking, but each
relation's own limit still ranks every match first, which is H1.

## Proposed follow-up beads

None are filed. `bd_create` was refused by the harness permission check, and the audit did not work
around it. The bead asks for these to be filed, so this acceptance item is open. Each has a
complexity tier and no model, per the routing rule.

| # | Repo | Tier | Priority | Title | Measured target |
| ---: | --- | --- | ---: | --- | --- |
| 1 | catalog-api | MEDIUM | 1 | Make cold search results fast: drop the shared tsquery CTE and rank before reading documents | `q=love` at most 1.5 s warm and 0.8M buffers at 3.97M releases, with bound parameters; order, rank, and highlight unchanged |
| 2 | catalog-api | SIMPLE | 1 | Stop ranking and sorting rows just to count search matches | `_run_total` at most 200 ms for `q=love` at 3.97M; totals unchanged, filtered and capped included |
| 3 | catalog-api | MEDIUM | 2 | Keep a burst of cold searches from starving the shared pool | 8 cold searches at full-scale durations: no `ConnectionEstablishmentError`, login probe under 1 s p95; a timeout is a defined error; page 2 runs only the results statement. Backstop to 1 and 2 |
| 4 | catalog-api | SIMPLE | 2 | Read each field once in the data-completeness scan, and find out why `with_image` is zero | At most 16 buffers per row with identical counts; `with_image` reports real coverage or is removed with a recorded reason |
| 5 | database-schema | MEDIUM | 2 | Add `(tier, rarity_score DESC, release_id)` on `insights.release_rarity`; keyset deep pages | `tier=common` page 1 under 10 ms at 4M rows or more, and offset no longer scales with depth |
| 6 | database-schema | COMPLEX | 2 | Maintain the Identity Ledger per item instead of recomputing all history | Refresh after no change under 2 s and after 1,000 changed items under 5 s on a catalog of lux's size; rows equal a full rebuild; schedule before the MusicBrainz load |
| 7 | deployment | SIMPLE | 2 | Create the `pg_stat_statements` extension on deployed databases | `SELECT ... FROM pg_stat_statements` works on lux and a baseline is captured after the first full load |
| 8 | database-schema | SIMPLE | 3 | Index `observations` by `owned_copy_id` | `_SELECT_OBSERVATIONS` uses an index on the plan check |
| 9 | catalog-api | SIMPLE | 2 | Model identifier aliases in the ledger scale harness | Seeded alias-to-item ratio within 5% of a stated lux measurement; coordinate with `gm-catalog-api-kmlo` |
| 10 | catalog-api | SIMPLE | 3 | Move the `groovemap-database-schema` dev pin to the deployed contract | Pin matches the contract's producer version; integration suite passes |

Not proposed yet: `/api/enrichment/status` has no measured target until MusicBrainz loads.

## Live reads still wanted

After the maintenance window and the full load:

- The v0.6.1 index state, and H1 and H2 re-run at 19.6M to replace the extrapolations.
- A real `data-completeness` run time at a quiet moment. This audit sampled instead.
- Slow-log frequency for `identity_ledger_refresh`, and `refreshed_at` history.
- Real rarity scores, to re-run H6.

## Appendix: statement inventory

Evidence codes are defined in [Inventory](#inventory). Routes come from a name-based call graph, so
a shared helper can list more routes than truly reach it. `job` marks a CLI, loop, or other non-route
entry point.

| Location | Symbol | Verb | Relations | Route or job | Evidence |
| --- | --- | --- | --- | --- | ---: |
| `activity.py:159` | `_UPSERT_SUBJECT` | INSERT | activity.user_subjects | `GET /api/fit/release/{release_id}`, `GET /api/lookup/{provider}/{value}` +9 more | P |
| `activity.py:168` | `_SELECT_ACTIVE_PURPOSES` | SELECT | activity.consent_grants | `GET /api/fit/release/{release_id}`, `GET /api/lookup/{provider}/{value}` +9 more | I |
| `activity.py:177` | `_ENSURE_PARTITION` | SELECT |  | `GET /api/fit/release/{release_id}`, `GET /api/lookup/{provider}/{value}` +10 more | P |
| `activity.py:192` | `_INSERT_EVENT` | INSERT | activity.events | `GET /api/lookup/{provider}/{value}`, `GET /api/search` +4 more | P |
| `activity.py:206` | `_INSERT_IMPRESSION` | INSERT | activity.impressions | `GET /api/fit/release/{release_id}`, `GET /api/recommend/explore/{entity_type}/{entity_id}` +2 more | P |
| `admin_setup.py:45` | `add_admin` | INSERT | users | `job admin_setup.main` | P |
| `admin_setup.py:61` | `list_admins` | SELECT | users | `job admin_setup.main` | Q |
| `api.py:533` | `_get_app_config` | SELECT | app_config | `job api._get_app_config` | I |
| `api.py:543` | `_get_discogs_app_config` | SELECT | app_config | `GET /api/oauth/authorize/discogs`, `POST /api/oauth/verify/discogs` | I |
| `api.py:724` | `verify_discogs` | INSERT | oauth_tokens | `POST /api/oauth/verify/discogs` | P |
| `api.py:778` | `discogs_status` | SELECT | oauth_tokens | `GET /api/oauth/status/discogs` | I |
| `api.py:815` | `revoke_discogs` | DELETE | oauth_tokens | `DELETE /api/oauth/revoke/discogs` | I |
| `app_tokens.py:86` | `mint_token` | INSERT | app_tokens | `job app_tokens.mint_token` | P |
| `app_tokens.py:105` | `list_user_tokens` | SELECT | app_tokens | `job app_tokens.list_user_tokens` | Q |
| `app_tokens.py:129` | `revoke_token` | UPDATE | app_tokens | `job app_tokens.revoke_token` | I |
| `app_tokens.py:146` | `_touch_last_used_at` | UPDATE | app_tokens | `GET /api/fit/release/{release_id}`, `GET /api/user/collection` +10 more | I |
| `app_tokens.py:191` | `_lookup_active_token` | SELECT | app_tokens, users | `GET /api/fit/release/{release_id}`, `GET /api/user/collection` +10 more | I |
| `audit_log.py:33` | `record_audit_entry` | INSERT | admin_audit_log | `POST /api/admin/auth/login`, `POST /api/admin/auth/logout` +4 more | P |
| `audit_log.py:41` | `_INSERT_ENTRY` | INSERT | admin_audit_log | `POST /api/admin/identity/reattach`, `job api.lifespan` +3 more | P |
| `audit_log.py:42` | `_UPDATE_ENTRY` | UPDATE | admin_audit_log | `POST /api/admin/identity/reattach`, `job api.lifespan` +4 more | I |
| `catalog_merge.py:96` | `_LOCK_ITEM` | SELECT | catalog_items | `POST /api/admin/identity/reattach`, `job api.lifespan` +4 more | I |
| `catalog_merge.py:98` | `_CURRENT_SURVIVORS` | SELECT | catalog_item_supersessions | `POST /api/admin/identity/reattach`, `job api.lifespan` +3 more | I |
| `catalog_merge.py:104` | `_OPEN` | INSERT | catalog_item_supersessions | `POST /api/admin/identity/reattach`, `job api.lifespan` +3 more | P |
| `catalog_merge.py:113` | `_CLOSE_SURVIVED` | UPDATE | catalog_item_supersessions | `POST /api/admin/identity/reattach`, `job api.lifespan` +3 more | I |
| `catalog_merge.py:120` | `_OPEN_COMPRESSED` | INSERT | catalog_item_supersessions | `POST /api/admin/identity/reattach`, `job api.lifespan` +3 more | P |
| `catalog_merge.py:130` | `_REPOINT` | WITH+W | catalog_item_moves | `POST /api/admin/identity/reattach`, `job api.lifespan` +3 more | I |
| `catalog_merge.py:143` | `_RECOMPUTE` | WITH+W | provider_aliases | `POST /api/admin/identity/reattach`, `job api.lifespan` +4 more | R |
| `catalog_merge.py:159` | `_LOCK_SUPERSESSION` | SELECT | catalog_item_supersessions | `job catalog_merge.revert_supersession` | I |
| `catalog_merge.py:161` | `_CLOSE` | UPDATE | catalog_item_supersessions | `job catalog_merge.revert_supersession` | I |
| `catalog_merge.py:163` | `_CLOSE_COMPRESSED` | UPDATE | catalog_item_supersessions | `job catalog_merge.revert_supersession` | I |
| `catalog_merge.py:172` | `_REOPEN_PREDECESSORS` | UPDATE | catalog_item_supersessions | `job catalog_merge.revert_supersession` | I |
| `catalog_merge.py:182` | `_MOVE_BACK` | WITH+W | catalog_item_moves | `job catalog_merge.revert_supersession` | I |
| `dependencies.py:254` | `require_admin` | SELECT | users | `GET /api/admin/audit-log`, `GET /api/admin/auth/session` +23 more | I |
| `identity.py:57` | `_SELECT_NATIVE_IDS` | SELECT | provider_aliases | `GET /api/admin/identity/ledger/works/{work_id}`, `GET /api/collection/gaps/artist/{artist_id}` +12 more | L |
| `identity.py:70` | `_SELECT_OWNED_COPY_IDS` | SELECT | user_collections | `GET /api/user/collection`, `GET /api/user/wantlist` | I |
| `ledger_backfill.py:477` | `_HANDLED` | SELECT | admin_audit_log | `job ledger_backfill.main` | I |
| `ledger_backfill.py:479` | `_INSERT_EVENTS` | WITH+W | identity_link_events | `job ledger_backfill.main` | P |
| `ledger_refresh.py:103` | `_WATERMARK` | SELECT | pg_stat_user_tables | `POST /api/admin/identity/reattach`, `job api.lifespan` +3 more | L |
| `ledger_refresh.py:112` | `_STORED` | SELECT | identity_ledger_refresh_state, pg_class | `job api.lifespan` | Q |
| `media_backfill.py:86` | `backfill_collection` | SELECT | user_collections | `job media_backfill.main` | Q |
| `media_backfill.py:110` | `backfill_collection` | UPDATE | user_collections | `job media_backfill.main` | I |
| `media_backfill.py:140` | `backfill_wantlist` | SELECT | user_wantlists | `job media_backfill.main` | Q |
| `media_backfill.py:163` | `backfill_wantlist` | UPDATE | user_wantlists | `job media_backfill.main` | I |
| `projection.py:43` | `_SELECT_PAGE` | SELECT | provider_aliases | `POST /api/admin/identity/project`, `job api.lifespan` +2 more | L |
| `queries/admin_queries.py:29` | `get_user_stats` | SELECT | oauth_tokens, sync_history, users | `GET /api/admin/users/stats` | I |
| `queries/admin_queries.py:46` | `get_user_stats` | SELECT | users | `GET /api/admin/users/stats` | Q |
| `queries/admin_queries.py:58` | `get_user_stats` | SELECT | users | `GET /api/admin/users/stats` | Q |
| `queries/admin_queries.py:70` | `get_user_stats` | SELECT | users | `GET /api/admin/users/stats` | Q |
| `queries/admin_queries.py:99` | `get_sync_activity._query_period` | SELECT | sync_history | `GET /api/admin/users/sync-activity` | I |
| `queries/admin_queries.py:177` | `get_postgres_storage` | SELECT | pg_stat_user_tables | `GET /api/admin/storage` | I |
| `queries/admin_queries.py:199` | `get_postgres_storage` | SELECT |  | `GET /api/admin/storage` | P |
| `queries/admin_queries.py:212` | `_AUDIT_COUNT_UNFILTERED` | SELECT | admin_audit_log | `GET /api/admin/audit-log` | I |
| `queries/admin_queries.py:213` | `_AUDIT_COUNT_ACTION` | SELECT | admin_audit_log | `GET /api/admin/audit-log` | I |
| `queries/admin_queries.py:214` | `_AUDIT_COUNT_ADMIN` | SELECT | admin_audit_log | `GET /api/admin/audit-log` | I |
| `queries/admin_queries.py:215` | `_AUDIT_COUNT_BOTH` | SELECT | admin_audit_log | `GET /api/admin/audit-log` | I |
| `queries/admin_queries.py:218` | `_AUDIT_SELECT` | SELECT | admin_audit_log, users | `GET /api/admin/audit-log` | R |
| `queries/collection_media_queries.py:19` | `_FAMILY_COUNTS_SQL` | SELECT | user_collections | `GET /api/collection/media` | I |
| `queries/collection_media_queries.py:30` | `_MEDIUM_COUNTS_SQL` | SELECT | user_collections | `GET /api/collection/media` | I |
| `queries/fit_queries.py:133` | `_RELEASE_RARITY_SQL` | SELECT | insights.release_rarity | `GET /api/fit/release/{release_id}` | I |
| `queries/identity_ledger_queries.py:49` | `_READ_ONLY` | SET |  | `GET /api/admin/identity/ledger/density`, `GET /api/admin/identity/ledger/works` +1 more | n/a |
| `queries/identity_ledger_queries.py:142` | `_FRESHNESS` | SELECT | identity_ledger_refresh_state, pg_class | `GET /api/admin/identity/ledger/density`, `GET /api/admin/identity/ledger/works` | S |
| `queries/identity_ledger_queries.py:149` | `_REFRESHED_AT` | SELECT | identity_ledger_refresh_state | `GET /api/admin/identity/ledger/works/{work_id}` | S |
| `queries/identity_ledger_queries.py:155` | `_WORKS` | WITH |  | `GET /api/admin/identity/ledger/works` | S |
| `queries/identity_ledger_queries.py:196` | `_WORK_SUMMARIES` | WITH | catalog_item_supersessions | `GET /api/admin/identity/ledger/works` | S |
| `queries/identity_ledger_queries.py:217` | `_WORK_PROVIDERS` | SELECT | provider_aliases | `GET /api/admin/identity/ledger/works` | S |
| `queries/identity_ledger_queries.py:224` | `_DENSITY` | SELECT |  | `GET /api/admin/identity/ledger/density` | S |
| `queries/identity_ledger_queries.py:231` | `_RESOLVE` | SELECT | catalog_items | `GET /api/admin/identity/ledger/works/{work_id}` | S |
| `queries/identity_ledger_queries.py:238` | `_EDITIONS` | WITH | catalog_item_supersessions, provider_aliases | `GET /api/admin/identity/ledger/works/{work_id}` | L |
| `queries/identity_ledger_queries.py:254` | `_GROUPS` | SELECT | catalog_item_supersessions | `GET /api/admin/identity/ledger/works/{work_id}` | S |
| `queries/identity_ledger_queries.py:260` | `_ALIASES` | SELECT | provider_aliases | `GET /api/admin/identity/ledger/works/{work_id}` | S |
| `queries/identity_ledger_queries.py:267` | `_SUPERSESSIONS` | SELECT | catalog_item_supersessions | `GET /api/admin/identity/ledger/works/{work_id}` | S |
| `queries/identity_ledger_queries.py:273` | `_MOVE_COUNTS` | SELECT | catalog_item_moves | `GET /api/admin/identity/ledger/works/{work_id}` | S |
| `queries/identity_ledger_queries.py:280` | `_DISCOGS_RELEASES` | SELECT | graph.derived_from, public.releases | `GET /api/admin/identity/ledger/works/{work_id}` | L |
| `queries/identity_ledger_queries.py:287` | `_MUSICBRAINZ_RELEASES` | SELECT | musicbrainz.releases | `GET /api/admin/identity/ledger/works/{work_id}` | S |
| `queries/identity_ledger_queries.py:293` | `_DISCOGS_HOLDERS` | SELECT | provider_aliases | `GET /api/admin/identity/ledger/works/{work_id}` | S |
| `queries/identity_ledger_queries.py:299` | `_MASTER_WORKS` | SELECT | provider_aliases | `GET /api/admin/identity/ledger/works/{work_id}` | S |
| `queries/identity_ledger_queries.py:308` | `_LINK_EVENTS` | SELECT | identity_link_events | `GET /api/admin/identity/ledger/works/{work_id}` | S |
| `queries/insights_pg_queries.py:46` | `<module>` | SELECT |  | `GET /api/internal/insights/data-completeness` | L |
| `queries/lookup_queries.py:36` | `_SELECT_ALIAS_NATIVE_ID` | SELECT | provider_aliases | `GET /api/lookup/{provider}/{value}` | L |
| `queries/lookup_queries.py:45` | `_SELECT_ALIAS_NATIVE_IDS` | SELECT | provider_aliases | `GET /api/lookup/{provider}/{value}` | L |
| `queries/lookup_queries.py:58` | `_SELECT_RELEASES_BY_NATIVE_ID` | SELECT | musicbrainz.releases, releases | `GET /api/lookup/{provider}/{value}` | L |
| `queries/media_coverage_queries.py:93` | `_coverage_query` | SELECT |  | `GET /api/admin/media/unmapped` | L |
| `queries/media_coverage_queries.py:108` | `_top_names_query` | SELECT |  | `GET /api/admin/media/unmapped` | L |
| `queries/media_coverage_queries.py:115` | `_top_names_query` | SELECT |  | `GET /api/admin/media/unmapped` | L |
| `queries/musicbrainz_queries.py:63` | `get_artist_external_links` | SELECT | musicbrainz.artists, musicbrainz.external_links | `GET /api/artist/{artist_id}/external-links` | I |
| `queries/musicbrainz_queries.py:91` | `get_enrichment_status` | SELECT |  | `GET /api/enrichment/status` | R |
| `queries/musicbrainz_queries.py:98` | `get_enrichment_status` | SELECT |  | `GET /api/enrichment/status` | R |
| `queries/musicbrainz_queries.py:106` | `get_enrichment_status` | SELECT | musicbrainz.relationships | `GET /api/enrichment/status` | I |
| `queries/rarity_queries.py:267` | `_load_community_counts` | SELECT | insights.community_counts | `GET /api/internal/insights/rarity-scores` | Q |
| `queries/rarity_queries.py:508` | `get_rarity_for_release` | SELECT | insights.release_rarity | `GET /api/rarity/{release_id}` | S |
| `queries/rarity_queries.py:535` | `get_rarity_leaderboard` | SELECT | insights.release_rarity | `GET /api/rarity/leaderboard` | S |
| `queries/rarity_queries.py:547` | `get_rarity_leaderboard` | SELECT | insights.release_rarity | `GET /api/rarity/leaderboard` | S |
| `queries/rarity_queries.py:552` | `get_rarity_leaderboard` | SELECT | insights.release_rarity | `GET /api/rarity/leaderboard` | S |
| `queries/rarity_queries.py:563` | `get_rarity_leaderboard` | SELECT | insights.release_rarity | `GET /api/rarity/leaderboard` | S |
| `queries/rarity_queries.py:583` | `get_rarity_hidden_gems` | SELECT | insights.release_rarity | `GET /api/rarity/hidden-gems` | S |
| `queries/rarity_queries.py:595` | `get_rarity_hidden_gems` | SELECT | insights.release_rarity | `GET /api/rarity/hidden-gems` | S |
| `queries/rarity_queries.py:641` | `get_rarity_by_artist` | SELECT | insights.release_rarity | `GET /api/rarity/artist/{artist_id}` | S |
| `queries/rarity_queries.py:653` | `get_rarity_by_artist` | SELECT | insights.release_rarity | `GET /api/rarity/artist/{artist_id}` | S |
| `queries/rarity_queries.py:699` | `get_rarity_by_label` | SELECT | insights.release_rarity | `GET /api/rarity/label/{label_id}` | S |
| `queries/rarity_queries.py:711` | `get_rarity_by_label` | SELECT | insights.release_rarity | `GET /api/rarity/label/{label_id}` | S |
| `queries/release_media_queries.py:42` | `get_release_media` | SELECT | releases | `GET /api/node/{node_id}` | L |
| `queries/release_media_queries.py:82` | `get_release_catalog_blocks` | SELECT | releases | `GET /api/node/{node_id}` | L |
| `queries/search_queries.py:376` | `_run_results` | WITH |  | `GET /api/search`, `job api.lifespan` | L |
| `queries/search_queries.py:425` | `_run_total` | WITH |  | `GET /api/search`, `job api.lifespan` | L |
| `queries/search_queries.py:445` | `_run_type_counts` | SELECT |  | `GET /api/search`, `job api.lifespan` | L |
| `queries/search_queries.py:457` | `_run_type_counts` | WITH |  | `GET /api/search`, `job api.lifespan` | L |
| `queries/search_queries.py:470` | `_run_genre_facets` | WITH | releases | `GET /api/search`, `job api.lifespan` | L |
| `queries/search_queries.py:494` | `_run_decade_facets` | WITH | masters, releases | `GET /api/search`, `job api.lifespan` | L |
| `queries/search_queries.py:526` | `_run_media_facets` | WITH | releases | `GET /api/search`, `job api.lifespan` | L |
| `queries/similar_precomputed_queries.py:31` | `_CURRENT_LIST_SQL` | SELECT | public.artist_embedding_releases, public.artist_similar_artists | `GET /api/recommend/similar/artist/{artist_id}` | I |
| `queries/similar_precomputed_queries.py:42` | `_EMBEDDINGS_SQL` | SELECT | public.artist_embeddings | `GET /api/recommend/similar/artist/{artist_id}` | R |
| `queries/strata_queries.py:130` | `_MB_CANDIDATES` | WITH | musicbrainz.release_groups, musicbrainz.releases | `GET /api/explore/strata` | I |
| `queries/strata_queries.py:153` | `_OWNED_COPIES` | SELECT | user_collections | `GET /api/explore/strata`, `job explore.read_marks` | I |
| `queries/strata_queries.py:162` | `_STATEMENT_TIMEOUT` | SELECT |  | `GET /api/explore/strata`, `job explore.read_marks` | P |
| `queries/strata_queries.py:164` | `_MB_SIBLINGS` | SELECT | musicbrainz.releases | `GET /api/explore/strata` | I |
| `reattach.py:167` | `_LINKED` | SELECT | provider_aliases | `POST /api/admin/identity/reattach`, `job api.lifespan` +5 more | S |
| `reattach.py:233` | `_CENSUS` | SELECT |  | `POST /api/admin/identity/reattach`, `job api.lifespan` +3 more | S |
| `reattach.py:278` | `_IDENTIFIERS` | SELECT | provider_aliases | `POST /api/admin/identity/reattach`, `job api.lifespan` +3 more | S |
| `reattach.py:294` | `_CANDIDATE_PAGE` | SELECT |  | `POST /api/admin/identity/reattach`, `job api.lifespan` +3 more | S |
| `reattach.py:303` | `_LOCK_ROW` | SELECT |  | `POST /api/admin/identity/reattach`, `job api.lifespan` +3 more | R |
| `reattach.py:305` | `_LOCK_ALIASES` | SELECT | provider_aliases | `POST /api/admin/identity/reattach`, `job api.lifespan` +3 more | I |
| `reattach.py:316` | `_CLOSE_ALIASES` | UPDATE | provider_aliases | `POST /api/admin/identity/reattach`, `job api.lifespan` +3 more | I |
| `reattach.py:325` | `_REINSERT_ALIASES` | INSERT | provider_aliases | `POST /api/admin/identity/reattach`, `job api.lifespan` +3 more | P |
| `reattach.py:333` | `_CURRENT_HOLDERS` | SELECT | provider_aliases | `POST /api/admin/identity/reattach`, `job api.lifespan` +3 more | I |
| `reattach.py:341` | `_SET_GM_ITEM_ID` | UPDATE |  | `POST /api/admin/identity/reattach`, `job api.lifespan` +3 more | R |
| `reattach.py:454` | `run_census` | SET |  | `POST /api/admin/identity/reattach`, `job api.lifespan` +3 more | n/a |
| `reattach.py:738` | `_SELECT_ADMIN` | SELECT | users | `job projection.main`, `job reattach.main` | I |
| `reattach_trigger.py:97` | `_LATCH_PRESENT` | SELECT |  | `job api.lifespan` | P |
| `reattach_trigger.py:99` | `_LATEST_COMPLETE` | SELECT | public.loader_extraction_latch | `job api.lifespan` | I |
| `reattach_trigger.py:107` | `_HANDLED` | SELECT | admin_audit_log | `job api.lifespan` | I |
| `reattach_trigger.py:112` | `_ENSURE_ACTOR` | INSERT | users | `job api.lifespan`, `job ledger_backfill.main` | P |
| `reattach_trigger.py:117` | `_SELECT_ACTOR` | SELECT | users | `job api.lifespan`, `job ledger_backfill.main` | I |
| `routers/activity.py:128` | `_SELECT_CONSENT` | SELECT | activity.consent_grants | `GET /api/user/consent` | I |
| `routers/activity.py:138` | `_INSERT_GRANT` | INSERT | activity.consent_grants | `PUT /api/user/consent/{purpose}` | I |
| `routers/activity.py:150` | `_REVOKE_GRANT` | UPDATE | activity.consent_grants | `PUT /api/user/consent/{purpose}` | I |
| `routers/activity.py:294` | `_ENABLE_ERASURE` | SET |  | `POST /api/user/erasure` | n/a |
| `routers/activity.py:300` | `_COUNT_IDEMPOTENCY_KEYS` | SELECT | activity.event_idempotency | `POST /api/user/erasure` | I |
| `routers/activity.py:301` | `_DELETE_EVENTS` | DELETE | activity.events | `POST /api/user/erasure` | I |
| `routers/activity.py:302` | `_DELETE_IMPRESSIONS` | DELETE | activity.impressions | `POST /api/user/erasure` | I |
| `routers/activity.py:303` | `_DELETE_SUBJECT` | DELETE | activity.user_subjects | `POST /api/user/erasure` | I |
| `routers/activity.py:308` | `_INSERT_ERASURE` | INSERT | activity.erasures | `POST /api/user/erasure` | P |
| `routers/activity.py:323` | `_USER_OWNED_DELETES` | DELETE | catalog_item_moves | `POST /api/user/erasure` | I |
| `routers/activity.py:324` | `_USER_OWNED_DELETES` | DELETE | observations | `POST /api/user/erasure` | I |
| `routers/activity.py:325` | `_USER_OWNED_DELETES` | DELETE | collection_snapshots | `POST /api/user/erasure` | I |
| `routers/activity.py:326` | `_USER_OWNED_DELETES` | DELETE | owned_copies | `POST /api/user/erasure` | I |
| `routers/activity.py:327` | `_USER_OWNED_DELETES` | DELETE | user_collections | `POST /api/user/erasure` | I |
| `routers/activity.py:328` | `_USER_OWNED_DELETES` | DELETE | user_wantlists | `POST /api/user/erasure` | I |
| `routers/activity.py:329` | `_USER_OWNED_DELETES` | DELETE | sync_history | `POST /api/user/erasure` | I |
| `routers/activity.py:330` | `_USER_OWNED_DELETES` | DELETE | app_tokens | `POST /api/user/erasure` | Q |
| `routers/activity.py:331` | `_USER_OWNED_DELETES` | DELETE | oauth_tokens | `POST /api/user/erasure` | I |
| `routers/activity.py:339` | `_SOFT_ERASE_USER` | UPDATE | users | `POST /api/user/erasure` | I |
| `routers/activity.py:353` | `_SELECT_CREDENTIALS` | SELECT | users | `POST /api/user/erasure` | I |
| `routers/activity.py:359` | `_EXPORT_EVENTS` | SELECT | activity.events | `GET /api/user/export` | I |
| `routers/activity.py:367` | `_EXPORT_IMPRESSIONS` | SELECT | activity.impressions | `GET /api/user/export` | I |
| `routers/activity.py:375` | `_EXPORT_COLLECTION` | SELECT | user_collections | `GET /api/user/export` | I |
| `routers/activity.py:383` | `_EXPORT_WANTLIST` | SELECT | user_wantlists | `GET /api/user/export` | I |
| `routers/activity.py:391` | `_EXPORT_OWNED_COPIES` | SELECT | owned_copies | `GET /api/user/export` | I |
| `routers/activity.py:398` | `_EXPORT_OBSERVATIONS` | SELECT | observations | `GET /api/user/export` | I |
| `routers/activity.py:408` | `_EXPORT_CATALOG_ITEM_MOVES` | SELECT | catalog_item_moves | `GET /api/user/export` | I |
| `routers/activity.py:417` | `_EXPORT_SNAPSHOTS` | SELECT | collection_snapshots | `GET /api/user/export` | I |
| `routers/activity.py:424` | `_EXPORT_CONSENT` | SELECT | activity.consent_grants | `GET /api/user/export` | I |
| `routers/admin.py:108` | `admin_login` | SELECT | users | `POST /api/admin/auth/login` | I |
| `routers/admin.py:202` | `list_extractions` | SELECT | extraction_history | `GET /api/admin/extractions` | I |
| `routers/admin.py:207` | `list_extractions` | SELECT | extraction_history | `GET /api/admin/extractions` | I |
| `routers/admin.py:253` | `get_extraction` | SELECT | extraction_history | `GET /api/admin/extractions/{extraction_id}` | I |
| `routers/admin.py:454` | `_track_extraction` | UPDATE | extraction_history | `POST /api/admin/extractions/trigger` | I |
| `routers/admin.py:472` | `_track_extraction` | UPDATE | extraction_history | `POST /api/admin/extractions/trigger` | I |
| `routers/admin.py:493` | `_track_extraction` | UPDATE | extraction_history | `POST /api/admin/extractions/trigger` | I |
| `routers/admin.py:508` | `_track_extraction` | UPDATE | extraction_history | `POST /api/admin/extractions/trigger` | I |
| `routers/admin.py:535` | `_track_extraction` | UPDATE | extraction_history | `POST /api/admin/extractions/trigger` | I |
| `routers/admin.py:544` | `_track_extraction` | UPDATE | extraction_history | `POST /api/admin/extractions/trigger` | I |
| `routers/admin.py:554` | `_track_extraction` | UPDATE | extraction_history | `POST /api/admin/extractions/trigger` | I |
| `routers/admin.py:567` | `_track_extraction` | UPDATE | extraction_history | `POST /api/admin/extractions/trigger` | I |
| `routers/admin.py:596` | `trigger_extraction` | INSERT | extraction_history | `POST /api/admin/extractions/trigger` | P |
| `routers/admin.py:605` | `trigger_extraction` | UPDATE | extraction_history | `POST /api/admin/extractions/trigger` | I |
| `routers/admin.py:622` | `trigger_extraction` | UPDATE | extraction_history | `POST /api/admin/extractions/trigger` | I |
| `routers/admin.py:642` | `trigger_extraction` | DELETE | extraction_history | `POST /api/admin/extractions/trigger` | I |
| `routers/admin.py:653` | `trigger_extraction` | UPDATE | extraction_history | `POST /api/admin/extractions/trigger` | I |
| `routers/admin.py:663` | `trigger_extraction` | UPDATE | extraction_history | `POST /api/admin/extractions/trigger` | I |
| `routers/auth.py:116` | `register` | INSERT | users | `POST /api/auth/register` | P |
| `routers/auth.py:172` | `login` | SELECT | users | `POST /api/auth/login` | I |
| `routers/auth.py:273` | `get_me` | SELECT | users | `GET /api/auth/me` | I |
| `routers/auth.py:398` | `_commit_password_change` | UPDATE | users | `POST /api/auth/change-password`, `POST /api/auth/reset-confirm` | I |
| `routers/auth.py:407` | `_commit_password_change` | UPDATE | app_tokens | `POST /api/auth/change-password`, `POST /api/auth/reset-confirm` | I |
| `routers/auth.py:442` | `reset_request` | SELECT | users | `POST /api/auth/reset-request` | I |
| `routers/auth.py:492` | `change_password` | SELECT | users | `POST /api/auth/change-password` | I |
| `routers/auth.py:540` | `twofa_setup` | SELECT | users | `POST /api/auth/2fa/setup` | I |
| `routers/auth.py:563` | `twofa_setup` | UPDATE | users | `POST /api/auth/2fa/setup` | I |
| `routers/auth.py:606` | `twofa_confirm` | SELECT | users | `POST /api/auth/2fa/confirm` | I |
| `routers/auth.py:630` | `twofa_confirm` | UPDATE | users | `POST /api/auth/2fa/confirm` | I |
| `routers/auth.py:686` | `twofa_verify` | SELECT | users | `POST /api/auth/2fa/verify` | I |
| `routers/auth.py:716` | `twofa_verify` | UPDATE | users | `POST /api/auth/2fa/verify` | I |
| `routers/auth.py:767` | `twofa_verify` | UPDATE | users | `POST /api/auth/2fa/verify` | I |
| `routers/auth.py:831` | `twofa_recovery` | UPDATE | users | `POST /api/auth/2fa/recovery` | I |
| `routers/auth.py:894` | `twofa_disable` | SELECT | users | `POST /api/auth/2fa/disable` | I |
| `routers/auth.py:922` | `twofa_disable` | UPDATE | users | `POST /api/auth/2fa/disable` | I |
| `routers/collection.py:96` | `collection_formats` | SELECT | user_collections | `GET /api/collection/formats` | I |
| `routers/insights_compute.py:329` | `_enrich_community_counts` | SELECT | insights.community_counts, user_collections, user_wantlists | `GET /api/internal/insights/community-enrichment` | I |
| `routers/insights_compute.py:366` | `_enrich_community_counts` | SELECT | oauth_tokens | `GET /api/internal/insights/community-enrichment` | I |
| `routers/insights_compute.py:382` | `_enrich_community_counts` | SELECT | app_config | `GET /api/internal/insights/community-enrichment` | I |
| `routers/insights_compute.py:472` | `_enrich_community_counts` | INSERT | insights.community_counts | `GET /api/internal/insights/community-enrichment` | P |
| `routers/observations.py:45` | `_INSERT_OBSERVATION` | INSERT | observations, owned_copies | `POST /api/user/copies/{copy_id}/observations` | I |
| `routers/observations.py:53` | `_SELECT_OBSERVATIONS` | SELECT | observations, owned_copies | `GET /api/user/copies/{copy_id}/observations` | Q |
| `routers/observations.py:62` | `_SELECT_COPY` | SELECT | owned_copies | `GET /api/user/copies/{copy_id}/observations` | I |
| `routers/sync.py:141` | `_start_sync` | SELECT | sync_history | `POST /api/sync` | I |
| `routers/sync.py:150` | `_start_sync` | INSERT | sync_history | `POST /api/sync` | P |
| `routers/sync.py:188` | `sync_status` | SELECT | sync_history | `GET /api/sync/status` | I |
| `setup.py:60` | `show_config` | SELECT | app_config | `job setup.main` | I |
| `setup.py:76` | `set_config` | INSERT | app_config | `job setup.main` | P |
| `syncer.py:104` | `_UPSERT_COLLECTION` | INSERT | user_collections | `POST /api/sync` | P |
| `syncer.py:129` | `_UPSERT_WANTLIST` | INSERT | user_wantlists | `POST /api/sync` | P |
| `syncer.py:155` | `_SELECT_COLLECTION_PAGE` | SELECT | user_collections | `POST /api/sync` | I |
| `syncer.py:161` | `_SELECT_WANTLIST_PAGE` | SELECT | user_wantlists | `POST /api/sync` | I |
| `syncer.py:172` | `_MINT_OWNED_COPIES` | INSERT | owned_copies | `POST /api/sync` | P |
| `syncer.py:188` | `_LINK_OWNED_COPIES` | UPDATE | owned_copies, user_collections | `POST /api/sync` | I |
| `syncer.py:204` | `_DELETE_STALE_COLLECTION` | DELETE | user_collections | `POST /api/sync` | I |
| `syncer.py:209` | `_DELETE_STALE_WANTLIST` | DELETE | user_wantlists | `POST /api/sync` | I |
| `syncer.py:214` | `_SELECT_SNAPSHOT_COPY_IDS` | SELECT | owned_copies | `POST /api/sync` | I |
| `syncer.py:220` | `_INSERT_COLLECTION_SNAPSHOT` | INSERT | collection_snapshots | `POST /api/sync` | P |
| `syncer.py:1023` | `_run_full_sync` | SELECT | oauth_tokens | `POST /api/sync` | I |
| `syncer.py:1040` | `_run_full_sync` | SELECT | app_config | `POST /api/sync` | I |
| `syncer.py:1118` | `_run_full_sync` | UPDATE | sync_history | `POST /api/sync` | I |
| `syncer.py:1155` | `reconcile_stale_sync_history` | UPDATE | sync_history | `job api.lifespan` | I |
