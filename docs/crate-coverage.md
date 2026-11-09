# Bounded joint crate coverage (offline prototype)

`api.crate_coverage.compare_crates` compares three deterministic strategies on one shared
catalog snapshot, authorized collection, scope preview, and capped candidate pool. It is a
pure transient projection, with no HTTP route, storage, graph inference, purchase behavior,
prices, or saved scenarios. Existing CrateFit and its genre bridge heuristic are unchanged.

## Inputs and identity

Supply the existing folded collection and its `AuthorizedCollection` adapter from
`api.pursuit_scope`. Their source release IDs must agree. Candidate contexts are the existing
`api.fit.compute_fit` input shape, with source provider release IDs, not native edition IDs.
Every candidate uses the collection's provider namespace; mixed-provider Fit folds are not
supported by the existing baseline. Native editions and confirmed works come from the same
`CatalogSnapshot` used by `preview_scope`.

The scope preview retains owned, missing, ownership-review, needs-review, and excluded
buckets separately. Only confirmed missing source rows enter the comparison. Unknown fields
included by scope policy still cannot supply guaranteed gain here. Incomplete ownership
conservatively withholds the entire candidate pool and marks all unowned objective units as
ownership-review: an unmapped owned source row might otherwise fabricate a gap. This may
understate useful coverage until identities are resolved.

By default the objective counts the scope's native edition/work units. A caller may instead
supply `CoverageObjective`: a named, snapshot-matched denominator of concrete artist, label,
or credit IDs, plus an explicit mapping from eligible native scope units to those IDs.
The mapping is caller-checked evidence, not discovered or validated artist/credit attribution.
Unmapped units supply no coverage; unknown IDs do not become communities. Already-owned
scope units close only the declared units explicitly attributed to them. This lets one record
cover several declared units, so joint coverage differs from adding individual Fit scores.

Canonical native aliases and copies collapse before comparison. Edition scopes retain distinct
native editions; confirmed equivalent editions collapse to one work only with `UnitKind.WORK`.
No equivalence is inferred from a title, artist, or Fit redundancy score. For aliases, the
lexically first source row supplies Fit/popularity for every strategy, even if another alias
has a higher score. Conflicting rows for one source ID are rejected.

## Bounds and policies

The input hard limits are 200 candidate rows, 5,000 snapshot rows, and 2,000 declared objective
units. Oversized input fails explicitly. The shared candidate limit is 1–200 (default 100),
and selection limit is 0–20 (default 5). After canonicalization, a sorted native-key prefix
applies the candidate cap without favoring any strategy. The report lists the actual pool,
canonical count, omitted count, controls, and withheld source rows.

- Greedy coverage chooses the largest remaining marginal unit gain, then the native key,
  recomputing after each addition. It stops at zero gain; it is not an exact optimum solver.
- Individual Fit sorts the unchanged `compute_fit` result against the original collection,
  then the native key. It never refolds a simulated collection.
- Popularity sorts the caller-supplied nonnegative count, then the native key.

All policies use that same capped pool and selection limit. The reference sorts can select
zero-gain additions, exposing redundancy rather than disguising it as new coverage. Each
addition reports source/native identities, actual Fit decomposition, popularity, marginal
new units, overlapping units, and before/after covered counts. Native scope counts are
reported separately from the declared objective denominator. `scope_after.owned` is the
hypothetical count if the selected missing native units were added; no ownership is changed.
`projection_only` is always true. Coverage fractions refer only to the declared denominator
and remain unavailable during ownership review; they do not claim catalog completeness.

## Reproducible synthetic report

Run from the repository root:

```sh
uv run python -m api.evaluation.crate_coverage
uv run python -m api.evaluation.crate_coverage --candidate-limit 2 --selection-limit 1
```

The frozen fixture is named `synthetic-crate-catalog-v1`, with collection snapshot
`synthetic-collection-v1`. A and B each cover artist-one, label-one, and credit-one; C covers
artist-two and label-two. Artist/label facts and Fit contexts agree. Credit-one is explicitly
hand-declared supplemental fixture evidence. The collector owns one other eligible edition.
All three Fit scores saturate at 1.0, so the baseline's native-key tie order picks A then B.
Popularity counts 100, 99, and 98 also pick A then B. With two selection slots:

| Strategy | Additions | Marginal gains | Objective before → after | Native owned/missing before → after |
| --- | --- | --- | --- | --- |
| Greedy coverage | A, C | 3, 2 | 0/5 → 5/5 | 1/3 → 3/1 |
| Individual Fit | A, B | 3, 0 | 0/5 → 3/5 | 1/3 → 3/1 |
| Popularity | A, B | 3, 0 | 0/5 → 3/5 | 1/3 → 3/1 |

B's second addition overlaps all three units already covered by A. C contributes two
concrete different units. Both native scope counts and the declared objective counts retain
their original denominators (4 native units and 5 declared units). The JSON output includes
all per-addition evidence and version IDs (`bounded-joint-coverage-v1`, `cratefit_v0`).

This is a hand-counted synthetic complementarity demonstration. Real checked feedback labels
remain zero, omitted labels remain unknown, and real usefulness is pending. The arithmetic
is not evidence of a real 15% usefulness improvement, learned preference quality, or true
graph communities. There is no production integration or deployment in this prototype.
