"""``explore_artist`` returns the same numbers from stored stats as from the traversal, on a real Neo4j.

Audit H15-adjacent (``gm-catalog-api-87pz``): an artist's release and label counts were a traversal on
every Explore request (339,676 db hits for Bowie). The API now reads ``Artist.release_count`` and
``Artist.label_count`` when the post-import stats wrote them, and falls back to the traversal when it
did not. ``_ENRICHER_STATS`` below is the statement discogs-graph-enricher's stats pass must run to write
them; the test applies it and requires the stored read to equal the live one, including an artist with
several labels per release, releases on no label, a release credited to two artists, and an artist with
no releases. A mock cannot show the two agree.

Nothing here touches a shared database: ``just test-integration`` starts throwaway containers.
"""

from __future__ import annotations

import pytest
from common import AsyncResilientNeo4jDriver

from api.queries.helpers import run_query
from api.queries.neo4j_queries import explore_artist
from tests.test_real_databases import neo4j_driver


__all__ = ["neo4j_driver"]

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

# The matching change for discogs-graph-enricher: run per batch of artists after an import.
_ENRICHER_STATS = """
MATCH (a:Artist)
CALL (a) {
    OPTIONAL MATCH (r:Release)-[:BY]->(a)
    WITH a, count(DISTINCT r) AS release_count
    OPTIONAL MATCH (r2:Release)-[:BY]->(a)
    OPTIONAL MATCH (r2)-[:ON]->(l:Label)
    WITH a, release_count, count(DISTINCT l) AS label_count
    SET a.release_count = release_count, a.label_count = label_count
}
"""

_SEED = """
CREATE (a:Artist {id: 'A1', name: 'Heavy'}), (b:Artist {id: 'A2', name: 'Partner'}), (:Artist {id: 'A3', name: 'Empty'}),
       (g:Artist {id: 'A4', name: 'Group'}), (a)-[:MEMBER_OF]->(g), (a)-[:ALIAS_OF]->(:Artist {id: 'A5', name: 'Alias'})
WITH a, b
UNWIND range(1, 60) AS i
CREATE (r:Release {id: 'r' + i})-[:BY]->(a)
WITH a, b, r, i
CALL (r, b, i) { WITH r, b, i WHERE i % 4 = 0 CREATE (r)-[:BY]->(b) }
CALL (r, i) { MATCH (l:Label {id: 'L' + (i % 7)}) CREATE (r)-[:ON]->(l) }
CALL (r, i) { WITH r, i WHERE i % 10 = 0 MATCH (l:Label {id: 'L' + ((i + 3) % 7)}) CREATE (r)-[:ON]->(l) }
"""


async def _seed(driver: AsyncResilientNeo4jDriver) -> None:
    await run_query(driver, "MATCH (n) DETACH DELETE n", database="neo4j", timeout=None)
    await run_query(driver, "UNWIND range(0, 6) AS i CREATE (:Label {id: 'L' + i, name: 'Label ' + i})", database="neo4j", timeout=None)
    # releases 5, 15, ... are on no label: the fixture's CALL above skips them by construction of the ids below
    await run_query(driver, _SEED, database="neo4j", timeout=None)
    await run_query(driver, "MATCH (r:Release {id: 'r5'})-[o:ON]->() DELETE o", database="neo4j", timeout=None)


async def test_stored_counts_equal_the_traversal(neo4j_driver: AsyncResilientNeo4jDriver) -> None:
    await _seed(neo4j_driver)
    names = ["Heavy", "Partner", "Empty", "Group", "Alias"]
    live = {name: await explore_artist(neo4j_driver, name) for name in names}  # no stored stats yet: the fallback

    await run_query(neo4j_driver, _ENRICHER_STATS, database="neo4j", timeout=None)
    stored = {name: await explore_artist(neo4j_driver, name) for name in names}

    assert stored == live
    assert live["Heavy"] is not None
    assert (live["Heavy"]["release_count"], live["Heavy"]["label_count"]) == (60, 7)
    assert live["Partner"] is not None
    assert live["Partner"]["release_count"] == 15
    assert live["Empty"] is not None
    assert (live["Empty"]["release_count"], live["Empty"]["label_count"]) == (0, 0)
    assert live["Heavy"]["alias_count"] == 2  # the group and the alias, read live in both runs


async def test_the_stored_values_are_what_is_returned(neo4j_driver: AsyncResilientNeo4jDriver) -> None:
    """With both properties present no traversal is needed, so they are returned as written."""
    await _seed(neo4j_driver)
    await run_query(neo4j_driver, "MATCH (a:Artist {name: 'Heavy'}) SET a.release_count = 1234, a.label_count = 56", database="neo4j", timeout=None)

    result = await explore_artist(neo4j_driver, "Heavy")

    assert result is not None
    assert (result["release_count"], result["label_count"]) == (1234, 56)


async def test_one_missing_property_falls_back_to_the_live_counts(neo4j_driver: AsyncResilientNeo4jDriver) -> None:
    await _seed(neo4j_driver)
    await run_query(neo4j_driver, "MATCH (a:Artist {name: 'Heavy'}) SET a.release_count = 1234", database="neo4j", timeout=None)

    result = await explore_artist(neo4j_driver, "Heavy")

    assert result is not None
    assert (result["release_count"], result["label_count"]) == (60, 7)
