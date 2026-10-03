"""Keep the exact assertion-audit exclusions actionable as tests evolve."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.audit_test_assertions import audit, triage


ROOT = Path(__file__).resolve().parents[1]


def _source(tmp_path: Path, source: str) -> Path:
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_sample.py").write_text(source)
    return tests


def _exclusion(identifier: str, kind: str = "assertion_free") -> dict[str, str]:
    return {"id": identifier, "disposition": "intentional", "current_kind": kind, "reason": "Expected no-raise boundary"}


def test_audit_distinguishes_outcomes_calls_and_nested_helpers(tmp_path: Path) -> None:
    result = audit(
        _source(
            tmp_path,
            """import pytest

def test_without_assertion():
    pass

def test_calls(mock):
    mock.assert_called_once()

def test_outcome():
    assert 1 == 1

def test_raises():
    with pytest.raises(ValueError):
        raise ValueError()

def test_nested_helper():
    def helper():
        assert False

@pytest.fixture
def test_named_fixture():
    pass
""",
        )
    )
    assert result.test_functions == 5
    assert result.assertion_free_count == 2
    assert result.call_only_count == 1
    assert set(result.finding_ids.values()) == {
        "tests/test_sample.py::test_without_assertion",
        "tests/test_sample.py::test_calls",
        "tests/test_sample.py::test_nested_helper",
    }


def test_class_qualified_ids_survive_line_moves_without_collisions(tmp_path: Path) -> None:
    tests = _source(tmp_path, "class First:\n    def test_same(self): pass\nclass Second:\n    def test_same(self): pass\n")
    before = audit(tests)
    path = tests / "test_sample.py"
    path.write_text("\n\n" + path.read_text())
    after = audit(tests)
    assert (
        set(before.finding_ids.values())
        == set(after.finding_ids.values())
        == {
            "tests/test_sample.py::First.test_same",
            "tests/test_sample.py::Second.test_same",
        }
    )
    assert before.assertion_free != after.assertion_free


def test_exclusion_does_not_hide_new_or_changed_findings(tmp_path: Path) -> None:
    result = audit(_source(tmp_path, "def test_known(mock): mock.assert_called_once()\ndef test_new(): pass\n"))
    review = triage(result, [_exclusion("tests/test_sample.py::test_known")])
    assert review.unexplained == ["tests/test_sample.py::test_known", "tests/test_sample.py::test_new"]
    assert review.intentional == {}


def test_stale_exclusion_is_reported_when_test_gains_an_outcome(tmp_path: Path) -> None:
    result = audit(_source(tmp_path, "def test_known(): assert True\n"))
    review = triage(result, [_exclusion("tests/test_sample.py::test_known")])
    assert review.unexplained == []
    assert review.stale_exclusions == ["tests/test_sample.py::test_known"]


def test_intentional_exclusion_requires_a_nonempty_reason(tmp_path: Path) -> None:
    result = audit(_source(tmp_path, "def test_known(): pass\n"))
    entry = _exclusion("tests/test_sample.py::test_known")
    entry["reason"] = " "
    with pytest.raises(ValueError, match="reason"):
        triage(result, [entry])


def test_cli_check_fails_for_unexplained_findings(tmp_path: Path) -> None:
    tests = _source(tmp_path, "def test_new(): pass\n")
    exclusions = tmp_path / "triage.json"
    exclusions.write_text("[]")
    completed = subprocess.run(  # noqa: S603
        [sys.executable, str(ROOT / "scripts/audit_test_assertions.py"), "--root", str(tests), "--triage", str(exclusions), "--json", "--check"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 1
    assert json.loads(completed.stdout)["triage"]["unexplained"] == ["tests/test_sample.py::test_new"]


def test_repository_findings_have_exact_explained_dispositions() -> None:
    entries = json.loads((ROOT / "docs/test-assertion-triage.json").read_text())
    assert len(entries) == 56
    assert len({entry["id"] for entry in entries}) == len(entries)
    assert sum(entry["origin"] == "historical-44" for entry in entries) == 44
    assert all(entry["reason"].strip() for entry in entries)
    review = triage(audit(ROOT / "tests"), entries)
    assert review.unexplained == []
    assert review.stale_exclusions == []
