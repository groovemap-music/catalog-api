"""Protect the producer-owned route contract consumed by Graph Explorer."""

import json
from pathlib import Path

from api.api import app
from tests.contract_routes import assert_contract_matches_openapi


def test_graph_explorer_contract_matches_openapi() -> None:
    """Every promised consumer operation must exist with the promised method."""
    contract_path = Path(__file__).parents[1] / "api/contracts/graph-explorer/v1/routes.json"
    operations = json.loads(contract_path.read_text())["operations"]
    assert_contract_matches_openapi(operations, app.openapi()["paths"])


def test_graph_explorer_promotes_only_the_implemented_missing_and_copy_reads() -> None:
    """Keep these new promises tied to the bounded, session-only route shapes."""
    contract_path = Path(__file__).parents[1] / "api/contracts/graph-explorer/v1/routes.json"
    operations = json.loads(contract_path.read_text())["operations"]
    assert operations["user_missing_preview"] == {
        "method": "POST",
        "path": "/api/user/missing/preview",
    }
    assert operations["user_items_copies"] == {
        "method": "GET",
        "path": "/api/user/items/{native_item_id}/copies",
        "parameters": ["limit"],
    }

    schema = app.openapi()
    missing = schema["paths"]["/api/user/missing/preview"]["post"]
    assert missing["security"] == [{"HTTPBearer": []}]
    request_ref = missing["requestBody"]["content"]["application/json"]["schema"]["$ref"]
    request = schema["components"]["schemas"][request_ref.rsplit("/", 1)[-1]]
    assert set(request["required"]) == {"target_kind", "target_id"}
    assert request["additionalProperties"] is False
    target_kind_ref = request["properties"]["target_kind"]["$ref"]
    assert schema["components"]["schemas"][target_kind_ref.rsplit("/", 1)[-1]]["enum"] == ["artist", "label"]
    assert request["properties"]["target_id"]["pattern"] == "^[1-9][0-9]{0,18}$"
    assert request["properties"]["limit"]["default"] == 100
    assert request["properties"]["limit"]["minimum"] == 1
    assert request["properties"]["limit"]["maximum"] == 200
    assert "503" in missing["responses"]  # source unavailable is a documented route failure

    copies = schema["paths"]["/api/user/items/{native_item_id}/copies"]["get"]
    assert copies["security"] == [{"HTTPBearer": []}]
    limit = next(parameter for parameter in copies["parameters"] if parameter["name"] == "limit")
    assert limit["schema"] == {
        "type": "integer",
        "maximum": 100,
        "minimum": 1,
        "default": 50,
        "title": "Limit",
    }
