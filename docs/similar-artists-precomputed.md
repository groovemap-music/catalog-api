# Precomputed similar artists

`GET /api/recommend/similar/artist/{artist_id}` can serve each artist's monthly exact top-K
neighbours instead of the graph heuristic (gm-catalog-api-2zsq.1; ADR 0013, decision "D" of
gm-analytics-engine-8ts). The claim is limited to what the lists measure: artists connected in
the catalog graph's embedding, not user-perceived similarity or discovery.

## Switch and fallbacks

| Variable | Default | Meaning |
| --- | --- | --- |
| `SIMILAR_ARTISTS_PRECOMPUTED_ENABLED` | `false` | Serve from the lists. Off means the heuristic path, exactly as before |
| `SIMILAR_ARTISTS_FUSION_ALPHA` | `0.8` | Weight on the embedding cosine; `1.0` is the pure list |

With the switch on, one query reads the `is_current` row of `artist_embedding_releases` and the
artist's `(release_id, artist_id)` primary-key row of `artist_similar_artists`. The heuristic
result is served instead, never an error, when:

- the switch is off;
- no release is current;
- the artist is not in the current release;
- the tables do not exist or PostgreSQL cannot be read (logged as a warning);
- fusion is on and the stored vectors cannot be read (`artist_embeddings` needs pgvector).

The 404 (unknown artist) and 422 (fewer than 3 releases) answers are unchanged.

## Ranking

`fused = alpha * cosine + (1 - alpha) * heuristic`, the harness formula of gm-design-chw.2 and
8ts, so alpha weights the embedding. Candidates are the precomputed top-K together with the
heuristic candidates.

- Precomputed neighbours use the batch's stored cosine; the ones the heuristic did not surface
  are profiled in the graph and scored by the same heuristic.
- Heuristic-only candidates get their cosine from the two stored `artist_embeddings` vectors
  (`model_version` of the current release; a primary-key point lookup, parsed from pgvector's text
  form). A candidate without a stored vector scores cosine 0.0, as in the harness.
- Ties break by artist id. `breakdown` keeps the four heuristic dimensions and adds `embedding`.
- `alpha = 1.0` skips the heuristic entirely: batch order, batch cosine, names from one graph read.

Responses are cached in Valkey under a key naming the `model_version` and alpha
(`recommend:similar:artist:{id}:precomputed:{model_version}:{alpha}`), so a monthly publish or a
config change never serves the other path's body; the heuristic keeps its own key. A rejected
cache write is counted and swallowed by `RecommendCache`, as for the heuristic. Impressions are
recorded under the policy `similar_artist_precomputed_fused_v1` when a list served the request.

## Offline evaluation

`api.evaluation.precomputed` replays the path as a candidate, scored on the same split and metric
against the latest heuristic baseline (`similar-artist-all-signals-2026-09`) and the frozen one.
Snapshot: `tests/fixtures/golden/expected-precomputed-similar-artist-metrics.json`.

The embeddings are **synthetic**: a seeded random projection of each artist's observed
genre/style/label/collaborator profile, with exact top-50 lists over it. They are built from the
heuristic's own signals, so this verifies the wiring and fixes a comparison shape; it does not
measure how real FastRP vectors rank. Recall on the golden set (12 collectors):

| Method | recall@10 | recall@25 |
| --- | --- | --- |
| `heuristics-2026-09` (legacy) | 0.4451 | 0.5201 |
| `similar-artist-all-signals-2026-09` (latest heuristic) | 0.5361 | 0.6250 |
| precomputed, pure (alpha 1.0) | 0.4196 | 0.6811 |
| precomputed, fused (alpha 0.8) | 0.4562 | 0.6950 |

On this fixture neither precomputed variant beats the latest heuristic at k=10; both do at k=25.
That is not evidence about production quality in either direction.

## Latency

Measured on the integration tier (`scripts/test-integration.sh`) against
`postgres:18-alpine@sha256:d3e1620b530c944afa6e887d22eb899824da68e19c52024bf98f5220c88a65b2`
(PostgreSQL 18.6, no pgvector), on a developer laptop, 100,000 artists x K=50, 2,000 sampled
reads after warm-up, via `tests/test_similar_precomputed_integration.py`:

| What is timed | p50 | p95 |
| --- | --- | --- |
| Release + list read (`get_current_list`), a primary-key lookup | 1.26 ms | 1.92 ms |
| Pure path (`alpha = 1.0`): that read plus one Neo4j lookup of the 50 names | 6.15 ms | 7.74 ms |

The second row excludes HTTP, auth, rate limiting, the identity check, and the alias lookup, so it
is not the full-endpoint p95. The fused path additionally runs the heuristic's graph queries and
is dominated by them; it was not given a latency figure here.

**This is not a PostgreSQL 19 measurement.** The integration tier pins PostgreSQL 18, and no
official PostgreSQL 19 GA image exists yet. The bead's p95 criterion is therefore open: re-measure
on PG19 GA by pointing `POSTGRES_INTEGRATION_IMAGE` at it and rerunning `just test-integration`.

The pinned `groovemap-database-schema` revision still declares the superseded
one-row-per-neighbour tables (gm-database-schema-2xe0), not the compact per-artist contract
(gm-database-schema-hqjp) this reads. The integration test installs the compact DDL itself and
restores the pinned tables afterwards; bumping the pin retires that workaround. Because the image
has no pgvector, the vector lookup used by fusion is covered by unit tests only.
