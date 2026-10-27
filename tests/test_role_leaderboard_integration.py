"""Engine-backed checks for the precomputed role leaderboard (gm-catalog-api-f2g9).

Runs on the integration tier (`just test-integration`). The seeded graph is counted two ways
on a real Neo4j: the live ``CREDITED_ON`` count, and the ``Person.credit_count_<category>``
properties written with the discogs-graph-enricher recompute
(``graphinator.person_credits.build_credit_count_cypher``, reproduced inline).

The schema revision pinned for this repo (database-schema 00b74e7) predates the
``person_credit_count_<category>`` indexes, so the fixture creates them with the same DDL
database-schema now publishes; the EXPLAIN test therefore proves the query shape can use
those indexes, not that the pinned schema creates them.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from common import AsyncResilientNeo4jDriver
from common.credit_roles import ALL_CATEGORIES

from api.queries import credits_queries
from api.queries.credits_queries import _LIVE_LEADERBOARD_CYPHER, get_role_leaderboard
from api.queries.helpers import run_query


pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

# (person, release, role, category)
CREDITS = [
    ("Alice", "r1", "Mastered By", "mastering"),
    ("Alice", "r2", "Mastered By", "mastering"),
    ("Alice", "r3", "Mastered By", "mastering"),
    ("Alice", "r1", "Producer", "production"),
    # two roles in one category on the same release: counts once
    ("Bob", "r1", "Mastered By", "mastering"),
    ("Bob", "r1", "Lacquer Cut By", "mastering"),
    ("Bob", "r2", "Mastered By", "mastering"),
    ("Carol", "r1", "Producer", "production"),
    ("Carol", "r2", "Producer", "production"),
    # credits only in other categories
    ("Dave", "r3", "Producer", "production"),
    ("Erin", "r2", "Design", "design"),
    # Frank ties Bob at 2 in mastering
    ("Frank", "r1", "Mastered By", "mastering"),
    ("Frank", "r3", "Mastered By", "mastering"),
]


def _enricher_cypher() -> str:
    sums = ",\n            ".join(f"sum(CASE WHEN '{c}' IN categories THEN 1 ELSE 0 END) AS count_{c}" for c in ALL_CATEGORIES)
    sets = ",\n            ".join(f"p.credit_count_{c} = count_{c}" for c in ALL_CATEGORIES)
    return f"""
    MATCH (p:Person)
    CALL {{
        WITH p
        OPTIONAL MATCH (p)-[c:CREDITED_ON]->(r:Release)
        WITH p, r, collect(DISTINCT c.category) AS categories
        WITH p, count(r) AS total, {sums}
        SET p.credit_count = total, {sets}
    }} IN TRANSACTIONS OF 100 ROWS
    """


async def _run(driver: AsyncResilientNeo4jDriver, cypher: str, **params: Any) -> list[dict[str, Any]]:
    return await run_query(driver, cypher, database="neo4j", **params)


@pytest_asyncio.fixture
async def driver() -> AsyncIterator[AsyncResilientNeo4jDriver]:
    drv = AsyncResilientNeo4jDriver(
        uri=os.environ["NEO4J_HOST"],
        auth=(os.environ["NEO4J_USERNAME"], os.environ["NEO4J_PASSWORD"]),
        max_retries=1,
    )
    await _run(drv, "MATCH (n) DETACH DELETE n")
    for c in ALL_CATEGORIES:
        await _run(drv, f"CREATE INDEX person_credit_count_{c} IF NOT EXISTS FOR (p:Person) ON (p.credit_count_{c})")
    await _run(drv, "CALL db.awaitIndexes(60)")
    await _run(
        drv,
        "UNWIND $rows AS row MERGE (p:Person {name: row[0]}) MERGE (r:Release {id: row[1]}) "
        "CREATE (p)-[:CREDITED_ON {role: row[2], category: row[3]}]->(r)",
        rows=[list(c) for c in CREDITS],
    )
    credits_queries._fallback_logged = False
    try:
        yield drv
    finally:
        await _run(drv, "MATCH (n) DETACH DELETE n")
        await drv.close()


async def _live(driver: AsyncResilientNeo4jDriver, category: str, limit: int = 20) -> list[dict[str, Any]]:
    return await _run(driver, _LIVE_LEADERBOARD_CYPHER, category=category, limit=limit)


def _key(rows: list[dict[str, Any]]) -> list[tuple[str, int]]:
    return [(r["name"], r["credit_count"]) for r in rows]


@pytest.mark.parametrize("category", ALL_CATEGORIES)
async def test_precomputed_leaderboard_matches_live_count(driver: AsyncResilientNeo4jDriver, category: str) -> None:
    await _run(driver, _enricher_cypher())
    precomputed = await get_role_leaderboard(driver, category, limit=20)  # type: ignore[arg-type]
    live = await _live(driver, category)
    assert _key(precomputed) == _key(live)
    assert all(r["credit_count"] > 0 for r in precomputed)


async def test_seed_exercises_the_interesting_cases(driver: AsyncResilientNeo4jDriver) -> None:
    await _run(driver, _enricher_cypher())
    mastering = _key(await get_role_leaderboard(driver, "mastering"))  # type: ignore[arg-type]
    assert mastering[0] == ("Alice", 3)
    assert dict(mastering)["Bob"] == 2  # two roles on r1 count once
    assert "Dave" not in dict(mastering) and "Erin" not in dict(mastering)  # zero counts excluded
    zero = await _run(driver, "MATCH (p:Person {name: 'Dave'}) RETURN p.credit_count_mastering AS n")
    assert zero == [{"n": 0}]  # present as 0, so not mistaken for "never computed"


async def test_limit_is_applied(driver: AsyncResilientNeo4jDriver) -> None:
    await _run(driver, _enricher_cypher())
    rows = await get_role_leaderboard(driver, "mastering", limit=1)  # type: ignore[arg-type]
    assert _key(rows) == [("Alice", 3)]


@pytest.mark.parametrize("category", ["mastering", "production"])
async def test_uncomputed_graph_falls_back_to_live_result(driver: AsyncResilientNeo4jDriver, category: str) -> None:
    props = await _run(driver, "MATCH (p:Person) RETURN count(p.credit_count_mastering) AS n")
    assert props == [{"n": 0}]
    rows = await get_role_leaderboard(driver, category, limit=20)  # type: ignore[arg-type]
    live = await _live(driver, category)
    assert rows
    assert _key(rows) == _key(live)


async def test_order_by_uses_the_category_index(driver: AsyncResilientNeo4jDriver) -> None:
    await _run(driver, _enricher_cypher())
    seen: list[str] = []
    real = credits_queries.run_query

    async def spy(drv: Any, cypher: str, **kw: Any) -> Any:
        seen.append(cypher)
        return await real(drv, cypher, **kw)

    credits_queries.run_query = spy  # type: ignore[assignment]
    try:
        await get_role_leaderboard(driver, "mastering", limit=5)  # type: ignore[arg-type]
    finally:
        credits_queries.run_query = real  # type: ignore[assignment]
    main = seen[-1]
    async with driver.session(database="neo4j") as session:
        result = await session.run("EXPLAIN " + main, {"limit": 5})
        summary = await result.consume()
    operators: list[str] = []
    stack = [summary.plan]
    while stack:
        node = stack.pop()
        operators.append(f"{node['operatorType']} {node['args'].get('Details', '')}")
        stack.extend(node.get("children", []))
    plan = "\n".join(operators)
    # Neo4j prints the indexed property, not the index name, in the operator details.
    assert "NodeIndexSeekByRange" in plan and "INDEX p:Person(credit_count_mastering)" in plan, plan
    assert "PartialTop" in plan, plan  # consumes index order; no full sort of every person
    assert "NodeByLabelScan" not in plan, plan
