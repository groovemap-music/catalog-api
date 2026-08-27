"""Generate the Analytics consumer artifact from Catalog API-owned OpenAPI."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import yaml


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
OPENAPI_PATH = Path(__file__).resolve().parent / "internal-insights" / "v1" / "openapi.yaml"
OUTPUT_PATH = Path(__file__).resolve().parent / "internal-insights" / "v1" / "python" / "catalog_api_contract.py"


def render() -> str:
    """Render operation constants from the versioned OpenAPI document."""
    document: dict[str, Any] = yaml.safe_load(OPENAPI_PATH.read_text(encoding="utf-8"))
    operations: dict[str, str] = {}
    processing_budgets: dict[str, int | float] = {}
    for path, path_item in document["paths"].items():
        operation_id = path_item["get"]["operationId"]
        operations[operation_id] = path
        max_processing_seconds = path_item["get"].get("x-groovemap-max-processing-seconds")
        if max_processing_seconds is not None:
            processing_budgets[operation_id] = max_processing_seconds
    constants = "\n".join(f"{name.upper()}_PATH = {json.dumps(path)}" for name, path in sorted(operations.items()))
    budget_constants = "\n".join(
        f"{name.upper()}_MAX_PROCESSING_SECONDS = {json.dumps(seconds)}" for name, seconds in sorted(processing_budgets.items())
    )
    return f'''"""Generated from api/contracts/internal-insights/v1/openapi.yaml; do not edit."""

CONTRACT_VERSION = {json.dumps(document["info"]["version"])}
{constants}
{budget_constants}
'''


def main() -> int:
    """Generate the artifact, or verify committed output."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    expected = render()
    if args.check:
        if not OUTPUT_PATH.exists() or OUTPUT_PATH.read_text(encoding="utf-8") != expected:
            sys.stderr.write(f"stale API contract artifact: {OUTPUT_PATH.relative_to(REPOSITORY_ROOT)}\n")
            return 1
        return 0
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(expected, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
