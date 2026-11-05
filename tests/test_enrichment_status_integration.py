"""The typed MusicBrainz relationship count equals a full count, on a real Neo4j.

Audit H4 (``gm-catalog-api-nhux``): ``/api/enrichment/status`` counted ``MATCH ()-[r]->() WHERE r.source =
'musicbrainz'``, which reads a property on every relationship in the graph. The oracle below is that
statement, kept verbatim, over a graph that holds every type the enricher writes with that source and
decoys on every side of the filter: the same types with another source or none, and other types with
either. A mock cannot tell a count that moved from one that did not.

Nothing here touches a shared database: ``just test-integration`` starts throwaway containers.
"""

from __future__ import annotations

import pytest
from common import AsyncResilientNeo4jDriver

from api.queries.helpers import run_single
from api.queries.musicbrainz_queries import MB_SOURCED_RELATIONSHIP_TYPES, count_mb_sourced_relationships
from tests.test_real_databases import neo4j_driver


__all__ = ["neo4j_driver"]

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_OLD_FULL_COUNT = "MATCH ()-[r]->() WHERE r.source = 'musicbrainz' RETURN COUNT(r) AS total"
# Types the enricher never writes with a MusicBrainz source; one of them also carries it here, as a decoy.
_OTHER_TYPES = ("BY", "ON", "IS", "ALIAS_OF", "DERIVED_FROM", "CREDITED_ON")


async def _seed(driver: AsyncResilientNeo4jDriver) -> int:
    """Return how many relationships the enricher's types carry with the MusicBrainz source."""
    await run_single(driver, "MATCH (n) DETACH DELETE n RETURN 1 AS done", database="neo4j", timeout=None)
    await run_single(
        driver,
        "UNWIND range(1, 40) AS i CREATE (:Artist {id: toString(i)}) RETURN count(*) AS n",
        database="neo4j",
        timeout=None,
    )
    expected = 0
    for index, rel_type in enumerate(MB_SOURCED_RELATIONSHIP_TYPES):
        edges = index + 2  # a different count per type, so a type dropped from the list changes the total
        expected += edges
        for source in ("musicbrainz", "discogs", None):
            count = edges if source == "musicbrainz" else 3
            await run_single(
                driver,
                f"""UNWIND range(1, $count) AS i
                    MATCH (a:Artist {{id: toString(i)}}), (b:Artist {{id: toString(i + 10)}})
                    CREATE (a)-[r:{rel_type} {{source: $source}}]->(b)
                    RETURN count(*) AS n""",
                database="neo4j",
                timeout=None,
                count=count,
                source=source,
            )
    for rel_type in _OTHER_TYPES:
        for source in ("discogs", None):
            await run_single(
                driver,
                f"""UNWIND range(1, 5) AS i
                    MATCH (a:Artist {{id: toString(i)}}), (b:Artist {{id: toString(i + 20)}})
                    CREATE (a)-[r:{rel_type} {{source: $source}}]->(b)
                    RETURN count(*) AS n""",
                database="neo4j",
                timeout=None,
                source=source,
            )
    return expected


async def test_typed_count_equals_the_full_count(neo4j_driver: AsyncResilientNeo4jDriver) -> None:
    expected = await _seed(neo4j_driver)

    full = await run_single(neo4j_driver, _OLD_FULL_COUNT, database="neo4j")
    typed = await count_mb_sourced_relationships(neo4j_driver)

    assert full is not None
    assert typed == full["total"] == expected
    assert expected > 0


async def test_a_type_outside_the_list_is_the_one_thing_the_typed_count_would_miss(neo4j_driver: AsyncResilientNeo4jDriver) -> None:
    """The guard on the list: an unlisted type with the source makes the two counts differ."""
    expected = await _seed(neo4j_driver)
    await run_single(
        neo4j_driver,
        """MATCH (a:Artist {id: '1'}), (b:Artist {id: '2'}) CREATE (a)-[r:CO_DIRECTED {source: 'musicbrainz'}]->(b) RETURN count(*) AS n""",
        database="neo4j",
    )

    full = await run_single(neo4j_driver, _OLD_FULL_COUNT, database="neo4j")
    typed = await count_mb_sourced_relationships(neo4j_driver)

    assert full is not None
    assert (full["total"], typed) == (expected + 1, expected)
