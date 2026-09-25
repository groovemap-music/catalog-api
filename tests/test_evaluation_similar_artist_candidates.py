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
# differs from the gm-design-chw.2 spike's dump-wide "later collaborators" methodology), the
# bar is cleared cleanly once the MIN_ARTIST_RELEASES floor is restored on the candidate
# scope (review round 1 caught that it had been dropped): overall recall@10 rises from
# 0.44507 (legacy) to 0.53614 (new), a +20.5% relative gain, and every one of the three
# reported media families improves at k=10 too (vinyl 0.40741->0.55556, optical
# 0.30769->0.34615, tape 0.65217->0.73913) -- not just at k=25, which is what the review
# asked to see reported. Before the floor was restored, recall@10 measured marginally
# *below* the legacy baseline (0.44229 vs 0.44507); the floor turned out to matter for
# ranking quality, not just cost: without it, low-signal candidates (a single shared release
# on one dimension) diluted the top-10 with weak matches. Exact numbers below, not rounded.


def test_recall_at_10_and_25_improve_overall_with_no_family_regression(report: dict) -> None:
    new = report["metrics"]["similar_artist"]["overall"]
    legacy = report["metrics"]["similar_artist_legacy"]["overall"]
    assert new["recall_at_10"] == pytest.approx(0.5361441798941798, abs=1e-9)
    assert legacy["recall_at_10"] == pytest.approx(0.4450727513227513, abs=1e-9)
    assert new["recall_at_10"] >= legacy["recall_at_10"]
    assert new["recall_at_25"] >= legacy["recall_at_25"]
    assert new["hit_rate"] >= legacy["hit_rate"]
    for family in REPORTED_FAMILIES:
        new_family = report["metrics"]["similar_artist"]["by_format"][family]
        legacy_family = report["metrics"]["similar_artist_legacy"]["by_format"][family]
        assert new_family["recall_at_10"] >= legacy_family["recall_at_10"], f"{family} regressed at k=10"
        assert new_family["recall_at_25"] >= legacy_family["recall_at_25"], f"{family} regressed at k=25"


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
