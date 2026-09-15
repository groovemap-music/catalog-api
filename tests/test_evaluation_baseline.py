"""The baseline is frozen, versioned, and reproducible."""

from __future__ import annotations

import pytest

from api.evaluation.baseline import (
    BASELINE_CURRENT_YEAR,
    BASELINE_VERSION,
    RECOMMENDATION_LIMIT,
    BaselineRun,
    frozen_weight_tables,
    live_weight_tables,
    run_baseline,
)
from api.evaluation.fixtures import load_golden_set
from api.evaluation.graph import CollectorHoldings, GoldenGraph
from api.rarity import RARITY_TIERS


@pytest.fixture(scope="module")
def golden():
    return load_golden_set()


@pytest.fixture(scope="module")
def run(golden) -> BaselineRun:
    return run_baseline(golden)


# ── The freeze ──────────────────────────────────────────────────────


def test_the_baseline_version_is_pinned() -> None:
    assert BASELINE_VERSION == "heuristics-2026-09"


def test_the_frozen_tables_cover_every_live_table() -> None:
    assert set(frozen_weight_tables()) == set(live_weight_tables())


@pytest.mark.parametrize("name", sorted(frozen_weight_tables()))
def test_live_weights_still_equal_the_frozen_baseline(name: str) -> None:
    frozen = frozen_weight_tables()[name]
    live = live_weight_tables()[name]
    assert live == frozen, (
        f"{name} no longer matches its frozen copy in api/evaluation/baseline.py. "
        "Every number in tests/fixtures/golden/expected-metrics.json is computed from these "
        "weights, so changing one is a baseline change: bump BASELINE_VERSION, update the "
        "frozen copy, regenerate the snapshot with `just evaluate`, and commit all three "
        "together."
    )


def test_the_core_weights_leave_room_for_the_family_extension() -> None:
    core = frozen_weight_tables()["api.rarity.core.CORE_SIGNAL_WEIGHTS"]
    grooved = frozen_weight_tables()["api.rarity.families.module_weights()"]["grooved"]
    assert abs(sum(core.values()) + sum(grooved.values()) - 1.0) < 1e-12


def test_the_reference_year_is_frozen_rather_than_read_from_the_clock(run: BaselineRun) -> None:
    assert run.current_year == BASELINE_CURRENT_YEAR == 2026


# ── The run ─────────────────────────────────────────────────────────


def test_two_runs_over_the_same_fixture_are_equal(golden) -> None:
    assert run_baseline(golden) == run_baseline(golden)


def test_a_graph_and_its_golden_set_produce_the_same_run(golden) -> None:
    assert run_baseline(GoldenGraph(golden)) == run_baseline(golden)


def test_every_collector_is_ranked_to_the_limit(run: BaselineRun, golden) -> None:
    assert set(run.recommendations) == set(golden.collector_ids)
    for collector_id, rows in run.recommendations.items():
        assert len(rows) == RECOMMENDATION_LIMIT, collector_id
        assert [row["score"] for row in rows] == sorted((row["score"] for row in rows), reverse=True)


def test_recommendations_never_include_what_the_collector_already_holds(run: BaselineRun, golden) -> None:
    for collector_id, rows in run.recommendations.items():
        owned = set(golden.collectors[collector_id].release_ids)
        assert not {row["id"] for row in rows} & owned


def test_ranked_ids_are_unique_and_real(run: BaselineRun, golden) -> None:
    for collector_id in golden.collector_ids:
        ranked = run.ranked_ids(collector_id)
        assert len(set(ranked)) == len(ranked)
        assert set(ranked) <= set(golden.releases)


def test_ranked_ids_are_empty_for_an_unknown_collector(run: BaselineRun) -> None:
    assert run.ranked_ids("nope") == ()


def test_every_recommendation_names_the_signals_that_produced_it(run: BaselineRun) -> None:
    for rows in run.recommendations.values():
        for row in rows:
            assert row["reasons"]
            assert all(reason.split(":")[0] in {"artist", "label", "blind_spot"} for reason in row["reasons"])


def test_discoveries_and_similar_artists_are_produced_for_every_collector(run: BaselineRun, golden) -> None:
    assert set(run.discoveries) == set(golden.collector_ids)
    assert set(run.similar_artists) == set(golden.collector_ids)
    assert all(rows for rows in run.discoveries.values()), "a collector produced no discoveries"
    assert all(rows for rows in run.similar_artists.values()), "a collector produced no similar artists"


# ── Rarity ──────────────────────────────────────────────────────────


def test_every_release_is_scored(run: BaselineRun, golden) -> None:
    assert set(run.rarity) == set(golden.releases)


def test_rarity_scores_and_tiers_are_well_formed(run: BaselineRun) -> None:
    tiers = {tier for _threshold, tier in RARITY_TIERS}
    for result in run.rarity.values():
        assert 0.0 <= result.score <= 100.0
        assert result.tier in tiers
        assert 0.0 <= result.hidden_gem_score <= result.score + 1e-9
        assert result.signals
        assert result.format_rarity > 0.0


def test_only_grooved_releases_carry_a_pressing_signal(run: BaselineRun) -> None:
    grooved = 0
    for result in run.rarity.values():
        if "vinyl" in result.families:
            grooved += 1
            assert result.family_signals["grooved"]["pressing_scarcity"] > 0.0
            assert "pressing_scarcity" in result.signals
        else:
            assert result.family_signals == {}
            assert "pressing_scarcity" not in result.signals
    assert grooved == 40


def test_the_fixture_produces_a_spread_of_tiers(run: BaselineRun) -> None:
    assert len({result.tier for result in run.rarity.values()}) >= 3


def test_a_later_reference_year_raises_temporal_scarcity(golden) -> None:
    older = run_baseline(golden, current_year=BASELINE_CURRENT_YEAR)
    newer = run_baseline(golden, current_year=BASELINE_CURRENT_YEAR + 20)
    assert newer != older
    raised = [
        newer.rarity[release_id].signals["temporal_scarcity"] > older.rarity[release_id].signals["temporal_scarcity"]
        for release_id in golden.release_ids
    ]
    assert any(raised), "no release aged"


# ── Degenerate inputs ───────────────────────────────────────────────


def test_a_collector_holding_nothing_is_ranked_empty(golden) -> None:
    graph = GoldenGraph(golden, {"empty": CollectorHoldings("empty", (), frozenset())})
    run = run_baseline(graph)
    assert run.recommendations["empty"] == ()
    assert run.discoveries["empty"] == ()
    assert run.similar_artists["empty"] == ()
    assert len(run.rarity) == len(golden.releases), "rarity is a catalog property, not a collector one"
