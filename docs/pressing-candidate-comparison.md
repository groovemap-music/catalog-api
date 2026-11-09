# Known-release comparison prototype

`api.pressing_comparison.compare_known_candidates` compares a bounded list of releases that the
caller has already selected with observations already authorized for one explicit owned copy.
It does not discover candidates, query stores, resolve provider aliases, or write/publish
identity claims. Native release UUIDs identify candidate rows; source IDs remain bundled in
provider-qualified references. Repeated aliases belong under one native candidate. The selected
copy ID is separate from every catalog release ID, and evidence from different copies cannot be
combined.

## Comparison and ranking

Catalog fields carry explicit source references. A field is discriminatory for a candidate pair
only when both candidates have known, non-overlapping values. Missing values are neutral: they
increase `unknown_pair_count` but never count as matches or differences. Suggested next fields are
ranked by the fraction of all candidate pairs they are proven to distinguish, then known-pair
count, then field name. `known_pair_fraction` separately reports how much of the candidate set
had values for that field. Numeric catalog values compare to canonical numeric text (for example
year `1978`); string identifiers remain exact, preserving leading zeroes and case. No confidence
or prior is invented. Shared barcode, cover-art, or catalog-group values are just field values;
equality cannot prove edition identity.

Observation checks report compatible, contradictory, or unknown evidence candidate by candidate.
Contradictions remain inspectable and do not remove a candidate. Typed caller-supplied runout
rules use the pure `api.runout` interpreter; only supported attributions are compared with
catalog fields, even when another span is ambiguous. Raw matrix/runout observations are also
compared when a corresponding catalog field is supplied. Ambiguous and unknown runout spans
remain visible and do not become evidence. Stored observation confidence is passed through as
user metadata; it is not treated as a probability or used to confirm identity.
Every candidate remains unresolved; `identity_promoted` is always false. Existing ADR 0014
section 6 and ADR 0016 matching DEFER remain in force. Real quality evidence remains PENDING;
the accompanying cases are synthetic only.

## Reproducible synthetic example

```bash
uv run python scripts/compare_known_candidates.py
uv run pytest tests/test_pressing_comparison.py
```

The example uses only `fixture:*` identifiers and synthetic provenance. It demonstrates stable
ranking and traceable evidence, not production matching quality. Serving this comparison from
Search, a release drawer, or Fitpicker remains a separate gated change.
