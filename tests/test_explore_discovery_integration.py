"""Discoveries equal the old traversal's, node for node, on a real Neo4j; hubs equal the live walk (audit H2).

``gm-catalog-api-zazs``: the old statement enumerated every path and then applied ``LIMIT 100``. Its text, with the limit
removed, is the oracle for *which nodes are discovered and how far away they are*. The new selection must reproduce
that set exactly for genres and styles, cap artists and labels at 100 nearest-first by popularity then id, and the
background hub computation must agree with the live walk on a start both can answer.

Nothing here touches a shared database: ``just test-integration`` starts throwaway containers.
"""

from __future__ import annotations

from typing import Any

import pytest
from common import AsyncResilientNeo4jDriver

from api import explore_discovery as ed
from api.queries.helpers import run_query
from tests.test_real_databases import neo4j_driver


__all__ = ["neo4j_driver"]

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_ORACLE = """
MATCH (start {{{key}: $entity_id}}) WHERE start:{label}
MATCH path = (start)-[:BY|ON|IS|ALIAS_OF|MEMBER_OF|DERIVED_FROM*1..{hops}]-(discovered)
WHERE discovered <> start AND (discovered:Artist OR discovered:Label OR discovered:Genre OR discovered:Style)
WITH discovered, min(length(path)) AS dist
RETURN labels(discovered)[0] AS label, coalesce(discovered.id, discovered.name) AS id, dist
"""

_GRAPH = """
CREATE (a1:Artist {id: 'A1', name: 'Alpha'}), (a2:Artist {id: 'A2', name: 'Beta'}), (a3:Artist {id: 'A3', name: 'Gamma'}),
       (a4:Artist {id: 'A4', name: 'Alpha Alias'}), (g1:Artist {id: 'G1', name: 'The Group'}), (a5:Artist {id: 'A5', name: 'Epsilon'}),
       (l1:Label {id: 'L1', name: 'One', release_count: 2}), (l2:Label {id: 'L2', name: 'Two', release_count: 1}),
       (rock:Genre {name: 'Rock', release_count: 2}), (jazz:Genre {name: 'Jazz', release_count: 1}), (punk:Style {name: 'Punk', release_count: 1}),
       (bop:Style {name: 'Bop', release_count: 1}),
       (r1:Release {id: 'r1', title: 'First'}), (r2:Release {id: 'r2', title: 'Second'}), (r3:Release {id: 'r3', title: 'Third'}),
       (m1:Master {id: 'm1', title: 'Master One'}),
       (r1)-[:BY]->(a1), (r1)-[:BY]->(a2), (r2)-[:BY]->(a1), (r2)-[:BY]->(a3), (r3)-[:BY]->(a3), (r3)-[:BY]->(a5),
       (r1)-[:ON]->(l1), (r2)-[:ON]->(l1), (r3)-[:ON]->(l2),
       (r1)-[:IS]->(rock), (r2)-[:IS]->(rock), (r3)-[:IS]->(jazz), (r1)-[:IS]->(punk), (r3)-[:IS]->(bop),
       (a4)-[:ALIAS_OF]->(a1), (a3)-[:MEMBER_OF]->(g1), (r1)-[:DERIVED_FROM]->(m1)
"""

_STARTS = [("artist", "A1"), ("label", "L1"), ("genre", "Rock"), ("style", "Punk"), ("release", "r1"), ("master", "m1")]


async def _seed(driver: AsyncResilientNeo4jDriver) -> None:
    await run_query(driver, "MATCH (n) DETACH DELETE n", database="neo4j", timeout=None)
    await run_query(driver, _GRAPH, database="neo4j", timeout=None)


async def _oracle(driver: AsyncResilientNeo4jDriver, entity_type: str, entity_id: str, hops: int) -> set[tuple[str, str, int]]:
    key = "name" if entity_type in ("genre", "style") else "id"
    cypher = _ORACLE.format(key=key, label=ed._ALLOWED[entity_type], hops=hops)
    rows = await run_query(driver, cypher, database="neo4j", entity_id=entity_id)
    return {(r["label"].lower(), str(r["id"]), r["dist"]) for r in rows}


def _keys(rows: list[dict[str, Any]]) -> set[tuple[str, str, int]]:
    return {(r["type"], str(r["id"]), r["dist"]) for r in rows}


