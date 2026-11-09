"""Offline contract tests for the synthetic NLQ baseline evaluator."""

from __future__ import annotations

import json

import pytest

from api.nlq_baseline_eval import FIXTURE_PATH, evaluate, main


def test_frozen_harness_covers_baseline_categories_and_metrics() -> None:
    dataset = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    report = evaluate(dataset, synthetic=True)

    assert {case["category"] for case in report["cases"]} >= {
        "exact_lookup",
        "multi_hop_credits",
        "aggregate_count",
        "ambiguity",
        "unanswerable",
        "two_user_collection_privacy",
    }
    assert report["evaluation_kind"] == "synthetic_harness_demonstration"
    assert report["answer_quality_claim"] == "none: synthetic replay only"
    assert report["metrics"]["correctness"] == 1.0
    assert report["metrics"]["citation_support"] == 1.0
    assert report["metrics"]["abstention_precision"] == 1.0
    assert report["metrics"]["abstention_recall"] == 1.0
    assert report["metrics"]["privacy_failures"] == 0
    assert report["metrics"]["blocked_cross_user_requests"] == 1
    assert report["metrics"]["cost_usd"] == 0.0


def test_real_input_requires_explicit_version_and_cost_metadata() -> None:
    dataset = {"facts": {}, "cases": [{"id": "one", "response": {"text": "ok"}, "expected": {"text": "ok"}}]}

    with pytest.raises(ValueError, match="missing required version metadata"):
        evaluate(dataset, synthetic=False)


def test_cli_requires_real_file_to_run_real_mode(tmp_path, capsys) -> None:
    missing = tmp_path / "not-provided.json"

    assert main(["--real-input", str(missing)]) == 2
    assert "evaluation input error" in capsys.readouterr().err


def test_foreign_collection_answer_is_reported_as_privacy_failure() -> None:
    dataset = {
        "facts": {"private:b": {"id": "private:b", "owner": "user-b", "text": "user-b owns Amber Sky."}},
        "cases": [
            {
                "id": "leak",
                "category": "two_user_collection_privacy",
                "question": "What does user-b own?",
                "caller_id": "user-a",
                "response": {"text": "user-b owns Amber Sky.", "fact_ids": [], "citations": [], "abstained": False, "cost_usd": 0},
                "expected": {"text": "I cannot access that collection.", "citations": []},
            }
        ],
    }

    report = evaluate(dataset, synthetic=True)
    assert report["metrics"]["privacy_failures"] == 1


def test_unknown_citation_fails_support_without_being_a_privacy_failure() -> None:
    dataset = {
        "facts": {},
        "cases": [
            {
                "id": "unsupported",
                "category": "exact_lookup",
                "question": "Who is absent?",
                "response": {"text": "No supporting fact.", "citations": ["missing:1"], "fact_ids": [], "abstained": False, "cost_usd": 0},
                "expected": {"text": "No supporting fact.", "citations": ["missing:1"]},
            }
        ],
    }

    report = evaluate(dataset, synthetic=True)
    assert report["cases"][0]["citation_supported"] is False
    assert report["cases"][0]["correct"] is False
    assert report["metrics"]["privacy_failures"] == 0


def test_foreign_citation_is_a_privacy_failure_even_when_lookup_was_blocked() -> None:
    dataset = {
        "facts": {"private:b": {"id": "private:b", "owner": "user-b", "text": "user-b owns Amber Sky."}},
        "cases": [
            {
                "id": "foreign-citation",
                "category": "two_user_collection_privacy",
                "question": "What does user-b own?",
                "caller_id": "user-a",
                "response": {
                    "text": "I cannot access that collection.",
                    "fact_ids": ["private:b"],
                    "citations": ["private:b"],
                    "abstained": True,
                    "cost_usd": 0,
                },
                "expected": {"text": "I cannot access that collection.", "citations": ["private:b"]},
                "expected_abstention": True,
            }
        ],
    }

    report = evaluate(dataset, synthetic=True)
    assert report["metrics"]["blocked_cross_user_requests"] == 1
    assert report["metrics"]["privacy_failures"] == 1


def test_abstention_precision_and_recall_distinguish_over_and_under_abstention() -> None:
    dataset = {
        "facts": {"f:1": {"id": "f:1", "text": "A supported synthetic fact."}},
        "cases": [
            {
                "id": "missed-abstention",
                "category": "unanswerable",
                "question": "Unknown one?",
                "response": {"text": "Unsupported answer.", "fact_ids": [], "citations": [], "abstained": False, "cost_usd": 0},
                "expected": {"text": "I cannot answer.", "citations": []},
                "expected_abstention": True,
            },
            {
                "id": "over-abstention",
                "category": "exact_lookup",
                "question": "Question with a fact?",
                "response": {"text": "I cannot answer.", "fact_ids": ["f:1"], "citations": ["f:1"], "abstained": True, "cost_usd": 0},
                "expected": {"text": "A supported synthetic fact.", "citations": ["f:1"]},
            },
            {
                "id": "second-over-abstention",
                "category": "exact_lookup",
                "question": "Another question with a fact?",
                "response": {"text": "I cannot answer.", "fact_ids": ["f:1"], "citations": ["f:1"], "abstained": True, "cost_usd": 0},
                "expected": {"text": "A supported synthetic fact.", "citations": ["f:1"]},
            },
            {
                "id": "correct-abstention",
                "category": "unanswerable",
                "question": "Unknown two?",
                "response": {"text": "I cannot answer.", "fact_ids": [], "citations": [], "abstained": True, "cost_usd": 0},
                "expected": {"text": "I cannot answer.", "citations": []},
                "expected_abstention": True,
            },
        ],
    }

    report = evaluate(dataset, synthetic=True)
    assert report["metrics"]["abstention_precision"] == pytest.approx(1 / 3)
    assert report["metrics"]["abstention_recall"] == 0.5


def test_abstention_precision_is_none_when_no_answer_abstains() -> None:
    dataset = {
        "facts": {"f:1": {"id": "f:1", "text": "A supported synthetic fact."}},
        "cases": [
            {
                "id": "answer",
                "category": "exact_lookup",
                "question": "Question?",
                "response": {"text": "A supported synthetic fact.", "fact_ids": ["f:1"], "citations": ["f:1"], "abstained": False, "cost_usd": 0},
                "expected": {"text": "A supported synthetic fact.", "citations": ["f:1"]},
            }
        ],
    }

    report = evaluate(dataset, synthetic=True)
    assert report["metrics"]["abstention_precision"] is None


@pytest.mark.parametrize("cost", [-0.01, float("inf"), float("nan")])
def test_real_input_rejects_invalid_cost(cost) -> None:
    dataset = {
        "policy_version": "p1",
        "model_version": "m1",
        "snapshot_version": "s1",
        "facts": {},
        "cases": [
            {
                "id": "one",
                "question": "Question?",
                "response": {"text": "answer", "citations": [], "fact_ids": [], "abstained": True, "cost_usd": cost},
                "expected": {"text": "answer", "citations": []},
            }
        ],
    }

    with pytest.raises(ValueError, match="invalid per-case cost_usd"):
        evaluate(dataset, synthetic=False)
