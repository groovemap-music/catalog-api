"""Offline baseline evaluator for the existing NLQ answer contract.

The default run is deliberately synthetic: it replays frozen questions and
canned responses against synthetic facts. It does not call Claude or a catalog
service. A real evaluation requires an explicit JSON input via ``--real-input``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

from api.nlq.engine import NLQContext, NLQEngine


if TYPE_CHECKING:
    from api.nlq.config import NLQConfig
    from api.nlq.tools import NLQToolRunner


SYNTHETIC_POLICY = "synthetic-baseline-v1"
SYNTHETIC_MODEL = "canned-response-v1"
SYNTHETIC_SNAPSHOT = "synthetic-facts-v1"
FIXTURE_PATH = Path(__file__).with_name("nlq_baseline_fixtures.json")


@dataclass(frozen=True)
class CaseResult:
    case_id: str
    category: str
    correct: bool
    citation_supported: bool
    abstention_expected: bool
    abstained: bool
    privacy_failures: int
    blocked_cross_user_requests: int
    cost_usd: float


class SyntheticFacts:
    """Small, isolated catalog and per-user fact set used by the replay."""

    def __init__(self, facts: dict[str, dict[str, Any]]) -> None:
        self._facts = facts

    def read(self, fact_ids: list[str], *, caller_id: str | None) -> tuple[list[dict[str, Any]], int]:
        rows = [self._facts[fact_id] for fact_id in fact_ids if fact_id in self._facts]
        visible = [row for row in rows if row.get("owner") is None or row.get("owner") == caller_id]
        return visible, len(rows) - len(visible)

    def foreign_rows(self, *, caller_id: str | None) -> list[dict[str, Any]]:
        return [row for row in self._facts.values() if row.get("owner") is not None and row.get("owner") != caller_id]


class CannedModelAdapter:
    """Supply canned Claude-shaped tool and final turns to the real NLQ engine."""

    def __init__(self, turns: list[dict[str, Any]]) -> None:
        self._turns = iter(turns)
        self.messages = self

    async def create(self, **_request: Any) -> Any:
        turn = next(self._turns)
        blocks = []
        for block in turn.get("content", []):
            if block["type"] == "tool_use":
                blocks.append(SimpleNamespace(**block))
            else:
                blocks.append(SimpleNamespace(**block))
        return SimpleNamespace(stop_reason=turn["stop_reason"], content=blocks)


class SyntheticToolAdapter:
    """Answer a tiny lookup tool with synthetic, caller-filtered facts."""

    def __init__(self, facts: SyntheticFacts, *, caller_id: str | None) -> None:
        self._facts = facts
        self._caller_id = caller_id
        self.returned_ids: list[str] = []
        self.blocked_requests = 0

    async def execute(self, name: str, params: dict[str, Any], user_id: str | None, **_kwargs: Any) -> dict[str, Any]:
        if name != "synthetic_lookup":
            raise ValueError(f"unsupported synthetic tool: {name}")
        visible, blocked = self._facts.read(list(params.get("fact_ids", [])), caller_id=user_id)
        self.returned_ids.extend(row["id"] for row in visible)
        self.blocked_requests += blocked
        return {"facts": visible}

    def extract_entities(self, _name: str, _result: dict[str, Any]) -> list[dict[str, Any]]:
        return []


async def _run_engine_case(case: dict[str, Any], facts: SyntheticFacts) -> tuple[str, list[str], int]:
    response = case["response"]
    requested = list(response.get("fact_ids", []))
    final_text = response.get("summary", response.get("text", ""))
    # The model transcript exercises NLQEngine's real tool-use loop. The
    # separate annotations (citations/abstention/cost) are evaluator labels,
    # since today's NLQResult has no citation metadata field.
    turns: list[dict[str, Any]] = []
    if requested:
        turns.append(
            {
                "stop_reason": "tool_use",
                "content": [{"type": "tool_use", "id": f"{case['id']}-lookup", "name": "synthetic_lookup", "input": {"fact_ids": requested}}],
            }
        )
    turns.append({"stop_reason": "end_turn", "content": [{"type": "text", "text": final_text}]})
    runner = SyntheticToolAdapter(facts, caller_id=case.get("caller_id"))
    engine = NLQEngine(
        config=cast("NLQConfig", SimpleNamespace(model="canned-response-v1", max_iterations=3)),
        client=CannedModelAdapter(turns),
        tool_runner=cast("NLQToolRunner", runner),
    )
    result = await engine.run(case["question"], NLQContext(user_id=case.get("caller_id")))
    return result.summary, runner.returned_ids, runner.blocked_requests


def _norm(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().casefold())


def _valid_cost(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def evaluate(dataset: dict[str, Any], *, synthetic: bool) -> dict[str, Any]:
    """Score responses and fact references without making network requests."""
    if not synthetic:
        missing_versions = [key for key in ("policy_version", "model_version", "snapshot_version") if not dataset.get(key)]
        missing_costs = [case.get("id", "<unknown>") for case in dataset.get("cases", []) if "cost_usd" not in case.get("response", {})]
        invalid_costs = [
            case.get("id", "<unknown>") for case in dataset.get("cases", []) if not _valid_cost(case.get("response", {}).get("cost_usd"))
        ]
        if missing_versions or missing_costs or invalid_costs:
            details = []
            if missing_versions:
                details.append(f"missing required version metadata: {', '.join(missing_versions)}")
            if missing_costs:
                details.append(f"missing per-case cost_usd: {', '.join(missing_costs)}")
            if invalid_costs:
                details.append(f"invalid per-case cost_usd: {', '.join(invalid_costs)}")
            raise ValueError("; ".join(details))
    facts = SyntheticFacts(dataset.get("facts", {}))
    results: list[CaseResult] = []
    for case in dataset["cases"]:
        response = case["response"]
        caller = case.get("caller_id")
        answer_text, returned_ids, blocked_requests = asyncio.run(_run_engine_case(case, facts))
        visible_ids = set(returned_ids)
        citations = list(response.get("citations", []))
        expected = case["expected"]
        is_abstention = bool(response.get("abstained", False))
        citation_supported = bool(citations) and all(cid in visible_ids for cid in citations)
        if not citations:
            citation_supported = case.get("expected_abstention", False)
        foreign_rows = facts.foreign_rows(caller_id=caller)
        leaked_foreign_fact = any(row["text"].casefold() in answer_text.casefold() for row in foreign_rows)
        foreign_citation = any(cid in facts._facts and facts._facts[cid].get("owner") not in (None, caller) for cid in citations)
        privacy_failures = int(leaked_foreign_fact or foreign_citation)
        # A response is correct only when its normalized text and abstention
        # state match the frozen expectation and it cites the required facts.
        correct = (
            _norm(answer_text) == _norm(expected["text"])
            and is_abstention == bool(case.get("expected_abstention", False))
            and set(citations) == set(expected.get("citations", []))
            and citation_supported
            and privacy_failures == 0
        )
        results.append(
            CaseResult(
                case_id=case["id"],
                category=case["category"],
                correct=correct,
                citation_supported=citation_supported,
                abstention_expected=bool(case.get("expected_abstention", False)),
                abstained=is_abstention,
                privacy_failures=privacy_failures,
                blocked_cross_user_requests=blocked_requests,
                cost_usd=float(response.get("cost_usd", 0.0)),
            )
        )

    count = len(results)
    expected_abstentions = [row for row in results if row.abstention_expected]
    emitted_abstentions = [row for row in results if row.abstained]
    correct_abstentions = sum(row.abstained and row.abstention_expected for row in results)
    return {
        "evaluation_kind": "synthetic_harness_demonstration" if synthetic else "provided_real_evaluation_input",
        "answer_quality_claim": "none: synthetic replay only" if synthetic else "scored against caller-provided expectations",
        "versions": {
            "policy": dataset.get("policy_version", SYNTHETIC_POLICY),
            "model": dataset.get("model_version", SYNTHETIC_MODEL),
            "snapshot": dataset.get("snapshot_version", SYNTHETIC_SNAPSHOT),
        },
        "metrics": {
            "cases": count,
            "correctness": sum(row.correct for row in results) / count if count else 0.0,
            "citation_support": sum(row.citation_supported for row in results) / count if count else 0.0,
            "abstention_precision": correct_abstentions / len(emitted_abstentions) if emitted_abstentions else None,
            "abstention_recall": correct_abstentions / len(expected_abstentions) if expected_abstentions else None,
            "privacy_failures": sum(row.privacy_failures for row in results),
            "blocked_cross_user_requests": sum(row.blocked_cross_user_requests for row in results),
            "cost_usd": round(sum(row.cost_usd for row in results), 8),
        },
        "cases": [
            {
                "id": row.case_id,
                "category": row.category,
                "correct": row.correct,
                "citation_supported": row.citation_supported,
                "expected_abstention": row.abstention_expected,
                "abstained": row.abstained,
                "privacy_failures": row.privacy_failures,
                "blocked_cross_user_requests": row.blocked_cross_user_requests,
                "cost_usd": row.cost_usd,
            }
            for row in results
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--real-input",
        type=Path,
        help="explicit JSON evaluation data supplied by the operator; no private data is bundled",
    )
    args = parser.parse_args(argv)
    synthetic = args.real_input is None
    source = FIXTURE_PATH if synthetic else args.real_input
    try:
        dataset = json.loads(source.read_text(encoding="utf-8"))
        report = evaluate(dataset, synthetic=synthetic)
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        sys.stderr.write(f"evaluation input error: {exc}\n")
        return 2
    sys.stdout.write(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
