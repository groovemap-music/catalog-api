"""Producer-side compatibility tests for operations-console proxy routes."""

import json
from pathlib import Path

from api.api import app


CONTRACT = Path(__file__).parents[1] / "api/contracts/operations-console/v1/routes.json"


def test_operations_console_contract_routes_exist() -> None:
    """Every promoted proxy target must remain mounted with its declared method."""
    document = json.loads(CONTRACT.read_text())
    mounted = {(method.upper(), path) for path, path_item in app.openapi()["paths"].items() for method in path_item}

    for name, operation in document["operations"].items():
        assert (operation["method"], operation["path"]) in mounted, name
