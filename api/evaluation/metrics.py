"""Metrics for the time-split evaluation: ranking quality, coverage, rarity, stability.

Everything here is a pure function of a :class:`~api.evaluation.baseline.BaselineRun` and the
splits it was produced under, so a model's run drops into the same functions the baseline's
run goes through. That is the point: the comparison is only meaningful if both sides are
measured by identical code.

Two averaging conventions are used, and which one applies is stated per metric:

* **Macro** -- the mean of per-collector values. Every collector counts once, so a
  15-release collector is not drowned out by a 40-release one. Used for the headline
  precision and recall, and for the collector-size breakdown.
* **Micro** -- hits pooled across collectors, then divided once. Used for the per-format
  breakdown, where a per-collector denominator would be zero whenever a collector happened
  to hold out nothing on that format.
"""

from __future__ import annotations

import math
from collections.abc import Collection, Mapping, Sequence
from typing import Any, Final

from api.evaluation.baseline import BaselineRun
from api.evaluation.fixtures import GoldenSet
from api.evaluation.split import TimeSplit


#: The cut-offs the headline ranking metrics are reported at.
K_VALUES: Final[tuple[int, ...]] = (10, 25)

#: Observed-collection size buckets, as ``(label, exclusive upper bound)``. The last bucket
#: catches everything larger.
SIZE_BUCKETS: Final[tuple[tuple[str, int], ...]] = (("small", 15), ("medium", 23))
LARGEST_BUCKET: Final[str] = "large"

#: The families the golden set is balanced across, and therefore the ones broken out.
REPORTED_FAMILIES: Final[tuple[str, ...]] = ("vinyl", "optical", "tape")


# ── Ranking primitives ──────────────────────────────────────────────


def precision_at_k(ranked: Sequence[str], relevant: Collection[str], k: int) -> float:
    """Share of the top ``k`` ranked items that were later acquired.

    The denominator is ``k`` itself, not the length of the ranking. A recommender that
    returns three items and gets one right has not achieved precision@10 of 0.33.
    """
    if k <= 0:
        return 0.0
    relevant_set = set(relevant)
    return sum(1 for release_id in ranked[:k] if release_id in relevant_set) / k


def recall_at_k(ranked: Sequence[str], relevant: Collection[str], k: int) -> float:
    """Share of the later acquisitions that appear in the top ``k``."""
    relevant_set = set(relevant)
    if not relevant_set:
        return 0.0
    return sum(1 for release_id in ranked[:k] if release_id in relevant_set) / len(relevant_set)


def hits_at_k(ranked: Sequence[str], relevant: Collection[str], k: int) -> int:
    """How many of the top ``k`` were later acquired."""
    relevant_set = set(relevant)
    return sum(1 for release_id in ranked[:k] if release_id in relevant_set)


def catalogue_coverage(rankings: Collection[Sequence[str]], catalog_size: int) -> float:
    """Share of the catalog that appears in at least one ranking.

    A recommender that scores well by showing everyone the same twenty releases is a
    different product from one that scores the same while reaching the whole catalog, and
    precision alone cannot tell them apart.
    """
    if catalog_size <= 0:
        return 0.0
    return len({release_id for ranking in rankings for release_id in ranking}) / catalog_size


def _mean(values: Sequence[float]) -> float:
    """Arithmetic mean, ``0.0`` for an empty sequence, summed with ``fsum`` for stability."""
    if not values:
        return 0.0
    return math.fsum(values) / len(values)


# ── Rank correlation ────────────────────────────────────────────────


def _average_ranks(values: Sequence[float]) -> list[float]:
    """Return 1-based ranks, tied values sharing their average rank."""
    order = sorted(range(len(values)), key=lambda index: values[index])
    ranks = [0.0] * len(values)
    start = 0
    while start < len(order):
        end = start
        while end + 1 < len(order) and values[order[end + 1]] == values[order[start]]:
            end += 1
        shared = (start + end) / 2 + 1
        for position in range(start, end + 1):
            ranks[order[position]] = shared
        start = end + 1
    return ranks


def spearman(first: Sequence[float], second: Sequence[float]) -> float:
    """Spearman rank correlation between two equal-length score sequences.

    Ties share an average rank, so this is Pearson correlation on the ranks rather than the
    six-d-squared shortcut, which is wrong in the presence of ties.

    Returns:
        The correlation, clamped into ``[-1, 1]``: the unclamped quotient can land a couple of
        ulps outside it for a perfectly correlated pair, and a stability metric that reports
        1.0000000000000002 is a metric nobody can assert on. When one sequence has no rank
        variance at all the
        correlation is undefined; ``1.0`` is returned if the two sequences are identical and
        ``0.0`` otherwise, so a degenerate input cannot masquerade as disagreement.

    Raises:
        ValueError: If the sequences differ in length.
    """
    if len(first) != len(second):
        raise ValueError("spearman needs two sequences of the same length")
    if not first:
        return 0.0
    ranks_a = _average_ranks(first)
    ranks_b = _average_ranks(second)
    mean_a = _mean(ranks_a)
    mean_b = _mean(ranks_b)
    covariance = math.fsum((a - mean_a) * (b - mean_b) for a, b in zip(ranks_a, ranks_b, strict=True))
    variance_a = math.fsum((a - mean_a) ** 2 for a in ranks_a)
    variance_b = math.fsum((b - mean_b) ** 2 for b in ranks_b)
    if variance_a == 0.0 or variance_b == 0.0:
        return 1.0 if list(first) == list(second) else 0.0
    # One sqrt of the product, not a product of two sqrts: sqrt(2) * sqrt(2) is not 2.0, and
    # that single ulp is the difference between a perfect correlation reading 1.0 and
    # 0.9999999999999998 -- which no committed snapshot can be asserted against cleanly.
    return max(-1.0, min(1.0, covariance / math.sqrt(variance_a * variance_b)))


