# Prototype baselines and serving proposal

This is the technical index for eight bounded GrooveMap prototypes. It records the measured
offline outputs and the next engineering disposition. **A GO here never approves serving,
release, or deployment.** The prototypes use synthetic or caller-supplied inputs; real-user
usefulness remains pending unless stated otherwise.

The placement and existing UI homes follow [ADR 0019](https://github.com/groovemap-music/design/blob/6303d58e89be71b651ea55d4d9c9588641b82475/docs/adr/0019-metadata-and-collection-prototype-boundaries.md),
accepted for bounded offline prototypes. The API production baseline is still tag `v0.11.1`
at `c04b540b8c005612505be99f3b614e0f90e04d3a`; these prototype commits are assembled in the
`gm-catalog-api-ifqv` epic and are not a released API change.

## Disposition

| ID | Prototype | Engineering disposition | Serving proposal |
| --- | --- | --- | --- |
| [GM01](#gm01--separate-evidence-views) | Authority-separated evidence views | ITERATE | Keep catalog, owner-copy, and admin-history paths distinct. A future surface needs route-level auth and source-completeness proofs. |
| [GM05](#gm05--ask-baseline-evaluator) | Ask baseline evaluator | GO for operator-private evaluation only | Do not change Ask behavior based on this synthetic replay. Obtain an explicitly supplied checked corpus/model run first. |
| [GM06](#gm06--missing-scope-preview) | Missing-scope preview | GO to controlled integration design after completeness and ownership proofs | Candidate for the existing Missing pane; the preview remains transient. |
| [GM07](#gm07--discover-replay) | Catalog-context discovery replay | ITERATE | No ranking-path change until consented historical data or checked relevance labels support it. |
| [GM08](#gm08--crate-comparison) | Bounded crate comparison | ITERATE | No new recommendation feed; evaluate real caller-defined objectives and collection authorization first. |
| [GM10](#gm10--copy-observation-selection) | Owner-scoped copy selection | GO to controlled integration design after owner-read and contract proofs | Candidate for explicit Collection copy detail and Strata copy selection. Existing observation semantics remain unchanged. |
| [GM12](#gm12--run-out-interpreter) | Run-out interpretation | ITERATE | Release-detail presentation needs a reviewed manifest and exact source-span evidence. |
| [GM13](#gm13--selected-release-comparison) | Selected-release comparison | ITERATE | Restrict any design to releases the user already selected; production matching remains deferred. |

“GO to design” means an owner can prepare a separately reviewed integration design. It does not
authorize a UI/API contract, a persistence change, production rollout, or release. No prototype
is PARKed: each has a bounded technical use, while most lack the real evidence needed to justify
serving it.

## Shared evidence limits

- Every result below is synthetic, a hand-checked example, or a pure projection of supplied
  records. None establishes real-user quality or a 10%/15% usefulness gain.
- A missing private or real-user corpus/label set is **PENDING**, never a negative label or a
  zero-quality observation. Synthetic positives are not user satisfaction.
- Except for GM05's canned harness accounting, no prototype reports dollar cost or production
  latency. Local deterministic work is not a production resource benchmark.
- The individual `just check` counts are from different commits and test trees; they are CI
  evidence, not product metrics. The assembled checks preserve the repository's 98.46% coverage
  gate. The most recent per-slice checks are cited below.
- Graph Explorer owns Ask, Missing, Discover, Will it fit?, and its Search/release-detail
  surfaces. Operations Console owns OpsLedger. The surfaces listed below are current homes from
  ADR 0019, not approval to expose these prototypes.

## GM01 — Separate evidence views

**Owner / home:** `catalog-api` owns the projections. ADR 0019 places public release facts in
Graph Explorer's release drawer, private observations in Collection copy detail, and identity
history in authorized Operations Console OpsLedger.

**Implementation:** [authority-separated readers](../evidence-readers.md),
[`api/evidence_readers.py`](../../api/evidence_readers.py),
[`tests/test_evidence_readers.py`](../../tests/test_evidence_readers.py). Bead
`gm-catalog-api-13b0`, commit `9bd1568cc7e62ff8a7015ce33574a0feffde074f`.

**Reproduce:** `uv run pytest tests/test_evidence_readers.py -q` (14 tests; module coverage
100%). Its full `just check` passed 4,186 tests, 382 deselected, at 98.52% total coverage.

The synthetic mixed-record cases kept public, owner-A, owner-B, and admin projections in their
own authority paths; blank owner context failed closed, and non-admin payloads were rejected by
the prototype helper. The catalog query does not return underlying provider provenance, fact
timestamps, or source-text spans; the projection leaves those unknown. Native `subject_ref` is
caller-supplied context and is not resolved here. This helper is not an auth boundary: a real
reader must use the existing authenticated and owner-scoped reads, and the admin view must use
`require_admin`.

No separately versioned fixture file, model, or live endpoint evaluation exists. Output is
capped at 50 entries by default and 100 maximum; callers must also bound upstream reads. Real
source completeness and route-level privacy evidence remain pending.

## GM05 — Ask baseline evaluator

**Owner / home:** `catalog-api` evaluates the existing NLQ engine; Graph Explorer owns Ask.
Implementation: [`docs/nlq-baseline-evaluator.md`](../nlq-baseline-evaluator.md),
[`api/nlq_baseline_eval.py`](../../api/nlq_baseline_eval.py),
[`api/nlq_baseline_fixtures.json`](../../api/nlq_baseline_fixtures.json). Bead
`gm-catalog-api-ep74`, commit `07ff736070781a4052f460d50ea06f76f5bc18b9`.

**Reproduce:** `uv run python -m api.nlq_baseline_eval`. Fixture SHA-256:
`a2cb15d8f3e629a5d85bc7b6cf32788c5b2e3e5950b2046048a9939a4dc46a64`.
Versions: policy `synthetic-baseline-v1`, model adapter `canned-response-v1`, snapshot
`synthetic-facts-v1`; 8 frozen questions.

Synthetic harness scores: correctness 8/8, citation support 8/8, abstention precision/recall
1.0/1.0, one blocked cross-user request, zero annotated privacy failures, and reported cost
$0. The $0 is because no model was called; it is not a Claude cost estimate. Canned Claude-shaped
turns pass through the existing `NLQEngine`, while caller-filtered tool responses are synthetic.
The exact fact-reference/text scan misses paraphrases and indirect leaks, so zero synthetic
failures are not a privacy assurance. No real model/corpus run is supplied. Explicit real
evaluation input is opt-in and must carry its own model/version, labels, fact IDs, and cost.

Full check for this slice: 4,090 passed, 382 deselected, 98.50% coverage. **GO is limited to
keeping this operator-private evaluation harness.** Ask runtime behavior remains unchanged.

## GM06 — Missing-scope preview

**Owner / home:** `catalog-api` owns the pure scope/counting core; Graph Explorer's existing
Missing pane is the proposed transient home, with Collection as a later entry point.
Implementation: [`docs/pursuit-scope-preview.md`](../pursuit-scope-preview.md),
[`api/pursuit_scope.py`](../../api/pursuit_scope.py),
[`tests/test_pursuit_scope.py`](../../tests/test_pursuit_scope.py). Bead `gm-catalog-api-jqji`,
commit `a2746b31901f7943ca77d30c064e38a27012f88e`.

**Reproduce:** `uv run pytest tests/test_pursuit_scope.py -q`. The two synthetic examples show:
(1) one eligible unit with unknown ownership stays in review (`denominator=1`, owned 0, missing
0, ownership-review 1, completion fraction null); and (2) two provider aliases mapped to one
native edition count once (`denominator=1`, owned 1). These are hand-checked examples, not a
quality sample. Snapshot IDs are caller supplied; no versioned checked-in demo dataset or
real-source SHA was recorded.

The core does not read or authorize a collection, resolve identity, persist a pursuit, or schedule
anything. Denominator completeness depends on the caller's catalog snapshot and provider-to-native
mapping. No user outcome, cost, or latency metric is available. Full check: 4,126 passed, 382
deselected, 98.47% coverage; focused tests 26 and module coverage 100%.

**GO to controlled integration design only after** catalog completeness, authenticated collection
ownership, and producer/consumer contract proofs. Saved pursuit state requires a separate consent,
retention, and erasure design.

## GM07 — Discover replay

**Owner / home:** `catalog-api` extends its existing evaluator; Graph Explorer owns Discover.
Implementation: [`docs/evaluation.md`](../evaluation.md),
[`api/evaluation/discovery.py`](../../api/evaluation/discovery.py),
[`tests/test_evaluation_discovery.py`](../../tests/test_evaluation_discovery.py). Bead
`gm-catalog-api-20ei`, commit `6ad8097167d903bdf1e8285b1b696e1e61ef0599`.

**Reproduce:** `uv run python -m api.evaluation.discovery`; control:
`uv run python -m api.evaluation.discovery --familiarity-penalty 0.0`. Policies are
`heuristics-2026-09`, `metadata-nearest-neighbor-v1`, and
`catalog-context-discovery-v1`. The synthetic GoldenSet is `golden-2026-09`, split at
`2023-01-01`; the candidate policy's default familiarity control is 0.35. Fixture SHA-256:
`tests/fixtures/golden/catalog.json` `984ebc0d6ebc2eb9106905e0e036e03efeef5994aeb09a5e7f438f685f1c5304`;
`collectors.json` `4a4698b03ed1dc90de4643da61ceef1d81e7eb7a7b42109e33d16f62d4f4588f`;
`expected-metrics.json` `5571276e500cc0c84a9d8e1d7c011a5133dd0f5ff6f9a3aabd5c6c96e04ac969`.

On synthetic held-out acquisitions, default recall@10 was 0.288922 for catalog-context,
0.284425 for metadata-nearest-neighbor, and 0.187897 for current ranking. Ranked candidate
coverage@10 was 0.550, 0.517, and 0.483 respectively. With the familiarity control at 0.0,
catalog-context recall@10 was 0.239980. These are positive-only synthetic proxies, not checked
relevance labels; missing feedback remains unknown. The default pool uses the frozen baseline's
scored candidates, cutoff 25; all policies see the same pool. No embeddings, network calls,
live impression writes, production latency, or dollar cost were measured.

Full check: 4,114 passed, 382 deselected, 98.48%; 14 focused tests and 100% module coverage.
**ITERATE** until the same frozen candidate pool can be evaluated with consented historical input
or explicit checked labels. No ranking path change or real 10% gain is established.

## GM08 — Crate comparison

**Owner / home:** `catalog-api` reuses Fit and the scope core; Graph Explorer owns Will it fit?.
Implementation: [`docs/crate-coverage.md`](../crate-coverage.md),
[`api/crate_coverage.py`](../../api/crate_coverage.py),
[`api/evaluation/crate_coverage.py`](../../api/evaluation/crate_coverage.py),
[`tests/test_crate_coverage.py`](../../tests/test_crate_coverage.py). Bead `gm-catalog-api-7ox1`,
commit `07134cc9ce1beb70a754d73ca07f7ccfbad6ce5d`.

**Reproduce:** `uv run python -m api.evaluation.crate_coverage`; constrained example:
`uv run python -m api.evaluation.crate_coverage --candidate-limit 2 --selection-limit 1`.
Versions: `bounded-joint-coverage-v1`, `cratefit_v0`; synthetic catalog
`synthetic-crate-catalog-v1`, collection `synthetic-collection-v1`, objective
`synthetic-declared-artist-label-credit-v1` (5 hand-declared units).

With two selections, greedy coverage chose A,C and covered 5/5 declared units; Fit and popularity
chose A,B and covered 3/5. The 4-unit native scope moved from 1 owned/3 missing to 3 owned/1
missing in every hypothetical strategy. This is a deliberately hand-built complementarity case;
credit attribution is declared by the fixture, not independently verified. Incomplete ownership
withholds the candidate pool. No real checked labels or usefulness lift exists. Caps: 200
candidates, 5,000 catalog rows, 2,000 declared units, 20 selections (default 5). Full check:
4,168 passed, 382 deselected, 98.51%; 28 focused crate tests, 117 focused with scope-fit, and
100% module coverage.

**ITERATE** until caller-defined objectives and ownership evidence are checked on real inputs.
This remains a comparison over selected candidates, not a recommendation feed or buying action.

## GM10 — Copy observation selection

**Owner / home:** `catalog-api` owns authenticated observation reads/writes and copy selection;
Graph Explorer owns Collection copy detail and explicit Strata copy selection.
Implementation: existing [`api/routers/observations.py`](../../api/routers/observations.py),
[`docs/identity-and-activity.md`](../identity-and-activity.md),
[`tests/test_observations.py`](../../tests/test_observations.py). Bead `gm-catalog-api-s5o6`,
commit `1802366e1693bf1bd5792a1dc6066741978879e4`.

**Reproduce:** `uv run pytest tests/test_observations.py -q`. The reader accepts a native item
ID, uses the trusted server principal's owner ID, and returns separate physical copies. Its
limit is 1–100 and it fetches at most limit+1 rows to set `truncated`. Observation GET/POST reuse
existing copy-scoped routes; no new route or schema was added. There is no versioned synthetic
quality fixture, correctness/usefulness metric, or measured cost/latency. The per-copy timeline
GET is existing behavior and this change does not add a timeline length cap or live-store
integration test. Evidence record: 32 focused tests and 100% module coverage; the recorded full
check passed with at least 98.46% total coverage, but its exact test count is unavailable.

**GO to controlled integration design only after** proving the UI uses authenticated owner context,
the selected copy is explicit, and the producer/consumer contract covers the existing read shape.
Editing, retracting, correcting, or superseding an observation remains separately gated.

## GM12 — Run-out interpreter

**Owner / home:** `catalog-api` owns the pure parser/evaluator; Graph Explorer owns release detail.
Implementation: [`docs/runout-interpreter.md`](../runout-interpreter.md),
[`api/runout/interpreter.py`](../../api/runout/interpreter.py),
[`api/runout/evaluation.py`](../../api/runout/evaluation.py),
[`scripts/evaluate_runout.py`](../../scripts/evaluate_runout.py). Bead `gm-catalog-api-4t51`,
commit `61e2c46ddb91439ef15476b72d23f465dacd3e55`.

**Reproduce:** `uv run python scripts/evaluate_runout.py`. Synthetic fixture SHAs:
`tests/fixtures/runout/synthetic-manifest.json`
`dd82b000d6fc38f429968c92c85e2e420d9f9baf0d1c0339f1ff5c704f44d196`;
`synthetic-signatures.json`
`289a6d7d3983aa08e01f5418c0e496d72ddb7d5b92c8c79fd245ac31c8d7ca5d`. Eight synthetic cases:
the interpreter accepted 3 cases at precision 1.0 and coverage 3/8; exact whole-inscription
lookup accepted 2 at precision 1.0 and coverage 2/8. Unknown and ambiguous spans remain visible.
No reviewed evidence manifest was supplied; the review quality indicator remains missing. A
100-case reviewer attestation is only reported as attested, not independently verified. Full
check: 4,089 passed, 382 deselected, 98.49%; cost and runtime are not benchmarked.

**ITERATE** pending at least 100 reviewed examples and release-detail source traces. The parser
does not identify a plant/engineer, normalize an alias, or write identity.

## GM13 — Selected-release comparison

**Owner / home:** `catalog-api` owns the comparison core/possible future API; Graph Explorer owns
Search, release detail, and Fit picker. Implementation:
[`docs/pressing-candidate-comparison.md`](../pressing-candidate-comparison.md),
[`api/pressing_comparison.py`](../../api/pressing_comparison.py),
[`scripts/compare_known_candidates.py`](../../scripts/compare_known_candidates.py),
[`tests/test_pressing_comparison.py`](../../tests/test_pressing_comparison.py). Bead
`gm-catalog-api-h08c`, commit `8172006d07525a7d9cf76546dd3f732b38325939`.

**Reproduce:** `uv run python scripts/compare_known_candidates.py`.
The inline fixture is synthetic and has no separate version/SHA. It compares two already-known
candidates: both remain unresolved, `year` is the next field distinguishing the pair, and the
supplied run-out signature is supported for one candidate while one candidate check is
compatible and one contradictory. This is one pair, not real edition evidence. Bounds: 2–12
candidates, 100 observations, 32 fields per candidate. No real reviewed edition labels, serving
cost, or latency were measured. Full check: 4,156 passed, 382 deselected, 98.52%; 12 focused
tests and 100% module coverage.

**ITERATE** on already-selected candidates only. The result never prunes or promotes identity;
ADR 0014 section 6 matching remains DEFER.

## Integration gates

The next two candidates for controlled integration design are GM06 Missing preview and GM10
copy selection. Before either becomes a serving implementation, its owning consumer and producer
must prove source completeness, authenticated owner scope where applicable, and a versioned
contract through their normal repository review. The other prototypes remain in their limited
offline/selected-input roles until their per-feature evidence gates are met.

No saved pursuit persistence or observation correction semantics are included. Those need
separate design decisions for consent, retention, erasure, correction, and ownership. Matching
and identity promotion remain deferred; GM12 and GM13 consume evidence but do not create aliases
or catalog-wide candidates. A production exposure plan, schema/contract changes, release, and
deployment each require their own owner reviews and gates.
