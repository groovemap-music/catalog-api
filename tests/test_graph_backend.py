"""Tests for the graph-backend seam: `ApiConfig.graph_backend` and the family selector.

Phase 0 of ADR 0012 (Neo4j -> PostgreSQL 19 property graph migration). No backend
behavior changes here — this only proves the selector resolves the way later beads
depend on.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import psycopg
import pytest
from common.db_resilience import CircuitOpenError, ConnectionEstablishmentError, DatabaseUnavailableError
from neo4j.exceptions import ClientError as Neo4jClientError
from neo4j.exceptions import ServiceUnavailable as Neo4jServiceUnavailable
from neo4j.exceptions import SessionExpired as Neo4jSessionExpired

from api.config import ApiConfig
from api.graph_backend import (
    GRAPH_BACKEND_ERROR_TYPES,
    GraphBackendUnavailableError,
    get_backend,
    get_collaborators_backend,
    is_graph_backend_unavailable,
    is_graph_query_timeout,
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


class TestIsGraphQueryTimeout:
    """`is_graph_query_timeout` — the backend-neutral *query*-timeout predicate.

    A router catches `GRAPH_BACKEND_ERROR_TYPES`, then asks this predicate whether the
    caught exception is a specific query running out of time (→ 504) — as opposed to the
    backend being unreachable at all (`is_graph_backend_unavailable`, → 503) or a genuine
    backend bug (→ re-raise, 500). Every case here is a member of `GRAPH_BACKEND_ERROR_TYPES`.
    """

    def test_neo4j_transaction_timed_out_is_a_timeout(self) -> None:
        assert is_graph_query_timeout(Neo4jClientError("TransactionTimedOut")) is True

    def test_neo4j_transaction_timed_out_client_configuration_is_a_timeout(self) -> None:
        # Substring match, so the client-configuration variant of the same code counts too.
        assert is_graph_query_timeout(Neo4jClientError("TransactionTimedOutClientConfiguration")) is True

    def test_neo4j_other_client_error_is_not_a_timeout(self) -> None:
        assert is_graph_query_timeout(Neo4jClientError("SomeOtherError")) is False

    def test_postgres_query_canceled_is_a_timeout(self) -> None:
        # SQLSTATE 57014 — the server cancelled the statement because it hit
        # `statement_timeout`. This is psycopg's exact exception shape for that.
        assert is_graph_query_timeout(psycopg.errors.QueryCanceled("canceling statement due to statement timeout")) is True

    def test_postgres_connection_establishment_error_is_not_a_timeout(self) -> None:
        # This is backend UNAVAILABILITY, not a query timeout: no query ever ran, so it
        # must not get the 504/"try reducing depth or limit" message — see
        # `TestIsGraphBackendUnavailable.test_postgres_connection_establishment_error_is_unavailable`.
        assert is_graph_query_timeout(ConnectionEstablishmentError("Failed to get PostgreSQL connection after 5 attempts")) is False

    def test_postgres_circuit_open_error_is_not_a_timeout(self) -> None:
        assert is_graph_query_timeout(CircuitOpenError("AsyncPostgreSQL: Circuit breaker is OPEN")) is False

    def test_postgres_bare_database_unavailable_error_is_not_a_timeout(self) -> None:
        assert is_graph_query_timeout(DatabaseUnavailableError("database unavailable")) is False

    def test_neo4j_service_unavailable_is_not_a_timeout(self) -> None:
        assert is_graph_query_timeout(Neo4jServiceUnavailable("Failed to establish connection")) is False

    def test_neo4j_session_expired_is_not_a_timeout(self) -> None:
        assert is_graph_query_timeout(Neo4jSessionExpired("Session expired")) is False

    def test_postgres_non_timeout_operational_error_is_not_a_timeout(self) -> None:
        # A real backend problem (e.g. the connection was dropped mid-query) — must
        # re-raise to a 500, not be swallowed into the same 504 as an actual timeout.
        assert is_graph_query_timeout(psycopg.OperationalError("server closed the connection unexpectedly")) is False

    def test_postgres_programming_error_is_not_a_timeout(self) -> None:
        assert is_graph_query_timeout(psycopg.errors.UndefinedTable("relation does not exist")) is False

    def test_every_timeout_case_is_also_a_caught_backend_error_type(self) -> None:
        # `GRAPH_BACKEND_ERROR_TYPES` is what a router's `except` clause names; if a case
        # above were not a member, the router would never call `is_graph_query_timeout` on
        # it in the first place and it would escape uncaught instead of becoming a 500.
        timeouts: list[BaseException] = [
            Neo4jClientError("TransactionTimedOut"),
            psycopg.errors.QueryCanceled("canceling statement due to statement timeout"),
        ]
        for exc in timeouts:
            assert isinstance(exc, GRAPH_BACKEND_ERROR_TYPES)


class TestIsGraphBackendUnavailable:
    """`is_graph_backend_unavailable` — the backend-neutral *unreachability* predicate.

    Distinct from `is_graph_query_timeout`: this covers the backend itself failing to
    respond at all — no statement ever ran — so a router reacts with the same 503 an
    unconfigured backend already returns, never the query-timeout 504.
    """

    def test_neo4j_service_unavailable_is_unavailable(self) -> None:
        assert is_graph_backend_unavailable(Neo4jServiceUnavailable("Failed to establish connection")) is True

    def test_neo4j_session_expired_is_unavailable(self) -> None:
        assert is_graph_backend_unavailable(Neo4jSessionExpired("Session expired")) is True

    def test_neo4j_transaction_timed_out_is_not_unavailable(self) -> None:
        # A `ClientError` timeout is a query-level failure, not a reachability failure.
        assert is_graph_backend_unavailable(Neo4jClientError("TransactionTimedOut")) is False

    def test_neo4j_other_client_error_is_not_unavailable(self) -> None:
        assert is_graph_backend_unavailable(Neo4jClientError("SomeOtherError")) is False

    def test_postgres_connection_establishment_error_is_unavailable(self) -> None:
        # The pool's own "gave up waiting for a connection" shape, raised after it
        # exhausts its checkout retries.
        assert is_graph_backend_unavailable(ConnectionEstablishmentError("Failed to get PostgreSQL connection after 5 attempts")) is True

    def test_postgres_circuit_open_error_is_unavailable(self) -> None:
        assert is_graph_backend_unavailable(CircuitOpenError("AsyncPostgreSQL: Circuit breaker is OPEN")) is True

    def test_postgres_bare_database_unavailable_error_is_unavailable(self) -> None:
        assert is_graph_backend_unavailable(DatabaseUnavailableError("database unavailable")) is True

    def test_postgres_query_canceled_is_not_unavailable(self) -> None:
        # A statement timeout is a query-level failure, not a reachability failure.
        assert is_graph_backend_unavailable(psycopg.errors.QueryCanceled("canceling statement due to statement timeout")) is False

    def test_postgres_non_timeout_operational_error_is_not_unavailable(self) -> None:
        assert is_graph_backend_unavailable(psycopg.OperationalError("server closed the connection unexpectedly")) is False

    def test_postgres_programming_error_is_not_unavailable(self) -> None:
        assert is_graph_backend_unavailable(psycopg.errors.UndefinedTable("relation does not exist")) is False

    def test_every_unavailable_case_is_also_a_caught_backend_error_type(self) -> None:
        # As with the timeout predicate: if a case here were not a member of
        # `GRAPH_BACKEND_ERROR_TYPES`, a router would never reach this predicate at all.
        unavailable: list[BaseException] = [
            Neo4jServiceUnavailable("Failed to establish connection"),
            Neo4jSessionExpired("Session expired"),
            ConnectionEstablishmentError("Failed to get PostgreSQL connection after 5 attempts"),
            CircuitOpenError("AsyncPostgreSQL: Circuit breaker is OPEN"),
        ]
        for exc in unavailable:
            assert isinstance(exc, GRAPH_BACKEND_ERROR_TYPES)

    def test_timeout_and_unavailable_are_mutually_exclusive(self) -> None:
        # No case is ever both — a router's if/elif ordering does not matter.
        cases: list[BaseException] = [
            Neo4jClientError("TransactionTimedOut"),
            Neo4jClientError("SomeOtherError"),
            Neo4jServiceUnavailable("Failed to establish connection"),
            Neo4jSessionExpired("Session expired"),
            psycopg.errors.QueryCanceled("canceling statement due to statement timeout"),
            psycopg.OperationalError("server closed the connection unexpectedly"),
            psycopg.errors.UndefinedTable("relation does not exist"),
            ConnectionEstablishmentError("Failed to get PostgreSQL connection after 5 attempts"),
            CircuitOpenError("AsyncPostgreSQL: Circuit breaker is OPEN"),
            DatabaseUnavailableError("database unavailable"),
        ]
        for exc in cases:
            assert not (is_graph_query_timeout(exc) and is_graph_backend_unavailable(exc))
