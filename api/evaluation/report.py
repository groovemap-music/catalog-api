"""Assemble one evaluation run into a report, and the ``just evaluate`` entry point.

Two artifacts come out of a run, and the split between them is deliberate:

* ``reports/<timestamp>.json`` -- the full run, timestamp included. Gitignored, matching the
  repository's existing rule that raw benchmark artifacts stay outside the repository.
* ``tests/fixtures/golden/expected-metrics.json`` -- the time-invariant slice of the same
  report. Committed, and compared against a fresh run by ``tests/test_evaluation_metrics.py``
  at a tolerance of 1e-9. It is small, it is diffable, and a weight change moves it visibly.

Anything that varies between two identical runs -- the timestamp, the output path -- is in the
first and not the second. That is what makes the snapshot a regression test rather than a log.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from api.evaluation.baseline import BaselineRun, run_baseline
from api.evaluation.fixtures import EXPECTED_METRICS_FILE, GOLDEN_DIR, GoldenSet, load_golden_set
from api.evaluation.metrics import rank_stability, rarity_metrics, recommendation_metrics, similar_artist_metrics
from api.evaluation.split import SPLIT_CUT, observed_graph, split_golden_set


#: Raw run artifacts land here. Gitignored; see docs/evaluation.md.
REPORTS_DIR: Final[Path] = Path(__file__).resolve().parents[2] / "reports"

#: Committed snapshot the regression test compares a fresh run against.
EXPECTED_METRICS_PATH: Final[Path] = GOLDEN_DIR / EXPECTED_METRICS_FILE

#: How far two runs of the same baseline may differ. The harness is deterministic, so this is
#: a floating-point allowance, not a noise budget.
METRICS_TOLERANCE: Final[float] = 1e-9

#: The gm-catalog-api-tsmu.1 baseline's own committed snapshot, kept separate from
#: ``EXPECTED_METRICS_PATH`` so registering it never touches ``heuristics-2026-09``'s bytes.
SIMILAR_ARTIST_EXPECTED_METRICS_PATH: Final[Path] = GOLDEN_DIR / "expected-similar-artist-metrics.json"


def evaluate(golden: GoldenSet | None = None) -> tuple[dict[str, Any], BaselineRun]:
    """Run the baseline under the time split and score it.

    The baseline is run twice over the same observed graph. The second run exists only to be
    correlated against the first: a harness that cannot reproduce its own numbers cannot judge
    anything else's.

    Args:
        golden: The fixture to evaluate. Defaults to the committed golden set.

    Returns:
        The report body and the first run, so a caller can inspect the ranking itself.
    """
    fixture = golden if golden is not None else load_golden_set()
    splits = split_golden_set(fixture, SPLIT_CUT)
    graph = observed_graph(fixture, SPLIT_CUT)
    run = run_baseline(graph)
    repeat = run_baseline(graph)

    report = {
        "baseline_version": run.version,
        "current_year": run.current_year,
        "golden_set": {
            "generator_version": fixture.generator_version,
            "seed": fixture.seed,
            "releases": len(fixture.releases),
            "collectors": len(fixture.collectors),
            "family_counts": dict(sorted(fixture.family_counts().items())),
        },
        "split": {
            "cut": SPLIT_CUT.isoformat(),
            "observed_total": sum(split.observed_size for split in splits.values()),
            "held_out_total": sum(split.held_out_size for split in splits.values()),
        },
        "metrics": {
            "recommendation": recommendation_metrics(run, splits, fixture),
            "rarity": rarity_metrics(run),
            "stability": rank_stability(run, repeat),
        },
    }
    return report, run


def metrics_snapshot(report: dict[str, Any]) -> dict[str, Any]:
    """Return the time-invariant slice of ``report`` -- the part that is committed."""
    return {
        "baseline_version": report["baseline_version"],
        "current_year": report["current_year"],
        "golden_set_generator_version": report["golden_set"]["generator_version"],
        "split_cut": report["split"]["cut"],
        "metrics": report["metrics"],
    }


def load_expected_metrics(path: Path = EXPECTED_METRICS_PATH) -> dict[str, Any]:
    """Load the committed expected-metrics snapshot."""
    loaded: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return loaded


def serialize(document: dict[str, Any]) -> str:
    """Render a report or snapshot as the exact bytes written to disk."""
    return json.dumps(document, indent=2, ensure_ascii=False, sort_keys=False) + "\n"


def write_report(report: dict[str, Any], directory: Path = REPORTS_DIR, *, now: datetime | None = None) -> Path:
    """Write the full report, timestamp included, into ``directory``."""
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{stamp}.json"
    path.write_text(serialize({"generated_at": stamp, **report}), encoding="utf-8")
    return path


def write_expected_metrics(report: dict[str, Any], path: Path = EXPECTED_METRICS_PATH) -> Path:
    """Overwrite the committed snapshot from ``report``. Run when a baseline change is intended."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(serialize(metrics_snapshot(report)), encoding="utf-8")
    return path


# ── gm-catalog-api-tsmu.1: the similar-artist candidate baseline ──────
#
# A second, narrower harness registered alongside the one above rather than folded into it:
# ``evaluate()`` and ``EXPECTED_METRICS_PATH`` stay exactly as they were so heuristics-2026-09's
# committed snapshot never moves, and this one scores only what changed -- the similar-artist
# candidate generator -- comparing the new scope against the frozen one on the identical split.


