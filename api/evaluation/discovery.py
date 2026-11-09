"""Replay comparable catalog-context discovery rankings on one frozen candidate pool.

This harness is offline and synthetic. It reuses the existing frozen recommendation run and
GoldenGraph rather than changing the production recommender. Only explicitly supplied relevance
labels are scored; an unlabeled candidate is unknown, never a negative example.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from api.evaluation.baseline import BASELINE_VERSION, RECOMMENDATION_LIMIT, run_baseline
from api.evaluation.fixtures import GoldenSet, load_golden_set
from api.evaluation.graph import GoldenGraph
from api.evaluation.split import SPLIT_CUT, observed_graph, split_golden_set


DISCOVERY_REPLAY_VERSION: Final[str] = "catalog-context-discovery-v1"
METADATA_NEIGHBOR_VERSION: Final[str] = "metadata-nearest-neighbor-v1"
FAMILIARITY_PENALTY_DEFAULT: Final[float] = 0.35
FAMILIARITY_PENALTY_MIN: Final[float] = 0.0
FAMILIARITY_PENALTY_MAX: Final[float] = 1.0
SPARSE_COLLECTION_SIZE: Final[int] = 3
RANK_CUTOFFS: Final[tuple[int, ...]] = (10, 25)


@dataclass(frozen=True)
class PolicyResult:
    """Policy version, ordered rows, and control metadata for one collector."""

    version: str
    rankings: Mapping[str, tuple[dict[str, Any], ...]]
    controls: Mapping[str, Any]


def _features(release: Any) -> dict[str, set[str]]:
    """Namespaced metadata features; IDs from different types never imply equivalence."""
    return {
        "artist": {f"artist:{artist_id}" for artist_id in release.artist_ids},
        "label": {f"label:{release.label_id}"},
        "genre": {f"genre:{genre}" for genre in release.genres},
        "style": {f"style:{style}" for style in release.styles},
    }


def _feature_set(features: Mapping[str, set[str]]) -> set[str]:
    return set().union(*features.values()) if features else set()


def _metadata_similarity(candidate: Mapping[str, set[str]], profile: Mapping[str, set[str]]) -> float:
    left, right = _feature_set(candidate), _feature_set(profile)
    return len(left & right) / len(left | right) if left or right else 0.0


def _row(release_id: str, golden: GoldenSet, score: float, reasons: list[dict[str, Any]]) -> dict[str, Any]:
    release = golden.releases[release_id]
    artist_names = [golden.artists[artist_id].name for artist_id in release.artist_ids if artist_id in golden.artists]
    return {
        "release_id": release_id,
        "title": release.title,
        "artist_names": artist_names,
        "label_name": golden.labels[release.label_id].name,
        "score": round(float(score), 8),
        "reasons": reasons,
    }


def _collection_profile(graph: GoldenGraph, collector_id: str, golden: GoldenSet) -> tuple[dict[str, set[str]], dict[str, Counter[str]]]:
    features: dict[str, set[str]] = {kind: set() for kind in ("artist", "label", "genre", "style")}
    counts: dict[str, Counter[str]] = {kind: Counter() for kind in features}
    holding = graph.holdings.get(collector_id)
    if holding is None:
        return features, counts
    for release_id in holding.owned:
        release = golden.releases.get(release_id)
        if release is None:
            continue
        current = _features(release)
        for kind, values in current.items():
            features[kind].update(values)
            counts[kind].update(values)
    return features, counts


def _normalize_candidate_pool(
    golden: GoldenSet,
    graph: GoldenGraph,
    collector_ids: Sequence[str],
    baseline_rankings: Mapping[str, Sequence[Mapping[str, Any]]],
    candidate_ids: Mapping[str, Sequence[str]] | None,
) -> dict[str, tuple[str, ...]]:
    pools: dict[str, tuple[str, ...]] = {}
    for collector_id in collector_ids:
        raw_ids = (
            candidate_ids[collector_id]
            if candidate_ids is not None and collector_id in candidate_ids
            else [row["id"] for row in baseline_rankings.get(collector_id, ())]
        )
        holding = graph.holdings.get(collector_id)
        owned = set(holding.owned) if holding is not None else set()
        candidates = {release_id for release_id in raw_ids if release_id in golden.releases and release_id not in owned}
        baseline_candidates = {row["id"] for row in baseline_rankings.get(collector_id, ())}
        unsupported = candidates - baseline_candidates
        if unsupported:
            raise ValueError(
                f"candidate pool for {collector_id} may only narrow the frozen baseline pool; unsupported IDs: {', '.join(sorted(unsupported))}"
            )
        pools[collector_id] = tuple(sorted(candidates))
    return pools


def _current_policy(rankings: Mapping[str, Sequence[Mapping[str, Any]]], pools: Mapping[str, Sequence[str]]) -> PolicyResult:
    result: dict[str, tuple[dict[str, Any], ...]] = {}
    for collector_id, candidate_ids in pools.items():
        source_rows = {row["id"]: row for row in rankings.get(collector_id, ())}
        ordered = sorted(set(candidate_ids), key=lambda release_id: (-float(source_rows.get(release_id, {}).get("score", 0.0)), release_id))
        result[collector_id] = tuple(
            {
                "release_id": release_id,
                "score": round(float(source_rows.get(release_id, {}).get("score", 0.0)), 8),
                "reasons": [
                    {"code": "existing_recommendation_reason", "text": reason} for reason in source_rows.get(release_id, {}).get("reasons", [])
                ],
            }
            for release_id in ordered
        )
    return PolicyResult(BASELINE_VERSION, result, {"source": "existing multi-signal ranking"})


def _metadata_policy(
    golden: GoldenSet,
    graph: GoldenGraph,
    pools: Mapping[str, Sequence[str]],
) -> PolicyResult:
    result: dict[str, tuple[dict[str, Any], ...]] = {}
    for collector_id, candidate_ids in pools.items():
        profile, _counts = _collection_profile(graph, collector_id, golden)
        scored: list[tuple[str, float, dict[str, set[str]]]] = []
        for release_id in candidate_ids:
            features = _features(golden.releases[release_id])
            scored.append((release_id, _metadata_similarity(features, profile), features))
        scored.sort(key=lambda item: (-item[1], item[0]))
        rows = []
        for release_id, similarity, features in scored:
            reasons = [
                {"code": f"shared_{kind}", "source_ids": sorted(features[kind] & profile[kind])}
                for kind in ("artist", "label", "genre", "style")
                if features[kind] & profile[kind]
            ]
            if not reasons:
                reasons = [{"code": "no_metadata_overlap", "source_ids": []}]
            rows.append(_row(release_id, golden, similarity, reasons))
        result[collector_id] = tuple(rows)
    return PolicyResult(METADATA_NEIGHBOR_VERSION, result, {"similarity": "namespaced-feature-jaccard"})


def _candidate_policy(
    golden: GoldenSet,
    graph: GoldenGraph,
    pools: Mapping[str, Sequence[str]],
    familiarity_penalty: float,
) -> PolicyResult:
    result: dict[str, tuple[dict[str, Any], ...]] = {}
    for collector_id, candidate_ids in pools.items():
        profile, counts = _collection_profile(graph, collector_id, golden)
        scored: list[tuple[str, float, list[dict[str, Any]]]] = []
        for release_id in candidate_ids:
            features = _features(golden.releases[release_id])
            artist_matches = sorted(features["artist"] & profile["artist"])
            label_matches = sorted(features["label"] & profile["label"])
            genre_matches = sorted(features["genre"] & profile["genre"])
            style_matches = sorted(features["style"] & profile["style"])
            familiarity_load = sum(max(0, counts["artist"][item] - 1) for item in artist_matches)
            familiarity_load += sum(max(0, counts["label"][item] - 1) for item in label_matches)
            raw_score = 3 * len(artist_matches) + 2 * len(label_matches) + len(genre_matches) + len(style_matches)
            penalty = familiarity_penalty * familiarity_load
            reasons: list[dict[str, Any]] = []
            for code, values in (
                ("shared_artist", artist_matches),
                ("shared_label", label_matches),
                ("shared_genre", genre_matches),
                ("shared_style", style_matches),
            ):
                if values:
                    reasons.append({"code": code, "source_ids": values})
            if familiarity_load:
                reasons.append({"code": "familiarity_penalty", "control": familiarity_penalty, "repeat_count": familiarity_load})
            if not reasons:
                reasons.append({"code": "no_context_overlap", "source_ids": []})
            scored.append((release_id, raw_score - penalty, reasons))
        scored.sort(key=lambda item: (-item[1], item[0]))
        result[collector_id] = tuple(_row(release_id, golden, score, reasons) for release_id, score, reasons in scored)
    return PolicyResult(DISCOVERY_REPLAY_VERSION, result, {"familiarity_penalty": familiarity_penalty})


def _checked_labels(
    splits: Mapping[str, Any],
    checked_labels: Mapping[str, Mapping[str, bool]] | None,
) -> tuple[dict[str, dict[str, bool]], str]:
    if checked_labels is not None:
        return ({collector_id: dict(labels) for collector_id, labels in checked_labels.items()}, "explicit-checked-labels")
    # This fixture's only explicit positive signal is a later acquisition. Candidates with no
    # later acquisition remain unknown; they are not turned into negative feedback.
    return ({collector_id: dict.fromkeys(split.held_out, True) for collector_id, split in splits.items()}, "synthetic-held-out-acquisitions")


def _sparse_context_failures(graph: GoldenGraph, golden: GoldenSet, collector_ids: Sequence[str]) -> list[dict[str, Any]]:
    failures = []
    for collector_id in collector_ids:
        observed_size = len(graph.holdings[collector_id].owned)
        profile, _counts = _collection_profile(graph, collector_id, golden)
        if observed_size < SPARSE_COLLECTION_SIZE:
            failures.append({"collector_id": collector_id, "reason": "fewer_than_three_observed_releases", "observed_size": observed_size})
        elif not _feature_set(profile):
            failures.append({"collector_id": collector_id, "reason": "no_observed_metadata_context", "observed_size": observed_size})
    return failures


def _ranking_metrics(
    policy: PolicyResult,
    pools: Mapping[str, Sequence[str]],
    labels: Mapping[str, Mapping[str, bool]],
    golden: GoldenSet,
) -> dict[str, Any]:
    labeled_total = 0
    positive_total = 0
    covered_labels = 0
    precision_by_k: dict[str, float | None] = {}
    annotation_coverage_by_k: dict[str, float | None] = {}
    recall_by_k: dict[str, float | None] = {}
    hit_rate_by_k: dict[str, float | None] = {}
    for collector_id, pool in pools.items():
        collector_labels = labels.get(collector_id, {})
        valid_labels = {release_id: value for release_id, value in collector_labels.items() if release_id in golden.releases}
        ranked = [row["release_id"] for row in policy.rankings.get(collector_id, ())]
        labeled_total += len(valid_labels)
        positive_total += sum(valid_labels.values())
        covered_labels += sum(release_id in set(pool) for release_id in valid_labels)
    for k in RANK_CUTOFFS:
        precisions: list[float] = []
        recalls: list[float] = []
        hits: list[float] = []
        judged_count = 0
        returned_count = 0
        for collector_id, pool in pools.items():
            all_collector_labels = {release_id: value for release_id, value in labels.get(collector_id, {}).items() if release_id in golden.releases}
            collector_labels = {release_id: value for release_id, value in all_collector_labels.items() if release_id in set(pool)}
            ranked = [row["release_id"] for row in policy.rankings.get(collector_id, ())][:k]
            judged = [release_id for release_id in ranked if release_id in collector_labels]
            judged_count += len(judged)
            returned_count += len(ranked)
            true_positives = sum(bool(collector_labels[release_id]) for release_id in judged)
            positives = sum(bool(value) for value in all_collector_labels.values())
            if judged:
                precisions.append(true_positives / len(judged))
            if positives:
                recalls.append(true_positives / positives)
            if judged:
                hits.append(1.0 if true_positives else 0.0)
        precision_by_k[str(k)] = sum(precisions) / len(precisions) if precisions else None
        recall_by_k[str(k)] = sum(recalls) / len(recalls) if recalls else None
        hit_rate_by_k[str(k)] = sum(hits) / len(hits) if hits else None
        annotation_coverage_by_k[str(k)] = judged_count / returned_count if returned_count else None

    distinct_artist_counts: list[float] = []
    distinct_label_counts: list[float] = []
    for collector_id in pools:
        top = [row["release_id"] for row in policy.rankings.get(collector_id, ())][:10]
        if top:
            distinct_artist_counts.append(float(len({artist_id for release_id in top for artist_id in golden.releases[release_id].artist_ids})))
            distinct_label_counts.append(float(len({golden.releases[release_id].label_id for release_id in top})))
    candidate_pool_coverage = len({release_id for pool in pools.values() for release_id in pool}) / len(golden.releases) if golden.releases else 0.0
    recommendation_coverage = {
        str(k): len({row["release_id"] for rows in policy.rankings.values() for row in rows[:k]}) / len(golden.releases) if golden.releases else 0.0
        for k in RANK_CUTOFFS
    }
    return {
        "judged_labels": labeled_total,
        "positive_labels": positive_total,
        "label_coverage": covered_labels / labeled_total if labeled_total else None,
        "annotation_coverage_at_k": annotation_coverage_by_k,
        "judged_precision_at_k": precision_by_k,
        "recall_at_k": recall_by_k,
        "hit_rate_at_k": hit_rate_by_k,
        "candidate_pool_coverage": candidate_pool_coverage,
        "recommendation_coverage_at_k": recommendation_coverage,
        "distinct_artist_count_at_10": sum(distinct_artist_counts) / len(distinct_artist_counts) if distinct_artist_counts else 0.0,
        "distinct_label_count_at_10": sum(distinct_label_counts) / len(distinct_label_counts) if distinct_label_counts else 0.0,
    }


def replay_discovery(
    golden: GoldenSet | None = None,
    *,
    candidate_ids: Mapping[str, Sequence[str]] | None = None,
    checked_labels: Mapping[str, Mapping[str, bool]] | None = None,
    familiarity_penalty: float = FAMILIARITY_PENALTY_DEFAULT,
) -> dict[str, Any]:
    """Compare existing, metadata-neighbor, and catalog-context policies on identical pools.

    ``checked_labels`` contains only explicit boolean annotations. Omitted candidates remain
    unknown. When absent, later acquisitions from the synthetic time split are used as positive
    behavioral proxies only; no negative labels are invented.
    """
    if not FAMILIARITY_PENALTY_MIN <= familiarity_penalty <= FAMILIARITY_PENALTY_MAX:
        raise ValueError(f"familiarity_penalty must be in [{FAMILIARITY_PENALTY_MIN}, {FAMILIARITY_PENALTY_MAX}]")
    fixture = golden or load_golden_set()
    splits = split_golden_set(fixture, SPLIT_CUT)
    graph = observed_graph(fixture, SPLIT_CUT)
    baseline = run_baseline(graph, limit=RECOMMENDATION_LIMIT)
    collector_ids = sorted(graph.holdings)
    pools = _normalize_candidate_pool(fixture, graph, collector_ids, baseline.recommendations, candidate_ids)
    current = _current_policy(baseline.recommendations, pools)
    metadata = _metadata_policy(fixture, graph, pools)
    candidate = _candidate_policy(fixture, graph, pools, familiarity_penalty)
    labels, label_source = _checked_labels(splits, checked_labels)

    pool_identity = {collector_id: list(ids) for collector_id, ids in pools.items()}
    policies = {"current": current, "metadata_nearest_neighbor": metadata, "catalog_context": candidate}
    rankings = {
        name: {
            collector_id: [
                {"release_id": row["release_id"], "score": row["score"], "reasons": row["reasons"]} for row in policy.rankings.get(collector_id, ())
            ]
            for collector_id in collector_ids
        }
        for name, policy in policies.items()
    }
    sparse_context_failures = _sparse_context_failures(graph, fixture, collector_ids)

    return {
        "synthetic": True,
        "real_quality_status": "PENDING: no real checked feedback labels supplied",
        "versions": {
            "current": current.version,
            "metadata_nearest_neighbor": metadata.version,
            "catalog_context": candidate.version,
        },
        "candidate_pool_source": "existing frozen recommendation candidates (explicit pools may only narrow)",
        "candidate_pools": pool_identity,
        "labels": {
            "source": label_source,
            "missing_labels_are_negative": False,
            "real_checked_feedback_labels": 0,
            "note": "Synthetic acquisition proxies are positive-only and are not human-checked feedback.",
        },
        "controls": dict(candidate.controls),
        "sparse_context_failures": sparse_context_failures,
        "metrics": {name: _ranking_metrics(policy, pools, labels, fixture) for name, policy in policies.items()},
        "rankings": rankings,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--familiarity-penalty", type=float, default=FAMILIARITY_PENALTY_DEFAULT)
    args = parser.parse_args(argv)
    try:
        report = replay_discovery(familiarity_penalty=args.familiarity_penalty)
    except ValueError as exc:
        sys.stderr.write(f"discovery replay input error: {exc}\n")
        return 2
    sys.stdout.write(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
