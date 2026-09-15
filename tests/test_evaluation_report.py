"""`just evaluate` assembles a run, writes the raw report, and prints the summary."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from api.evaluation.baseline import BASELINE_VERSION
from api.evaluation.report import (
    EXPECTED_METRICS_PATH,
    REPORTS_DIR,
    evaluate,
    load_expected_metrics,
    main,
    metrics_snapshot,
    serialize,
    summary_lines,
    write_expected_metrics,
    write_report,
)
from api.evaluation.split import SPLIT_CUT


@pytest.fixture(scope="module")
def report() -> dict:
    body, _run = evaluate()
    return body


def test_the_report_names_the_baseline_fixture_and_split(report: dict) -> None:
    assert report["baseline_version"] == BASELINE_VERSION
    assert report["split"]["cut"] == SPLIT_CUT.isoformat()
    assert report["golden_set"]["releases"] == 120
    assert report["golden_set"]["collectors"] == 12
    assert report["golden_set"]["family_counts"] == {"optical": 40, "tape": 40, "vinyl": 40}


def test_the_split_totals_account_for_every_acquisition(report: dict) -> None:
    assert report["split"]["observed_total"] > 0
    assert report["split"]["held_out_total"] == report["metrics"]["recommendation"]["held_out_total"]


def test_the_report_carries_all_three_metric_families(report: dict) -> None:
    assert set(report["metrics"]) == {"recommendation", "rarity", "stability"}


def test_evaluate_returns_the_run_behind_the_numbers() -> None:
    _body, run = evaluate()
    assert run.version == BASELINE_VERSION
    assert run.recommendations


def test_two_evaluations_produce_the_same_report() -> None:
    first, _ = evaluate()
    second, _ = evaluate()
    assert first == second


def test_reports_are_written_under_a_timestamped_name(tmp_path: Path, report: dict) -> None:
    path = write_report(report, tmp_path)
    assert path.parent == tmp_path
    assert path.suffix == ".json"
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["generated_at"] == path.stem
    assert written["baseline_version"] == BASELINE_VERSION


def test_the_raw_report_directory_is_outside_version_control() -> None:
    """Raw run artifacts stay out of the repository; only the small snapshot is committed."""
    gitignore = (REPORTS_DIR.parent / ".gitignore").read_text(encoding="utf-8")
    assert "/reports/" in gitignore
    assert REPORTS_DIR.name == "reports"


def test_the_snapshot_is_the_time_invariant_slice(report: dict) -> None:
    snapshot = metrics_snapshot(report)
    assert snapshot["metrics"] == report["metrics"]
    assert "generated_at" not in snapshot
    assert set(snapshot) == {"baseline_version", "current_year", "golden_set_generator_version", "split_cut", "metrics"}


def test_writing_the_snapshot_reproduces_the_committed_bytes(tmp_path: Path, report: dict) -> None:
    written = write_expected_metrics(report, tmp_path / "expected-metrics.json")
    assert written.read_bytes() == EXPECTED_METRICS_PATH.read_bytes()


def test_the_committed_snapshot_round_trips(report: dict) -> None:
    assert load_expected_metrics() == json.loads(serialize(metrics_snapshot(report)))


def test_the_summary_reports_every_headline_number(report: dict) -> None:
    lines = summary_lines(report)
    text = "\n".join(lines)
    assert BASELINE_VERSION in text
    for label in ("golden set", "time split", "precision", "recall", "hit rate", "coverage", "rarity", "stability"):
        assert label in text
    for family in ("vinyl", "optical", "tape"):
        assert family in text


def test_main_writes_a_report_and_prints_the_summary(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--out", str(tmp_path)]) == 0
    printed = capsys.readouterr().out
    assert BASELINE_VERSION in printed
    written = list(tmp_path.glob("*.json"))
    assert len(written) == 1
    assert str(written[0]) in printed


def test_main_refreshes_the_snapshot_only_when_asked(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    before = EXPECTED_METRICS_PATH.read_bytes()
    assert main(["--out", str(tmp_path)]) == 0
    assert "snapshot" not in capsys.readouterr().out

    assert main(["--out", str(tmp_path), "--update-expected"]) == 0
    assert "snapshot" in capsys.readouterr().out
    assert EXPECTED_METRICS_PATH.read_bytes() == before, "an unchanged baseline must rewrite identical bytes"