def evaluate_similar_artist_candidates(golden: GoldenSet | None = None) -> tuple[dict[str, Any], BaselineRun, BaselineRun]:
    """Score the new similar-artist candidate scope against the frozen one, same split.

    Args:
        golden: The fixture to evaluate. Defaults to the committed golden set.

    Returns:
        The report body, the new-candidate-scope run (``SIMILAR_ARTIST_CANDIDATES_VERSION``),
        and the legacy run (``BASELINE_VERSION``) it is compared against.
    """
    fixture = golden if golden is not None else load_golden_set()
    splits = split_golden_set(fixture, SPLIT_CUT)
    graph = observed_graph(fixture, SPLIT_CUT)
    legacy_run = run_baseline(graph)
    all_signals_run = run_baseline(graph, similar_artist_candidates="all_signals")

    report = {
        "baseline_version": all_signals_run.version,
        "compared_to_baseline_version": legacy_run.version,
        "golden_set": {
            "generator_version": fixture.generator_version,
            "seed": fixture.seed,
        },
        "split": {"cut": SPLIT_CUT.isoformat()},
        "metrics": {
            "similar_artist": similar_artist_metrics(all_signals_run, splits, fixture),
            "similar_artist_legacy": similar_artist_metrics(legacy_run, splits, fixture),
        },
    }
    return report, all_signals_run, legacy_run


def similar_artist_metrics_snapshot(report: dict[str, Any]) -> dict[str, Any]:
    """Return the time-invariant slice of an ``evaluate_similar_artist_candidates`` report."""
    return {
        "baseline_version": report["baseline_version"],
        "compared_to_baseline_version": report["compared_to_baseline_version"],
        "golden_set_generator_version": report["golden_set"]["generator_version"],
        "split_cut": report["split"]["cut"],
        "metrics": report["metrics"],
    }


def load_similar_artist_expected_metrics(path: Path = SIMILAR_ARTIST_EXPECTED_METRICS_PATH) -> dict[str, Any]:
    """Load the committed similar-artist-candidates snapshot."""
    loaded: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return loaded


def write_similar_artist_expected_metrics(report: dict[str, Any], path: Path = SIMILAR_ARTIST_EXPECTED_METRICS_PATH) -> Path:
    """Overwrite the committed similar-artist-candidates snapshot from ``report``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(serialize(similar_artist_metrics_snapshot(report)), encoding="utf-8")
    return path


def summary_lines(report: dict[str, Any]) -> list[str]:
    """Render the human-readable summary ``just evaluate`` prints."""
    recommendation = report["metrics"]["recommendation"]
    overall = recommendation["overall"]
    rarity = report["metrics"]["rarity"]
    lines = [
        f"baseline {report['baseline_version']} on golden set {report['golden_set']['generator_version']} (seed {report['golden_set']['seed']})",
        f"  golden set        {report['golden_set']['releases']} releases, {report['golden_set']['collectors']} collectors, "
        + ", ".join(f"{family} {count}" for family, count in report["golden_set"]["family_counts"].items()),
        f"  time split        cut {report['split']['cut']}: {report['split']['observed_total']} observed, {report['split']['held_out_total']} held out",
        f"  precision         @10 {overall['precision_at_10']:.4f}   @25 {overall['precision_at_25']:.4f}",
        f"  recall            @10 {overall['recall_at_10']:.4f}   @25 {overall['recall_at_25']:.4f}",
        f"  hit rate          {overall['hit_rate']:.4f} of collectors got at least one held-out release in their top 25",
        f"  coverage          {recommendation['coverage']['catalogue_coverage']:.4f} of the catalog was recommended to someone",
    ]
    lines.extend(
        f"  {family:<17} recall@25 {block['recall_at_25']:.4f}  ({block['recommended_share']:.1%} of recommendations, {block['held_out_share']:.1%} of held-out)"
        for family, block in recommendation["by_format"].items()
    )
    lines.extend(
        f"  {bucket + ' collectors':<17} n={block['collectors']}  precision@25 {block['precision_at_25']:.4f}  recall@25 {block['recall_at_25']:.4f}"
        for bucket, block in recommendation["by_collector_size"].items()
    )
    lines.append(
        f"  rarity            {rarity['releases']} scored, tiers "
        + ", ".join(f"{tier} {count}" for tier, count in rarity["tier_distribution"].items())
    )
    lines.append(f"  stability         spearman {report['metrics']['stability']['spearman']:.4f} between two runs")
    return lines


def main(argv: Sequence[str] | None = None) -> int:
    """Run the harness, write the report, and print the summary."""
    parser = argparse.ArgumentParser(description="Run the offline evaluation harness against the committed golden set.")
    parser.add_argument("--out", type=Path, default=REPORTS_DIR, help="directory for the raw report (gitignored)")
    parser.add_argument(
        "--update-expected",
        action="store_true",
        help="overwrite tests/fixtures/golden/expected-metrics.json; only with a deliberate baseline change",
    )
    arguments = parser.parse_args(argv)

    report, _run = evaluate()
    path = write_report(report, arguments.out)
    for line in summary_lines(report):
        print(line)
    print(f"  report            {path}")
    if arguments.update_expected:
        print(f"  snapshot          {write_expected_metrics(report)}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
