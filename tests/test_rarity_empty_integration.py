"""The empty-table probe and the by-label and by-artist reads agree with a real PostgreSQL and Neo4j.

``gm-catalog-api-2x1z``: on lux ``insights.release_rarity`` is empty until the first rarity run, and
``/api/rarity/label/{id}`` still read every release id of the label out of Neo4j before the SQL could
return nothing. The reads now probe the table first. These tests run the probe on the real schema, empty
and populated, and run both reads end to end on a real Neo4j, including the case that must not change: a
node that does not exist is still not found.

Nothing here touches a shared database: ``just test-integration`` starts throwaway containers.
"""

from __future__ import annotations

import pytest
from common import AsyncPostgreSQLPool, AsyncResilientNeo4jDriver

from api.queries.helpers import run_query
from api.queries.rarity_queries import _release_rarity_has_rows, get_rarity_by_artist, get_rarity_by_label
from tests.test_real_databases import neo4j_driver, postgres_pool


__all__ = ["neo4j_driver", "postgres_pool"]

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def _graph(driver: AsyncResilientNeo4jDriver) -> None:
    await run_query(driver, "MATCH (n) DETACH DELETE n", database="neo4j", timeout=None)
    await run_query(
        driver,
        """CREATE (l:Label {id: 'L1', name: 'Label'}), (a:Artist {id: 'A1', name: 'Artist'})
           WITH l, a UNWIND [1, 2, 3] AS i
           CREATE (r:Release {id: toString(i)})-[:ON]->(l), (r)-[:BY]->(a)""",
        database="neo4j",
        timeout=None,
    )


async def test_the_probe_sees_an_empty_and_a_populated_table(postgres_pool: AsyncPostgreSQLPool) -> None:
    async with postgres_pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("TRUNCATE insights.release_rarity")
    assert await _release_rarity_has_rows(postgres_pool) is False

    async with postgres_pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("INSERT INTO insights.release_rarity (release_id, rarity_score, tier) VALUES (1, 50, 'common')")
    assert await _release_rarity_has_rows(postgres_pool) is True


async def test_reads_end_to_end_on_empty_then_populated_tables(postgres_pool: AsyncPostgreSQLPool, neo4j_driver: AsyncResilientNeo4jDriver) -> None:
    await _graph(neo4j_driver)
    async with postgres_pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("TRUNCATE insights.release_rarity")

    assert await get_rarity_by_label(neo4j_driver, postgres_pool, "L1") == ([], 0)
    assert await get_rarity_by_artist(neo4j_driver, postgres_pool, "A1") == ([], 0)
    assert await get_rarity_by_label(neo4j_driver, postgres_pool, "missing") is None
    assert await get_rarity_by_artist(neo4j_driver, postgres_pool, "missing") is None

    async with postgres_pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO insights.release_rarity (release_id, title, rarity_score, tier) VALUES (1, 'a', 90, 'rare'), (2, 'b', 10, 'common'), (9, 'x', 99, 'rare')"
        )
    label = await get_rarity_by_label(neo4j_driver, postgres_pool, "L1")
    artist = await get_rarity_by_artist(neo4j_driver, postgres_pool, "A1")
    assert label is not None
    assert artist is not None
    assert [row["release_id"] for row in label[0]] == [1, 2]  # release 9 is not on the label
    assert label[1] == 2
    assert [row["release_id"] for row in artist[0]] == [1, 2]
    assert artist[1] == 2
