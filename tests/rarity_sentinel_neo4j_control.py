"""Owned disposable Neo4j proof for zero-master rarity sentinels.

Run only through just integration-rarity-neo4j. The runner starts a fresh labeled
Neo4j container, publishes a random loopback port, and removes that container on exit. This
control refuses non-loopback URIs and never connects to a shared or live graph.
"""

from __future__ import annotations

import os
from urllib.parse import urlparse

from neo4j import GraphDatabase

from api.queries.rarity_queries import _TEMPORAL_QUERY
from api.rarity.families.grooved import PRESSING_QUERY, compute_pressing_scarcity_score


UNGUARDED_PRESSING_QUERY = """
UNWIND $ids AS rid
MATCH (r:Release {id: rid})
OPTIONAL MATCH (r)-[:DERIVED_FROM]->(m:Master)
OPTIONAL MATCH (m)<-[:DERIVED_FROM]-(sibling:Release)
WHERE sibling <> r
WITH r, m, count(DISTINCT sibling) AS sibling_count
WITH r, CASE WHEN m IS NULL THEN 0 ELSE sibling_count + 1 END AS pressing_count
RETURN r.id AS release_id, pressing_count
"""

UNGUARDED_TEMPORAL_QUERY = """
UNWIND $ids AS rid
MATCH (r:Release {id: rid})
OPTIONAL MATCH (r)-[:DERIVED_FROM]->(m:Master)<-[:DERIVED_FROM]-(sibling:Release)
WHERE sibling.year IS NOT NULL AND sibling <> r
WITH r.id AS release_id, r.year AS year, max(sibling.year) AS latest_sibling_year
RETURN release_id, year, latest_sibling_year
"""

FIXTURE_RELEASES = {
    "no_master": "rarity-it-no-master",
    "string_sentinel_target": "rarity-it-string-sentinel-target",
    "string_sentinel_reissue": "rarity-it-string-sentinel-reissue",
    "numeric_sentinel_target": "rarity-it-numeric-sentinel-target",
    "numeric_sentinel_reissue": "rarity-it-numeric-sentinel-reissue",
    "unique": "rarity-it-unique",
    "multi_target": "rarity-it-multi-target",
    "multi_old": "rarity-it-multi-old",
    "multi_latest": "rarity-it-multi-latest",
}


def _assert_loopback(uri: str) -> None:
    parsed = urlparse(uri)
    if parsed.scheme not in {"bolt", "neo4j"} or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise RuntimeError("rarity control requires a loopback bolt URI from its disposable runner")


