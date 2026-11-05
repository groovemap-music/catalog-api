"""The index-order ``before_year`` page equals the page it replaced, on a real Neo4j (audit H7, ``gm-catalog-api-9d8z``).

``ORDER BY year IS NULL ASC, year DESC, id`` sorted every release; with ``before_year`` no year can be null,
so ``r.year DESC, r.id`` is meant to be the same order. The old text is the oracle, over every page, with ties
on year, ids that sort differently as strings than as numbers, and releases the filter must drop.

Nothing here touches a shared database: ``just test-integration`` starts throwaway containers.
"""

from __future__ import annotations

import pytest
from common import AsyncResilientNeo4jDriver

from api.queries import neo4j_queries as q
from api.queries.helpers import run_query
from tests.test_real_databases import neo4j_driver


__all__ = ["neo4j_driver"]

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_OLD = """
MATCH (r:Release)-[:IS]->(g:Genre {name: $name}) WHERE r.year <= $before_year AND r.year > 0
RETURN r.id AS id, r.title AS name, 'release' AS type, CASE WHEN r.year > 0 THEN r.year ELSE null END AS year
ORDER BY year IS NULL ASC, year DESC, id
SKIP $offset LIMIT $limit
"""

# 40 releases: years cycle 1950-1999 with many ties, ids "r1".."r40" (so "r10" < "r2"), some year 0 or missing.
_SEED = """
MERGE (g:Genre {name: 'Rock'})
WITH g UNWIND range(1, 40) AS i
CREATE (r:Release {id: 'r' + toString(i), title: 'T' + toString(i)})
SET r.year = CASE WHEN i % 9 = 0 THEN 0 WHEN i % 11 = 0 THEN null ELSE 1950 + (i * 7) % 50 END
CREATE (r)-[:IS]->(g)
"""


async def test_every_page_matches_the_old_order(neo4j_driver: AsyncResilientNeo4jDriver) -> None:
    await run_query(neo4j_driver, "MATCH (n) DETACH DELETE n", database="neo4j", timeout=None)
    await run_query(neo4j_driver, _SEED, database="neo4j", timeout=None)
    for before_year in (1960, 1975, 1999):
        old_all = await run_query(neo4j_driver, _OLD, database="neo4j", name="Rock", before_year=before_year, offset=0, limit=500)
        assert old_all, "the seed must leave rows to compare"
        for limit, offset in [(7, 0), (7, 7), (7, 14), (50, 0), (3, 10)]:
            old = await run_query(neo4j_driver, _OLD, database="neo4j", name="Rock", before_year=before_year, offset=offset, limit=limit)
            new = await q.expand_genre_releases(neo4j_driver, "Rock", limit, offset, before_year=before_year)
            assert new == old, (before_year, limit, offset)
        assert await q.expand_genre_releases(neo4j_driver, "Rock", 500, 0, before_year=before_year) == old_all
        assert all(r["year"] and r["year"] > 0 for r in old_all)
