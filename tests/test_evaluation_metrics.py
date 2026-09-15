"""The metrics measure what they claim to, and the committed snapshot still holds."""

from __future__ import annotations

import pytest

from api.evaluation.baseline import run_baseline
from api.evaluation.fixtures import load_golden_set
from api.evaluation.metrics import (
    K_VALUES,
    LARGEST_BUCKET,
    REPORTED_FAMILIES,
    SIZE_BUCKETS,
    catalogue_coverage,
    hits_at_k,
    precision_at_k,
    rank_stability,
    rarity_metrics,
    recall_at_k,
    recommendation_metrics,
    size_bucket,
    spearman,
)
from api.evaluation.report import METRICS_TOLERANCE, evaluate, load_expected_metrics, metrics_snapshot
from api.evaluation.split import observed_graph, split_golden_set


@pytest.fixture(scope="module")
def golden():
    return load_golden_set()


@pytest.fixture(scope="module")
def scored(golden):
    splits = split_golden_set(golden)
    run = run_baseline(observed_graph(golden))
    return run, splits


# ── Ranking primitives ──────────────────────────────────────────────


def test_precision_divides_by_k_not_by_the_ranking_length() -> None:
    assert precision_at_k(["a", "b", "c"], {"a"}, 10) == 0.1
    assert precision_at_k(["a", "b"], {"a", "b"}, 2) == 1.0


def test_precision_of_an_empty_or_nonpositive_k_is_zero() -> None:
    assert precision_at_k(["a"], {"a"}, 0) == 0.0
    assert precision_at_k([], {"a"}, 10) == 0.0


def test_recall_divides_by_the_held_out_count() -> None:
    assert recall_at_k(["a", "b", "c"], {"a", "z"}, 3) == 0.5
    assert recall_at_k(["a"], {"a"}, 1) == 1.0


def test_recall_without_anything_held_out_is_zero() -> None:
    assert recall_at_k(["a"], set(), 10) == 0.0


def test_only_the_top_k_counts() -> None:
    assert precision_at_k(["x", "a"], {"a"}, 1) == 0.0
    assert recall_at_k(["x", "a"], {"a"}, 1) == 0.0
    assert hits_at_k(["x", "a"], {"a"}, 2) == 1


def test_coverage_is_the_union_over_the_catalog() -> None:
    assert catalogue_coverage([["a", "b"], ["b", "c"]], 4) == 0.75
    assert catalogue_coverage([], 4) == 0.0
    assert catalogue_coverage([["a"]], 0) == 0.0


# ── Rank correlation ────────────────────────────────────────────────


def test_spearman_is_one_for_an_identical_ranking() -> None:
    assert spearman([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]) == 1.0


def test_spearman_is_minus_one_for_a_reversed_ranking() -> None:
    assert spearman([1.0, 2.0, 3.0], [3.0, 2.0, 1.0]) == -1.0


def test_spearman_ranks_monotone_transforms_identically() -> None:
    assert spearman([1.0, 2.0, 3.0], [10.0, 200.0, 3000.0]) == 1.0


def test_spearman_averages_tied_ranks() -> None:
    assert spearman([1.0, 1.0, 2.0], [1.0, 1.0, 2.0]) == 1.0
    assert 0.0 < spearman([1.0, 1.0, 2.0], [5.0, 6.0, 7.0]) < 1.0


def test_spearman_of_a_constant_sequence_is_defined_rather_than_undefined() -> None:
    assert spearman([1.0, 1.0], [1.0, 1.0]) == 1.0
    assert spearman([1.0, 1.0], [1.0, 2.0]) == 0.0


def test_spearman_of_empty_input_is_zero() -> None:
    assert spearman([], []) == 0.0


def test_spearman_rejects_mismatched_lengths() -> None:
    with pytest.raises(ValueError, match="same length"):
        spearman([1.0], [1.0, 2.0])


# ── Buckets ─────────────────────────────────────────────────────────


