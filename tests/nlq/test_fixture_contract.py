"""Contract tests for the NLQ database fixture aliases."""

from unittest.mock import MagicMock


def test_nlq_uses_shared_interface_faithful_database_chains(
    mock_neo4j_driver: MagicMock,
    mock_neo4j: MagicMock,
    mock_pg_pool: MagicMock,
    mock_pool: MagicMock,
) -> None:
    assert mock_neo4j_driver is mock_neo4j
    assert mock_pg_pool is mock_pool
