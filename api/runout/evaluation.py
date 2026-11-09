"""Offline comparison of bounded signature interpretation with exact lookup baseline."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from api.runout.interpreter import Candidate, SignatureRule, exact_signature_candidates, interpret


def _candidate_key(candidate: Candidate) -> tuple[str, str]:
    return candidate.role, candidate.value


def _measure(predictions: Sequence[set[tuple[str, str]]], expected: Sequence[set[tuple[str, str]]]) -> dict[str, Any]:
    accepted_count = sum(len(items) for items in predictions)
    correct_count = sum(len(prediction & truth) for prediction, truth in zip(predictions, expected, strict=True))
    covered_cases = sum(bool(items) for items in predictions)
    total = len(expected)
    return {
        "accepted_prediction_count": accepted_count,
        "correct_prediction_count": correct_count,
        "covered_case_count": covered_cases,
        "accepted_precision": correct_count / accepted_count if accepted_count else None,
        "coverage": covered_cases / total if total else None,
    }


def evaluate_manifest(manifest: Mapping[str, Any], rules: Sequence[SignatureRule]) -> dict[str, Any]:
    """Compare substring interpretation against exact whole-inscription lookup.

    A manifest may attest that it contains 100 reviewed cases. The evaluator reports that
    attestation but cannot independently establish that people reviewed the annotations.
    Manifests explicitly marked synthetic can never satisfy the reviewed quality indicator.
    """
    cases = manifest.get("cases")
    if not isinstance(cases, list):
        raise ValueError("manifest.cases must be a list")

    expected_by_case: list[set[tuple[str, str]]] = []
    interpreter_predictions: list[set[tuple[str, str]]] = []
    baseline_predictions: list[set[tuple[str, str]]] = []
    case_results: list[dict[str, Any]] = []

    for offset, case in enumerate(cases):
        if not isinstance(case, Mapping):
            raise ValueError(f"manifest.cases[{offset}] must be an object")
        inscription = case.get("inscription")
        expected_items = case.get("expected_attributions")
        if not isinstance(inscription, str):
            raise ValueError(f"manifest.cases[{offset}].inscription must be a string")
        if not isinstance(expected_items, list):
            raise ValueError(f"manifest.cases[{offset}].expected_attributions must be a list")
        expected: set[tuple[str, str]] = set()
        for index, attribution in enumerate(expected_items):
            if (
                not isinstance(attribution, Mapping)
                or not isinstance(attribution.get("role"), str)
                or not isinstance(attribution.get("candidate"), str)
            ):
                raise ValueError(f"manifest.cases[{offset}].expected_attributions[{index}] needs string role and candidate")
            expected.add((attribution["role"], attribution["candidate"]))

        parsed = interpret(inscription, rules)
        interpreted = {_candidate_key(candidate) for candidate in parsed.accepted}
        exact = exact_signature_candidates(inscription, rules)
        exact_keys = {_candidate_key(candidate) for candidate in exact}
        baseline = exact_keys if len(exact_keys) == 1 else set()
        expected_by_case.append(expected)
        interpreter_predictions.append(interpreted)
        baseline_predictions.append(baseline)
        case_results.append(
            {
                "id": case.get("id", str(offset + 1)),
                "interpretation": {
                    "status": parsed.status.value,
                    "matched_spans": [
                        {
                            "text": match.text,
                            "start": match.start,
                            "end": match.end,
                            "status": match.status.value,
                            "candidates": [
                                {"role": candidate.role, "candidate": candidate.value, "source_ref": candidate.source_ref}
                                for candidate in match.candidates
                            ],
                        }
                        for match in parsed.matches
                    ],
                    "unknown_spans": [
                        {"text": span.text, "start": span.start, "end": span.end, "kind": span.kind.value} for span in parsed.unknown_spans
                    ],
                    "accepted": [{"role": role, "candidate": candidate} for role, candidate in sorted(interpreted)],
                },
                "exact_signature_baseline": {
                    "status": "supported" if len(exact_keys) == 1 else "ambiguous" if exact_keys else "unknown",
                    "accepted": [{"role": role, "candidate": candidate} for role, candidate in sorted(baseline)],
                },
                "expected_attributions": [{"role": role, "candidate": candidate} for role, candidate in sorted(expected)],
            }
        )

    review = manifest.get("review")
    review = review if isinstance(review, Mapping) else {}
    complete_attestation = (
        manifest.get("kind") == "reviewed"
        and len(cases) == 100
        and review.get("status") == "reviewed"
        and isinstance(review.get("reviewer"), str)
        and bool(review.get("reviewer", "").strip())
        and isinstance(review.get("reviewed_at"), str)
        and bool(review.get("reviewed_at", "").strip())
    )
    if complete_attestation:
        quality_evidence: dict[str, Any] = {
            "status": "reviewer_attested_100_cases",
            "reviewer": review["reviewer"],
            "reviewed_at": review["reviewed_at"],
            "independently_verified": False,
        }
    else:
        quality_evidence = {
            "status": "missing",
            "reason": "A non-synthetic 100-case manifest with reviewer and reviewed_at attestation was not supplied.",
            "independently_verified": False,
        }

    return {
        "interpreter": _measure(interpreter_predictions, expected_by_case),
        "exact_signature_baseline": _measure(baseline_predictions, expected_by_case),
        "case_count": len(cases),
        "quality_evidence": quality_evidence,
        "cases": case_results,
    }
