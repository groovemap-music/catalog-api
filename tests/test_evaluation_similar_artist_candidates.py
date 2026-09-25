"""gm-catalog-api-tsmu.1: the similar-artist all-signal candidate baseline.

Registered alongside ``heuristics-2026-09`` rather than in place of it: ``evaluate()`` and
``tests/fixtures/golden/expected-metrics.json`` are untouched by this module (see
``test_evaluation_baseline.py`` and ``test_evaluation_report.py`` for the frozen-baseline
tests that prove that). This module only exercises the second, narrower harness in
``api.evaluation.report`` that compares the new candidate scope
(``GoldenGraph.candidate_artists_all_signals``) against the frozen one on the identical
golden set and time split.
"""

from __future__ import annotations

import json

import pytest

from api.evaluation.baseline import BASELINE_VERSION, SIMILAR_ARTIST_CANDIDATES_VERSION, run_baseline
from api.evaluation.fixtures import load_golden_set
from api.evaluation.metrics import REPORTED_FAMILIES
from api.evaluation.report import (
    SIMILAR_ARTIST_EXPECTED_METRICS_PATH,
    evaluate_similar_artist_candidates,
    load_similar_artist_expected_metrics,
    serialize,
    similar_artist_metrics_snapshot,
    write_similar_artist_expected_metrics,
)
from api.evaluation.split import observed_graph


@pytest.fixture(scope="module")
def golden():
    return load_golden_set()


@pytest.fixture(scope="module")
def report(golden):
    body, _new_run, _legacy_run = evaluate_similar_artist_candidates(golden)
    return body


def test_the_new_run_is_registered_as_its_own_version(golden) -> None:
    graph = observed_graph(golden)
    legacy = run_baseline(graph)
    all_signals = run_baseline(graph, similar_artist_candidates="all_signals")
    assert legacy.version == BASELINE_VERSION
    assert all_signals.version == SIMILAR_ARTIST_CANDIDATES_VERSION
    assert SIMILAR_ARTIST_CANDIDATES_VERSION != BASELINE_VERSION


def test_recommendations_and_rarity_are_unaffected_by_the_candidate_scope(golden) -> None:
    """Only similar_artists differs; everything else is the identical heuristics-2026-09 run."""
    graph = observed_graph(golden)
    legacy = run_baseline(graph)
    all_signals = run_baseline(graph, similar_artist_candidates="all_signals")
    assert all_signals.recommendations == legacy.recommendations
    assert all_signals.discoveries == legacy.discoveries
    assert all_signals.rarity == legacy.rarity
    assert all_signals.similar_artists != legacy.similar_artists


def test_an_invalid_candidate_scope_is_rejected(golden) -> None:
    with pytest.raises(ValueError, match="similar_artist_candidates"):
        run_baseline(observed_graph(golden), similar_artist_candidates="nope")


def test_the_report_names_both_baseline_versions(report: dict) -> None:
    assert report["baseline_version"] == SIMILAR_ARTIST_CANDIDATES_VERSION
    assert report["compared_to_baseline_version"] == BASELINE_VERSION


def test_two_evaluations_produce_the_same_report(golden) -> None:
    first, _new, _legacy = evaluate_similar_artist_candidates(golden)
    second, _new, _legacy = evaluate_similar_artist_candidates(golden)
    assert first == second


# ── The acceptance comparison ──────────────────────────────────────────
#
# gm-catalog-api-tsmu.1's acceptance criteria calls for the new baseline's committed
# recall@10 to be equal to or better than heuristics-2026-09's, with no media family
# regressing. On this synthetic 12-collector golden set, adapted to this harness's
# collector-acquisition ground truth (see ``similar_artist_metrics``'s docstring for why this
# differs from the gm-design-chw.2 spike's dump-wide "later collaborators" methodology),
# recall@25 and hit_rate clear that bar with room to spare and no family's recall@25
# regresses, but headline recall@10 itself comes in marginally *below* the frozen baseline
# (0.4423 vs 0.4451, about a 0.6% relative difference on n=12 collectors). The numbers below
# pin that finding rather than asserting an inequality that is not actually true on this
# fixture; see the bead report for why this is flagged for review rather than papered over.


def test_recall_at_25_and_hit_rate_improve_with_no_family_regression(report: dict) -> None:
    new = report["metrics"]["similar_artist"]["overall"]
    legacy = report["metrics"]["similar_artist_legacy"]["overall"]
    assert new["recall_at_25"] >= legacy["recall_at_25"]
    assert new["hit_rate"] >= legacy["hit_rate"]
    for family in REPORTED_FAMILIES:
        new_family = report["metrics"]["similar_artist"]["by_format"][family]
        legacy_family = report["metrics"]["similar_artist_legacy"]["by_format"][family]
        assert new_family["recall_at_25"] >= legacy_family["recall_at_25"], f"{family} regressed at k=25"


def test_recall_at_10_is_flagged_rather_than_silently_regressed(report: dict) -> None:
    """Pins the measured recall@10 gap for reviewer visibility -- see the module docstring."""
    new = report["metrics"]["similar_artist"]["overall"]["recall_at_10"]
    legacy = report["metrics"]["similar_artist_legacy"]["overall"]["recall_at_10"]
    assert legacy - new == pytest.approx(0.0027777777777777623, abs=1e-9), (
        "the recall@10 gap moved: re-examine whether the candidate scope changed, not just this pin"
    )


# ── The committed snapshot ─────────────────────────────────────────────


def test_the_snapshot_is_the_time_invariant_slice(report: dict) -> None:
    snapshot = similar_artist_metrics_snapshot(report)
    assert snapshot["metrics"] == report["metrics"]
    assert set(snapshot) == {"baseline_version", "compared_to_baseline_version", "golden_set_generator_version", "split_cut", "metrics"}


def test_writing_the_snapshot_reproduces_the_committed_bytes(tmp_path, report: dict) -> None:
    written = write_similar_artist_expected_metrics(report, tmp_path / "expected-similar-artist-metrics.json")
    assert written.read_bytes() == SIMILAR_ARTIST_EXPECTED_METRICS_PATH.read_bytes()


def test_the_committed_snapshot_round_trips(report: dict) -> None:
    assert load_similar_artist_expected_metrics() == json.loads(serialize(similar_artist_metrics_snapshot(report)))
