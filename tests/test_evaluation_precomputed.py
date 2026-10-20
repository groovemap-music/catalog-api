"""gm-catalog-api-2zsq.1: the precomputed-list similar-artist candidate, on synthetic data.

Embeddings and lists are synthetic (api.evaluation.precomputed), so these tests pin the wiring and
the committed snapshot. They deliberately do not assert that the candidate beats the heuristic:
vectors built from the heuristic's own signals say nothing about real FastRP quality.
"""

from __future__ import annotations

import pytest

from api.config import DEFAULT_FUSION_ALPHA
from api.evaluation.baseline import SIMILAR_ARTIST_CANDIDATES_VERSION
from api.evaluation.fixtures import load_golden_set
from api.evaluation.precomputed import (
    PRECOMPUTED_FUSED_VERSION,
    PRECOMPUTED_VERSION,
    synthetic_embeddings,
    synthetic_lists,
)
from api.evaluation.report import (
    PRECOMPUTED_EXPECTED_METRICS_PATH,
    evaluate_precomputed_similar_artists,
    load_precomputed_expected_metrics,
    precomputed_metrics_snapshot,
    serialize,
    write_precomputed_expected_metrics,
)
from api.evaluation.split import SPLIT_CUT, observed_graph


@pytest.fixture(scope="module")
def golden():
    return load_golden_set()


@pytest.fixture(scope="module")
def report(golden):
    return evaluate_precomputed_similar_artists(golden)


def test_the_report_names_the_latest_heuristic_baseline_and_both_candidates(report: dict) -> None:
    assert report["baseline_version"] == SIMILAR_ARTIST_CANDIDATES_VERSION
    assert report["candidate_versions"] == [PRECOMPUTED_VERSION, PRECOMPUTED_FUSED_VERSION]
    assert report["fusion_alpha"] == DEFAULT_FUSION_ALPHA == 0.8
    assert report["synthetic_embeddings"] is True
    assert set(report["metrics"]) == {
        "similar_artist_precomputed",
        "similar_artist_precomputed_fused",
        "similar_artist_baseline",
        "similar_artist_legacy",
    }


def test_the_baseline_block_equals_the_committed_all_signals_numbers(report: dict) -> None:
    """The comparison point is the existing baseline, untouched."""
    assert report["metrics"]["similar_artist_baseline"]["overall"]["recall_at_10"] == pytest.approx(0.5361441798941798, abs=1e-9)


def test_synthetic_lists_are_deterministic_ranked_and_exclude_self(golden) -> None:
    graph = observed_graph(golden, SPLIT_CUT)
    first = synthetic_lists(synthetic_embeddings(graph))
    assert first == synthetic_lists(synthetic_embeddings(graph))
    for artist_id, neighbours in first.items():
        assert artist_id not in {other for other, _ in neighbours}
        scores = [score for _, score in neighbours]
        assert scores == sorted(scores, reverse=True)


def test_two_evaluations_produce_the_same_report(golden, report: dict) -> None:
    assert evaluate_precomputed_similar_artists(golden) == report


def test_the_committed_snapshot_matches_a_fresh_run(report: dict) -> None:
    committed = load_precomputed_expected_metrics()
    fresh = precomputed_metrics_snapshot(report)
    assert committed["baseline_version"] == fresh["baseline_version"]
    for name, block in fresh["metrics"].items():
        for key, value in block["overall"].items():
            assert committed["metrics"][name]["overall"][key] == pytest.approx(value, abs=1e-9), f"{name} {key}"


def test_writing_the_snapshot_reproduces_the_committed_bytes(tmp_path, report: dict) -> None:
    written = write_precomputed_expected_metrics(report, tmp_path / "expected.json")
    assert written.read_bytes() == PRECOMPUTED_EXPECTED_METRICS_PATH.read_bytes()
    assert serialize(precomputed_metrics_snapshot(report)).encode() == written.read_bytes()
