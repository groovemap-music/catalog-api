"""The streamed rarity walk returns the rows the list-building walk did, on a real Neo4j.

``gm-catalog-api-jlyv``: ``GET /rarity-scores`` held one dict per release for the whole catalog, which
is what grew the API process to 23 GB on lux. The walk now counts the quality-signal distributions
first and scores one page at a time. The oracle below is the old arithmetic, a sorted list of every
positive value ranked with :func:`_percentile_rank`, fed from the same three statements on the whole
id set at once, so a mock cannot hide a rank that moved.

Nothing here touches a shared database: ``just test-integration`` starts throwaway containers.
"""

from __future__ import annotations

import pytest
from common import AsyncResilientNeo4jDriver

from api.queries import rarity_queries
from api.queries.helpers import run_query
from tests.test_real_databases import neo4j_driver


__all__ = ["neo4j_driver"]

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_RELEASES = 53


async def _seed(driver: AsyncResilientNeo4jDriver) -> None:
    await run_query(driver, "MATCH (n) DETACH DELETE n", database="neo4j", timeout=None)
    await run_query(
        driver,
        """
        UNWIND range(1, 9) AS i CREATE (:Artist {id: toString(i), name: 'A' + i})
        """,
        database="neo4j",
        timeout=None,
    )
    await run_query(
        driver,
        "UNWIND range(1, 5) AS i CREATE (:Label {id: toString(i), release_count: i * i * 3})",
        database="neo4j",
        timeout=None,
    )
    await run_query(
        driver,
        "UNWIND range(1, 4) AS i CREATE (:Genre {id: toString(i), name: 'G' + i, release_count: i * 1000})",
        database="neo4j",
        timeout=None,
    )
    # Artists, labels and genres of uneven fan-out, so the degrees repeat and differ; every seventh release
    # has no label and every eleventh no genre, so zero and missing values reach the distributions.
    await run_query(
        driver,
        """
        UNWIND range(1, $n) AS i
        CREATE (r:Release {id: 'r' + right('000' + toString(i), 3), title: 'T' + i, year: 1960 + i % 40, formats: ['LP']})
        WITH r, i
        MATCH (a:Artist {id: toString(1 + i * i % 9)})
        CREATE (r)-[:BY]->(a)
        WITH r, i
        CALL (r, i) { MATCH (l:Label {id: toString(1 + i % 5)}) WHERE i % 7 <> 0 CREATE (r)-[:ON]->(l) }
        CALL (r, i) { MATCH (g:Genre {id: toString(1 + i % 4)}) WHERE i % 11 <> 0 CREATE (r)-[:IS]->(g) }
        """,
        database="neo4j",
        timeout=None,
        n=_RELEASES,
    )


async def _oracle_hidden_gems(driver: AsyncResilientNeo4jDriver, rows: list[dict[str, object]]) -> list[float]:
    ids = sorted(str(row["release_id"]) for row in rows)
    artist = {
        r["release_id"]: r["artist_max_degree"] for r in await run_query(driver, rarity_queries._ARTIST_DEGREE_QUERY, database="neo4j", ids=ids)
    }
    label = {r["release_id"]: r["label_max_catalog"] for r in await run_query(driver, rarity_queries._LABEL_SIZE_QUERY, database="neo4j", ids=ids)}
    genre = {
        r["release_id"]: r["genre_max_release_count"] for r in await run_query(driver, rarity_queries._GENRE_COUNT_QUERY, database="neo4j", ids=ids)
    }
    artists, labels, genres = (sorted(v for v in m.values() if v) for m in (artist, label, genre))
    gems = []
    for row in rows:
        rid = str(row["release_id"])
        multiplier = (
            0.4 * rarity_queries._percentile_rank(artist.get(rid, 0) or 0, artists)
            + 0.3 * rarity_queries._percentile_rank(label.get(rid, 0) or 0, labels)
            + 0.3 * rarity_queries._percentile_rank(genre.get(rid, 0) or 0, genres)
        )
        gems.append(round(float(row["rarity_score"]) * multiplier, 1))  # type: ignore[arg-type]
    return gems


@pytest.mark.parametrize("page_size", [7, 20, 1000])
async def test_streamed_rows_are_complete_ordered_and_ranked_as_before(neo4j_driver: AsyncResilientNeo4jDriver, page_size: int) -> None:
    await _seed(neo4j_driver)

    streamed = [row async for row in rarity_queries.iter_rarity_signals(neo4j_driver, None, page_size=page_size)]

    assert [row["release_id"] for row in streamed] == [f"r{i:03d}" for i in range(1, _RELEASES + 1)]
    assert len({row["hidden_gem_score"] for row in streamed}) > 1  # the ranks actually differ across releases
    # rarity_score is rounded in the row and hidden_gem is derived from the unrounded score, so the oracle
    # can only reproduce it to the rounding of the stored score: compare with that tolerance.
    expected = await _oracle_hidden_gems(neo4j_driver, streamed)
    assert [row["hidden_gem_score"] for row in streamed] == pytest.approx(expected, abs=0.11)


async def test_page_size_does_not_change_any_row(neo4j_driver: AsyncResilientNeo4jDriver) -> None:
    await _seed(neo4j_driver)

    by_page_size = [await rarity_queries.fetch_all_rarity_signals(neo4j_driver, None, page_size=size) for size in (5, 53, 1000)]

    assert by_page_size[0] == by_page_size[1] == by_page_size[2]
