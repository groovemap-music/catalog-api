"""The rebuilt genre tree equals what the replaced on-request statement returned, on a real Neo4j.

Audit H3 (``gm-catalog-api-somq``): the old statement walked every ``IS`` edge twice per request. Its text
is kept here as the oracle, so a mock cannot hide a change in a genre's or a style's count. The store is
then driven end to end: a build, a stored copy that serves with no Neo4j read, and a rebuild when an import
changes the counts.

Nothing here touches a shared database: ``just test-integration`` starts throwaway containers.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from common import AsyncResilientNeo4jDriver
from neo4j.exceptions import ServiceUnavailable

from api import genre_tree_store as store
from api.queries import genre_tree_queries
from api.queries.helpers import run_query
from tests.test_real_databases import neo4j_driver


__all__ = ["neo4j_driver"]

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_OLD_TREE = """
MATCH (r:Release)-[:IS]->(g:Genre)
WITH g.name AS genre, count(DISTINCT r) AS genre_count
ORDER BY genre_count DESC
WITH collect({name: genre, count: genre_count}) AS genres
UNWIND genres AS gi
OPTIONAL MATCH (r2:Release)-[:IS]->(g2:Genre {name: gi.name}),
               (r2)-[:IS]->(s:Style)
WITH gi, s.name AS style, count(DISTINCT r2) AS style_count
ORDER BY gi.name, style_count DESC
WITH gi, collect(
       CASE WHEN style IS NOT NULL
         THEN {name: style, release_count: style_count}
         ELSE NULL END
     ) AS raw_styles
WITH gi, [s IN raw_styles WHERE s IS NOT NULL] AS styles
RETURN gi.name AS name, gi.count AS release_count, styles
ORDER BY gi.count DESC
"""

# Rock: r1 (Punk, Garage), r2 (Punk), r3 (Bop, shared with Jazz). Jazz: r3, r4 (Bop, Cool). Folk: r5 (no styles).
_GRAPH = """
CREATE (rock:Genre {name: 'Rock', release_count: 3, style_count: 2}),
       (jazz:Genre {name: 'Jazz', release_count: 2, style_count: 2}),
       (folk:Genre {name: 'Folk', release_count: 1, style_count: 0}),
       (punk:Style {name: 'Punk'}), (garage:Style {name: 'Garage'}), (bop:Style {name: 'Bop'}), (cool:Style {name: 'Cool'}),
       (r1:Release {id: 'r1'}), (r2:Release {id: 'r2'}), (r3:Release {id: 'r3'}), (r4:Release {id: 'r4'}), (r5:Release {id: 'r5'}),
       (r1)-[:IS]->(rock), (r1)-[:IS]->(punk), (r1)-[:IS]->(garage),
       (r2)-[:IS]->(rock), (r2)-[:IS]->(punk),
       (r3)-[:IS]->(rock), (r3)-[:IS]->(jazz), (r3)-[:IS]->(bop),
       (r4)-[:IS]->(jazz), (r4)-[:IS]->(bop), (r4)-[:IS]->(cool),
       (r5)-[:IS]->(folk)
"""


async def _seed(driver: AsyncResilientNeo4jDriver) -> None:
    await run_query(driver, "MATCH (n) DETACH DELETE n", database="neo4j", timeout=None)
    await run_query(driver, _GRAPH, database="neo4j", timeout=None)


async def _old(driver: AsyncResilientNeo4jDriver) -> list[dict[str, Any]]:
    rows = await run_query(driver, _OLD_TREE.replace("gi.count", "gi.count"), database="neo4j")
    return [{"name": r["name"], "release_count": r["release_count"], "styles": r["styles"]} for r in rows]


def _canonical(tree: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Order styles by count then name: the old statement left ties unordered."""
    return [
        {"name": g["name"], "release_count": g["release_count"], "styles": sorted(g["styles"], key=lambda s: (-s["release_count"], s["name"]))}
        for g in tree
    ]


