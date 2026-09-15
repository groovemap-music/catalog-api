"""The evaluation page stays pinned to the harness it documents.

A page describing a version, a cut, or a recipe that no longer exists is worse than no page,
because a reader trusts it. These assertions are the cheapest way to keep it honest.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from api.evaluation.baseline import BASELINE_VERSION
from api.evaluation.fixtures import EXPECTED_METRICS_FILE, load_golden_set
from api.evaluation.split import SPLIT_CUT


ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "docs" / "evaluation.md"


@pytest.fixture(scope="module")
def page() -> str:
    return PAGE.read_text(encoding="utf-8")


def test_the_page_is_indexed() -> None:
    assert "](evaluation.md)" in (ROOT / "docs" / "README.md").read_text(encoding="utf-8")


def test_the_page_names_the_current_baseline_and_cut(page: str) -> None:
    assert BASELINE_VERSION in page
    assert SPLIT_CUT.isoformat() in page


def test_the_page_describes_the_golden_set_as_generated(page: str) -> None:
    golden = load_golden_set()
    assert str(len(golden.releases)) in page
    assert str(len(golden.collectors)) in page
    for family, count in golden.family_counts().items():
        assert f"{family} {count}" in page


@pytest.mark.parametrize(
    "recipe",
    ["just evaluate", "just generate-golden-set", "uv run python -m api.evaluation.report --update-expected"],
)
def test_the_page_documents_the_recipes_that_exist(page: str, recipe: str) -> None:
    assert recipe in page
    if recipe.startswith("just "):
        assert f"\n{recipe.removeprefix('just ')}:" in (ROOT / "Justfile").read_text(encoding="utf-8")


def test_the_page_states_where_raw_reports_go_and_that_they_stay_out_of_git(page: str) -> None:
    assert "reports/<timestamp>.json" in page
    assert "gitignored" in page
    assert EXPECTED_METRICS_FILE in page


@pytest.mark.parametrize("module", ["fixtures", "graph", "split", "baseline", "metrics", "report"])
def test_the_page_lists_every_module_of_the_harness(page: str, module: str) -> None:
    assert f"api/evaluation/{module}.py" in page
    assert (ROOT / "api" / "evaluation" / f"{module}.py").is_file()


@pytest.mark.parametrize("metric", ["precision_at_k", "recall_at_k", "catalogue_coverage", "tier_distribution", "stability.spearman"])
def test_the_page_explains_every_reported_metric(page: str, metric: str) -> None:
    assert metric in page
