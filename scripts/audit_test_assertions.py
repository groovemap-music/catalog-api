#!/usr/bin/env python3
"""Inventory tests that have no outcome assertion or only mock-call assertions."""

from __future__ import annotations

import argparse
import ast
import json
from dataclasses import asdict, dataclass
from pathlib import Path


MOCK_CALL_ASSERTIONS = frozenset(
    {
        "assert_any_await",
        "assert_any_call",
        "assert_awaited",
        "assert_awaited_once",
        "assert_awaited_once_with",
        "assert_awaited_with",
        "assert_called",
        "assert_called_once",
        "assert_called_once_with",
        "assert_called_with",
        "assert_has_awaits",
        "assert_has_calls",
        "assert_not_awaited",
        "assert_not_called",
    }
)
PYTEST_OUTCOME_HELPERS = frozenset({"deprecated_call", "raises", "warns"})


@dataclass(frozen=True)
class AuditResult:
    """Stable, serializable assertion audit output."""

    test_functions: int
    assertion_free_count: int
    call_only_count: int
    assertion_free: list[str]
    call_only: list[str]
    finding_ids: dict[str, str]


class _AssertionVisitor(ast.NodeVisitor):
    """Count outcome and mock-call assertions without entering nested scopes."""

    def __init__(self) -> None:
        self.outcome_assertions = 0
        self.mock_call_assertions = 0

    def visit_Assert(self, node: ast.Assert) -> None:
        self.outcome_assertions += 1
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Attribute):
            method = node.func.attr
            if method in MOCK_CALL_ASSERTIONS:
                self.mock_call_assertions += 1
            elif method.startswith("assert"):
                # unittest-style outcome assertions, e.g. self.assertEqual(...).
                self.outcome_assertions += 1
            elif method in PYTEST_OUTCOME_HELPERS and isinstance(node.func.value, ast.Name) and node.func.value.id == "pytest":
                self.outcome_assertions += 1
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        """Do not attribute assertions in a nested helper to its parent test."""

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        """Do not attribute assertions in a nested helper to its parent test."""

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        """Do not enter a class defined inside a test."""

    def visit_Lambda(self, node: ast.Lambda) -> None:
        """Do not attribute assertions in a nested lambda to its parent test."""


def audit(root: Path) -> AuditResult:
    """Audit all test functions below *root* in deterministic path/line order."""
    tests: list[tuple[str, str, ast.FunctionDef | ast.AsyncFunctionDef]] = []
    base = root.parent
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        relative_path = path.relative_to(base).as_posix()
        parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_") and not _is_pytest_fixture(node):
                names = [node.name]
                parent = parents.get(node)
                while parent is not None:
                    if isinstance(parent, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                        names.append(parent.name)
                    parent = parents.get(parent)
                stable_id = f"{relative_path}::{'.'.join(reversed(names))}"
                tests.append((f"{relative_path}:{node.lineno}:{node.name}", stable_id, node))

    assertion_free: list[str] = []
    call_only: list[str] = []
    finding_ids: dict[str, str] = {}
    for identifier, stable_id, node in sorted(tests):
        visitor = _AssertionVisitor()
        for statement in node.body:
            visitor.visit(statement)
        if visitor.outcome_assertions == 0:
            finding_ids[identifier] = stable_id
            if visitor.mock_call_assertions == 0:
                assertion_free.append(identifier)
            else:
                call_only.append(identifier)

    return AuditResult(
        test_functions=len(tests),
        assertion_free_count=len(assertion_free),
        call_only_count=len(call_only),
        assertion_free=assertion_free,
        call_only=call_only,
        finding_ids=finding_ids,
    )


def _is_pytest_fixture(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for decorator in node.decorator_list:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name):
            if target.value.id == "pytest" and target.attr == "fixture":
                return True
        elif isinstance(target, ast.Name) and target.id == "fixture":
            return True
    return False


@dataclass(frozen=True)
class TriageResult:
    """Dispositions stay stable when a test moves to a different line."""

    unexplained: list[str]
    stale_exclusions: list[str]
    intentional: dict[str, str]


def triage(result: AuditResult, entries: list[dict[str, str]]) -> TriageResult:
    """Apply exact test-and-kind exclusions; never hide a new weak test."""
    exclusions: dict[str, dict[str, str]] = {}
    for entry in entries:
        if entry["disposition"] == "intentional":
            if not entry["reason"].strip() or entry["id"] in exclusions:
                raise ValueError("Intentional exclusions need unique test IDs and a reason")
            exclusions[entry["id"]] = entry

    current = {
        result.finding_ids[identifier]: kind
        for kind, identifiers in (("assertion_free", result.assertion_free), ("call_only", result.call_only))
        for identifier in identifiers
    }
    intentional = {
        identifier: exclusions[identifier]["reason"]
        for identifier, kind in current.items()
        if identifier in exclusions and exclusions[identifier]["current_kind"] == kind
    }
    return TriageResult(
        unexplained=sorted(set(current) - set(intentional)),
        stale_exclusions=sorted(set(exclusions) - set(current)),
        intentional=intentional,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("tests"), help="test tree to scan (default: tests)")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_argument("--triage", type=Path, default=Path(__file__).resolve().parents[1] / "docs/test-assertion-triage.json")
    parser.add_argument("--check", action="store_true", help="fail for unexplained findings or stale intentional exclusions")
    args = parser.parse_args()

    result = audit(args.root)
    review = triage(result, json.loads(args.triage.read_text()))
    if args.json:
        print(json.dumps({**asdict(result), "triage": asdict(review)}, indent=2))
        if args.check and (review.unexplained or review.stale_exclusions):
            raise SystemExit(1)
        return

    print(f"test functions: {result.test_functions}")
    print(f"assertion-free: {result.assertion_free_count}")
    print(f"call-only: {result.call_only_count}")
    print(f"intentional: {len(review.intentional)}")
    print(f"unexplained: {len(review.unexplained)}")
    print(f"stale exclusions: {len(review.stale_exclusions)}")
    for heading, identifiers in (("assertion-free tests", result.assertion_free), ("call-only tests", result.call_only)):
        print(f"\n{heading}:")
        for identifier in identifiers:
            print(f"  {identifier}")
    if args.check and (review.unexplained or review.stale_exclusions):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
