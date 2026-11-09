"""Tests for external-manifest comparison and quality evidence reporting."""

import json
from pathlib import Path
from typing import Any

from api.runout import SignatureRule
from api.runout.evaluation import evaluate_manifest


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "runout"


def _fixtures() -> tuple[dict[str, Any], tuple[SignatureRule, ...]]:
    manifest = json.loads((FIXTURE_DIR / "synthetic-manifest.json").read_text())
    signature_document = json.loads((FIXTURE_DIR / "synthetic-signatures.json").read_text())
    rules = tuple(SignatureRule(row["signature"], row["role"], row["candidate"], row["source_ref"]) for row in signature_document["entries"])
    return manifest, rules


def test_synthetic_fixture_compares_interpreter_to_exact_baseline() -> None:
    manifest, rules = _fixtures()

    report = evaluate_manifest(manifest, rules)

    assert report["case_count"] == 8
    assert report["interpreter"]["accepted_prediction_count"] == 3
    assert report["interpreter"]["accepted_precision"] == 1.0
    assert report["interpreter"]["coverage"] == 3 / 8
    assert report["exact_signature_baseline"]["accepted_prediction_count"] == 2
    assert report["exact_signature_baseline"]["accepted_precision"] == 1.0
    assert report["exact_signature_baseline"]["coverage"] == 2 / 8
    assert report["quality_evidence"]["status"] == "missing"
    assert report["cases"][3]["interpretation"]["unknown_spans"]


def test_synthetic_fixture_cannot_become_reviewed_evidence_by_attestation() -> None:
    manifest, rules = _fixtures()
    manifest["cases"] = [manifest["cases"][0]] * 100
    manifest["review"] = {"status": "reviewed", "reviewer": "reviewer-1", "reviewed_at": "2026-11-01"}

    report = evaluate_manifest(manifest, rules)

    assert report["quality_evidence"]["status"] == "missing"


def test_external_100_case_reviewer_attestation_is_reported_unverified() -> None:
    manifest, rules = _fixtures()
    manifest["kind"] = "reviewed"
    manifest["cases"] = [manifest["cases"][0]] * 100
    manifest["review"] = {"status": "reviewed", "reviewer": "reviewer-1", "reviewed_at": "2026-11-01"}

    report = evaluate_manifest(manifest, rules)

    assert report["quality_evidence"]["status"] == "reviewer_attested_100_cases"
    assert report["quality_evidence"]["independently_verified"] is False


def test_missing_review_attestation_does_not_claim_quality_evidence() -> None:
    manifest, rules = _fixtures()
    manifest["kind"] = "reviewed"
    manifest["review"] = {"status": "reviewed", "reviewer": "reviewer-1"}

    report = evaluate_manifest(manifest, rules)

    assert report["quality_evidence"]["status"] == "missing"
