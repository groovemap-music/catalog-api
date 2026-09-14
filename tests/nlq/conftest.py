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
def mock_redis_client() -> AsyncMock:
    """Mock Redis client for NLQ tool tests."""
    redis: AsyncMock = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    redis.setex = AsyncMock()
    return redis
