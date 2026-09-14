"""Contract tests for the shared database-boundary fixtures."""

from unittest.mock import MagicMock

import pytest


@pytest.mark.asyncio
async def test_postgres_fixture_chain_matches_runtime_protocol(
    mock_pool: MagicMock,
    mock_conn: MagicMock,
    mock_cur: MagicMock,
    mock_transaction: MagicMock,
) -> None:
    async with mock_pool.connection() as connection:
        async with connection.cursor() as cursor:
            await cursor.execute("SELECT 1")
        async with connection.transaction() as transaction:
            assert transaction is mock_transaction

    assert connection is mock_conn
    assert cursor is mock_cur
    mock_pool.connection.assert_called_once_with()
    mock_conn.cursor.assert_called_once_with()
    mock_cur.execute.assert_awaited_once_with("SELECT 1")
    with pytest.raises(AttributeError):
        _missing_pool_method = mock_pool.not_a_runtime_pool_method


@pytest.mark.asyncio
async def test_neo4j_fixture_chain_matches_runtime_protocol(
    mock_neo4j: MagicMock,
    mock_neo4j_session: MagicMock,
    mock_neo4j_result: MagicMock,
) -> None:
    async with mock_neo4j.session(database="neo4j") as session:
        result = await session.run("RETURN 1")
        records = [record async for record in result]

    assert session is mock_neo4j_session
    assert result is mock_neo4j_result
    assert records == []
    mock_neo4j.session.assert_called_once_with(database="neo4j")
    mock_neo4j_session.run.assert_awaited_once_with("RETURN 1")
    with pytest.raises(AttributeError):
        _missing_driver_method = mock_neo4j.not_a_runtime_driver_method