async def test_rebuilt_tree_equals_the_replaced_statement(neo4j_driver: AsyncResilientNeo4jDriver) -> None:
    await _seed(neo4j_driver)
    built = await genre_tree_queries.build_genre_tree(neo4j_driver)
    assert _canonical(built) == _canonical(await _old(neo4j_driver))
    assert [g["name"] for g in built] == ["Rock", "Jazz", "Folk"]
    # r3 is tagged Rock and Bop, so Bop is a Rock style too: a style belongs to every genre it co-occurs with.
    rock = next(g for g in built if g["name"] == "Rock")
    assert rock["styles"] == [{"name": "Punk", "release_count": 2}, {"name": "Bop", "release_count": 1}, {"name": "Garage", "release_count": 1}]
    assert next(g for g in built if g["name"] == "Folk")["styles"] == []


async def test_stored_counts_equal_real_counts_in_this_graph(neo4j_driver: AsyncResilientNeo4jDriver) -> None:
    """The store trusts Genre.release_count: prove it matches the edges where the enricher is correct."""
    await _seed(neo4j_driver)
    rows = await run_query(
        neo4j_driver,
        "MATCH (g:Genre) OPTIONAL MATCH (g)<-[:IS]-(r:Release) RETURN g.name AS name, g.release_count AS stored, count(DISTINCT r) AS actual",
        database="neo4j",
    )
    assert all(r["stored"] == r["actual"] for r in rows)


async def test_store_serves_the_built_tree_with_no_neo4j_read_and_rebuilds_after_an_import(neo4j_driver: AsyncResilientNeo4jDriver) -> None:
    await _seed(neo4j_driver)
    store.configure(None)
    assert await store.refresh_once(neo4j_driver) is True
    first = await store.get_genre_tree()
    assert first[0]["name"] == "Rock"

    with patch("api.queries.helpers.run_query", AsyncMock(side_effect=AssertionError("request read Neo4j"))):
        assert await store.get_genre_tree() == first  # served from the stored tree

    assert await store.refresh_once(neo4j_driver) is False  # nothing changed

    # An import adds a release and the enricher bumps the counts.
    await run_query(
        neo4j_driver,
        "MATCH (g:Genre {name: 'Jazz'}), (c:Style {name: 'Cool'}) CREATE (r:Release {id: 'r6'})-[:IS]->(g), (r)-[:IS]->(c) SET g.release_count = 3",
        database="neo4j",
        timeout=None,
    )
    assert await store.refresh_once(neo4j_driver) is True
    jazz = next(g for g in await store.get_genre_tree() if g["name"] == "Jazz")
    assert jazz["release_count"] == 3
    assert {"name": "Cool", "release_count": 2} in jazz["styles"]


class _FlakyDriver:
    """The real driver, except the first ``failures`` sessions fail the way a restarting Neo4j does."""

    def __init__(self, real: AsyncResilientNeo4jDriver, failures: int) -> None:
        self._real = real
        self.failures = failures
        self.attempts = 0

    def session(self, **kwargs: Any) -> Any:
        self.attempts += 1
        if self.failures > 0:
            self.failures -= 1
            raise ServiceUnavailable("neo4j restarting")
        return self._real.session(**kwargs)


async def test_loop_recovers_after_a_real_failure_without_waiting_the_interval(neo4j_driver: AsyncResilientNeo4jDriver) -> None:
    await _seed(neo4j_driver)
    store.configure(None)
    flaky = _FlakyDriver(neo4j_driver, failures=1)
    task = asyncio.create_task(store.run_refresh_loop(flaky, interval=3600, retry_initial=0.05, retry_max=0.2))
    try:
        for _ in range(100):
            if store.current() is not None:
                break
            await asyncio.sleep(0.05)
        assert store.current() is not None, "the tree was not stored after the failure"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert flaky.attempts > 1  # failed once, retried in well under the one-hour interval
    assert [g["name"] for g in await store.get_genre_tree()] == ["Rock", "Jazz", "Folk"]
