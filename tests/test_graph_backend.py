"""Tests for the graph-backend seam: `ApiConfig.graph_backend` and the family selector.

Phase 0 of ADR 0012 (Neo4j -> PostgreSQL 19 property graph migration). No backend
behavior changes here — this only proves the selector resolves the way later beads
depend on.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from api.config import ApiConfig
from api.graph_backend import get_backend
from api.queries import network_queries


REQUIRED_ENV = {
    "POSTGRES_HOST": "localhost",
    "POSTGRES_USERNAME": "test",
    "POSTGRES_PASSWORD": "test",
    "POSTGRES_DATABASE": "test",
    "JWT_SECRET_KEY": "secret",
    "NEO4J_HOST": "localhost",
    "NEO4J_USERNAME": "neo4j",
    "NEO4J_PASSWORD": "pass",
}


def _config(**overrides: str) -> ApiConfig:
    environment = {**REQUIRED_ENV, **overrides}
    with patch.dict(os.environ, environment, clear=True):
        return ApiConfig.from_env()


def test_graph_backend_defaults_to_neo4j() -> None:
    assert _config().graph_backend == "neo4j"


@pytest.mark.parametrize("value", ["neo4j", "postgres"])
def test_graph_backend_accepts_valid_values(value: str) -> None:
    assert _config(GRAPH_BACKEND=value).graph_backend == value


def test_graph_backend_rejects_invalid_value() -> None:
    with pytest.raises(ValueError, match="Unsupported graph backend"):
        _config(GRAPH_BACKEND="mysql")


class TestGetBackend:
    """`api.graph_backend.get_backend` — the per-family selector."""

    @pytest.mark.parametrize("backend", ["neo4j", "postgres"])
    def test_collaborators_family_resolves_to_neo4j_implementation(self, backend: str) -> None:
        # Both values resolve to the Neo4j implementation until the PostgreSQL pilot
        # (gm-catalog-api-0da.2) registers "postgres" separately.
        assert get_backend("collaborators", backend) is network_queries

    def test_unknown_family_raises(self) -> None:
        with pytest.raises(KeyError, match="Unknown graph query family"):
            get_backend("not-a-family", "neo4j")

    def test_unregistered_backend_for_known_family_raises(self) -> None:
        with pytest.raises(KeyError, match="No 'mysql' implementation registered"):
            get_backend("collaborators", "mysql")
