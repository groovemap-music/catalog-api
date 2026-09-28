"""Unit tests for the PostgreSQL MusicBrainz read family."""

import pytest

from api.queries.musicbrainz_pg_queries import (
    ARTIST_RELATIONSHIPS_SQL,
    PROJECTED_RELATIONSHIP_COUNT_SQL,
    get_artist_external_links,
    get_artist_mb_relationships,
    get_artist_musicbrainz,
    get_enrichment_status,
)
from api.queries.musicbrainz_pipeline import MusicBrainzHandles
from tests.fake_postgres import FakePool


def _handles(pool: FakePool) -> MusicBrainzHandles:
    return MusicBrainzHandles(graph=pool, relational=pool)


@pytest.mark.asyncio
async def test_artist_metadata_reads_graph_mb_artist() -> None:
    pool = FakePool([[("00000000-0000-0000-0000-000000000001", "Person", None, "1970", None, "UK", "London", "")]])
    assert await get_artist_musicbrainz(_handles(pool), "1") == {
        "discogs_id": "1",
        "mbid": "00000000-0000-0000-0000-000000000001",
        "type": "Person",
        "gender": None,
        "begin_date": "1970",
        "end_date": None,
        "area": "UK",
        "begin_area": "London",
        "disambiguation": "",
    }
    assert "FROM graph.mb_artist" in pool.sql


@pytest.mark.asyncio
async def test_relationships_read_mapped_type_and_drop_unmapped_or_unbound_rows() -> None:
    pool = FakePool([[("MEMBER_OF", "2", "Band", "outgoing", "1990", None, ["vocals"])]])
    assert await get_artist_mb_relationships(_handles(pool), "1") == [
        {
            "type": "MEMBER_OF",
            "target_id": "2",
            "target_name": "Band",
            "direction": "outgoing",
            "begin_date": "1990",
            "end_date": None,
            "attributes": ["vocals"],
        }
    ]
    assert "relationship.relationship_type IS NOT NULL" in ARTIST_RELATIONSHIPS_SQL
    assert "raw_relationship_type" not in ARTIST_RELATIONSHIPS_SQL
    assert "target.discogs_artist_id IS NOT NULL" in ARTIST_RELATIONSHIPS_SQL
    assert 'attributes::text COLLATE "C" NULLS FIRST' in ARTIST_RELATIONSHIPS_SQL


@pytest.mark.asyncio
async def test_external_links_use_the_shared_relational_store() -> None:
    pool = FakePool([[("wikidata", "https://www.wikidata.org/wiki/Q1")]])
    assert await get_artist_external_links(_handles(pool), 1) == [{"service": "wikidata", "url": "https://www.wikidata.org/wiki/Q1"}]


@pytest.mark.asyncio
async def test_status_counts_only_relationships_the_enricher_can_project() -> None:
    pool = FakePool(
        [
            [(2,)],
            [(2,)],
            [(2,)],
            [(1,)],
            [(1,)],
            [(1,)],
            [(1,)],
            [(1,)],
            [(1,)],
            [(3,)],
            [(1,)],
        ]
    )
    result = await get_enrichment_status(_handles(pool))
    assert result["musicbrainz"]["artists"] == {
        "total_mb": 2,
        "matched_to_discogs": 2,
        "enriched_in_neo4j": 2,
    }
    assert result["musicbrainz"]["relationships"] == {"total_in_mb": 3, "created_in_neo4j": 1}
    assert "graph.mb_relationship_type" not in PROJECTED_RELATIONSHIP_COUNT_SQL
    assert PROJECTED_RELATIONSHIP_COUNT_SQL.count("relationship.relationship_type IS NOT NULL") == 16
    assert "relationship.relationship_id" not in PROJECTED_RELATIONSHIP_COUNT_SQL
    assert PROJECTED_RELATIONSHIP_COUNT_SQL.count("\nUNION\n") == 15
    assert "\nUNION ALL\n" not in PROJECTED_RELATIONSHIP_COUNT_SQL
