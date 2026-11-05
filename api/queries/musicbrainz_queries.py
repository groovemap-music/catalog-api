"""Query functions for MusicBrainz enrichment data."""

from typing import Any

from common.query_debug import execute_sql
from psycopg import sql
from psycopg.rows import dict_row

from api.queries.helpers import run_query, run_single


# The relationship types musicbrainz-graph-enricher writes between two artists (its
# MB_RELATIONSHIP_MAP values). Cypher cannot take a type as a parameter, so the list is a code
# constant. tests/test_musicbrainz_relationship_types.py pins it to a snapshot of the enricher map
# and fails when the enricher adds a type this list omits.
MB_ARTIST_RELATIONSHIP_TYPES: tuple[str, ...] = (
    "MEMBER_OF",
    "COLLABORATED_WITH",
    "TAUGHT",
    "TRIBUTE_TO",
    "FOUNDED",
    "SUPPORTED",
    "SUBGROUP_OF",
    "RENAMED_TO",
)
_MB_TYPES = "|".join(MB_ARTIST_RELATIONSHIP_TYPES)

# Every relationship type that carries `source = 'musicbrainz'`: the artist types above, and the
# ISSUED_ON edge the enricher writes from a release to its Medium (MEDIA_SOURCE in its _projections.py).
# ISSUED_ON stays counted, as it always was. A type the enricher starts writing with that source and
# that is missing here would be left out of `created_in_neo4j`, so the list is pinned by a test.
MB_SOURCED_RELATIONSHIP_TYPES: tuple[str, ...] = (*MB_ARTIST_RELATIONSHIP_TYPES, "ISSUED_ON")

# `MATCH ()-[r]->() WHERE r.source = ...` read the `source` property of every relationship in the graph,
# 263M of them on lux. A statement per type reads only the edges of that type, through the planner's
# relationship-type scan: 22M edges here. One statement over all the types is NOT the same: with several
# types in one pattern the planner starts from every node and expands, which read 804M db hits and took
# 43 s on lux, worse than the single-type reads it replaces. Same count either way, because nothing
# else is written with that source.
_MB_SOURCED_COUNT_TEMPLATE = "MATCH ()-[r:{rel_type}]->() WHERE r.source = 'musicbrainz' RETURN COUNT(r) AS total"


def mb_sourced_count_cypher(rel_type: str) -> str:
    """The count statement for one relationship type, which must be one of ``MB_SOURCED_RELATIONSHIP_TYPES``."""
    if rel_type not in MB_SOURCED_RELATIONSHIP_TYPES:
        raise ValueError(f"{rel_type!r} is not a MusicBrainz-sourced relationship type")
    return _MB_SOURCED_COUNT_TEMPLATE.format(rel_type=rel_type)


async def count_mb_sourced_relationships(neo4j_driver: Any) -> int:
    """Count the MusicBrainz-sourced relationships, one type at a time, each under the default read timeout."""
    total = 0
    for rel_type in MB_SOURCED_RELATIONSHIP_TYPES:
        row = await run_single(neo4j_driver, mb_sourced_count_cypher(rel_type))
        total += row["total"] if row else 0
    return total


async def get_artist_musicbrainz(neo4j_driver: Any, discogs_id: int | str) -> dict[str, Any] | None:
    """Fetch MusicBrainz metadata for a Discogs artist from Neo4j."""
    row = await run_single(
        neo4j_driver,
        """MATCH (a:Artist {id: $discogs_id})
           WHERE a.mbid IS NOT NULL
           RETURN a.mbid AS mbid, a.mb_type AS type, a.mb_gender AS gender,
                  a.mb_begin_date AS begin_date, a.mb_end_date AS end_date,
                  a.mb_area AS area, a.mb_begin_area AS begin_area,
                  a.mb_disambiguation AS disambiguation""",
        discogs_id=discogs_id,
    )
    if not row:
        return None
    return {
        "discogs_id": discogs_id,
        "mbid": row["mbid"],
        "type": row["type"],
        "gender": row["gender"],
        "begin_date": row["begin_date"],
        "end_date": row["end_date"],
        "area": row["area"],
        "begin_area": row["begin_area"],
        "disambiguation": row["disambiguation"],
    }


