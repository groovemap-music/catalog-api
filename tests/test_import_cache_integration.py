"""The graph latch query reads the maintenance marker from a real ExtractionCompletion node (gm-catalog-api-64bl).

Nothing here touches a shared database: ``just test-integration`` starts throwaway containers.
"""

from __future__ import annotations

import pytest
from common import AsyncResilientNeo4jDriver

from api import import_cache
from api.queries.helpers import run_query
from tests.test_real_databases import neo4j_driver


__all__ = ["neo4j_driver"]

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def _node(driver: AsyncResilientNeo4jDriver, set_clause: str) -> None:
    await run_query(driver, "MATCH (n) DETACH DELETE n", database="neo4j", timeout=None)
    await run_query(
        driver,
        f"CREATE (m:ExtractionCompletion {{version: '20260801', signals: $signals}}) {set_clause}",
        database="neo4j",
        timeout=None,
        signals=list(import_cache.DISCOGS_DATA_TYPES),
    )


async def test_a_loaded_but_unmaintained_version_is_complete_but_not_maintained(neo4j_driver: AsyncResilientNeo4jDriver) -> None:
    await _node(neo4j_driver, "")
    assert await import_cache.graph_extraction_complete(neo4j_driver, "20260801") is True
    assert await import_cache.graph_maintenance(neo4j_driver, "20260801") == (False, None)


async def test_maintained_without_stats_version_then_with_it(neo4j_driver: AsyncResilientNeo4jDriver) -> None:
    await _node(neo4j_driver, "SET m.maintained_at = datetime()")
    assert await import_cache.graph_maintenance(neo4j_driver, "20260801") == (True, None)
    await run_query(neo4j_driver, "MATCH (m:ExtractionCompletion) SET m.stats_version = 1", database="neo4j", timeout=None)
    assert await import_cache.graph_maintenance(neo4j_driver, "20260801") == (True, 1)


async def test_an_unknown_version_is_not_maintained(neo4j_driver: AsyncResilientNeo4jDriver) -> None:
    await _node(neo4j_driver, "SET m.maintained_at = datetime(), m.stats_version = 1")
    assert await import_cache.graph_maintenance(neo4j_driver, "other") == (False, None)
    assert await import_cache.graph_extraction_complete(neo4j_driver, "other") is False
