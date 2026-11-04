# catalog-api Cypher performance audit, 2026-11-04

Read-only audit for bead `gm-catalog-api-0n2z`. It covers every Cypher statement in `api/`, ranks
the hot spots with PROFILE evidence, and links a follow-up bead for each worthwhile fix. Nothing
here changes production code.

See the [documentation index](README.md) and the
[earlier query performance decisions](query-performance-optimizations.md).

## Summary

- **Cost follows the hubs, and the worst hubs have no name.** Various (artist 194, 1.53M `BY`
  edges), Unknown Artist (355, 95K) and Not On Label (label 1818, 763K) all carry a NULL `name`.
  `/api/expand` matches by name and cannot reach them. Every id-keyed route can.
- **One small request can hold Neo4j for minutes.** 119 of 150 statements set no server timeout,
  and lux's `db.transaction.timeout` is 10 minutes. `db.memory.transaction.max` is unlimited
  against a 3 GiB shared pool.
- **The two worst routes need bounding.** `/api/network/artist/{id}/collaborators` cost 15.2M db
  hits and 6.1 s for an artist with 28 neighbours. `/api/recommend/explore` used 463 MB for a genre
  of 75K releases, so Rock would exhaust the pool. Neither was run at its worst on lux, on purpose.
- **`/api/genre-tree` cannot finish in its 30-second budget** on the full graph: about 78 s by
  extrapolation from two measured parts.
- **Several routes recount what the graph already stores.** `/api/node` for Rock takes 39.8 s and
  112M db hits where a property read takes 3 hits and 8 ms.
- **Cold reads cost 10 to 18 times warm reads.** That, not query shape, explains most of the 0.85 s
  and 2.44 s signals in the bead. Heavy pages stay linear in degree, so they need caching.

## Evidence and limits

