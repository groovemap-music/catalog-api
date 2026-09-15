"""The time split holds out later acquisitions and keeps them out of the graph."""

from __future__ import annotations

from datetime import date

import pytest

from api.evaluation.fixtures import Acquisition, Collector, load_golden_set
from api.evaluation.split import (
    SPLIT_CUT,
    held_out_by_collector,
    observed_graph,
    observed_holdings,
    split_collector,
    split_golden_set,
)


@pytest.fixture(scope="module")
def golden():
    return load_golden_set()


@pytest.fixture(scope="module")
def splits(golden):
    return split_golden_set(golden)


def test_the_cut_is_the_documented_date() -> None:
    assert date(2023, 1, 1) == SPLIT_CUT


def test_the_cut_matches_the_one_the_generator_recorded(golden) -> None:
    assert golden.split_cut == SPLIT_CUT


def test_every_acquisition_lands_on_exactly_one_side(golden, splits) -> None:
    for collector_id, split in splits.items():
        collector = golden.collectors[collector_id]
        assert split.observed_size + split.held_out_size == len(collector.collected)
        assert not set(split.observed) & set(split.held_out)
        assert set(split.observed) | set(split.held_out) == set(collector.release_ids)


def test_observed_is_strictly_before_the_cut_and_held_out_is_not(golden, splits) -> None:
    for collector_id, split in splits.items():
        dates = {item.release_id: item.date_added for item in golden.collectors[collector_id].collected}
        assert all(dates[release_id] < SPLIT_CUT for release_id in split.observed)
        assert all(dates[release_id] >= SPLIT_CUT for release_id in split.held_out)


def test_both_sides_are_non_empty_for_every_collector(splits) -> None:
    assert all(split.observed_size > 0 and split.held_out_size > 0 for split in splits.values())


def test_a_boundary_acquisition_is_held_out_not_observed() -> None:
    """The cut is inclusive on the held-out side, so a release added exactly on it is future."""
    collector = Collector(
        id="u",
        name="U",
        taste_genres=(),
        collected=(
            Acquisition("before", date(2022, 12, 31)),
            Acquisition("on", SPLIT_CUT),
            Acquisition("after", date(2023, 1, 2)),
        ),
        wants=frozenset(),
    )
    split = split_collector(collector)
    assert split.observed == ("before",)
    assert split.held_out == ("on", "after")


def test_a_different_cut_moves_the_boundary() -> None:
    collector = Collector(
        id="u",
        name="U",
        taste_genres=(),
        collected=(Acquisition("a", date(2019, 5, 1)), Acquisition("b", date(2021, 5, 1))),
        wants=frozenset(),
    )
    assert split_collector(collector, date(2020, 1, 1)).held_out == ("b",)
    assert split_collector(collector, date(2018, 1, 1)).observed == ()


def test_observed_holdings_carry_the_wantlist_through(golden) -> None:
    holdings = observed_holdings(golden)
    for collector_id, holding in holdings.items():
        assert holding.wants == golden.collectors[collector_id].wants


def test_the_observed_graph_cannot_see_a_held_out_acquisition(golden, splits) -> None:
    graph = observed_graph(golden)
    for collector_id, split in splits.items():
        assert graph.holdings[collector_id].owned == split.observed
        assert not set(graph.holdings[collector_id].owned) & set(split.held_out)


def test_hiding_later_acquisitions_lowers_the_collector_counts(golden) -> None:
    full = observed_graph(golden, date(2030, 1, 1))
    observed = observed_graph(golden)
    full_total = sum(full.collector_counts(golden.release_ids).values())
    observed_total = sum(observed.collector_counts(golden.release_ids).values())
    assert observed_total < full_total
    assert observed_total == sum(len(holding.owned) for holding in observed.holdings.values())


def test_hiding_later_acquisitions_lowers_node_degree(golden, splits) -> None:
    full = observed_graph(golden, date(2030, 1, 1))
    observed = observed_graph(golden)
    held_out = {release_id for split in splits.values() for release_id in split.held_out}
    assert any(observed.release_degree(release_id) < full.release_degree(release_id) for release_id in sorted(held_out))


def test_held_out_by_collector_is_a_membership_index(golden, splits) -> None:
    index = held_out_by_collector(splits)
    assert set(index) == set(golden.collector_ids)
    for collector_id, releases in index.items():
        assert releases == frozenset(splits[collector_id].held_out)
