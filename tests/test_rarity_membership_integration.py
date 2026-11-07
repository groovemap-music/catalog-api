"""By-label and by-artist rarity filter inside PostgreSQL, and agree with the graph path (gm-catalog-api-pk6n).

Real PostgreSQL and Neo4j, no mocks. The same group is read two ways: the new single statement over
``label_ids`` / ``artist_ids``, and the old Neo4j id list shipped as ``release_id = ANY(...)`` (reached by
saying the membership columns cannot be trusted). Both must return the same pages and totals. Also covered:
the id list never leaves Neo4j on the new path, an unknown node is still a 404, a non-numeric or out-of-range
id is not a 500, and a NULL membership column anywhere in the table sends the read back to the graph.

Nothing here touches a shared database: ``just test-integration`` starts throwaway containers.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from common import AsyncPostgreSQLPool, AsyncResilientNeo4jDriver

from api.queries import rarity_queries
from api.queries.helpers import run_query
from api.queries.rarity_queries import get_rarity_by_artist, get_rarity_by_label
from tests.test_real_databases import neo4j_driver, postgres_pool


__all__ = ["neo4j_driver", "postgres_pool"]

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

BIG_LABEL = 1818
SMALL_LABEL = 7
BIG_ARTIST = 55
RELEASES = 2500  # BIG_LABEL holds every third release, BIG_ARTIST every second one


def _labels(i: int) -> list[int]:
    return sorted({*([BIG_LABEL] if i % 3 == 0 else []), *([SMALL_LABEL] if i % 500 == 0 else []), 9000 + i % 11})


def _artists(i: int) -> list[int]:
    return sorted({*([BIG_ARTIST] if i % 2 == 0 else []), 100 + i % 17})


@pytest.fixture(autouse=True)
def _fresh_membership_cache() -> Iterator[None]:
    rarity_queries._membership_checked.update(run_id=None, complete={})
    yield
    rarity_queries._membership_checked.update(run_id=None, complete={})


async def _seed(pool: AsyncPostgreSQLPool, driver: AsyncResilientNeo4jDriver, *, null_label_row: int | None = None) -> None:
    await run_query(driver, "MATCH (n) DETACH DELETE n", database="neo4j", timeout=None)
    await run_query(
        driver,
        """UNWIND range(1, $n) AS i
           MERGE (r:Release {id: toString(i)})
           WITH r, i
           FOREACH (_ IN CASE WHEN i % 3 = 0 THEN [1] ELSE [] END | MERGE (l:Label {id: '1818'}) MERGE (r)-[:ON]->(l))
           FOREACH (_ IN CASE WHEN i % 500 = 0 THEN [1] ELSE [] END | MERGE (l:Label {id: '7'}) MERGE (r)-[:ON]->(l))
           FOREACH (_ IN CASE WHEN i % 2 = 0 THEN [1] ELSE [] END | MERGE (a:Artist {id: '55'}) MERGE (r)-[:BY]->(a))""",
        database="neo4j",
        timeout=None,
        n=RELEASES,
    )
    # A label whose id is not a number, and one no BIGINT can hold: both exist in the graph.
    await run_query(
        driver,
        """MERGE (a:Label {id: 'not-a-number'}) MERGE (b:Label {id: '99999999999999999999999'})
           WITH a, b MATCH (r:Release {id: '3'}) MERGE (r)-[:ON]->(a) MERGE (r)-[:ON]->(b)""",
        database="neo4j",
        timeout=None,
    )
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("TRUNCATE insights.release_rarity")
        await cur.execute("DELETE FROM insights.computation_log WHERE insight_type = 'release_rarity'")
        await cur.executemany(
            "INSERT INTO insights.release_rarity (release_id, title, artist_name, year, rarity_score, tier, hidden_gem_score, label_ids, artist_ids)"
            " VALUES (%s, %s, 'a', 2000, %s, 'common', 1.0, %s, %s)",
            [
                # Scores collide on purpose, so the release_id tie-break is part of what is compared.
                (i, f"t{i}", (i * 37) % 101, None if i == null_label_row else _labels(i), _artists(i))
                for i in range(1, RELEASES + 1)
            ],
        )
        await cur.execute(
            "INSERT INTO insights.computation_log (insight_type, status, started_at, completed_at, rows_affected) VALUES ('release_rarity', 'completed', now(), now(), %s)",
            (RELEASES,),
        )


async def _graph_path(read: Any, driver: AsyncResilientNeo4jDriver, pool: AsyncPostgreSQLPool, key: str, page: int, size: int) -> Any:
    with patch("api.queries.rarity_queries._membership_columns_complete", new=AsyncMock(return_value=False)):
        return await read(driver, pool, key, page, size)


@pytest.mark.parametrize(
    ("read", "key"),
    [(get_rarity_by_label, "1818"), (get_rarity_by_label, "7"), (get_rarity_by_artist, "55"), (get_rarity_by_label, "9003")],
)
@pytest.mark.parametrize(("page", "size"), [(1, 20), (7, 20), (13, 100), (999, 20)])
async def test_the_statement_matches_the_graph_path(
    read: Any, key: str, page: int, size: int, postgres_pool: AsyncPostgreSQLPool, neo4j_driver: AsyncResilientNeo4jDriver
) -> None:
    await _seed(postgres_pool, neo4j_driver)
    if read is get_rarity_by_label and key == "9003":
        # Present in release_rarity.label_ids but never given a Label node: not found, as before.
        assert await read(neo4j_driver, postgres_pool, key, page, size) is None
        return
    new = await read(neo4j_driver, postgres_pool, key, page, size)
    old = await _graph_path(read, neo4j_driver, postgres_pool, key, page, size)
    assert new is not None
    assert old is not None
    assert new[1] == old[1]
    assert new[0] == old[0]
    assert all("total" not in row for row in new[0])


async def test_totals_are_exact_and_the_id_list_never_leaves_neo4j(
    postgres_pool: AsyncPostgreSQLPool, neo4j_driver: AsyncResilientNeo4jDriver
) -> None:
    await _seed(postgres_pool, neo4j_driver)
    cyphers: list[str] = []
    real = rarity_queries.run_query

    async def spy(driver: Any, query: str, **kwargs: Any) -> Any:
        cyphers.append(query)
        return await real(driver, query, **kwargs)

    with patch("api.queries.rarity_queries.run_query", new=spy):
        label = await get_rarity_by_label(neo4j_driver, postgres_pool, "1818", 1, 20)
        past_end = await get_rarity_by_label(neo4j_driver, postgres_pool, "1818", 999, 20)
        artist = await get_rarity_by_artist(neo4j_driver, postgres_pool, "55", 1, 20)
    assert label is not None
    assert past_end is not None
    assert artist is not None
    assert label[1] == RELEASES // 3
    assert len(label[0]) == 20
    assert past_end == ([], RELEASES // 3)  # the total survives a page past the end
    assert artist[1] == RELEASES // 2
    assert len(cyphers) == 3, cyphers  # one existence lookup each, no release id list
    assert all("RETURN n.id AS id" in cypher and "r.id" not in cypher for cypher in cyphers)


async def test_unknown_nodes_are_still_not_found(postgres_pool: AsyncPostgreSQLPool, neo4j_driver: AsyncResilientNeo4jDriver) -> None:
    await _seed(postgres_pool, neo4j_driver)
    assert await get_rarity_by_label(neo4j_driver, postgres_pool, "424242", 1, 20) is None
    assert await get_rarity_by_artist(neo4j_driver, postgres_pool, "424242", 1, 20) is None
    assert await get_rarity_by_label(neo4j_driver, postgres_pool, "x'; DROP TABLE y; --", 1, 20) is None


@pytest.mark.parametrize("key", ["not-a-number", "99999999999999999999999"])
async def test_a_label_id_a_bigint_cannot_hold_is_not_a_500(
    key: str, postgres_pool: AsyncPostgreSQLPool, neo4j_driver: AsyncResilientNeo4jDriver
) -> None:
    await _seed(postgres_pool, neo4j_driver)
    result = await get_rarity_by_label(neo4j_driver, postgres_pool, key, 1, 20)
    assert result is not None
    assert [row["release_id"] for row in result[0]] == [3]  # answered from the graph; only release 3 is on it
    assert result[1] == 1


async def test_a_null_membership_row_sends_the_read_back_to_the_graph(
    postgres_pool: AsyncPostgreSQLPool, neo4j_driver: AsyncResilientNeo4jDriver
) -> None:
    await _seed(postgres_pool, neo4j_driver, null_label_row=3)  # release 3 is on label 1818 but its label_ids is NULL
    got = await get_rarity_by_label(neo4j_driver, postgres_pool, "1818", 1, 100)
    assert got is not None
    assert got[1] == RELEASES // 3  # an @> filter alone would answer one short
    assert 3 in {row["release_id"] for row in (await get_rarity_by_label(neo4j_driver, postgres_pool, "1818", 1, 1000))[0]}  # type: ignore[index]
    # artist_ids is complete on every row, so the artist read still takes the statement.
    assert await rarity_queries._membership_columns_complete(postgres_pool, "artist_ids") is True
    assert await rarity_queries._membership_columns_complete(postgres_pool, "label_ids") is False


async def test_no_completed_run_means_the_columns_cannot_be_trusted(
    postgres_pool: AsyncPostgreSQLPool, neo4j_driver: AsyncResilientNeo4jDriver
) -> None:
    await _seed(postgres_pool, neo4j_driver)
    async with postgres_pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("DELETE FROM insights.computation_log WHERE insight_type = 'release_rarity'")
    assert await rarity_queries._membership_columns_complete(postgres_pool, "label_ids") is False
    got = await get_rarity_by_label(neo4j_driver, postgres_pool, "1818", 1, 20)
    assert got is not None
    assert got[1] == RELEASES // 3
