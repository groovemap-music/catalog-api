"""Producer-side compatibility tests for operations-console proxy routes."""

import asyncio
import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

from api.api import app
from api.identity_ledger import EVENT_TYPES, KINDS, LINK_DATED_FROM, LedgerRows, build_work
from api.queries.identity_ledger_queries import get_ledger_density, get_ledger_works
from tests.fake_postgres import FakePool


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


def test_admin_session_route_is_declared() -> None:
    """The console auth gate calls this route, so it must stay declared with its binding constants."""
    operation = json.loads(CONTRACT.read_text())["operations"]["admin_session"]
    assert operation == {"method": "GET", "path": "/api/admin/auth/session"}

    binding = _load_binding()
    assert operation["method"] == binding.ADMIN_SESSION_METHOD
    assert operation["path"] == binding.ADMIN_SESSION_PATH


def test_extraction_trigger_declares_validated_source_body() -> None:
    """The trigger body and response carry `source`, and the contract matches the mounted schema."""
    operation = json.loads(CONTRACT.read_text())["operations"]["admin_extraction_trigger"]
    schemas = app.openapi()["components"]["schemas"]

    body = schemas["ExtractionTriggerRequest"]
    assert body["required"] == operation["request_body"]["required"]
    assert body["properties"]["source"]["enum"] == operation["request_body"]["properties"]["source"]["enum"]
    assert set(operation["response_fields"]) == set(schemas["ExtractionTriggerResponse"]["properties"])

    binding = _load_binding()
    assert operation["request_body"]["properties"]["source"]["enum"] == binding.ADMIN_EXTRACTION_TRIGGER_SOURCES


def test_extraction_history_declares_nullable_source() -> None:
    """Legacy runs have no recorded source; the contract says so rather than guessing one."""
    operation = json.loads(CONTRACT.read_text())["operations"]["admin_extractions"]
    assert operation["response_fields_nullable"] == ["source"]
    source = app.openapi()["components"]["schemas"]["ExtractionHistoryResponse"]["properties"]["source"]
    assert {"type": "null"} in source["anyOf"]
    assert "ExtractionHistoryResponse" in app.openapi()["components"]["schemas"]


_DATED_FROM = ["groovemap", "musicbrainz_fullexport"]

_LEDGER_OPERATIONS = {
    "admin_identity_ledger_works": {
        "method": "GET",
        "path": "/api/admin/identity/ledger/works",
        "query": ["event", "page", "page_size", "since", "until"],
        "events": list(EVENT_TYPES),
        "response_fields": ["page", "page_size", "total", "works"],
    },
    "admin_identity_ledger_work": {
        "method": "GET",
        "path": "/api/admin/identity/ledger/works/{work_id}",
        "query": ["as_of", "edition_limit"],
        "dated_from": _DATED_FROM,
        "response_fields": ["as_of", "edition_count", "editions", "truncated", "work"],
    },
    "admin_identity_ledger_density": {
        "method": "GET",
        "path": "/api/admin/identity/ledger/density",
        "query": ["kind"],
        "kinds": list(KINDS),
        "dated_from": _DATED_FROM,
        "response_fields": ["buckets", "kind"],
    },
}


@pytest.mark.parametrize("name", sorted(_LEDGER_OPERATIONS))
def test_identity_ledger_routes_are_declared(name: str) -> None:
    """The console's Ledger tab reads these three; each is pinned with its binding constants."""
    operation = json.loads(CONTRACT.read_text())["operations"][name]
    assert operation == _LEDGER_OPERATIONS[name]

    binding = _load_binding()
    assert operation["method"] == getattr(binding, f"{name.upper()}_METHOD")
    assert operation["path"] == getattr(binding, f"{name.upper()}_PATH")

    mounted = app.openapi()["paths"][operation["path"]]["get"]
    assert sorted(p["name"] for p in mounted.get("parameters", []) if p["in"] == "query") == operation["query"]


def test_identity_ledger_vocabularies_match_the_producer() -> None:
    """The provenance labels the contract declares are exactly the ones events carry."""
    assert sorted(set(LINK_DATED_FROM.values())) == _DATED_FROM


def test_identity_ledger_response_fields_match_the_payloads() -> None:
    """Each declared response field set is exactly what the producer returns."""
    operations = json.loads(CONTRACT.read_text())["operations"]
    work = build_work(LedgerRows(aliases=[], supersessions=[]), "00000000-0000-4000-8000-000000000101", [])
    works = asyncio.run(get_ledger_works(FakePool([[], [(0, None, None, None, None, None)]])))
    density = asyncio.run(get_ledger_density(FakePool([[], []])))

    assert sorted(work) == operations["admin_identity_ledger_work"]["response_fields"]
    assert sorted(works) == operations["admin_identity_ledger_works"]["response_fields"]
    assert sorted(density) == operations["admin_identity_ledger_density"]["response_fields"]
