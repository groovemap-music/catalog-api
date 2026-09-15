"""The shared check both consumer route registries are verified with.

A registry entry promises a method at a path, and — since ADR 0011 — may additionally
promise the query parameters a consumer builds its own surface on top of. The check is one
function rather than two copies so a registry that learns a third kind of promise teaches
both consumers at once.
"""

from typing import Any


def assert_contract_matches_openapi(operations: dict[str, Any], paths: dict[str, Any]) -> None:
    """Assert every promised operation exists in the served OpenAPI document.

    Args:
        operations: The ``operations`` mapping from a consumer's ``routes.json``.
        paths: ``app.openapi()["paths"]``.
    """
    for name, operation in operations.items():
        path = operation["path"]
        method = operation["method"].lower()
        assert path in paths, name
        assert method in paths[path], name

        promised = operation.get("parameters")
        if not promised:
            continue
        served = {parameter["name"] for parameter in paths[path][method].get("parameters", [])}
        missing = sorted(set(promised) - served)
        assert not missing, f"{name}: {path} no longer accepts {', '.join(missing)}"