def test_size_buckets_partition_the_range() -> None:
    assert size_bucket(0) == SIZE_BUCKETS[0][0]
    assert size_bucket(SIZE_BUCKETS[0][1] - 1) == SIZE_BUCKETS[0][0]
    assert size_bucket(SIZE_BUCKETS[0][1]) == SIZE_BUCKETS[1][0]
    assert size_bucket(SIZE_BUCKETS[1][1]) == LARGEST_BUCKET
    assert size_bucket(10_000) == LARGEST_BUCKET


# ── Aggregations over the real run ──────────────────────────────────


def test_recommendation_metrics_report_every_block(scored, golden) -> None:
    run, splits = scored
    metrics = recommendation_metrics(run, splits, golden)
    assert metrics["collectors"] == len(golden.collectors)
    assert metrics["held_out_total"] == sum(split.held_out_size for split in splits.values())
    for k in K_VALUES:
        assert 0.0 <= metrics["overall"][f"precision_at_{k}"] <= 1.0
        assert 0.0 <= metrics["overall"][f"recall_at_{k}"] <= 1.0
    assert set(metrics["by_format"]) == set(REPORTED_FAMILIES)
    assert set(metrics["by_collector_size"]) <= {label for label, _bound in SIZE_BUCKETS} | {LARGEST_BUCKET}


def test_the_baseline_actually_ranks_held_out_releases(scored, golden) -> None:
    """A baseline that never surfaces a later acquisition would make every comparison vacuous."""
    run, splits = scored
    metrics = recommendation_metrics(run, splits, golden)
    assert metrics["overall"]["precision_at_10"] > 0.0
    assert metrics["overall"]["recall_at_25"] > 0.0
    assert metrics["overall"]["hit_rate"] > 0.5


def test_recall_at_25_is_at_least_recall_at_10(scored, golden) -> None:
    run, splits = scored
    metrics = recommendation_metrics(run, splits, golden)
    assert metrics["overall"]["recall_at_25"] >= metrics["overall"]["recall_at_10"]


def test_coverage_is_a_share_of_the_catalog(scored, golden) -> None:
    run, splits = scored
    coverage = recommendation_metrics(run, splits, golden)["coverage"]
    assert coverage["catalog_releases"] == len(golden.releases)
    assert 0.0 < coverage["catalogue_coverage"] <= 1.0
    assert coverage["recommended_releases"] <= coverage["catalog_releases"]


def test_the_format_breakdown_partitions_the_recommendations(scored, golden) -> None:
    run, splits = scored
    by_format = recommendation_metrics(run, splits, golden)["by_format"]
    assert abs(sum(block["recommended_share"] for block in by_format.values()) - 1.0) < 1e-9
    assert abs(sum(block["held_out_share"] for block in by_format.values()) - 1.0) < 1e-9
    assert sum(block["catalog_releases"] for block in by_format.values()) == len(golden.releases)


def test_every_format_is_recommended_and_scored(scored, golden) -> None:
    run, splits = scored
    for family, block in recommendation_metrics(run, splits, golden)["by_format"].items():
        assert block["recommended"] > 0, family
        assert block["held_out"] > 0, family
        assert 0.0 <= block["recall_at_25"] <= 1.0


def test_the_collector_size_breakdown_covers_every_collector(scored, golden) -> None:
    run, splits = scored
    by_size = recommendation_metrics(run, splits, golden)["by_collector_size"]
    assert sum(block["collectors"] for block in by_size.values()) == len(golden.collectors)
    assert len(by_size) > 1, "every collector fell in one bucket; the breakdown says nothing"


def test_rarity_metrics_summarise_the_whole_catalog(scored, golden) -> None:
    run, _splits = scored
    rarity = rarity_metrics(run)
    assert rarity["releases"] == len(golden.releases)
    assert sum(rarity["tier_distribution"].values()) == len(golden.releases)
    assert rarity["score"]["min"] <= rarity["score"]["mean"] <= rarity["score"]["max"]
    assert rarity["grooved_releases"] == 40


