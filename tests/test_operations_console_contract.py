"""Producer-side compatibility tests for operations-console proxy routes."""

import importlib.util
import json
from pathlib import Path
from typing import Any

from api.api import app


CONTRACT = Path(__file__).parents[1] / "api/contracts/operations-console/v1/routes.json"
BINDING = Path(__file__).parents[1] / "api/contracts/operations-console/v1/python/catalog_admin_contract.py"


def _load_binding() -> Any:
    """Import the generated binding from its path.

    The contract directory is hyphenated, so the published artifact is not importable
    as a package; consumers vendor the file. Load it the same way, by path.
    """
    spec = importlib.util.spec_from_file_location("catalog_admin_contract", BINDING)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_operations_console_contract_routes_exist() -> None:
    """Every promoted proxy target must remain mounted with its declared method."""
    document = json.loads(CONTRACT.read_text())
    mounted = {(method.upper(), path) for path, path_item in app.openapi()["paths"].items() for method in path_item}

    for name, operation in document["operations"].items():
        assert (operation["method"], operation["path"]) in mounted, name


def test_media_coverage_route_is_declared() -> None:
    """The console reads mapping coverage through the contract, so it must stay declared.

    The check above only walks whatever the document happens to declare, so dropping an
    operation from routes.json would silently pass it. This pins the one the console
    depends on, and the constants the generated binding publishes for it.
    """
    document = json.loads(CONTRACT.read_text())
    operation = document["operations"]["admin_unmapped_media"]
    assert operation == {"method": "GET", "path": "/api/admin/media/unmapped"}

    binding = _load_binding()
    assert operation["method"] == binding.ADMIN_UNMAPPED_MEDIA_METHOD
    assert operation["path"] == binding.ADMIN_UNMAPPED_MEDIA_PATH
