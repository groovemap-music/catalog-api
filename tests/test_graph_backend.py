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
from api.graph_backend import (
    GraphBackendUnavailableError,
    get_backend,
    get_collaborators_backend,
    verify_postgres_graph_backend,
)
from api.queries import network_pg_queries, network_queries
from tests.fake_postgres import FakePool


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

    def test_collaborators_family_resolves_to_the_neo4j_implementation(self) -> None:
        assert get_backend("collaborators", "neo4j") is network_queries

    def test_collaborators_family_resolves_to_the_postgres_implementation(self) -> None:
        assert get_backend("collaborators", "postgres") is network_pg_queries

    def test_typed_accessor_returns_the_same_module_object(self) -> None:
        # The typed accessor exists for mypy, not for runtime behavior: it must hand back
        # the module itself so `unittest.mock.patch` on a module attribute still lands.
        assert get_collaborators_backend("neo4j") is network_queries
        assert get_collaborators_backend("postgres") is network_pg_queries

    def test_unknown_family_raises(self) -> None:
        with pytest.raises(KeyError, match="Unknown graph query family"):
            get_backend("not-a-family", "neo4j")

    def test_unregistered_backend_for_known_family_raises(self) -> None:
        with pytest.raises(KeyError, match="No 'mysql' implementation registered"):
            get_backend("collaborators", "mysql")


class TestVerifyPostgresGraphBackend:
    """`verify_postgres_graph_backend` — the startup gate for GRAPH_BACKEND=postgres.

    Both conditions mirror the ones the schema producer applies when it decides whether to
    declare `graph.catalog`, read here from the consumer's side.
    """

    @pytest.mark.asyncio
    async def test_passes_on_postgresql_19_with_the_property_graph_present(self) -> None:
        pool = FakePool([[(190000,)], [(True,)]])
        await verify_postgres_graph_backend(pool)
        assert [call.sql for call in pool.calls] == [
            "SELECT current_setting('server_version_num')::int",
            pool.calls[1].sql,
        ]
        assert pool.calls[1].params == ("graph", "catalog")

    @pytest.mark.asyncio
    async def test_rejects_a_server_older_than_postgresql_19(self) -> None:
        pool = FakePool([[(180004,)]])
        with pytest.raises(GraphBackendUnavailableError) as failure:
            await verify_postgres_graph_backend(pool)
        message = str(failure.value)
        assert "PostgreSQL 19" in message
        assert "180004" in message
        assert "GRAPH_BACKEND=neo4j" in message
        # The version gate short-circuits: no point probing for a graph the server cannot hold.
        assert len(pool.calls) == 1

    @pytest.mark.asyncio
    async def test_rejects_postgresql_19_without_the_property_graph(self) -> None:
        pool = FakePool([[(190000,)], [(False,)]])
        with pytest.raises(GraphBackendUnavailableError) as failure:
            await verify_postgres_graph_backend(pool)
        message = str(failure.value)
        assert "graph.catalog" in message
        assert "SCHEMA_PROPERTY_GRAPH" in message

    @pytest.mark.asyncio
    async def test_treats_an_unreadable_server_version_as_too_old(self) -> None:
        pool = FakePool([[]])
        with pytest.raises(GraphBackendUnavailableError, match="server_version_num 0"):
            await verify_postgres_graph_backend(pool)
