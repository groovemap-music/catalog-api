"""Tests for replayable catalog-context policies over the synthetic frozen harness."""

from __future__ import annotations

import json

import pytest

from api.evaluation.baseline import BASELINE_VERSION, run_baseline
from api.evaluation.discovery import (
    DISCOVERY_REPLAY_VERSION,
    FAMILIARITY_PENALTY_DEFAULT,
    METADATA_NEIGHBOR_VERSION,
    PolicyResult,
    _candidate_policy,
    _checked_labels,
    _collection_profile,
    _current_policy,
    _metadata_policy,
    _metadata_similarity,
    _normalize_candidate_pool,
    _ranking_metrics,
    _sparse_context_failures,
    main,
    replay_discovery,
)
from api.evaluation.fixtures import load_golden_set
from api.evaluation.graph import CollectorHoldings, GoldenGraph
from api.evaluation.split import observed_graph, split_golden_set


@pytest.fixture(scope="module")
def golden():
    return load_golden_set()


def test_replay_is_stable_policy_versioned_and_uses_one_candidate_pool(golden) -> None:
    first = replay_discovery(golden)
    second = replay_discovery(golden)

    assert first == second
    assert first["synthetic"] is True
    assert first["real_quality_status"].startswith("PENDING")
    assert first["versions"] == {
        "current": BASELINE_VERSION,
        "metadata_nearest_neighbor": METADATA_NEIGHBOR_VERSION,
        "catalog_context": DISCOVERY_REPLAY_VERSION,
    }
    assert first["labels"]["missing_labels_are_negative"] is False
    assert first["labels"]["real_checked_feedback_labels"] == 0
    for collector_id, pool in first["candidate_pools"].items():
        for rows in first["rankings"].values():
            ranked = rows[collector_id]
            assert {row["release_id"] for row in ranked} == set(pool)
            assert len({row["release_id"] for row in ranked}) == len(ranked)
            assert all(row["reasons"] for row in ranked)


def test_candidate_pool_filters_duplicates_owned_and_unknown_ids(golden) -> None:
    graph = observed_graph(golden)
    baseline = run_baseline(graph)
    collector_id = golden.collector_ids[0]
    owned_id = graph.holdings[collector_id].owned[0]
    valid_id = baseline.recommendations[collector_id][0]["id"]
    pools = _normalize_candidate_pool(
        golden,
        graph,
        [collector_id],
        baseline.recommendations,
        {collector_id: [valid_id, valid_id, owned_id, "unknown-release"]},
    )

    assert pools[collector_id] == (valid_id,)


def test_candidate_pool_cannot_smuggle_in_releases_the_current_policy_never_scored(golden) -> None:
    graph = observed_graph(golden)
    baseline = run_baseline(graph)
    collector_id = golden.collector_ids[0]
    baseline_ids = {row["id"] for row in baseline.recommendations[collector_id]}
    owned = set(graph.holdings[collector_id].owned)
    outside = next(release_id for release_id in golden.release_ids if release_id not in baseline_ids and release_id not in owned)

    with pytest.raises(ValueError, match="may only narrow the frozen baseline pool"):
        _normalize_candidate_pool(golden, graph, [collector_id], baseline.recommendations, {collector_id: [outside]})


def test_same_candidate_pool_is_used_when_rankings_have_ties() -> None:
    pool = {"u1": ["r2", "r1", "r2"]}
    policy = _current_policy({"u1": []}, pool)

    assert [row["release_id"] for row in policy.rankings["u1"]] == ["r1", "r2"]
    assert policy.version == BASELINE_VERSION


def test_metadata_similarity_is_namespaced_and_empty_safe() -> None:
    assert _metadata_similarity({"artist": {"artist:a"}}, {"artist": {"artist:a"}}) == 1.0
    assert _metadata_similarity({"artist": {"artist:a"}}, {"label": {"label:a"}}) == 0.0
    assert _metadata_similarity({}, {}) == 0.0


def test_unknown_and_sparse_contexts_are_reported_without_failing_replay(golden) -> None:
    collector_id = golden.collector_ids[0]
    graph = observed_graph(golden)
    sparse_graph = GoldenGraph(golden, {collector_id: CollectorHoldings(collector_id, (), frozenset())})
    profile, counts = _collection_profile(sparse_graph, collector_id, golden)
    missing_profile, missing_counts = _collection_profile(sparse_graph, "unknown-collector", golden)

    assert profile == {kind: set() for kind in ("artist", "label", "genre", "style")}
    assert all(not value for value in counts.values())
    assert missing_profile == profile
    assert missing_counts == counts
    # A replay over the full graph still works when an unknown collector pool is supplied.
    report = replay_discovery(golden, candidate_ids={collector_id: [], "unknown-collector": [golden.release_ids[0]]})
    assert report["candidate_pools"][collector_id] == []
    assert "unknown-collector" not in report["candidate_pools"]
    assert graph.holdings[collector_id].owned


def test_profiles_skip_unknown_collection_ids_and_report_both_sparse_failures(golden) -> None:
    short_id, blank_id = "short-user", "blank-user"
    graph = GoldenGraph(
        golden,
        {
            short_id: CollectorHoldings(short_id, ("missing-release",), frozenset()),
            blank_id: CollectorHoldings(blank_id, ("missing-1", "missing-2", "missing-3"), frozenset()),
        },
    )
    profile, counts = _collection_profile(graph, short_id, golden)
    failures = _sparse_context_failures(graph, golden, [short_id, blank_id])

    assert all(not values for values in profile.values())
    assert all(not values for values in counts.values())
    assert failures == [
        {"collector_id": short_id, "reason": "fewer_than_three_observed_releases", "observed_size": 1},
        {"collector_id": blank_id, "reason": "no_observed_metadata_context", "observed_size": 3},
    ]


