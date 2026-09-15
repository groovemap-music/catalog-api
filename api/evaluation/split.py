"""The time-split protocol: rank from the past, score against the future.

Scoring a recommender on a collection it can already see measures memorisation. The split
here is by acquisition date, not at random: for each collector, everything added before
:data:`SPLIT_CUT` is the observed collection the baseline is allowed to see, and everything
added on or after it is held out and used only to score the ranking.

The cut is enforced by building the graph, not by filtering afterwards. A graph built from the
pre-cut acquisitions alone cannot leak a later purchase into a candidate list, an obscurity
count, or a node degree -- three places a post-hoc filter would miss.

Wantlists are not split. A want carries no date, and the golden set only ever draws a
wantlist from releases the collector never acquires, so nothing held out can reach the
baseline through one. Their only effect is the exclusion the real queries apply.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Final

from api.evaluation.fixtures import Collector, GoldenSet
from api.evaluation.graph import CollectorHoldings, GoldenGraph


#: Acquisitions on or after this date are held out. Moving it invalidates every committed
#: metric, so it is a baseline change like any weight change.
SPLIT_CUT: Final[date] = date(2023, 1, 1)


@dataclass(frozen=True)
class TimeSplit:
    """One collector's collection, divided at the cut."""

    collector_id: str
    cut: date
    observed: tuple[str, ...]
    held_out: tuple[str, ...]

    @property
    def observed_size(self) -> int:
        """How many releases the baseline was allowed to see."""
        return len(self.observed)

    @property
    def held_out_size(self) -> int:
        """How many later acquisitions the ranking is scored against."""
        return len(self.held_out)


def split_collector(collector: Collector, cut: date = SPLIT_CUT) -> TimeSplit:
    """Divide one collector's acquisitions at ``cut``, preserving acquisition order."""
    observed = tuple(item.release_id for item in collector.collected if item.date_added < cut)
    held_out = tuple(item.release_id for item in collector.collected if item.date_added >= cut)
    return TimeSplit(collector_id=collector.id, cut=cut, observed=observed, held_out=held_out)


def split_golden_set(golden: GoldenSet, cut: date = SPLIT_CUT) -> dict[str, TimeSplit]:
    """Divide every collector in the fixture at ``cut``."""
    return {collector_id: split_collector(golden.collectors[collector_id], cut) for collector_id in golden.collector_ids}


def observed_holdings(golden: GoldenSet, cut: date = SPLIT_CUT) -> dict[str, CollectorHoldings]:
    """Return the holdings a graph may see: pre-cut acquisitions, plus the wantlist."""
    return {
        split.collector_id: CollectorHoldings(
            collector_id=split.collector_id,
            owned=split.observed,
            wants=golden.collectors[split.collector_id].wants,
        )
        for split in split_golden_set(golden, cut).values()
    }


def observed_graph(golden: GoldenSet, cut: date = SPLIT_CUT) -> GoldenGraph:
    """Build the graph the baseline is scored from: pre-cut acquisitions only."""
    return GoldenGraph(golden, observed_holdings(golden, cut))


def held_out_by_collector(splits: Mapping[str, TimeSplit]) -> dict[str, frozenset[str]]:
    """Return each collector's held-out release ids as a set, for membership scoring."""
    return {collector_id: frozenset(split.held_out) for collector_id, split in splits.items()}
