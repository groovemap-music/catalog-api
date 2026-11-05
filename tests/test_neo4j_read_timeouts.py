"""Every request-path Neo4j read is bounded by a server timeout, and a timeout is a 504.

Audit C1 (``docs/cypher-performance-audit-2026-11-04.md``): 119 of 150 statements passed no timeout and
ran to lux's 10-minute ``db.transaction.timeout``. The helpers now default to 30 s. The guard below
fails when a call site opts out or sets a longer budget without a documented reason.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from neo4j import Query
from neo4j.exceptions import ClientError as Neo4jClientError

from api.neo4j_timeouts import install_neo4j_timeout_handler
from api.queries import helpers


API_ROOT = Path(__file__).resolve().parents[1] / "api"
HELPERS = {"run_query", "run_single", "run_count"}
REQUEST_BUDGET_SECONDS = 30.0

# File -> (longest budget in seconds, reason). A budget above 30 s, or ``timeout=None``, is allowed only here.
EXCEPTIONS: dict[str, tuple[float | None, str]] = {
    "api/projection.py": (None, "internal gm_id projection write batch, bounded by batch_size, not a request read"),
    "api/queries/insights_neo4j_queries.py": (300.0, "internal Analytics batch reads, documented 30 to 300 s"),
    "api/explore_discovery.py": (120.0, "background exact scan of one hub genre/style/label for explore discoveries, never a request"),
    "api/queries/genre_tree_queries.py": (
        120.0,
        "background genre/style co-occurrence scan, one genre at a time (Rock 16 s on lux), never a request",
    ),
    "api/queries/rarity_queries.py": (120.0, "internal rarity batch pages of 20k releases, not a request path"),
    "api/queries/taste_queries.py": (120.0, "personal taste reads over a user's collection; lux has no User nodes to measure"),
    "api/queries/recommend_queries.py": (60.0, "similar-artist candidate scan; audit measured light artists only (4.6 s)"),
    "api/queries/label_dna_queries.py": (60.0, "similar-label candidate scan; cached 24 h, cold path"),
}


def _module_constants(path: Path) -> dict[str, Any]:
    module = importlib.import_module(".".join(path.relative_to(API_ROOT.parent).with_suffix("").parts))
    return vars(module)


def _call_sites() -> list[tuple[str, int, str, Any]]:
    """(file, line, helper, resolved timeout) for every helper call; ``"default"`` when omitted."""
    sites: list[tuple[str, int, str, Any]] = []
    for path in sorted(API_ROOT.rglob("*.py")):
        if path.name == "helpers.py" and path.parent.name == "queries":
            continue
        tree = ast.parse(path.read_text())
        constants: dict[str, Any] | None = None
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
            if name not in HELPERS:
                continue
            rel = path.relative_to(API_ROOT.parent).as_posix()
            kw = next((k for k in node.keywords if k.arg == "timeout"), None)
            if kw is None:
                sites.append((rel, node.lineno, name, "default"))
            elif isinstance(kw.value, ast.Constant):
                sites.append((rel, node.lineno, name, kw.value.value))
            elif isinstance(kw.value, ast.Name):
                constants = constants if constants is not None else _module_constants(path)
                sites.append((rel, node.lineno, name, constants[kw.value.id]))
            else:
                sites.append((rel, node.lineno, name, "unresolved"))
    return sites


def test_scan_finds_the_request_path_calls() -> None:
    sites = _call_sites()
    assert len(sites) > 100
    assert sum(1 for s in sites if s[3] == "default") > 80


def test_every_helper_call_has_a_budget_of_30_seconds_or_a_documented_exception() -> None:
    offenders = []
    for rel, line, name, timeout in _call_sites():
        if timeout == "default":
            continue
        allowed = EXCEPTIONS.get(rel)
        if timeout == "unresolved":
            ok = False
        elif timeout is None:
            ok = allowed is not None and allowed[0] is None
        elif timeout <= REQUEST_BUDGET_SECONDS:
            ok = True
        else:
            ok = allowed is not None and (allowed[0] is None or timeout <= allowed[0])
        if not ok:
            offenders.append(f"{rel}:{line} {name} timeout={timeout!r}")
    assert not offenders, "\n".join(offenders)


def test_default_budget_is_thirty_seconds() -> None:
    assert helpers.DEFAULT_READ_TIMEOUT_SECONDS == REQUEST_BUDGET_SECONDS


@pytest.mark.parametrize(("given", "expected"), [(helpers.DEFAULT_TIMEOUT, 30.0), (5, 5), (None, None)])
def test_resolve_timeout(given: Any, expected: float | None) -> None:
    assert helpers.resolve_timeout(given) == expected


@pytest.mark.parametrize("fn", [helpers.run_query, helpers.run_single, helpers.run_count])
@pytest.mark.asyncio
async def test_helpers_send_the_default_timeout_to_neo4j(fn: Any) -> None:
    seen: list[Any] = []

    class _Result:
        def __aiter__(self) -> Any:
            return self._gen()

        async def _gen(self) -> Any:
            return
            yield

        async def single(self) -> dict[str, int]:
            return {"total": 0}

        async def consume(self) -> SimpleNamespace:
            return SimpleNamespace()

    class _Session:
        async def __aenter__(self) -> _Session:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def run(self, query: Any, _params: Any) -> _Result:
            seen.append(query)
            return _Result()

    driver = SimpleNamespace(session=lambda **_: _Session())
    await fn(driver, "RETURN 1 AS total")
    await fn(driver, "RETURN 1 AS total", timeout=None)
    assert isinstance(seen[0], Query)
    assert seen[0].timeout == 30.0
    assert seen[1] == "RETURN 1 AS total"


def _app_raising(exc: Exception) -> TestClient:
    app = FastAPI()
    install_neo4j_timeout_handler(app)

    @app.get("/boom")
    async def boom() -> None:
        raise exc

    return TestClient(app, raise_server_exceptions=False)


def test_transaction_timeout_is_504() -> None:
    client = _app_raising(Neo4jClientError._hydrate_neo4j(code="Neo.ClientError.Transaction.TransactionTimedOut", message="terminated"))
    response = client.get("/boom")
    assert response.status_code == 504
    assert "too long" in response.json()["error"]


def test_other_neo4j_client_errors_stay_500() -> None:
    client = _app_raising(Neo4jClientError._hydrate_neo4j(code="Neo.ClientError.Statement.SyntaxError", message="bad"))
    assert client.get("/boom").status_code == 500
