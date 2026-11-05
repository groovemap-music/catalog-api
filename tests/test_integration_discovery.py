"""The integration lane finds its test files; nobody lists them (gm-catalog-api-q4ri).

``scripts/test-integration.sh`` used to name every file by hand, with no trailing backslash on the last line, so any two beads that
added a test edited the same lines and conflicted at merge. ``scripts/integration-files.sh`` now discovers them.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"
DISCOVER = ROOT / "scripts" / "integration-files.sh"

# Files that carry the integration marker but run in another lane, with the reason. Everything else marked integration must be
# discovered by the integration lane, or this test fails.
OTHER_LANES = {
    "tests/test_cache_budget_real.py": "needs a real noeviction Valkey: scripts/test-integration-valkey.sh (just integration-valkey)",
    "tests/test_valkey_noeviction_real.py": "needs a real noeviction Valkey: scripts/test-integration-valkey.sh (just integration-valkey)",
}


def _discover(tests_dir: Path) -> list[str]:
    out = subprocess.run(["bash", str(DISCOVER), str(tests_dir)], check=True, capture_output=True, text=True).stdout  # noqa: S603, S607
    return out.split()


def test_a_new_integration_file_is_collected_without_editing_the_script(tmp_path: Path) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    for name in ("test_real_databases.py", "test_alpha_integration.py", "test_unit.py", "helper_integration_notes.txt", "conftest.py"):
        (tests / name).write_text("")
    assert _discover(tests) == ["tests/test_alpha_integration.py", "tests/test_real_databases.py"]

    (tests / "test_brand_new_integration.py").write_text("")  # a new bead's file: no edit to any script
    assert _discover(tests) == ["tests/test_alpha_integration.py", "tests/test_brand_new_integration.py", "tests/test_real_databases.py"]


def test_discovery_with_no_matching_files_prints_nothing(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    assert _discover(tmp_path / "tests") == []


def test_the_lane_script_names_no_test_file() -> None:
    script = (ROOT / "scripts" / "test-integration.sh").read_text()
    assert "integration-files.sh" in script
    assert "tests/test_" not in script, "a hand list of test files is back in test-integration.sh"


def test_every_marked_integration_file_is_in_a_lane() -> None:
    discovered = set(_discover(TESTS))
    marked = {
        f"tests/{path.name}"
        for path in TESTS.glob("*.py")
        if path.name != Path(__file__).name and "pytest.mark.integration" in path.read_text()  # this file only mentions the marker
    }
    assert discovered, "discovery found nothing"
    missing = sorted(marked - discovered - set(OTHER_LANES))
    assert not missing, f"marked integration but in no lane (name it *_integration.py): {missing}"
    assert "tests/test_real_databases.py" in discovered


@pytest.mark.parametrize("path", sorted(OTHER_LANES))
def test_files_in_another_lane_really_run_there(path: str) -> None:
    assert (ROOT / path).exists()
    assert path in (ROOT / "scripts" / "test-integration-valkey.sh").read_text()
    assert path not in _discover(TESTS)