# ── Aggregations ────────────────────────────────────────────────────


def size_bucket(observed_size: int) -> str:
    """Return the collector-size bucket an observed collection of this size falls in."""
    for label, upper in SIZE_BUCKETS:
        if observed_size < upper:
            return label
    return LARGEST_BUCKET


def _ranking_block(rankings: Sequence[tuple[Sequence[str], Collection[str]]]) -> dict[str, float]:
    """Macro-averaged precision and recall at every ``k``, plus the hit rate."""
    block: dict[str, float] = {}
    for k in K_VALUES:
        block[f"precision_at_{k}"] = _mean([precision_at_k(ranked, relevant, k) for ranked, relevant in rankings])
        block[f"recall_at_{k}"] = _mean([recall_at_k(ranked, relevant, k) for ranked, relevant in rankings])
    largest = max(K_VALUES)
    block["hit_rate"] = _mean([1.0 if hits_at_k(ranked, relevant, largest) else 0.0 for ranked, relevant in rankings])
    return block


def recommendation_metrics(run: BaselineRun, splits: Mapping[str, TimeSplit], golden: GoldenSet) -> dict[str, Any]:
    """Score every collector's ranking against their held-out acquisitions.

    Args:
        run: The baseline (or model) run to score.
        splits: The time splits the run was produced under.
        golden: The fixture, for catalog size and per-release families.

    Returns:
        The headline block, catalogue coverage, and the per-format and per-collector-size
        breakdowns.
    """
    collector_ids = sorted(splits)
    rankings = [(run.ranked_ids(collector_id), splits[collector_id].held_out) for collector_id in collector_ids]
    largest = max(K_VALUES)

    by_size: dict[str, list[tuple[Sequence[str], Collection[str]]]] = {}
    for collector_id in collector_ids:
        bucket = size_bucket(splits[collector_id].observed_size)
        by_size.setdefault(bucket, []).append((run.ranked_ids(collector_id), splits[collector_id].held_out))

    families_of_release = {release_id: release.families for release_id, release in golden.releases.items()}
    recommended_total = sum(len(ranked[:largest]) for ranked, _relevant in rankings)
    held_out_total = sum(len(split.held_out) for split in splits.values())

    by_format: dict[str, dict[str, float]] = {}
    for family in REPORTED_FAMILIES:
        in_family = {release_id for release_id, families in families_of_release.items() if family in families}
        recommended = sum(1 for ranked, _relevant in rankings for release_id in ranked[:largest] if release_id in in_family)
        held_out = sum(1 for _ranked, relevant in rankings for release_id in relevant if release_id in in_family)
        hits = sum(1 for ranked, relevant in rankings for release_id in ranked[:largest] if release_id in in_family and release_id in set(relevant))
        by_format[family] = {
            "catalog_releases": len(in_family),
            "recommended": recommended,
            "recommended_share": recommended / recommended_total if recommended_total else 0.0,
            "held_out": held_out,
            "held_out_share": held_out / held_out_total if held_out_total else 0.0,
            f"hits_at_{largest}": hits,
            f"recall_at_{largest}": hits / held_out if held_out else 0.0,
        }

    return {
        "collectors": len(collector_ids),
        "held_out_total": held_out_total,
        "overall": _ranking_block(rankings),
        "coverage": {
            "catalog_releases": len(golden.releases),
            "recommended_releases": len({release_id for ranked, _relevant in rankings for release_id in ranked}),
            "catalogue_coverage": catalogue_coverage([ranked for ranked, _relevant in rankings], len(golden.releases)),
        },
        "by_format": by_format,
        "by_collector_size": {bucket: {"collectors": len(entries), **_ranking_block(entries)} for bucket, entries in sorted(by_size.items())},
    }


def rarity_metrics(run: BaselineRun) -> dict[str, Any]:
    """Summarise the rarity pass: score spread and tier distribution, overall and per format."""
    results = [run.rarity[release_id] for release_id in sorted(run.rarity)]
    scores = [result.score for result in results]

    tiers: dict[str, int] = {}
    by_format: dict[str, dict[str, int]] = {family: {} for family in REPORTED_FAMILIES}
    for result in results:
        tiers[result.tier] = tiers.get(result.tier, 0) + 1
        for family in result.families:
            if family in by_format:
                by_format[family][result.tier] = by_format[family].get(result.tier, 0) + 1

    return {
        "releases": len(results),
        "score": {
            "min": min(scores) if scores else 0.0,
            "max": max(scores) if scores else 0.0,
            "mean": _mean(scores),
        },
        "tier_distribution": dict(sorted(tiers.items())),
        "tier_distribution_by_format": {family: dict(sorted(counts.items())) for family, counts in by_format.items()},
        "grooved_releases": sum(1 for result in results if result.family_signals),
    }


def rank_stability(first: BaselineRun, second: BaselineRun) -> dict[str, Any]:
    """Spearman correlation between two runs' rarity rankings over the same releases.

    A deterministic baseline scores 1.0 here by construction, which is exactly what makes the
    metric worth reporting: anything below 1.0 means the run is not reproducible, and no
    comparison against it means anything.
    """
    release_ids = sorted(set(first.rarity) & set(second.rarity))
    correlation = spearman(
        [first.rarity[release_id].score for release_id in release_ids], [second.rarity[release_id].score for release_id in release_ids]
    )
    return {
        "releases": len(release_ids),
        "spearman": correlation,
        "identical": all(first.rarity[release_id].score == second.rarity[release_id].score for release_id in release_ids),
    }
