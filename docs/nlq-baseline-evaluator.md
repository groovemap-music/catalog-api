# NLQ baseline evaluator

The frozen baseline is an offline harness demonstration, not a measurement of
real answer quality. Run it with:

```sh
uv run python -m api.nlq_baseline_eval
```

The checked-in question set and canned responses cover exact lookup, multi-hop
credits, aggregate count, ambiguity, unanswerable requests, and isolated
collections for two users. Every fact is synthetic. `CannedModelAdapter`
feeds canned Claude-shaped tool and final turns through the existing
`NLQEngine`; `SyntheticToolAdapter` supplies caller-filtered facts. The harness
has no model, database, cache, or network client.

## Report fields

The JSON output reports correctness against the frozen expected response,
citation support against the facts available to that caller, abstention
precision and recall, blocked cross-user requests, privacy leaks, policy/model/
snapshot versions, and total reported cost. Unknown or unsupported citations
fail citation support; foreign-owned citations or foreign collection details
in an answer count as privacy failures. A denied cross-user lookup is counted
as blocked and is not itself a privacy failure.

The synthetic privacy scan detects references to annotated foreign fact IDs and
exact foreign fact text. It does not detect every paraphrase or indirect
disclosure, and a zero count is not a privacy assurance.

## Evaluating an explicitly supplied run

Real evaluation is opt-in and consumes only a file the operator explicitly
provides:

```sh
uv run python -m api.nlq_baseline_eval --real-input /path/to/evaluation.json
```

The file must include `policy_version`, `model_version`, `snapshot_version`,
synthetic or real `facts`, and `cases`. Each case includes `id`, `category`,
`question`, `expected` (`text` and cited fact IDs), and `response` (`summary` or
`text`, `fact_ids`, `citations`, `abstained`, and `cost_usd`). Collection facts
include an `owner`; collection cases include their authenticated `caller_id`.
Missing version or cost metadata is an input error. Do not commit private
corpora, collection facts, prompts, responses, or run reports.

The current `NLQResult` exposes `summary`, `entities`, `tools_used`, and
`actions`, but does not carry citation IDs or a fact transcript. To evaluate a
real engine run, the operator must explicitly provide the output summary and
the corresponding tool fact IDs/citations in this evaluator's input format.
This evaluator does not invoke Claude, call production tools, or infer missing
evidence; its output is only as meaningful as the supplied facts and expected
answers. The synthetic default is always labeled
`synthetic_harness_demonstration` and carries
`answer_quality_claim: none: synthetic replay only`.
