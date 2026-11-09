# Run-out interpreter prototype

`api.runout` is a pure, deterministic prototype for interpreting run-out inscriptions. It preserves
the original inscription, partitions it into typed lexical spans, returns exact match spans with
original half-open character offsets, and exposes unmatched text as typed `unknown_spans`.

Attribution rules are supplied by the caller as literal inscription signatures with a role,
candidate ID, and source reference. Matching is case sensitive, permits flexible whitespace, and
requires token boundaries. It does not guess plants, engineers, or pressing lineage. Conflicting
candidate IDs for the same matched signature, or overlapping signatures that conflict for a role,
remain ambiguous. Multiple source references for the same role and candidate are corroborating
evidence. No catalog identifier is normalized or written. Signature whitespace handling preserves
case and punctuation in keeping with ADR 0011's matrix evidence.

## Offline evaluator

Run the included explicitly synthetic examples:

```bash
uv run python scripts/evaluate_runout.py
```

Supply a reviewed manifest and signature dictionary from outside the repository:

```bash
uv run python scripts/evaluate_runout.py \
  --manifest /path/to/reviewed-manifest.json \
  --signatures /path/to/reviewed-signatures.json
```

The manifest is JSON with `cases`, each containing `id`, `inscription`, and
`expected_attributions`, a list of `{ "role": "...", "candidate": "..." }` objects (empty when
no attribution is expected). To report a reviewer attestation, include `kind: "reviewed"` and a
`review` object with `status: "reviewed"`, a non-empty `reviewer`, and `reviewed_at`. Exactly 100
cases are required for the 100-case indicator. The evaluator reports that metadata as
reviewer-attested and explicitly says it has not independently verified the review. Manifests
marked `kind: "synthetic"` never satisfy the quality indicator.

The signature dictionary JSON has an `entries` array. Each entry contains a case-preserving
`signature`, `role`, `candidate`, and `source_ref`. Repeated signatures can represent ambiguity;
different signatures that overlap in the input are checked for contradictory same-role candidates.
Keep private real inscriptions and their labels in the external reviewed manifest/dictionary.

The report compares bounded interpretation to exact whole-inscription signature lookup.
`accepted_precision` is the share of accepted role/candidate predictions found in the expected set.
`coverage` is the share of cases with at least one accepted prediction. Ambiguous spans are not
accepted. When a metric's denominator is zero, it is `null`.

The checked-in fixtures use synthetic strings and `fixture:*` labels only. They demonstrate the
evaluation mechanics and are not reviewed real cases or evidence of real-world accuracy.
