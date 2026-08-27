"""Protect the producer-owned route contract consumed by Graph Explorer."""

import json
from pathlib import Path

from api.api import app


def test_graph_explorer_contract_matches_openapi() -> None:
    """Every promised consumer operation must exist with the promised method."""
    contract_path = Path(__file__).parents[1] / "api/contracts/graph-explorer/v1/routes.json"
    operations = json.loads(contract_path.read_text())["operations"]
    paths = app.openapi()["paths"]
    for name, operation in operations.items():
        assert operation["path"] in paths, name
        assert operation["method"].lower() in paths[operation["path"]], name