@pytest.mark.parametrize("hops", [1, 2, 3])
async def test_every_discovered_node_and_distance_matches_the_old_traversal(neo4j_driver: AsyncResilientNeo4jDriver, hops: int) -> None:
    await _seed(neo4j_driver)
    for entity_type, entity_id in _STARTS:
        expected = await _oracle(neo4j_driver, entity_type, entity_id, hops)
        actual = await ed.get_explore_traversal(neo4j_driver, entity_type, entity_id, hops)
        assert _keys(actual) == expected, (entity_type, entity_id, hops)


async def test_paths_are_real_shortest_paths_and_deterministic(neo4j_driver: AsyncResilientNeo4jDriver) -> None:
    await _seed(neo4j_driver)
    first = await ed.get_explore_traversal(neo4j_driver, "artist", "A1", 3)
    assert first == await ed.get_explore_traversal(neo4j_driver, "artist", "A1", 3)
    for row in first:
        assert row["path_names"][0] == "Alpha"
        assert row["path_names"][-1] == row["name"]
        assert len(row["path_names"]) == row["dist"] + 1
        assert len(row["rel_types"]) == row["dist"]


async def test_artists_and_labels_are_capped_at_100_nearest_then_most_established_then_id(neo4j_driver: AsyncResilientNeo4jDriver) -> None:
    await _seed(neo4j_driver)
    # 150 extra artists on release r1, with 0..149 extra credits elsewhere, plus two genres that must all survive.
    await run_query(
        neo4j_driver,
        """
        MATCH (r1:Release {id: 'r1'})
        UNWIND range(1, 150) AS i
        CREATE (x:Artist {id: 'X' + right('000' + toString(i), 3), name: 'X' + toString(i)}), (r1)-[:BY]->(x)
        WITH x, i
        UNWIND range(1, i % 7) AS k
        CREATE (:Release {id: x.id + '-' + toString(k)})-[:BY]->(x)
        """,
        database="neo4j",
        timeout=None,
    )
    rows = await ed.get_explore_traversal(neo4j_driver, "release", "r1", 2)
    people = [r for r in rows if r["type"] in ("artist", "label")]
    assert len(people) == 100
    assert {r["name"] for r in rows if r["type"] in ("genre", "style")} == {"Rock", "Punk"}
    dists = [r["dist"] for r in people]
    assert dists == sorted(dists)
    # Within the distance that is cut, the most credited come first, ties by id.
    cut = [r for r in people if r["dist"] == dists[-1]]
    credits = {
        r["id"]: (await run_query(neo4j_driver, "MATCH (a:Artist {id: $id}) RETURN COUNT { (a)<-[:BY]-() } AS c", database="neo4j", id=r["id"]))[0][
            "c"
        ]
        for r in cut
        if r["type"] == "artist"
    }
    ordered = [(-credits[r["id"]], r["id"]) for r in cut if r["type"] == "artist"]
    assert ordered == sorted(ordered)
    assert [r["id"] for r in rows] == [r["id"] for r in await ed.get_explore_traversal(neo4j_driver, "release", "r1", 2)]


async def test_hub_results_computed_in_the_background_equal_the_live_walk(
    neo4j_driver: AsyncResilientNeo4jDriver, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed(neo4j_driver)
    for entity_type, entity_id in [("genre", "Rock"), ("label", "L1")]:
        live = await ed.get_explore_traversal(neo4j_driver, entity_type, entity_id, 2)
        monkeypatch.setattr(ed, "HUB_MIN_RELEASES", 1)
        ed.configure(None)
        with pytest.raises(ed.HubNotReadyError):
            await ed.get_explore_traversal(neo4j_driver, entity_type, entity_id, 2)
        assert await ed.refresh_hubs(neo4j_driver) >= 1
        stored = await ed.get_explore_traversal(neo4j_driver, entity_type, entity_id, 3)  # 3 == 2 for a hub
        assert _keys(stored) == _keys(live), (entity_type, entity_id)
        assert stored == await ed.get_explore_traversal(neo4j_driver, entity_type, entity_id, 2)
        for row in stored:
            assert row["path_names"][0] == entity_id and row["path_names"][-1] == row["name"] and row["dist"] == 2
        assert await ed.get_explore_traversal(neo4j_driver, entity_type, entity_id, 1) == []
        assert await ed.refresh_hubs(neo4j_driver) == 0  # unchanged hubs are not rebuilt
        monkeypatch.setattr(ed, "HUB_MIN_RELEASES", 50_000)