def test_the_tier_distribution_is_broken_out_per_format(scored) -> None:
    run, _splits = scored
    by_format = rarity_metrics(run)["tier_distribution_by_format"]
    assert set(by_format) == set(REPORTED_FAMILIES)
    for family, counts in by_format.items():
        assert sum(counts.values()) == 40, family


def test_rank_stability_is_perfect_for_a_deterministic_baseline(golden) -> None:
    graph = observed_graph(golden)
    stability = rank_stability(run_baseline(graph), run_baseline(graph))
    assert stability["spearman"] == 1.0
    assert stability["identical"] is True
    assert stability["releases"] == len(golden.releases)


def test_rank_stability_falls_when_the_ranking_moves(golden) -> None:
    graph = observed_graph(golden)
    first = run_baseline(graph)
    shifted = run_baseline(graph, current_year=2200)
    stability = rank_stability(first, shifted)
    assert stability["identical"] is False
    assert stability["spearman"] < 1.0


# ── The committed snapshot ──────────────────────────────────────────


def _walk(prefix: str, value: object):
    if isinstance(value, dict):
        for key, nested in value.items():
            yield from _walk(f"{prefix}.{key}", nested)
    else:
        yield prefix, value


def test_a_fresh_run_matches_the_committed_expected_metrics() -> None:
    report, _run = evaluate()
    fresh = dict(_walk("", metrics_snapshot(report)))
    expected = dict(_walk("", load_expected_metrics()))

    assert set(fresh) == set(expected), (
        "the metrics shape changed; regenerate the snapshot with `uv run python -m api.evaluation.report --update-expected` and commit it"
    )
    for path, value in expected.items():
        actual = fresh[path]
        if isinstance(value, float) or isinstance(actual, float):
            assert abs(float(actual) - float(value)) <= METRICS_TOLERANCE, (
                f"{path}: {actual} != {value}. The harness is deterministic, so this is a real "
                "baseline change: bump BASELINE_VERSION and regenerate the snapshot with "
                "`uv run python -m api.evaluation.report --update-expected`"
            )
        else:
            assert actual == value, f"{path}: {actual} != {value}"


def test_the_snapshot_records_the_baseline_and_fixture_it_was_taken_from() -> None:
    from api.evaluation.baseline import BASELINE_CURRENT_YEAR, BASELINE_VERSION
    from api.evaluation.split import SPLIT_CUT

    expected = load_expected_metrics()
    assert expected["baseline_version"] == BASELINE_VERSION
    assert expected["current_year"] == BASELINE_CURRENT_YEAR
    assert expected["split_cut"] == SPLIT_CUT.isoformat()
    assert expected["golden_set_generator_version"] == load_golden_set().generator_version


def test_the_snapshot_carries_no_timestamp_or_path() -> None:
    """Anything that varies between two identical runs must stay out of the committed file."""
    flattened = dict(_walk("", load_expected_metrics()))
    assert not any("generated_at" in path or "report" in path for path in flattened)


def test_metrics_over_no_collectors_are_zero_rather_than_undefined(scored, golden) -> None:
    """A model scored before any collector qualifies must read as zero, not divide by zero."""
    run, _splits = scored
    metrics = recommendation_metrics(run, {}, golden)
    assert metrics["collectors"] == 0
    assert metrics["held_out_total"] == 0
    assert metrics["overall"] == {f"{name}_at_{k}": 0.0 for k in K_VALUES for name in ("precision", "recall")} | {"hit_rate": 0.0}
    assert metrics["coverage"]["catalogue_coverage"] == 0.0
    assert metrics["by_collector_size"] == {}
    for block in metrics["by_format"].values():
        assert block["recommended_share"] == 0.0
        assert block["held_out_share"] == 0.0
        assert block["recall_at_25"] == 0.0