# Typed on the enricher's relationship types so the planner reads only those edges, not every BY
# edge into the artist (audit H14). Output is the same rows as the untyped pattern for every type
# the enricher writes, which MB_ARTIST_RELATIONSHIP_TYPES lists.
_MB_RELATIONSHIPS_CYPHER = """MATCH (a:Artist {id: $discogs_id})-[r:__MB_TYPES__]->(target:Artist)
   WHERE r.source = 'musicbrainz'
   RETURN type(r) AS type, target.id AS target_id, target.name AS target_name,
          'outgoing' AS direction, r.begin_date AS begin_date,
          r.end_date AS end_date, r.attributes AS attributes
   UNION ALL
   MATCH (source:Artist)-[r:__MB_TYPES__]->(a:Artist {id: $discogs_id})
   WHERE r.source = 'musicbrainz'
   RETURN type(r) AS type, source.id AS target_id, source.name AS target_name,
          'incoming' AS direction, r.begin_date AS begin_date,
          r.end_date AS end_date, r.attributes AS attributes""".replace("__MB_TYPES__", _MB_TYPES)


async def get_artist_mb_relationships(neo4j_driver: Any, discogs_id: int | str) -> list[dict[str, Any]]:
    """Fetch MusicBrainz-sourced relationships for a Discogs artist from Neo4j."""
    return await run_query(neo4j_driver, _MB_RELATIONSHIPS_CYPHER, discogs_id=discogs_id)


async def get_artist_external_links(pool: Any, discogs_id: int) -> list[dict[str, Any]]:
    """Fetch external links for a Discogs artist from PostgreSQL."""
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await execute_sql(
            cur,
            """SELECT el.service_name AS service, el.url
               FROM musicbrainz.external_links el
               JOIN musicbrainz.artists a ON a.mbid = el.mbid
               WHERE a.discogs_artist_id = %s AND el.entity_type = 'artist'
               ORDER BY el.service_name""",
            (discogs_id,),
        )
        rows: list[dict[str, Any]] = await cur.fetchall()
        return rows


async def get_enrichment_status(pool: Any, neo4j_driver: Any) -> dict[str, Any]:
    """Fetch enrichment coverage statistics from both databases."""
    stats: dict[str, Any] = {"musicbrainz": {}}

    # PostgreSQL counts
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        for entity in ["artists", "labels", "releases"]:
            if entity == "releases":
                discogs_col = "discogs_release_id"
            elif entity == "artists":
                discogs_col = "discogs_artist_id"
            else:
                discogs_col = "discogs_label_id"

            # Safe: entity and discogs_col are from hardcoded lists, not user input.
            # Using sql.Identifier for defense-in-depth.
            total_query = sql.SQL(  # nosemgrep
                "SELECT COUNT(*) AS total FROM {schema}.{table}"
            ).format(schema=sql.Identifier("musicbrainz"), table=sql.Identifier(entity))
            await cur.execute(total_query)  # nosemgrep
            total_row = await cur.fetchone()
            total = total_row["total"] if total_row else 0

            matched_query = sql.SQL(  # nosemgrep
                "SELECT COUNT(*) AS matched FROM {schema}.{table} WHERE {col} IS NOT NULL"
            ).format(schema=sql.Identifier("musicbrainz"), table=sql.Identifier(entity), col=sql.Identifier(discogs_col))
            await cur.execute(matched_query)  # nosemgrep
            matched_row = await cur.fetchone()
            matched = matched_row["matched"] if matched_row else 0

            stats["musicbrainz"][entity] = {"total_mb": total, "matched_to_discogs": matched}

        await execute_sql(cur, "SELECT COUNT(*) AS total FROM musicbrainz.relationships")
        rel_row = await cur.fetchone()
        stats["musicbrainz"]["relationships"] = {"total_in_mb": rel_row["total"] if rel_row else 0}

    # Neo4j enrichment counts
    for entity, label in [("artists", "Artist"), ("labels", "Label"), ("releases", "Release")]:
        row = await run_single(
            neo4j_driver,
            f"MATCH (n:{label}) WHERE n.mbid IS NOT NULL RETURN COUNT(n) AS total",  # nosemgrep
        )
        stats["musicbrainz"][entity]["enriched_in_neo4j"] = row["total"] if row else 0

    stats["musicbrainz"]["relationships"]["created_in_neo4j"] = await count_mb_sourced_relationships(neo4j_driver)

    return stats