def _seed(driver) -> None:
    with driver.session(database="neo4j") as session:
        session.run("MATCH (n) WHERE n.rarity_sentinel_control = true DETACH DELETE n").consume()
        session.run(
            """
            CREATE (no_master:Release {id: $no_master, year: 1960, rarity_sentinel_control: true})
            CREATE (string_target:Release {id: $string_target, year: 1960, rarity_sentinel_control: true})
            CREATE (string_reissue:Release {id: $string_reissue, year: 2099, rarity_sentinel_control: true})
            CREATE (string_master:Master {id: '0', rarity_sentinel_control: true})
            CREATE (string_target)-[:DERIVED_FROM]->(string_master)
            CREATE (string_reissue)-[:DERIVED_FROM]->(string_master)
            CREATE (numeric_target:Release {id: $numeric_target, year: 1960, rarity_sentinel_control: true})
            CREATE (numeric_reissue:Release {id: $numeric_reissue, year: 2098, rarity_sentinel_control: true})
            CREATE (numeric_master:Master {id: 0, rarity_sentinel_control: true})
            CREATE (numeric_target)-[:DERIVED_FROM]->(numeric_master)
            CREATE (numeric_reissue)-[:DERIVED_FROM]->(numeric_master)
            CREATE (unique:Release {id: $unique, year: 1970, rarity_sentinel_control: true})
            CREATE (unique_master:Master {id: 'rarity-it-unique-master', rarity_sentinel_control: true})
            CREATE (unique)-[:DERIVED_FROM]->(unique_master)
            CREATE (multi_target:Release {id: $multi_target, year: 1970, rarity_sentinel_control: true})
            CREATE (multi_old:Release {id: $multi_old, year: 1990, rarity_sentinel_control: true})
            CREATE (multi_latest:Release {id: $multi_latest, year: 2020, rarity_sentinel_control: true})
            CREATE (multi_master:Master {id: 'rarity-it-multi-master', rarity_sentinel_control: true})
            CREATE (multi_target)-[:DERIVED_FROM]->(multi_master)
            CREATE (multi_target)-[:DERIVED_FROM]->(multi_master)
            CREATE (multi_old)-[:DERIVED_FROM]->(multi_master)
            CREATE (multi_latest)-[:DERIVED_FROM]->(multi_master)
            CREATE (multi_latest)-[:DERIVED_FROM]->(multi_master)
            """,
            no_master=FIXTURE_RELEASES["no_master"],
            string_target=FIXTURE_RELEASES["string_sentinel_target"],
            string_reissue=FIXTURE_RELEASES["string_sentinel_reissue"],
            numeric_target=FIXTURE_RELEASES["numeric_sentinel_target"],
            numeric_reissue=FIXTURE_RELEASES["numeric_sentinel_reissue"],
            unique=FIXTURE_RELEASES["unique"],
            multi_target=FIXTURE_RELEASES["multi_target"],
            multi_old=FIXTURE_RELEASES["multi_old"],
            multi_latest=FIXTURE_RELEASES["multi_latest"],
        ).consume()


def _rows(driver, query: str, ids: list[str]) -> dict[str, dict]:
    with driver.session(database="neo4j") as session:
        return {row["release_id"]: dict(row) for row in session.run(query, ids=ids)}


def main() -> None:
    uri = os.environ["NEO4J_HOST"]
    _assert_loopback(uri)
    ids = list(FIXTURE_RELEASES.values())
    with GraphDatabase.driver(uri, auth=(os.environ["NEO4J_USERNAME"], os.environ["NEO4J_PASSWORD"])) as driver:
        driver.verify_connectivity()
        _seed(driver)

        unguarded_pressing = _rows(driver, UNGUARDED_PRESSING_QUERY, ids)
        unguarded_temporal = _rows(driver, UNGUARDED_TEMPORAL_QUERY, ids)
        assert unguarded_pressing[FIXTURE_RELEASES["string_sentinel_target"]]["pressing_count"] == 2
        assert unguarded_pressing[FIXTURE_RELEASES["numeric_sentinel_target"]]["pressing_count"] == 2
        assert unguarded_temporal[FIXTURE_RELEASES["string_sentinel_target"]]["latest_sibling_year"] == 2099
        assert unguarded_temporal[FIXTURE_RELEASES["numeric_sentinel_target"]]["latest_sibling_year"] == 2098

        pressing = _rows(driver, PRESSING_QUERY, ids)
        temporal = _rows(driver, _TEMPORAL_QUERY, ids)

        assert pressing[FIXTURE_RELEASES["no_master"]]["pressing_count"] == 0
        for key in ("string_sentinel_target", "numeric_sentinel_target"):
            assert pressing[FIXTURE_RELEASES[key]]["pressing_count"] == 0
            assert temporal[FIXTURE_RELEASES[key]]["latest_sibling_year"] is None
        unique_count = pressing[FIXTURE_RELEASES["unique"]]["pressing_count"]
        assert unique_count == 1
        assert compute_pressing_scarcity_score(unique_count) == 100.0
        assert pressing[FIXTURE_RELEASES["multi_target"]]["pressing_count"] == 3
        assert temporal[FIXTURE_RELEASES["multi_target"]]["latest_sibling_year"] == 2020

    print("Unguarded controls failed as expected; guarded pressing and temporal Cypher passed.")
    print("Fixtures: no master, string/numeric sentinels, unique/multi masters, duplicate edges, false sentinel reissues.")


if __name__ == "__main__":
    main()
