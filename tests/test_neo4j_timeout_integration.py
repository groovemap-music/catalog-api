"""A real Neo4j terminates a read that outlives the default budget (audit C1, ``gm-catalog-api-jpyk``).

A mock cannot show that the server, not just the client, gave up: this runs a deliberately endless read
against a throwaway Neo4j and checks that the transaction is terminated and no longer listed.
Nothing here touches a shared database: ``just test-integration`` starts throwaway containers.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from common import AsyncResilientNeo4jDriver
from neo4j.exceptions import ClientError as Neo4jClientError

from api.neo4j_timeouts import is_transaction_timeout
from api.queries import helpers
from tests.test_real_databases import neo4j_driver


__all__ = ["neo4j_driver"]

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_ENDLESS_READ = "UNWIND range(1, 4000000000) AS n WITH n WHERE n % 7 = 0 RETURN count(n) AS total"
_BUDGET_SECONDS = 2.0


async def _running_endless_reads(driver: AsyncResilientNeo4jDriver) -> int:
    rows = await helpers.run_query(
        driver, "SHOW TRANSACTIONS YIELD currentQuery WHERE currentQuery CONTAINS 'range(1, 4000000000)' RETURN currentQuery", database="system"
    )
    return len([r for r in rows if "SHOW TRANSACTIONS" not in r["currentQuery"]])


async def test_default_budget_terminates_a_slow_read_and_neo4j_lists_no_survivor(
    neo4j_driver: AsyncResilientNeo4jDriver, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(helpers, "DEFAULT_READ_TIMEOUT_SECONDS", _BUDGET_SECONDS)

    started = time.monotonic()
    with pytest.raises(Neo4jClientError) as caught:
        await helpers.run_single(neo4j_driver, _ENDLESS_READ, database="neo4j")
    elapsed = time.monotonic() - started

    assert is_transaction_timeout(caught.value), caught.value.code
    assert _BUDGET_SECONDS <= elapsed < _BUDGET_SECONDS + 10
    await asyncio.sleep(1)
    assert await _running_endless_reads(neo4j_driver) == 0
