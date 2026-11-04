"""The collaborator statement returns the same ranking as the one it replaced, on a real Neo4j.

Audit H1 (``gm-catalog-api-p85n``): the old statement re-expanded the root per row and walked through
placeholder hubs such as "Various". The oracle below is the old statement, kept verbatim, so a mock
cannot hide a change in who is ranked first. The one intended difference is the hub bridge: an artist
over the credit cap, or without a name, no longer connects its co-credited artists.

Nothing here touches a shared database: ``just test-integration`` starts throwaway containers.
"""

from __future__ import annotations

import pytest
from common import AsyncResilientNeo4jDriver

from api.queries import network_queries
from api.queries.helpers import run_query
from tests.test_real_databases import neo4j_driver


__all__ = ["neo4j_driver"]

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_OLD_PAGE = """
MATCH (a:Artist {id: $artist_id})
CALL {
    WITH a
    MATCH path = (a)<-[:BY]-(:Release)-[:BY]->(hop1:Artist)
    WHERE hop1 <> a
    WITH a, hop1, count(DISTINCT nodes(path)[1]) AS shared
    RETURN hop1 AS collaborator, 1 AS hops, shared
    UNION
    WITH a
    MATCH (a)<-[:BY]-(:Release)-[:BY]->(mid:Artist)<-[:BY]-(:Release)-[:BY]->(hop2:Artist)
    WHERE mid <> a AND hop2 <> a AND $depth >= 2
          AND NOT EXISTS { MATCH (a)<-[:BY]-(:Release)-[:BY]->(hop2) }
    WITH hop2 AS collaborator, 2 AS hops, count(DISTINCT mid) AS shared
    WHERE hops <= $depth
    RETURN collaborator, hops, shared
}
WITH collaborator, min(hops) AS distance, sum(shared) AS collaboration_count
ORDER BY distance ASC, collaboration_count DESC, collaborator.id ASC
RETURN collaborator.id AS artist_id, collaborator.name AS artist_name, distance, collaboration_count
"""

# a worked with b (2 releases), c (1) and the placeholder hub h (1).
# b worked with d (1) and e (1); c worked with d (1); h worked with f (1), and nobody else reaches f.
# a also worked with e directly through r6, so e is distance 1 and must not be counted at distance 2.
_GRAPH = """
CREATE (a:Artist {id: 'a', name: 'A'}), (b:Artist {id: 'b', name: 'B'}), (c:Artist {id: 'c', name: 'C'}),
       (d:Artist {id: 'd', name: 'D'}), (e:Artist {id: 'e', name: 'E'}), (f:Artist {id: 'f', name: 'F'}),
       (h:Artist {id: 'h'})
WITH a, b, c, d, e, f, h
UNWIND [
  ['r1', ['a', 'b']], ['r2', ['a', 'b']], ['r3', ['a', 'c']], ['r4', ['a', 'h']],
  ['r5', ['b', 'd']], ['r6', ['a', 'e']], ['r7', ['b', 'e']], ['r8', ['c', 'd']], ['r9', ['h', 'f']]
] AS spec
CREATE (r:Release {id: spec[0]})
WITH r, spec
MATCH (x:Artist) WHERE x.id IN spec[1]
CREATE (r)-[:BY]->(x)
"""


async def _seed(driver: AsyncResilientNeo4jDriver) -> None:
    await run_query(driver, "MATCH (n) DETACH DELETE n", database="neo4j", timeout=None)
    await run_query(driver, _GRAPH, database="neo4j", timeout=None)


async def _old(driver: AsyncResilientNeo4jDriver, depth: int) -> list[dict[str, object]]:
    return await run_query(driver, _OLD_PAGE, database="neo4j", artist_id="a", depth=depth)


async def test_depth_1_matches_the_old_statement(neo4j_driver: AsyncResilientNeo4jDriver) -> None:
    await _seed(neo4j_driver)
    page, total, truncated = await network_queries.get_collaborator_page(neo4j_driver, "a", depth=1, limit=50)
    assert page == await _old(neo4j_driver, 1)
    assert total == len(page) == 4  # b, c, e and the hub itself
    assert truncated is False


async def test_depth_2_matches_the_old_statement_except_through_the_hub(neo4j_driver: AsyncResilientNeo4jDriver) -> None:
    await _seed(neo4j_driver)
    old = await _old(neo4j_driver, 2)
    page, total, truncated = await network_queries.get_collaborator_page(neo4j_driver, "a", depth=2, limit=50)

    assert {r["artist_id"] for r in old} == {"b", "c", "e", "h", "d", "f"}
    # Only f is lost: it is reachable solely through the nameless hub.
    assert page == [r for r in old if r["artist_id"] != "f"]
    assert total == len(page) == 5
    assert truncated is False
    by_id = {r["artist_id"]: r for r in page}
    assert by_id["b"]["distance"] == 1 and by_id["b"]["collaboration_count"] == 2
    assert by_id["e"]["distance"] == 1 and by_id["e"]["collaboration_count"] == 1  # not re-counted at hop 2
    assert by_id["d"]["distance"] == 2 and by_id["d"]["collaboration_count"] == 2  # via b and c


async def test_page_is_a_prefix_of_the_ranking_and_total_covers_all(neo4j_driver: AsyncResilientNeo4jDriver) -> None:
    await _seed(neo4j_driver)
    full, _, _ = await network_queries.get_collaborator_page(neo4j_driver, "a", depth=2, limit=50)
    page, total, _ = await network_queries.get_collaborator_page(neo4j_driver, "a", depth=2, limit=2)
    assert page == full[:2]
    assert total == len(full)


async def test_unknown_artist_is_empty(neo4j_driver: AsyncResilientNeo4jDriver) -> None:
    await _seed(neo4j_driver)
    assert await network_queries.get_collaborator_page(neo4j_driver, "nobody") == ([], 0, False)


async def test_bridge_over_the_credit_cap_is_not_walked(neo4j_driver: AsyncResilientNeo4jDriver, monkeypatch: pytest.MonkeyPatch) -> None:
    await _seed(neo4j_driver)
    monkeypatch.setattr(network_queries, "BRIDGE_ARTIST_MAX_CREDITS", 3)  # b has 4 credits, c has 2
    page, _, _ = await network_queries.get_collaborator_page(neo4j_driver, "a", depth=2, limit=50)
    d = next(r for r in page if r["artist_id"] == "d")
    assert d["collaboration_count"] == 1  # only through c


async def test_hub_root_reads_a_bounded_sample_and_says_so(neo4j_driver: AsyncResilientNeo4jDriver, monkeypatch: pytest.MonkeyPatch) -> None:
    await _seed(neo4j_driver)
    monkeypatch.setattr(network_queries, "HUB_ROOT_MAX_CREDITS", 3)  # a has 6 credits
    page, total, truncated = await network_queries.get_collaborator_page(neo4j_driver, "a", depth=2, limit=50)
    assert truncated is True
    assert all(r["distance"] == 1 for r in page)  # a hub root returns direct collaborators only
    assert total == len(page) <= 3  # three sampled releases, one co-credit each