| Source | Environment | Used for |
| --- | --- | --- |
| lux, 2026-11-04 19:59-20:30 UTC | Live Neo4j: Cypher 25, slotted runtime, Community, 42.5M nodes, about 227M relationships, 5 GiB heap, 11 GiB page cache, 3 GiB transaction pool. Graph load complete. | db hits, time, memory and rows. 185 runs of 123 statement and node pairs, one at a time through the operator-approved read-only wrapper. |
| Local synthetic graph | `neo4j:2026.10.0-community` (deployment pin) on Docker Desktop, 4 CPUs, 1.2 GB heap, 1 GB page cache, real `database-schema` indexes. 821K nodes, 4.0M relationships: about 1/50 of lux, shaped to match its degree skew. | Operator shapes (the wrapper's plain format returns none), user routes, and queries too risky for lux. |
| Static review | This revision of `api/`, the router code and `database-schema` | Inventory, callers, caches, timeouts. |

Read these numbers with the following limits:

- **PostgreSQL:** this report has no SQL timings. The load is partial there (3.97M of 19.6M
  releases), and `gm-catalog-api-6zq4` owns that audit.
- **Timing:** "first run" is the first execution in this audit, so it is cold only for pages nothing
  had touched. A fix run after its original benefits from warm pages. Compare db hits for fixes:
  they do not depend on run order.
- **PROFILE on lux gives no page-cache hit counts or operator tree.** Those columns in the design
  could not be filled live. Literals replaced parameters, because the wrapper takes one string.
- **HTTP cost is unmeasured:** Bolt transfer, JSON encoding and Python work are not in these times.
- **lux has no `User` nodes,** so collection, gap and fit reads have local evidence only, and taste
  reads were reviewed statically and not profiled.
- **Local timings are not predictions.** Local data is synthetic, and it is used for shape and
  ratios, never for absolute time on lux.
- **Extrapolations are labelled.** They scale a measured part by a live edge count.
- **`gm-deployment-0x54` has not published its baseline report or workload.** Its two figures in
  `gm-catalog-api-8fou` were used as a cross-check: its Rock trends read of 20.7M db hits matches
  the 20,715,337 here. Its 17.5 s compares with 3.9 s here, on pages already warmed by earlier Rock
  reads.

Safety on lux: every slow statement was followed by a seek canary, which stayed at 0-3 ms. No run
errored, and the longest took 45.5 s. The local run was cut from 1/10 to 1/50 scale when the shared
disk reached 3.1 GB free, and its container and data are gone.

## Ranked hot spots

Rank weighs live cost, exposure and whether a request can harm other requests. `Effort` is the
expected size of the fix. Measured fixes ran on lux unless marked.

| Rank | Route | Live cost today | Fix result | Gain | Risk | Effort | Bead |
| --- | --- | ---: | --- | --- | --- | --- | --- |
| H1 | `/api/network/artist/{id}/collaborators` | 15.2M hits, 6.1 s, light artist. Bowie not run, est. 10-minute timeout | 70.5K hits, 15 ms; Bowie 3.8M hits, 0.56 s | 215x, and unbounded becomes bounded | Medium: hub results change | L | `gm-catalog-api-p85n` |
| H2 | `/api/recommend/explore/{type}/{id}` | 2.9M hits, 7.4 s, 463 MB, 75K-release genre. Rock not run, est. pool exhaustion | 13.6K hits, 4 ms, 126 KB for Rock | Failure becomes 4 ms | Medium: sampling changes discoveries | L | `gm-catalog-api-zazs` |
| H3 | `/api/genre-tree` | Est. 78 s against a 30 s timeout; 1.05 GB in phase 1 | Phase 1 46 hits; phase 2 needs precomputing | Est. 504 becomes under 200 ms | Medium: refresh ownership | M | `gm-catalog-api-somq` |
| H4 | `/api/enrichment/status` | 45.3 s and 143.6M hits for CREDITED_ON alone; est. over 2 minutes in full | Not measured | Est. over 100x on a cache hit | Low | M | `gm-catalog-api-nhux` |
| H5 | `/api/node/{id}` | Rock 39.8 s, 112.2M hits; artist 194 45.5 s, 75.5M hits | 3 hits, 8 ms for Rock | 37M x in hits for genre, style, label | Low: stale until next maintenance | M | `gm-catalog-api-vus1` |
| H6 | `/api/network/cluster/{id}` | Bowie 29.6 s, 37.5M hits | 218 ms, 1.1M hits | 34x in hits | Low to medium: sampled genre | S | `gm-catalog-api-575h` |
| H7 | `/api/expand` | Rock releases 19.5 s + 3.5 s; `before_year` 8.6 s, 336 MB | `before_year` 0.36 s, 21 MB; totals 3 hits | 2.7x hits and about 15x memory; cache for the rest | Low | M | `gm-catalog-api-9d8z`, `gm-catalog-api-vus1` |
| H8 | `/api/user/collection`, `/wantlist`, `/api/collection/gaps/*` | No live users. Local: 731K and 2.73M hits | Local: 43K and 772K hits | 17x and 3.5x; memory over 800x | Low to medium: row selection | M | `gm-catalog-api-og3t` |
| H9 | `/api/label/{id}/dna`, `/similar` | Columbia 40M hits across 8 statements; similar 54.7M, 28.3 s | One pass 19.6M hits, but 113 MB | About 2x, cold only | Low | M | `gm-catalog-api-j4ml` |
| H10 | `/api/graph/stats` | 34.9M hits, 1.0 s | 6 hits, 1 ms | 5.8M x | Very low | S | `gm-catalog-api-88cu` |
| H11 | `/api/explore/strata` | Shared personnel 1,644 ms first, 80 ms repeat, every request | Cache not measured | Removes it from repeat requests | Low | S | `gm-catalog-api-a01i` |
| H12 | `/api/credits/person/{name}`, `/connections` | 8.2 s, 110 MB, 60,758 rows; connections 8.5 s, 23.7M hits | Not measured | Page-sized read | Medium: contract change | M | `gm-catalog-api-b78b` |
| H13 | `/api/rarity/label/{id}` | 15.7 s, 762,594 ids sent to Python, then twice to SQL | Not measured | Removes the Neo4j read | Medium: needs SQL design | M | `gm-catalog-api-2x1z` |
| H14 | `/api/artist/{id}/relationships` | Various 3.06M hits, 502 ms | 26 hits, 9 ms | 118K x | Low: type list drift | S | `gm-catalog-api-i6l1` |
| H15 | `/api/autocomplete` | 119 ms warm for `the*`, arbitrary top 10 | 4 ms with `{limit}`, still arbitrary | 30x; relevance needs more work | Medium: ranking change | M | `gm-catalog-api-4wgh` |

Suggested order: `gm-catalog-api-jpyk` (timeouts) first because it caps every hot spot, then the
small, safe wins `gm-catalog-api-88cu`, `gm-catalog-api-i6l1` and `gm-catalog-api-vus1`, then
`gm-catalog-api-p85n`, `gm-catalog-api-zazs` and `gm-catalog-api-somq`.

### H1: network collaborators

- **Evidence:** Indecent Noise, with 28 neighbours and a neighbour degree sum of 5,823, took 15,166,855
  hits and 6,053 ms. Local Bowie took 151,270,088 hits and 48.9 s on the 1/50 graph; local light took
  738,372 hits. At depth 1, Bowie takes 290,613 hits and 966 ms.
- **Cause:** `network_queries.py:50-52` walks four hops from a root, then runs `NOT EXISTS` per row,
  which re-expands the root. Neighbours are not deduplicated first, so each shared release repeats the
  walk through that neighbour. The route also runs the whole thing twice, once for the page and once
  for `count_multi_hop_collaborators`.
- **Why Bowie was not run:** he has 187 neighbours, including 3 releases with Various and 1 with
  Unknown Artist, for a neighbour degree sum of 2.08M. Those rows multiply by the per-row check, so
  the statement is expected to reach the 10-minute ceiling. Any artist with one Various co-credit
  shares this exposure.
- **Fix:** collect the direct set once, drop placeholder and over-cap neighbours, and return the page
  and total from one statement. The measured candidate peaked at 327 MB for Bowie, so it needs a
  top-K before adoption.
- **Also:** `depth=3` is accepted but only two hops are traversed.

### H2: recommend explore traversal

- **Evidence:** at the default `hops=2`, Brass & Military took 2,868,139 hits, 7,431 ms and 463,060,528
  bytes. Bowie took 542,507 hits, 902 ms and 84 MB. Local `hops=3` from Bowie passed the 100 s client
  limit, and local Rock `hops=2` used 575 MB on the 1/50 graph.
- **Extrapolation:** 6.2 KB per release reaches 3 GiB at about 520K releases. Ten genres and at least
  five styles exceed that. Rock at 6.37M releases implies about 39 GB. A pool failure hits concurrent
  transactions too, so Rock was not run on lux.
- **Cause:** `recommend_queries.py:447` enumerates every path through hubs, holds names and types for
  each, sorts them all, and applies `LIMIT 100` at the end. The route also has no server timeout and
  the 1-hour cache only fills on success.
- **Fix:** expand hop by hop with a cap per hop. The bounded candidate used a plain `LIMIT 500`, so the
  bead asks for a deliberate ranking rather than that sample.

### H3: genre tree

- **Evidence:** phase 1 took 12,092 ms, 59,134,448 hits and 1,045,920,240 bytes, the largest allocation
  in this audit. Phase 2 for Rock took 16,087 ms, 131,264,491 hits and 443 MB.
- **Extrapolation:** phase 2 scales with `IS` edges to genres, 26.1M against Rock's 6.37M, or about
  66 s and 538M hits. With phase 1 that is about 78 s, so the route should return 504 on every attempt
  and never fill its 5-minute cache. The full statement was not run, because its memory could exceed
  1.5 GB. Confirm by calling the route.
- **Fix:** read `Genre.release_count` for phase 1 and compute co-occurrence outside the request path.
  Replacing `count(DISTINCT r2)` with `count(*)` cut Brass & Military from 4.8 MB to 91 KB at equal
  hits, but only if duplicate edges are ruled out. No rewrite meets the budget alone.

### H4: enrichment status

- **Evidence:** `MATCH ()-[r:CREDITED_ON]->() WHERE r.source = 'musicbrainz'` took 45,346 ms and
  143,624,457 hits, or 2 hits per edge. That is 32% of all relationships, so the real statement at
  `musicbrainz_queries.py:120` is about 455M hits and over 2 minutes.
- **Cause:** an untyped scan with a property read on a public route, with no cache and no timeout.
- **Fix:** keep these counts outside the request path, or scan only source-bearing types and cache.

### H5: node details

- **Evidence:** `get_genre_details` for Rock took 39,817 ms and 112,161,063 hits. `get_label_details`
  for label 1818 took 1,047 ms. `get_artist_details` for 194 took 45,524 ms and 75,476,887 hits. A
  property read took 3 hits and 8 ms.
- **Cause:** `neo4j_queries.py:732-751` recounts values the enricher stores and `explore_*` reads.
- **Limit:** artists have no stored aggregate. Bare degree overcounts, because 3.19M `BY` edges start
  at `Master` nodes (817 of Bowie's 15,515). `gm-catalog-api-87pz` owns the artist aggregate.

### H6: network cluster

- **Evidence:** Bowie took 29,558 ms and 37,505,004 hits, against 218 ms and 1,112,658 hits with the
  limit first.
- **Cause:** `network_queries.py:147` computes a genre histogram for every neighbour before `LIMIT`, so
  one Various co-credit costs 1.53M releases.

### H7: expand

- **Evidence, no filter:**

| Parent | Page hits | Page first / repeat ms | Total hits | Total first ms |
| --- | ---: | ---: | ---: | ---: |
| Rock releases | 32,830,989 | 19,540 / not repeated | 14,343,985 | 3,520 |
| Columbia artists | 6,799,020 | 14,114 / 1,483 | 5,950,266 | 759 |
| Columbia releases | 1,683,408 | 5,823 / 353 | 702,371 | 221 |
| Beethoven releases | 290,079 | 1,406 / 77 | 132,101 | 49 |
| The Beatles releases | 176,548 | 829 / 51 | 76,402 | 38 |
| David Bowie releases | 72,846 | 267 / 27 | 31,036 | 19 |
| Indecent Noise releases | 1,775 | 19 / 1 | 804 | 5 |

- **Cause:** a page costs about 5 hits per edge because every row is read and sorted before `LIMIT`.
  `ORDER BY year IS NULL ASC, year DESC, id` cannot use an index, and deferring `r.title` saved nothing.
  The rewrite that helps is a cache, as `/api/explore` already has. Rock artists are not in the table:
  the total uses the same plan as H5 (112M hits, 39.8 s).
- **`before_year`:** the planner starts at `release_year_index` and sorts every older release, then
  expands into the genre. That inverts cost: Brass & Military took 5,441,915 hits, 8,638 ms and 336 MB,
  and Rock 5,336,097 hits, 2,513 ms and 335 MB.
- **Fix for `before_year`:** `ORDER BY r.year DESC, r.id` gives the same order, since the filter
  removes nulls, and lets the index supply it. Measured: 2.0M hits, 356-385 ms, 21-23 MB for both.
- **Rejected:** a `CALL` barrier, as `trends_genre` uses, helped Brass & Military (261,614 hits) but took
  Rock to 21,074,379 hits and 22.1 s.

### H8: collection and gap pages

- **Evidence, local only:** collection page 731,293 hits and 20.8 MB against 43,468 hits and 16 KB after
  deferring. Label gaps 2,731,113 hits and 65.9 MB against 771,811 hits and 81 KB, on a 50K-release label.
- **Cause:** `OPTIONAL MATCH` enrichment runs on every row before `SKIP` and `LIMIT`. Gap routes then
  repeat the traversal for the total and the summary, in sequence. Gap routes accept label 1818, whose
  degree is 15 times the local test label.

### H9: label DNA and similar labels

- **Evidence:** Columbia statements total 39.9M hits. `dna.formats` took 20,487 ms on first touch and
  `dna.decade` 4,871 ms. Similar labels took 54,744,661 hits and 28,275 ms. Local similar-label cost was
  the same for a heavy and a light label, because it is set by their top styles.
- **Fix:** read stored identity counts, merge year-based reads and cap style expansion. Both routes
  already cache for 24 hours, so this is the cold path.

### H10: graph stats

- **Evidence:** 34,907,529 hits and about 1,000 ms, against 6 hits and 1 ms with the same six rows.
- **Cause:** `RETURN 'artists' AS label, count(a)` groups on a literal, so the planner scans. Moving the
  count into a `WITH` lets it read the count store.

### H11: strata

- **Evidence:** ladder for Bowie 218 ms first, 132 ms repeat, 678,489 hits. Shared personnel for his 40
  largest masters 1,644 ms first, 80 ms repeat, 358,550 hits.
- **Cause:** the catalog is cached for 24 hours, but labels and shared personnel run on every request.
- **Rejected:** flattening the ladder kept 678,489 hits and raised memory from 393 KB to 5.2 MB.
- **Not measured:** MusicBrainz candidate SQL and `build_strata`, which are also in the 2.44 s signal.

### H12 to H15

- **H12, credits:** `get_person_credits` has no limit, so Bob Ludwig returns 60,758 rows at 8,227 ms and
  110 MB, cached for 24 hours. `get_person_connections` slices second hops before ranking them, and
  `depth=3` behaves like `depth=2`.
- **H13, rarity:** `rarity_queries.py:683` moves every release id into Python, then into two `ANY`
  queries. Its SQL cost is left to `gm-catalog-api-6zq4`.
- **H14, relationships:** the untyped pattern reads `source` on every incoming `BY` edge.
- **H15, autocomplete:** every wildcard term scores 1.0, so `ORDER BY score` adds nothing. Current and
  `{limit}` results shared 1 of 10 rows for `the*` and for `joh*`. Neither is a relevance ranking.

## Cross-cutting findings

- **C1, timeouts:** 119 of 150 statements pass no timeout, so the 504 handlers in `network.py` and others
  fire only after 10 minutes. Filed as `gm-catalog-api-jpyk`.
- **C2, memory (deployment):** `db.memory.transaction.max` is `0B`, unlimited, against a 3 GiB pool. Five
  production statement runs exceeded 300 MB: H3 phase 1, H3 phase 2 for Rock, H2, and H7 `before_year`
  twice. Proposed for
  deployment or homelab: a per-transaction limit, set after H2 and H3 land so it does not break them.
- **C3, page cache (deployment):** first runs cost 10 to 18 times repeats (Bowie 267/27 ms, Beatles
  829/51 ms, Beethoven 1,406/77 ms). lux has 11 GiB, and the production Compose comment assumes a 25 GB
  store and 131M relationships, against about 227M today. Store size could not be read through the
  wrapper. Proposed for `gm-deployment-0x54`: compare store size with the page cache.
- **C4, rate limits:** eight public Cypher routes have none: `/api/explore`, `/api/expand`, `/api/node`,
  `/api/trends`, `/api/path`, `/api/graph/stats`, `/api/explore/year-range` and
  `/api/explore/genre-emergence`. Six of those are also uncached. `gm-catalog-api-8fou` covers four;
  `gm-catalog-api-jpyk` should cover the rest.
- **C5, `Master` edges:** 3.19M `BY` edges start at `Master`, so bare `BY` degree is not a release count.
  The strata root guard reads that degree. The effect is small today, but it is a trap for anyone
  reaching for a degree shortcut.
- **C6, nameless hubs:** labels 1818 and 11358 and artists 194 and 355 have NULL names, so a search for
  `Not On Label` finds nothing. Name-keyed routes cannot open them, and id-keyed routes have no guard.
- **C7, Explore counts:** the current `explore_artist` statement returns 14,698 releases and 1,462 labels
  for Bowie from `BY` alone. That contradicts the explanation in `gm-catalog-api-64bl`, where 8,850 is
  said to be the `BY` figure. A 24-hour Explore cache entry written during the load is a likely cause.
  It was not confirmed, because Valkey was not read.

## Checked and cleared

- `size([(a)-[]-() | 1])` costs the same as `COUNT { (a)--() }`: 400,201 hits locally, so the warning in
  `rarity_queries.py` is not a live concern for these statements.
- `get_year_range` (6 hits), the role leaderboard (44 hits) and `get_release_context` (5,397 hits on the
  largest local master) use their indexes well.
- `/api/path` took 731 ms for Bowie to The Beatles and 947 ms for a light artist. Deep searches are
  covered by `gm-catalog-api-8fou`.
- `similar.candidates` is bounded by its inner caps at 8.6-9.0M hits. It costs the same for small and
  large artists, is cached for 24 hours, and is the target of `gm-catalog-api-2zsq`.
- No true N+1 was found: callers batch with `UNWIND`. Deep `SKIP` costs little extra, because each page
  already reads and sorts every row.
- All 47 schema indexes are online on lux. No missing index was found. `Genre.label_count` and the other
  stored aggregates are populated.

Not profiled on either graph: taste reads, internal Analytics batch reads (their code documents 30 to
110 s, and `label-longevity`, `genre-trends`, `artist-centrality` and `anniversaries` lack a budget), sync and projection
writes, erasure, and simple id lookups.

## Follow-up beads

Filed from this audit, all `discovered-from` `gm-catalog-api-0n2z`:

| Bead | Priority | Fix |
| --- | --- | --- |
| `gm-catalog-api-jpyk` | P1 | Default server timeout on every read; remaining rate limits |
| `gm-catalog-api-p85n` | P1 | Bound network collaborators (H1) |
| `gm-catalog-api-zazs` | P1 | Replace traversal enumeration (H2) |
| `gm-catalog-api-somq` | P1 | Serve a precomputed genre tree (H3) |
| `gm-catalog-api-vus1` | P1 | Read stored aggregates in node details and expand totals (H5, H7) |
| `gm-catalog-api-9d8z` | P1 | Index-ordered `before_year` and expand caching (H7) |
| `gm-catalog-api-nhux` | P2 | Enrichment status without a full scan (H4) |
| `gm-catalog-api-575h` | P2 | Limit before genre lookup in cluster (H6) |
| `gm-catalog-api-og3t` | P2 | Defer page enrichment (H8) |
| `gm-catalog-api-88cu` | P2 | Graph stats from the count store (H10) |
| `gm-catalog-api-a01i` | P2 | Cache shared personnel (H11) |
| `gm-catalog-api-i6l1` | P2 | Typed MusicBrainz relationships (H14) |
| `gm-catalog-api-4wgh` | P2 | Relevant, bounded autocomplete (H15) |
| `gm-catalog-api-j4ml` | P3 | Label DNA and similar-label cost (H9) |
| `gm-catalog-api-b78b` | P3 | Credits pagination (H12) |
| `gm-catalog-api-2x1z` | P3 | Rarity without full id lists (H13) |

Already filed elsewhere, and related: `gm-catalog-api-8fou` (rate limits and path bounds),
`gm-catalog-api-87pz` (artist aggregates), `gm-catalog-api-k0rl` (per-query latency metrics) and
`gm-catalog-api-64bl` (Explore counts).

Two corrections to filed text: the `gm-catalog-api-zazs` description says five genres exceed the pool
threshold, and this report's count of ten genres is correct. `gm-catalog-api-jpyk` overlaps
`gm-catalog-api-8fou` on rate limits for four routes.

## Appendix A: inventory by route

Every route that reaches Cypher: 62 routes and 150 statements. `T` marks a function that passes a
server timeout. `Cache` means a Valkey or in-process cache exists somewhere on the route, and gap routes
cache only their summary. The NLQ tools call the same query functions (`api/nlq/tools.py`), and sync
writes appear under `POST /api/sync`. Generated from the source with an AST walk that resolves imports.
Routes written `/internal-analytics/...` sit under the internal prefix declared in
`api/routers/insights_compute.py`.

| Route | Auth | Rate limit | Cache | Statements (`file:line`, function; `T` marks a server timeout) |
| --- | --- | --- | --- | --- |
| POST `/api/admin/identity/project` | admin | none | no | `projection.py:56` _set_gm_id_cypher<br>`projection.py:64` _set_gm_id_cypher |
| GET `/api/admin/storage` | admin | none | no | `admin_queries.py:141` get_neo4j_storage |
| GET `/api/artist/{artist_id}/musicbrainz` | public | 30/minute | no | `musicbrainz_queries.py:16` get_artist_musicbrainz |
| GET `/api/artist/{artist_id}/relationships` | public | 30/minute | no | `musicbrainz_queries.py:43` get_artist_mb_relationships |
| GET `/api/autocomplete` | public | 30/minute | yes | `neo4j_queries.py:56` _autocomplete |
| GET `/api/collaborators/{artist_id}` | public | 30/minute | no | `collaborator_queries.py:16` get_artist_identity<br>`collaborator_queries.py:25` get_collaborators<br>`collaborator_queries.py:46` count_collaborators |
| GET `/api/collection/gaps/artist/{artist_id}` | user | none | yes | `gap_queries.py:148` get_artist_gaps<br>`gap_queries.py:152` get_artist_gaps<br>`gap_queries.py:165` get_artist_gaps<br>`gap_queries.py:188` get_artist_gap_summary<br>`gap_queries.py:206` get_artist_metadata |
| GET `/api/collection/gaps/label/{label_id}` | user | none | yes | `gap_queries.py:114` get_label_gap_summary<br>`gap_queries.py:132` get_label_metadata<br>`gap_queries.py:74` get_label_gaps<br>`gap_queries.py:78` get_label_gaps<br>`gap_queries.py:91` get_label_gaps |
| GET `/api/collection/gaps/master/{master_id}` | user | none | yes | `gap_queries.py:222` get_master_gaps<br>`gap_queries.py:226` get_master_gaps<br>`gap_queries.py:242` get_master_gaps<br>`gap_queries.py:265` get_master_gap_summary<br>`gap_queries.py:283` get_master_metadata |
| GET `/api/credits/autocomplete` | public | 120/minute | no | `credits_queries.py:220` autocomplete_person |
| GET `/api/credits/connections/{name}` | public | 30/minute | no | `credits_queries.py:179` get_person_connections<br>`credits_queries.py:188` get_person_connections |
| GET `/api/credits/person/{name}` | public | 60/minute | yes | `credits_queries.py:30` get_person_credits |
| GET `/api/credits/person/{name}/profile` | public | 60/minute | no | `credits_queries.py:235` get_person_profile<br>`credits_queries.py:258` get_person_role_breakdown |
| GET `/api/credits/person/{name}/timeline` | public | 60/minute | no | `credits_queries.py:51` get_person_timeline |
| GET `/api/credits/release/{release_id}` | public | 60/minute | no | `credits_queries.py:67` get_release_credits |
| GET `/api/credits/role/{role}/top` | public | 30/minute | yes | `credits_queries.py:123` get_role_leaderboard<br>`credits_queries.py:133` get_role_leaderboard<br>`credits_queries.py:80` get_role_leaderboard |
| GET `/api/credits/shared` | public | 30/minute | no | `credits_queries.py:149` get_shared_credits |
| GET `/api/enrichment/status` | public | 10/minute | no | `musicbrainz_queries.py:114` get_enrichment_status<br>`musicbrainz_queries.py:120` get_enrichment_status |
| GET `/api/expand` | public | none | no | `neo4j_queries.py:359` _expand_aggregate<br>`neo4j_queries.py:411` _expand_releases<br>`neo4j_queries.py:449` expand_artist_aliases<br>`neo4j_queries.py:563` _count<br>`neo4j_queries.py:565` _count<br>`neo4j_queries.py:606` count_artist_aliases |
| GET `/api/explore` | public | none | yes | `neo4j_queries.py:180` explore_artist<br>`neo4j_queries.py:210` explore_genre<br>`neo4j_queries.py:239` explore_label<br>`neo4j_queries.py:261` explore_label<br>`neo4j_queries.py:285` explore_style |
| GET `/api/explore/genre-emergence` | public | none | no | `neo4j_queries.py:863` get_genre_emergence<br>`neo4j_queries.py:869` get_genre_emergence |
| GET `/api/explore/strata` | optional | 60/minute | yes | `strata_queries.py:105` get_shared_personnel T<br>`strata_queries.py:121` get_musicbrainz_only_releases T<br>`strata_queries.py:54` get_strata_root T<br>`strata_queries.py:61` get_strata_ladder T<br>`strata_queries.py:77` get_strata_marks T<br>`strata_queries.py:82` get_strata_labels T |
| GET `/api/explore/year-range` | public | none | no | `neo4j_queries.py:835` get_year_range |
| GET `/api/fit/release/{release_id}` | user | 30/minute | yes | `fit_queries.py:103` get_release_context<br>`fit_queries.py:83` get_collection_ids |
| GET `/api/genre-tree` | public | 30/minute | yes | `genre_tree_queries.py:14` get_genre_tree T |
| GET `/api/graph/stats` | public | none | no | `neo4j_queries.py:985` get_graph_stats |
| GET `/internal-analytics/anniversaries` | internal | 5/minute | no | `insights_neo4j_queries.py:261` query_monthly_anniversaries |
| GET `/internal-analytics/artist-centrality` | internal | 5/minute | no | `insights_neo4j_queries.py:50` query_artist_centrality |
| GET `/internal-analytics/community-enrichment` | internal | 1/minute | no | `routers/insights_compute.py:489` _enrich_community_counts T |
| GET `/internal-analytics/genre-trends` | internal | 5/minute | no | `insights_neo4j_queries.py:69` query_genre_trends<br>`insights_neo4j_queries.py:78` query_genre_trends |
| GET `/internal-analytics/label-longevity` | internal | 5/minute | no | `insights_neo4j_queries.py:216` query_label_longevity |
| GET `/internal-analytics/rarity-scores` | internal | 5/minute | no | `rarity_queries.py:122` _fetch_release_id_page T<br>`rarity_queries.py:131` _warn_on_incomplete_coverage T |
| GET `/internal-analytics/style-cotags` | internal | 5/minute | no | `insights_neo4j_queries.py:286` query_style_cotags T |
| GET `/internal-analytics/style-landmarks` | internal | 5/minute | no | `insights_neo4j_queries.py:177` query_style_landmarks T |
| GET `/internal-analytics/style-year-counts` | internal | 5/minute | no | `insights_neo4j_queries.py:107` query_style_year_counts T<br>`insights_neo4j_queries.py:115` query_style_year_counts T<br>`insights_neo4j_queries.py:126` query_style_year_counts T<br>`insights_neo4j_queries.py:94` query_style_year_counts T |
| GET `/api/label/dna/compare` | public | 30/minute | yes | `label_dna_queries.py:103` get_label_media_family_counts<br>`label_dna_queries.py:119` get_label_medium_counts<br>`label_dna_queries.py:136` get_label_media_families_fallback<br>`label_dna_queries.py:26` get_label_identity<br>`label_dna_queries.py:39` get_label_genre_profile<br>`label_dna_queries.py:50` get_label_style_profile<br>`label_dna_queries.py:61` get_label_decade_profile<br>`label_dna_queries.py:73` get_label_active_years<br>`label_dna_queries.py:85` get_label_format_profile |
| GET `/api/label/{label_id}/dna` | public | 30/minute | yes | `label_dna_queries.py:103` get_label_media_family_counts<br>`label_dna_queries.py:119` get_label_medium_counts<br>`label_dna_queries.py:136` get_label_media_families_fallback<br>`label_dna_queries.py:26` get_label_identity<br>`label_dna_queries.py:39` get_label_genre_profile<br>`label_dna_queries.py:50` get_label_style_profile<br>`label_dna_queries.py:61` get_label_decade_profile<br>`label_dna_queries.py:73` get_label_active_years<br>`label_dna_queries.py:85` get_label_format_profile |
| GET `/api/label/{label_id}/similar` | public | 30/minute | yes | `label_dna_queries.py:238` get_candidate_labels_genre_vectors T<br>`label_dna_queries.py:26` get_label_identity<br>`label_dna_queries.py:278` _fetch_batch T<br>`label_dna_queries.py:278` get_candidate_labels_genre_vectors T<br>`label_dna_queries.py:283` _fetch_batch T<br>`label_dna_queries.py:283` get_candidate_labels_genre_vectors T<br>`label_dna_queries.py:39` get_label_genre_profile |
| GET `/api/network/artist/{artist_id}/centrality` | public | 30/minute | yes | `network_queries.py:107` get_artist_centrality |
| GET `/api/network/artist/{artist_id}/collaborators` | public | 30/minute | no | `network_queries.py:20` get_artist_identity<br>`network_queries.py:40` get_multi_hop_collaborators<br>`network_queries.py:74` count_multi_hop_collaborators |
| GET `/api/network/cluster/{artist_id}` | public | 30/minute | yes | `network_queries.py:142` get_artist_cluster<br>`network_queries.py:20` get_artist_identity |
| GET `/api/node/{node_id}` | public | none | no | `neo4j_queries.py:681` get_artist_details<br>`neo4j_queries.py:704` get_release_details<br>`neo4j_queries.py:723` get_label_details<br>`neo4j_queries.py:734` get_genre_details<br>`neo4j_queries.py:745` get_style_details |
| GET `/api/path` | public | none | no | `neo4j_queries.py:134` find_shortest_path T<br>`neo4j_queries.py:180` explore_artist<br>`neo4j_queries.py:210` explore_genre<br>`neo4j_queries.py:239` explore_label<br>`neo4j_queries.py:261` explore_label<br>`neo4j_queries.py:285` explore_style |
| GET `/api/rarity/artist/{artist_id}` | public | 30/minute | no | `rarity_queries.py:618` get_rarity_by_artist<br>`rarity_queries.py:627` get_rarity_by_artist |
| GET `/api/rarity/label/{label_id}` | public | 30/minute | no | `rarity_queries.py:676` get_rarity_by_label<br>`rarity_queries.py:685` get_rarity_by_label |
| GET `/api/recommend/explore/{entity_type}/{entity_id}` | user | 30/minute | yes | `recommend_queries.py:445` get_explore_traversal<br>`taste_queries.py:124` get_blind_spots T<br>`taste_queries.py:34` get_taste_heatmap T<br>`taste_queries.py:41` get_taste_heatmap T |
| GET `/api/recommend/similar/artist/{artist_id}` | optional | 30/minute | yes | `recommend_queries.py:101` _batch_artist_profiles<br>`recommend_queries.py:107` _batch_artist_profiles<br>`recommend_queries.py:113` _batch_artist_profiles<br>`recommend_queries.py:156` get_profiled_artists<br>`recommend_queries.py:188` get_candidate_artists T<br>`recommend_queries.py:40` get_artist_identity<br>`recommend_queries.py:51` get_artist_profile<br>`recommend_queries.py:56` get_artist_profile<br>`recommend_queries.py:61` get_artist_profile<br>`recommend_queries.py:66` get_artist_profile<br>`recommend_queries.py:95` _batch_artist_profiles |
| POST `/api/sync` | public | 10/minute | yes | `syncer.py:620` sync_collection T<br>`syncer.py:728` _reconcile_stale_collection<br>`syncer.py:869` sync_wantlist T<br>`syncer.py:964` _reconcile_stale_wantlist |
| GET `/api/trends` | public | none | yes | `neo4j_queries.py:759` trends_artist<br>`neo4j_queries.py:778` trends_genre<br>`neo4j_queries.py:795` trends_label<br>`neo4j_queries.py:811` trends_style |
| GET `/api/user/collection` | user | 60/minute | no | `user_queries.py:65` get_user_collection<br>`user_queries.py:85` get_user_collection |
| GET `/api/user/collection/evolution` | user | none | yes | `user_queries.py:375` get_user_collection_evolution |
| GET `/api/user/collection/stats` | user | 60/minute | no | `user_queries.py:176` get_user_collection_stats<br>`user_queries.py:182` get_user_collection_stats<br>`user_queries.py:189` get_user_collection_stats<br>`user_queries.py:195` get_user_collection_stats<br>`user_queries.py:199` get_user_collection_stats<br>`user_queries.py:203` get_user_collection_stats<br>`user_queries.py:207` get_user_collection_stats |
| GET `/api/user/collection/timeline` | user | 60/minute | yes | `user_queries.py:254` get_user_collection_timeline |
| POST `/api/user/erasure` | user | none | yes | `routers/activity.py:355` _erase_neo4j |
| GET `/api/user/recommendations` | user | none | no | `recommend_queries.py:359` get_collector_counts<br>`recommend_queries.py:371` get_label_affinity_candidates<br>`recommend_queries.py:396` get_blindspot_candidates<br>`user_queries.py:146` get_user_recommendations |
| GET `/api/user/status` | optional | none | no | `user_queries.py:417` check_releases_user_status |
| GET `/api/user/taste-projection` | user | none | no | `taste_queries.py:168` get_taste_projection T |
| GET `/api/user/taste/blindspots` | user | none | no | `taste_queries.py:124` get_blind_spots T<br>`taste_queries.py:19` get_collection_count T |
| GET `/api/user/taste/card` | user | none | no | `taste_queries.py:103` get_taste_drift T<br>`taste_queries.py:150` get_top_labels T<br>`taste_queries.py:19` get_collection_count T<br>`taste_queries.py:34` get_taste_heatmap T<br>`taste_queries.py:41` get_taste_heatmap T<br>`taste_queries.py:62` get_obscurity_score T |
| GET `/api/user/taste/fingerprint` | user | none | no | `taste_queries.py:103` get_taste_drift T<br>`taste_queries.py:124` get_blind_spots T<br>`taste_queries.py:19` get_collection_count T<br>`taste_queries.py:34` get_taste_heatmap T<br>`taste_queries.py:41` get_taste_heatmap T<br>`taste_queries.py:62` get_obscurity_score T |
| GET `/api/user/taste/heatmap` | user | none | no | `taste_queries.py:19` get_collection_count T<br>`taste_queries.py:34` get_taste_heatmap T<br>`taste_queries.py:41` get_taste_heatmap T |
| GET `/api/user/wantlist` | user | none | no | `user_queries.py:106` get_user_wantlist<br>`user_queries.py:125` get_user_wantlist |

## Appendix B: lux measurements

Statement names are this audit's labels. `FIX.` rows are candidates, not production statements. Time is
the server's reported query time. One row is one statement and node pair. Repeat is `n/a` when a statement
was run once.

| Statement | Node | First run ms | Repeat ms | db hits | Peak memory bytes | Rows |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| `expand.artist.releases.page` | Beatles(38k) | 829 | 51 | 176,548 | 9,312 | 50 |
| `expand.artist.labels.page` | Beatles(38k) | 1,169 | 128 | 671,835 | 3,014,720 | 50 |
| `expand.artist.releases.count` | Beatles(38k) | 38 | 31 | 76,402 | 1,399,104 | 1 |
| `FIX.count.artist.releases.degree` | Beatles(38k) | 16 | 1 | 3 | 64 | 1 |
| `explore.artist` | Beatles(38k) | 172 | 122 | 662,947 | 1,573,128 | 1 |
| `expand.artist.releases.page` | Bowie(15.5k) | 267 | 27 | 72,846 | 9,312 | 50 |
| `expand.artist.labels.page` | Bowie(15.5k) | 649 | 65 | 342,885 | 1,436,456 | 50 |
| `expand.artist.releases.count` | Bowie(15.5k) | 19 | 19 | 31,036 | 613,288 | 1 |
| `FIX.count.artist.releases.degree` | Bowie(15.5k) | 7 | 1 | 3 | 64 | 1 |
| `explore.artist` | Bowie(15.5k) | 77 | 56 | 339,702 | 393,480 | 1 |
| `expand.artist.releases.page` | Beethoven(66k) | 1,406 | 77 | 290,079 | 9,312 | 50 |
| `expand.artist.labels.page` | Beethoven(66k) | 2,073 | 222 | 1,307,620 | 4,608,240 | 50 |
| `expand.artist.releases.count` | Beethoven(66k) | 49 | 48 | 132,101 | 2,458,624 | 1 |
| `FIX.count.artist.releases.degree` | Beethoven(66k) | 5 | 1 | 3 | 64 | 1 |
| `explore.artist` | Beethoven(66k) | 207 | 179 | 1,312,688 | 1,573,128 | 1 |
| `expand.artist.releases.page` | light | 19 | 1 | 1,775 | 9,312 | 50 |
| `expand.artist.labels.page` | light | 15 | 2 | 4,635 | 52,888 | 50 |
| `expand.artist.releases.count` | light | 5 | 0 | 804 | 11,712 | 1 |
| `FIX.count.artist.releases.degree` | light | 6 | 1 | 3 | 64 | 1 |
| `explore.artist` | light | 19 | 3 | 4,868 | 12,552 | 1 |
| `strata.ladder` | Beatles | 431 | 312 | 1,315,083 | 1,573,048 | 36,595 |
| `FIX.strata.ladder.flat` | Beatles | 429 | 331 | 1,315,083 | 12,812,416 | 36,595 |
| `node.artist` | Beatles | 222 | 191 | 1,712,405 | 1,413,616 | 1 |
| `strata.ladder` | Bowie | 218 | 132 | 678,489 | 393,400 | 14,698 |
| `FIX.strata.ladder.flat` | Bowie | 142 | 152 | 678,489 | 5,240,952 | 14,698 |
| `node.artist` | Bowie | 129 | 86 | 899,497 | 628,808 | 1 |
| `strata.ladder` | Beethoven | 1,669 | 616 | 2,827,990 | 1,573,048 | 59,051 |
| `FIX.strata.ladder.flat` | Beethoven | 656 | 674 | 2,827,990 | 21,021,552 | 59,051 |
| `node.artist` | Beethoven | 291 | 260 | 2,603,997 | 2,472,336 | 1 |
| `expand.label.releases.page` | Columbia(351k) | 5,823 | 353 | 1,683,408 | 9,312 | 50 |
| `expand.label.releases.count` | Columbia(351k) | 221 | 192 | 702,371 | 12,593,184 | 1 |
| `expand.label.artists.page` | Columbia(351k) | 14,114 | 1,483 | 6,799,020 | 36,022,752 | 50 |
| `expand.label.artists.count` | Columbia(351k) | 759 | 759 | 5,950,266 | 1,432,464 | 1 |
| `trends.label` | Columbia(351k) | 2,790 | 393 | 1,053,554 | 10,944,672 | 132 |
| `expand.label.releases.page` | light | 43 | 2 | 5,043 | 9,312 | 50 |
| `expand.label.releases.count` | light | 14 | 1 | 2,049 | 41,064 | 1 |
| `expand.label.artists.page` | light | 259 | 4 | 18,968 | 128,880 | 50 |
| `expand.label.artists.count` | light | 17 | 3 | 16,764 | 6,016 | 1 |
| `trends.label` | light | 19 | 3 | 3,071 | 46,352 | 29 |
| `node.label` | NoL(763k) | 1,047 | 445 | 1,525,194 | 26,632,064 | 1 |
| `node.label` | RCA(157k) | 170 | 115 | 314,183 | 5,853,136 | 1 |
| `expand.genre.releases.page` | BrassMil(75k) | 764 | 112 | 368,800 | 9,312 | 50 |
| `expand.genre.releases.count` | BrassMil(75k) | 72 | 56 | 168,585 | 2,837,536 | 1 |
| `expand.genre.artists.page` | BrassMil(75k) | 2,833 | n/a | 1,330,798 | 9,616,104 | 50 |
| `expand.genre.artists.count` | BrassMil(75k) | 189 | n/a | 1,133,176 | 1,199,088 | 1 |
| `trends.genre` | BrassMil(75k) | 116 | 89 | 243,424 | 27,776 | 133 |
| `node.genre` | BrassMil(75k) | 212 | n/a | 1,133,177 | 1,199,592 | 1 |
| `expand.genre.releases.page.before1970` | BrassMil(75k) | 8,638 | n/a | 5,441,915 | 336,227,784 | 50 |
| `FIX.count.genre.precomputed` | Rock | 8 | n/a | 3 | 64 | 1 |
| `credits.person` | Ludwig(59k) | 8,227 | n/a | 4,473,791 | 110,623,704 | 60,758 |
| `credits.profile` | Ludwig | 143 | n/a | 243,045 | 1,360 | 1 |
| `credits.timeline` | Ludwig | 98 | n/a | 223,601 | 65,536 | 167 |
| `credits.leaderboard` | mastering | 27 | n/a | 44 | 432 | 20 |
| `autocomplete.artist` | the* | 360 | 119 | 20 | 1,872 | 10 |
| `FIX.autocomplete.limit` | the* | 23 | 4 | 20 | 1,872 | 10 |
| `autocomplete.artist` | joh* | 71 | 58 | 20 | 1,872 | 10 |
| `FIX.autocomplete.limit` | joh* | 8 | 4 | 20 | 1,872 | 10 |
| `check.year_range` | n/a | 9 | n/a | 6 | 64 | 1 |
| `check.graph_stats` | n/a | 1,182 | n/a | 29,862,722 | 616 | 2 |
| `mb.relationships` | Bowie | 299 | n/a | 31,074 | 64 | 0 |
| `FIX.mb.relationships.typed` | Bowie | 46 | n/a | 28 | 64 | 0 |
| `network.collab.depth1` | Bowie | 966 | n/a | 290,613 | 165,704 | 50 |
| `similar.candidates` | Bowie | 8,115 | n/a | 8,611,112 | 21,311,632 | 200 |
| `path.artist_artist` | Bowie-Beatles | 731 | n/a | 278,283 | 4,080,384 | 1 |
| `check.graph_stats.full` | n/a | 1,068 | 1,004 | 34,907,529 | 696 | 6 |
| `FIX.graph_stats.store` | n/a | 31 | 1 | 6 | 64 | 6 |
| `network.collab.depth2` | light Indecent Noise | 6,053 | n/a | 15,166,855 | 1,276,480 | 50 |
| `node.artist` | Unknown(95k) | 3,628 | n/a | 3,003,826 | 3,207,704 | 1 |
| `network.centrality` | Unknown(95k) | 208 | n/a | 935,305 | 250,256 | 1 |
| `network.centrality` | Bowie | 48 | n/a | 290,528 | 48,248 | 1 |
| `network.cluster` | Bowie | 29,558 | n/a | 37,505,004 | 632,168 | 50 |
| `FIX.network.cluster.limit_first` | Bowie | 218 | n/a | 1,112,658 | 117,248 | 50 |
| `mb.relationships` | Various(1.53M) | 502 | n/a | 3,059,830 | 64 | 0 |
| `FIX.mb.relationships.typed` | Various(1.53M) | 9 | n/a | 26 | 64 | 0 |
| `dna.identity` | Columbia(351k) | 1,830 | n/a | 5,950,267 | 14,026,096 | 1 |
| `dna.genre` | Columbia(351k) | 946 | n/a | 7,049,660 | 19,808,816 | 15 |
| `dna.style` | Columbia(351k) | 1,024 | n/a | 7,018,208 | 18,895,488 | 646 |
| `dna.decade` | Columbia(351k) | 4,871 | n/a | 1,053,554 | 11,655,416 | 14 |
| `dna.years` | Columbia(351k) | 418 | n/a | 1,053,554 | 23,384 | 132 |
| `dna.formats` | Columbia(351k) | 20,487 | n/a | 1,053,554 | 13,315,512 | 39 |
| `dna.families` | Columbia(351k) | 1,190 | n/a | 8,028,995 | 37,223,872 | 7 |
| `dna.mediums` | Columbia(351k) | 1,249 | n/a | 8,735,186 | 39,508,168 | 34 |
| `FIX.dna.one_pass` | Columbia(351k) | 3,778 | n/a | 19,629,426 | 113,328,128 | 1 |
| `rarity.by_label.ids` | NoL(763k) | 15,672 | n/a | 2,287,786 | 64 | 762,594 |
| `label.similar.candidates` | Columbia | 28,275 | n/a | 54,744,661 | 80,554,808 | 100 |
| `credits.connections.d1` | Ludwig | 3,140 | n/a | 4,714,635 | 57,430,144 | 50 |
| `similar.candidates` | Beatles | 8,065 | n/a | 8,989,076 | 26,066,984 | 200 |
| `expand.genre.releases.count` | Rock | 3,520 | n/a | 14,343,985 | 286,177,464 | 1 |
| `expand.genre.releases.page` | Rock | 19,540 | n/a | 32,830,989 | 9,312 | 50 |
| `trends.genre` | Rock | 3,887 | n/a | 20,715,337 | 17,328 | 82 |
| `node.genre` | Rock | 39,817 | n/a | 112,161,063 | 39,546,656 | 1 |
| `genre_tree.phase1` | all genres | 12,092 | n/a | 59,134,448 | 1,045,920,240 | 15 |
| `enrichment.rel_scan.proxy` | CREDITED_ON only | 45,346 | n/a | 143,624,457 | 88 | 1 |
| `path.artist_artist` | light-light | 947 | n/a | 90,280 | 5,769,968 | 1 |
| `fanout.neighbours` | Bowie | 2,922 | n/a | 291,803 | 33,872 | 1 |
| `fanout.neighbours` | Beatles | 4,254 | n/a | 545,835 | 40,184 | 1 |
| `fanout.neighbours` | Beethoven | 7,234 | n/a | 1,389,480 | 1,658,792 | 1 |
| `fanout.hub_edges` | Bowie via 194/355 | 76 | n/a | 290,517 | 704 | 2 |
| `FIX.expand.before_year.barrier` | BrassMil | 2,392 | 92 | 261,614 | 8,904 | 50 |
| `FIX.expand.before_year.barrier` | Rock | 22,113 | n/a | 21,074,379 | 8,904 | 50 |
| `expand.genre.releases.page.before1970` | Rock | 2,513 | n/a | 5,336,097 | 334,822,096 | 50 |
| `FIX.expand.artist.page.defer_title` | Beatles | 336 | 59 | 176,548 | 12,576 | 50 |
| `FIX.node.artist.split` | Beatles | 1,855 | 220 | 1,518,592 | 1,399,168 | 1 |
| `node.artist` | Various(1.53M) | 45,524 | n/a | 75,476,887 | 49,399,160 | 1 |
| `network.centrality` | Various(1.53M) | 3,813 | n/a | 18,693,961 | 772,096 | 1 |
| `dna.identity` | NoL(763k) | 5,378 | n/a | 9,365,529 | 46,738,008 | 1 |
| `strata.shared_personnel` | Bowie top40 masters | 1,644 | 80 | 358,550 | 1,570,752 | 200 |
| `strata.labels.approx` | Bowie | 51 | n/a | 716 | 38,224 | 1 |
| `FIX.expand.before_year.index_order` | BrassMil | 900 | 356 | 1,980,591 | 21,339,560 | 50 |
| `FIX.expand.before_year.index_order` | Rock | 785 | 385 | 2,023,470 | 22,762,536 | 50 |
| `FIX.count.artist.releases.label` | Beatles | 34 | 23 | 76,402 | 88 | 1 |
| `recommend.traversal.artist.h2` | Bowie | 902 | n/a | 542,507 | 84,481,416 | 100 |
| `recommend.traversal.genre.h2` | BrassMil(75k) | 7,431 | n/a | 2,868,139 | 463,060,528 | 100 |
| `genre_tree.phase2.one_genre` | Rock | 16,087 | n/a | 131,264,491 | 443,217,712 | 1 |
| `credits.connections.d2` | Ludwig | 8,453 | n/a | 23,710,666 | 57,430,192 | 50 |
| `FIX.network.collab.depth2` | Indecent Noise | 123 | 15 | 70,526 | 26,270,256 | 50 |
| `FIX.network.collab.depth2` | Bowie | 893 | 558 | 3,801,732 | 327,213,568 | 50 |
| `FIX.network.collab.depth2` | Beatles | 576 | 514 | 3,111,977 | 158,286,888 | 50 |
| `FIX.traversal.capped` | Rock | 58 | 4 | 13,581 | 126,152 | 100 |
| `FIX.traversal.capped` | BrassMil | 37 | 4 | 12,590 | 126,888 | 100 |
| `FIX.genre_tree.phase1.precomputed` | all | 9 | n/a | 46 | 2,584 | 15 |
| `FIX.genre_tree.phase2.nodistinct` | Brass & Military | 217 | n/a | 1,331,363 | 91,040 | 1 |
| `genre_tree.phase2.one_genre` | Brass & Military | 255 | n/a | 1,331,363 | 4,817,648 | 1 |

## Appendix C: local synthetic measurements

Each value is from three runs on the 1/50 graph described above. Heavy nodes were scaled to it, except
Bowie, who keeps a degree near 8,800. Use these for ratios and operator shapes only.

| Statement | Node class | db hits | Peak memory bytes | Median ms | Note |
| --- | --- | ---: | ---: | ---: | --- |
| `expand.artist.releases.page` | heavy | 43,135 | 9,312 | 42 |  |
| `expand.artist.releases.page` | light | 89 | 2,800 | 6 |  |
| `expand.artist.releases.count` | heavy | 17,509 | 340,440 | 19 |  |
| `expand.artist.releases.count` | light | 33 | 872 | 4 |  |
| `FIX.count.artist.releases.degree` | heavy | 3 | 64 | 3 |  |
| `FIX.count.artist.releases.degree` | light | 3 | 64 | 3 |  |
| `expand.artist.labels.page` | heavy | 138,701 | 2,440,608 | 93 |  |
| `expand.artist.labels.page` | light | 209 | 12,064 | 5 |  |
| `expand.genre.releases.page` | heavy | 639,748 | 9,312 | 137 |  |
| `expand.genre.releases.page` | light | 3,769 | 9,312 | 8 |  |
| `expand.genre.releases.count` | heavy | 260,068 | 5,203,288 | 55 |  |
| `expand.genre.releases.count` | light | 1,514 | 26,528 | 27 |  |
| `expand.genre.releases.page.before1970` | heavy | 100,807 | 6,225,248 | 75 |  |
| `expand.genre.releases.page.before1970` | light | 435,716 | 11,517,352 | 390 |  |
| `expand.genre.artists.page` | heavy | 2,053,036 | 9,423,000 | 767 |  |
| `expand.genre.artists.page` | light | 11,790 | 173,264 | 34 |  |
| `expand.genre.artists.count` | heavy | 1,773,026 | 1,202,544 | 389 |  |
| `expand.genre.artists.count` | light | 10,142 | 20,648 | 10 |  |
| `FIX.count.genre.precomputed` | heavy | 3 | 64 | 3 |  |
| `FIX.count.genre.precomputed` | light | 3 | 64 | 4 |  |
| `expand.genre.labels.page` | heavy | 2,062,657 | 16,402,360 | 845 |  |
| `expand.genre.labels.page` | light | 11,815 | 375,680 | 20 |  |
| `expand.label.releases.page` | heavy | 245,145 | 9,312 | 68 |  |
| `expand.label.releases.page` | light | 194 | 5,944 | 5 |  |
| `expand.label.releases.count` | heavy | 99,626 | 2,236,864 | 74 |  |
| `expand.label.releases.count` | light | 67 | 1,528 | 3 |  |
| `expand.label.artists.page` | heavy | 784,047 | 10,810,832 | 327 |  |
| `expand.label.artists.page` | light | 485 | 29,688 | 10 |  |
| `expand.label.artists.count` | heavy | 677,265 | 660,664 | 203 |  |
| `expand.label.artists.count` | light | 415 | 1,600 | 3 |  |
| `strata.ladder` | heavy | 267,198 | 393,400 | 297 |  |
| `strata.ladder` | light | 401 | 992 | 7 |  |
| `strata.ladder.58k` | heavy | 361,877 | 393,400 | 596 |  |
| `strata.ladder.58k` | light | 9,718 | 12,472 | 14 |  |
| `FIX.strata.ladder.flat` | heavy | 267,198 | 3,098,112 | 191 |  |
| `FIX.strata.ladder.flat` | light | 401 | 5,288 | 4 |  |
| `strata.shared_personnel` | heavy | 233,339 | 16,826,184 | 415 |  |
| `strata.shared_personnel` | light | 20,511 | 2,298,512 | 60 |  |
| `explore.artist` | heavy | 140,523 | 393,480 | 99 |  |
| `explore.artist` | light | 266 | 2,800 | 8 |  |
| `node.artist` | heavy | 298,624 | 400,168 | 143 |  |
| `node.artist` | light | 455 | 4,992 | 3 |  |
| `node.genre` | heavy | 1,773,027 | 1,203,048 | 404 |  |
| `node.genre` | light | 10,143 | 21,152 | 16 |  |
| `node.label` | heavy | 99,628 | 2,237,368 | 35 |  |
| `node.label` | light | 69 | 2,032 | 2 |  |
| `trends.genre` | heavy | 390,100 | 21,744 | 80 |  |
| `trends.genre` | light | 2,269 | 16,776 | 10 |  |
| `trends.label` | heavy | 145,473 | 1,882,520 | 67 |  |
| `trends.label` | light | 98 | 11,408 | 8 |  |
| `genre_tree` | heavy | 8,450,529 | 34,815,976 | 2,367 |  |
| `path.artist_artist` | heavy | 10,120 | 854,016 | 27 |  |
| `path.artist_artist` | light | 3 | 64 | 2 |  |
| `enrichment.rel_scan` | heavy | 8,055,036 | 88 | 1,231 |  |
| `enrichment.artist_mbid` | heavy | 1 | 88 | 6 |  |
| `mb.relationships` | heavy | 71,405 | 64 | 106 |  |
| `mb.relationships` | light | 56 | 64 | 4 |  |
| `mb.relationships.bowie` | heavy | 22,978 | 64 | 22 |  |
| `FIX.mb.relationships.typed` | heavy | 12,630 | 64 | 18 |  |
| `FIX.mb.relationships.typed` | light | 58 | 64 | 4 |  |
| `autocomplete.artist` | heavy | 20 | 1,872 | 51 |  |
| `autocomplete.artist` | light |  | 112 | 7 | no matches |
| `FIX.autocomplete.limit` | heavy | 20 | 1,872 | 20 |  |
| `FIX.autocomplete.limit` | light |  | 112 | 10 | no matches |
| `network.collab.depth2` | heavy | 151,270,088 | 4,949,656 | 48,924 |  |
| `network.collab.depth2` | light | 738,372 | 5,523,472 | 432 |  |
| `network.collab.depth1` | heavy | 111,210 | 764,224 | 106 |  |
| `network.collab.depth1` | light | 170 | 2,288 | 6 |  |
| `network.centrality` | heavy | 340,938 | 151,376 | 136 |  |
| `network.centrality` | light | 203 | 1,912 | 3 |  |
| `network.cluster` | heavy | 1,822,034 | 3,448,040 | 820 |  |
| `network.cluster` | light | 1,973 | 7,024 | 7 |  |
| `FIX.network.cluster.limit_first` | heavy | 237,097 | 424,808 | 201 |  |
| `FIX.network.cluster.limit_first` | light | 1,973 | 2,512 | 10 |  |
| `analytics.centrality.size` | heavy | 400,201 | 11,848 | 89 |  |
| `FIX.analytics.centrality.count` | heavy | 400,201 | 11,848 | 104 |  |
| `dna.identity` | heavy | 677,266 | 2,897,984 | 430 |  |
| `dna.identity` | light | 14,291 | 80,904 | 70 |  |
| `dna.genre` | heavy | 817,283 | 2,430,904 | 469 |  |
| `dna.genre` | light | 17,250 | 56,320 | 26 |  |
| `dna.style` | heavy | 828,681 | 3,155,544 | 381 |  |
| `dna.style` | light | 17,530 | 270,904 | 17 |  |
| `dna.decade` | heavy | 145,473 | 1,745,112 | 89 |  |
| `dna.decade` | light | 3,094 | 42,456 | 12 |  |
| `dna.years` | heavy | 145,473 | 18,824 | 59 |  |
| `dna.years` | light | 3,094 | 14,304 | 4 |  |
| `dna.formats` | heavy | 149,437 | 2,160,072 | 227 |  |
| `dna.formats` | light | 3,184 | 46,368 | 7 |  |
| `dna.families` | heavy | 973,825 | 5,988,712 | 567 |  |
| `dna.families` | light | 20,611 | 120,088 | 22 |  |
| `dna.mediums` | heavy | 1,073,447 | 6,139,256 | 423 |  |
| `dna.mediums` | light | 22,731 | 146,784 | 14 |  |
| `FIX.dna.one_pass` | heavy | 2,271,104 | 32,123,960 | 1,107 |  |
| `FIX.dna.one_pass` | light | 47,963 | 748,072 | 43 |  |
| `label.similar.candidates` | heavy | 1,313,052 | 4,944,856 | 336 |  |
| `label.similar.candidates` | light | 1,139,417 | 5,631,984 | 316 |  |
| `similar.candidates` | heavy | 4,909,212 | 11,591,680 | 1,987 |  |
| `similar.candidates` | light | 4,625,120 | 11,780,352 | 1,304 |  |
| `recommend.traversal.artist.h2` | heavy | 263,160 | 37,592,936 | 200 |  |
| `recommend.traversal.artist.h2` | light | 489 | 102,576 | 4 |  |
| `recommend.traversal.artist.h3` | heavy |  |  |  | exceeded 100 s client limit |
| `recommend.traversal.artist.h3` | light | 3,780,426 | 5,222,288 | 397 |  |
| `recommend.traversal.genre.h2` | heavy | 3,730,265 | 575,458,032 | 1,934 |  |
| `recommend.traversal.genre.h2` | light | 21,043 | 2,588,656 | 13 |  |
| `credits.person` | heavy | 259,742 | 11,097,808 | 118 |  |
| `credits.person` | light | 448 | 18,160 | 4 |  |
| `credits.profile` | heavy | 23,867 | 1,424 | 25 |  |
| `credits.profile` | light | 50 | 1,184 | 3 |  |
| `credits.timeline` | heavy | 23,864 | 222,432 | 20 |  |
| `credits.timeline` | light | 39 | 4,760 | 4 |  |
| `credits.connections.d1` | heavy | 537,711 | 13,011,632 | 413 |  |
| `credits.connections.d1` | light | 959 | 102,968 | 22 |  |
| `credits.connections.d2` | heavy | 3,726,634 | 97,126,016 | 3,321 |  |
| `credits.connections.d2` | light | 1,679,671 | 151,964,480 | 1,544 |  |
| `credits.leaderboard` | heavy | 44 | 1,176 | 6 |  |
| `gaps.label.page` | heavy | 2,731,113 | 65,904,088 | 891 |  |
| `gaps.label.page` | light | 1,754 | 52,536 | 23 |  |
| `gaps.label.count` | heavy | 670,830 | 1,048 | 307 |  |
| `gaps.label.count` | light | 414 | 1,048 | 4 |  |
| `gaps.label.summary` | heavy | 774,244 | 12,890,128 | 194 |  |
| `gaps.label.summary` | light | 479 | 10,688 | 4 |  |
| `FIX.gaps.label.page.defer` | heavy | 771,811 | 81,168 | 460 |  |
| `FIX.gaps.label.page.defer` | light | 1,818 | 52,824 | 5 |  |
| `user.collection.page` | heavy | 731,293 | 20,772,624 | 236 |  |
| `user.collection.page` | light | 9,672 | 287,944 | 7 |  |
| `FIX.user.collection.page.defer` | heavy | 43,468 | 16,336 | 43 |  |
| `FIX.user.collection.page.defer` | light | 4,015 | 16,336 | 12 |  |
| `rarity.by_label.ids` | heavy | 149,437 | 64 | 192 |  |
| `rarity.by_label.ids` | light | 99 | 64 | 4 |  |
| `fit.release_context` | heavy | 5,397 | 184,304 | 14 |  |
| `fit.release_context` | light | 8 | 1,288 | 3 |  |
| `check.year_range` | heavy | 6 | 64 | 4 |  |
| `check.graph_stats` | heavy | 592,002 | 616 | 76 |  |
