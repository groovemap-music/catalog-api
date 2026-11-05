"""Credit pages and ranked second hops, on a real Neo4j (audit H12, ``gm-catalog-api-b78b``).

The old statement returned every credit at once and kept an arbitrary ten second hops per connection. Its text is the
oracle for the rows; the new pages must be those rows exactly, walkable to the end, and the second hops must be the true
best ten, ranked before they are cut. Nothing here touches a shared database: ``just test-integration`` starts
throwaway containers.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

import pytest
from common import AsyncResilientNeo4jDriver

from api.queries import credits_queries as cq
from api.queries.helpers import run_query
from tests.test_real_databases import neo4j_driver


__all__ = ["neo4j_driver"]

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_OLD_CREDITS = """
MATCH (p:Person {name: $name})-[c:CREDITED_ON]->(r:Release)
OPTIONAL MATCH (r)-[:BY]->(a:Artist)
OPTIONAL MATCH (r)-[:ON]->(l:Label)
RETURN r.id AS release_id, r.title AS title, r.year AS year, c.role AS role, c.category AS category,
       collect(DISTINCT a.name)[..3] AS artists, collect(DISTINCT l.name)[..1] AS labels
"""

# Ana is credited on 37 releases: years with ties and a null year, two roles on some releases, one duplicated edge.
_SEED_CREDITS = """
CREATE (ana:Person {name: 'Ana'})
WITH ana
UNWIND range(1, 37) AS i
CREATE (r:Release {id: 'r' + toString(i), title: 'T' + toString(i % 5), year: CASE WHEN i % 11 = 0 THEN null ELSE 1990 + i % 6 END})
CREATE (ar:Artist {name: 'A' + toString(i)}), (lb:Label {name: 'L' + toString(i)})
CREATE (r)-[:BY]->(ar), (r)-[:ON]->(lb)
CREATE (ana)-[:CREDITED_ON {role: 'Mastered By', category: 'mastering'}]->(r)
WITH ana, r, i
WHERE i % 4 = 0
CREATE (ana)-[:CREDITED_ON {role: 'Producer', category: 'production'}]->(r)
"""


async def _seed_credits(driver: AsyncResilientNeo4jDriver, *, duplicate: bool = True) -> None:
    await run_query(driver, "MATCH (n) DETACH DELETE n", database="neo4j", timeout=None)
    await run_query(driver, _SEED_CREDITS, database="neo4j", timeout=None)
    # The same credit recorded twice must still be one row, as the old grouping made it.
    if duplicate:
        await run_query(
            driver,
            "MATCH (:Person {name: 'Ana'})-[:CREDITED_ON]->(r:Release {id: 'r1'}), (a:Person {name: 'Ana'}) CREATE (a)-[:CREDITED_ON {role: 'Mastered By', category: 'mastering'}]->(r)",
            database="neo4j",
            timeout=None,
        )


def _key(row: dict[str, Any]) -> tuple[Any, ...]:
    return (row["release_id"], row["role"], row["category"])


async def test_pages_are_exactly_the_old_rows_and_walk_to_the_end(neo4j_driver: AsyncResilientNeo4jDriver) -> None:
    await _seed_credits(neo4j_driver)
    old = await run_query(neo4j_driver, _OLD_CREDITS, database="neo4j", name="Ana")
    total = await cq.count_person_credits(neo4j_driver, "Ana")
    assert len(old) == 37 + 9  # 37 mastering rows and 9 producer rows (i % 4 == 0); the repeated edge collapses
    assert total == len(old) + 1  # the total counts credit records: the one credit stored twice counts twice

    for limit in (1, 7, 10, 100):
        walked: list[dict[str, Any]] = []
        offset = 0
        while True:
            page = await cq.get_person_credits(neo4j_driver, "Ana", limit, offset)
            assert len(page) <= limit
            walked += page
            if offset + limit >= total:
                break
            offset += limit
        assert len(walked) <= total
        # Every credit is reached, and each row is the old row. A credit stored twice may show twice when its two
        # records straddle a page boundary; inside a page it collapses to one row, as the old grouping made it.
        as_text = lambda rows: {str(sorted(r.items())) for r in rows}  # noqa: E731
        assert as_text(walked) == as_text(old), limit


async def test_pages_follow_one_total_order(neo4j_driver: AsyncResilientNeo4jDriver) -> None:
    await _seed_credits(neo4j_driver, duplicate=False)
    everything = await cq.get_person_credits(neo4j_driver, "Ana", 500, 0)
    assert await cq.get_person_credits(neo4j_driver, "Ana", 500, 0) == everything  # deterministic
    for offset in (5, 20, 40):
        page = await cq.get_person_credits(neo4j_driver, "Ana", 5, offset)
        assert [_key(r) for r in page] == [_key(r) for r in everything[offset : offset + 5]]
    assert await cq.get_person_credits(neo4j_driver, "Ana", 10, 1000) == []
    assert await cq.count_person_credits(neo4j_driver, "Nobody") == 0


# Bob shares releases with p1..p15 (p_i shares i releases). Each p_i also shares with q1..q15 (q_j shares j releases with p_i,
# so a second hop's rank is j) and with Bob's other connections, which are not new people.
_SEED_HOPS = """
CREATE (bob:Person {name: 'Bob'})
WITH bob
UNWIND range(1, 15) AS i
CREATE (p:Person {name: 'p' + right('0' + toString(i), 2)})
WITH bob, p, i
UNWIND range(1, i) AS k
CREATE (r:Release {id: 'bp' + p.name + '-' + toString(k)})
CREATE (bob)-[:CREDITED_ON]->(r), (p)-[:CREDITED_ON]->(r)
"""
_SEED_SECOND = """
MATCH (p:Person {name: 'p15'})
UNWIND range(1, 15) AS j
MERGE (q:Person {name: 'q' + right('0' + toString(j), 2)})
WITH p, q, j
UNWIND range(1, j) AS k
CREATE (r:Release {id: 'pq' + q.name + '-' + toString(k)})
CREATE (p)-[:CREDITED_ON]->(r), (q)-[:CREDITED_ON]->(r)
"""


async def _seed_hops(driver: AsyncResilientNeo4jDriver) -> None:
    await run_query(driver, "MATCH (n) DETACH DELETE n", database="neo4j", timeout=None)
    await run_query(driver, _SEED_HOPS, database="neo4j", timeout=None)
    await run_query(driver, _SEED_SECOND, database="neo4j", timeout=None)


async def test_connections_rank_by_shared_releases_with_a_total_order(neo4j_driver: AsyncResilientNeo4jDriver) -> None:
    await _seed_hops(neo4j_driver)
    rows = await cq.get_person_connections(neo4j_driver, "Bob", depth=1, limit=50)
    assert [(r["name"], r["shared_count"]) for r in rows] == [(f"p{i:02d}", i) for i in range(15, 0, -1)]
    assert all("second_hops" not in r for r in rows)
    top3 = await cq.get_person_connections(neo4j_driver, "Bob", depth=1, limit=3)
    assert [r["name"] for r in top3] == ["p15", "p14", "p13"]


async def test_second_hops_are_the_true_best_ten_ranked_before_the_cut(neo4j_driver: AsyncResilientNeo4jDriver) -> None:
    await _seed_hops(neo4j_driver)
    rows = await cq.get_person_connections(neo4j_driver, "Bob", depth=2, limit=50)
    by_name = {r["name"]: r for r in rows}
    # p15 reaches q01..q15 with 1..15 shared releases, and Bob's other connections p01..p14 through Bob? No: through
    # their own releases only, and those are shared with Bob alone. The candidate pool is q01..q15; the best ten are q15..q06.
    assert by_name["p15"]["second_hops"] == [{"name": f"q{j:02d}", "via": "p15", "shared": j} for j in range(15, 5, -1)]
    assert by_name["p14"]["second_hops"] == []  # connected only through Bob himself
    assert [r["name"] for r in rows] == [f"p{i:02d}" for i in range(15, 0, -1)]  # connection order unchanged by second hops
    assert [(r["name"], r["shared_count"]) for r in rows] == [(f"p{i:02d}", i) for i in range(15, 0, -1)]
    # Never the root or the connection itself:
    flat = [h["name"] for r in rows for h in r["second_hops"]]
    assert "Bob" not in flat and Counter(flat)["p15"] == 0


async def test_depth_without_hops_is_refused(neo4j_driver: AsyncResilientNeo4jDriver) -> None:
    with pytest.raises(ValueError, match="depth must be between 1 and 2"):
        await cq.get_person_connections(neo4j_driver, "Bob", depth=3)
