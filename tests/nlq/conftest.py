"""Shared fixtures for NLQ tests."""

from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture
def mock_neo4j_driver(mock_neo4j: MagicMock) -> MagicMock:
    """Reuse the interface-faithful Neo4j driver chain for NLQ tests."""
    return mock_neo4j


@pytest.fixture
def mock_pg_pool(mock_pool: MagicMock) -> MagicMock:
    """Reuse the interface-faithful PostgreSQL pool chain for NLQ tests."""
    return mock_pool


@pytest.fixture
def mock_valkey_client() -> AsyncMock:
    """Mock Valkey client for NLQ tool tests."""
    valkey: AsyncMock = AsyncMock()
    valkey.get = AsyncMock(return_value=None)
    valkey.setex = AsyncMock()
    return valkey