def test_no_overlap_reasons_are_explicit_for_both_context_rankers(golden) -> None:
    collector_id = "empty-user"
    graph = GoldenGraph(golden, {collector_id: CollectorHoldings(collector_id, (), frozenset())})
    release_id = golden.release_ids[0]
    pools = {collector_id: [release_id]}
    metadata = _metadata_policy(golden, graph, pools)
    contextual = _candidate_policy(golden, graph, pools, familiarity_penalty=FAMILIARITY_PENALTY_DEFAULT)

    assert metadata.rankings[collector_id][0]["reasons"] == [{"code": "no_metadata_overlap", "source_ids": []}]
    assert contextual.rankings[collector_id][0]["reasons"] == [{"code": "no_context_overlap", "source_ids": []}]


def test_familiarity_control_endpoints_are_valid_and_reported(golden) -> None:
    no_penalty = replay_discovery(golden, familiarity_penalty=0.0)
    max_penalty = replay_discovery(golden, familiarity_penalty=1.0)

    assert no_penalty["controls"]["familiarity_penalty"] == 0.0
    assert max_penalty["controls"]["familiarity_penalty"] == 1.0
    assert no_penalty["candidate_pools"] == max_penalty["candidate_pools"]
    assert no_penalty["versions"]["catalog_context"] == max_penalty["versions"]["catalog_context"]
    with pytest.raises(ValueError, match="familiarity_penalty"):
        replay_discovery(golden, familiarity_penalty=1.01)
    with pytest.raises(ValueError, match="familiarity_penalty"):
        replay_discovery(golden, familiarity_penalty=-0.01)


def test_only_explicit_labels_are_judged_and_unlabeled_is_not_negative(golden) -> None:
    splits = split_golden_set(golden)
    explicit = {golden.collector_ids[0]: {golden.release_ids[0]: True}}
    labels, source = _checked_labels(splits, explicit)
    baseline = run_baseline(observed_graph(golden))
    report = replay_discovery(golden, checked_labels=explicit)
    first_collector = golden.collector_ids[0]

    assert source == "explicit-checked-labels"
    assert labels == explicit
    assert report["labels"]["source"] == source
    assert report["metrics"]["current"]["judged_labels"] == 1
    assert report["metrics"]["current"]["positive_labels"] == 1
    assert report["metrics"]["current"]["label_coverage"] in (0.0, 1.0)
    # The unlabeled releases are not included as negatives in precision denominators.
    assert report["metrics"]["current"]["judged_precision_at_k"]["10"] in (None, 0.0, 1.0)
    assert baseline.recommendations[first_collector]


def test_explicit_negative_is_counted_only_when_present(golden) -> None:
    collector_id = golden.collector_ids[0]
    first, second = golden.release_ids[:2]
    labels = {collector_id: {first: True, second: False}}
    report = replay_discovery(golden, checked_labels=labels)
    metric = report["metrics"]["catalog_context"]

    assert metric["judged_labels"] == 2
    assert metric["positive_labels"] == 1
    assert report["labels"]["missing_labels_are_negative"] is False


def test_empty_explicit_labels_return_pending_metrics(golden) -> None:
    report = replay_discovery(golden, checked_labels={})
    metrics = report["metrics"]["current"]

    assert metrics["judged_labels"] == 0
    assert metrics["positive_labels"] == 0
    assert metrics["label_coverage"] is None
    assert metrics["judged_precision_at_k"] == {"10": None, "25": None}
    assert metrics["annotation_coverage_at_k"] == {"10": 0.0, "25": 0.0}
    assert metrics["recall_at_k"] == {"10": None, "25": None}
    assert metrics["hit_rate_at_k"] == {"10": None, "25": None}


def test_metrics_separate_pool_coverage_ranked_coverage_and_judged_precision(golden) -> None:
    ids = list(golden.release_ids[:12])
    pool = {"u1": ids}
    policy = PolicyResult(
        "test-policy",
        {"u1": tuple({"release_id": release_id, "score": 1.0, "reasons": []} for release_id in ids)},
        {},
    )
    labels = {"u1": {ids[0]: True, ids[1]: False, golden.release_ids[15]: True}}

    metrics = _ranking_metrics(policy, pool, labels, golden)

    assert metrics["judged_labels"] == 3
    assert metrics["positive_labels"] == 2
    assert metrics["label_coverage"] == pytest.approx(2 / 3)
    assert metrics["annotation_coverage_at_k"] == {"10": pytest.approx(0.2), "25": pytest.approx(2 / 12)}
    assert metrics["judged_precision_at_k"] == {"10": 0.5, "25": 0.5}
    assert metrics["recall_at_k"] == {"10": 0.5, "25": 0.5}
    assert metrics["candidate_pool_coverage"] == pytest.approx(12 / len(golden.releases))
    assert metrics["recommendation_coverage_at_k"]["10"] == pytest.approx(10 / len(golden.releases))
    assert metrics["recommendation_coverage_at_k"]["25"] == pytest.approx(12 / len(golden.releases))
    assert metrics["distinct_artist_count_at_10"] >= 0
    assert metrics["distinct_label_count_at_10"] >= 0


def test_cli_is_reproducible_and_rejects_invalid_control(capsys) -> None:
    assert main(["--familiarity-penalty", str(FAMILIARITY_PENALTY_DEFAULT)]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["versions"]["catalog_context"] == DISCOVERY_REPLAY_VERSION
    assert main(["--familiarity-penalty", "2"]) == 2
    assert "familiarity_penalty" in capsys.readouterr().err
