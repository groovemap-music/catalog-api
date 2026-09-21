"""Neo4j implementation of the MusicBrainz enrichment read family."""

from typing import Any, cast

from common.query_debug import execute_sql
from psycopg import sql

from api.queries.helpers import run_query, run_single
from api.queries.musicbrainz_pipeline import MusicBrainzHandles


async def get_artist_musicbrainz(handles: MusicBrainzHandles, discogs_id: int | str) -> dict[str, Any] | None:
    """Fetch MusicBrainz metadata for a Discogs artist from Neo4j."""
    row = await run_single(
        handles.graph,
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
    return {"discogs_id": discogs_id, **row}


async def get_artist_mb_relationships(handles: MusicBrainzHandles, discogs_id: int | str) -> list[dict[str, Any]]:
    """Fetch MusicBrainz-sourced relationships for a Discogs artist from Neo4j."""
    return await run_query(
        handles.graph,
        """MATCH (a:Artist {id: $discogs_id})-[r]->(target:Artist)
           WHERE r.source = 'musicbrainz'
           RETURN type(r) AS type, target.id AS target_id, target.name AS target_name,
                  'outgoing' AS direction, r.begin_date AS begin_date,
                  r.end_date AS end_date, r.attributes AS attributes
           UNION ALL
           MATCH (source:Artist)-[r]->(a:Artist {id: $discogs_id})
           WHERE r.source = 'musicbrainz'
           RETURN type(r) AS type, source.id AS target_id, source.name AS target_name,
                  'incoming' AS direction, r.begin_date AS begin_date,
                  r.end_date AS end_date, r.attributes AS attributes
           ORDER BY type, target_id, direction, begin_date, end_date, attributes""",
        discogs_id=discogs_id,
    )


async def get_artist_external_links(handles: MusicBrainzHandles, discogs_id: int | str) -> list[dict[str, Any]]:
    """Fetch external links from the relational store shared by both backends."""
    async with handles.relational.connection() as conn, conn.cursor() as cursor_cm:
        cursor = cast("Any", cursor_cm)
        await execute_sql(
            cursor,
            """SELECT link.service_name AS service, link.url
               FROM musicbrainz.external_links AS link
               JOIN musicbrainz.artists AS artist ON artist.mbid = link.mbid
               WHERE artist.discogs_artist_id = %(discogs_id)s
                 AND link.entity_type = 'artist'
               ORDER BY link.service_name COLLATE "C", link.url COLLATE "C"
            """,
            {"discogs_id": discogs_id},
        )
        rows = await cursor.fetchall()
    return [{"service": row[0], "url": row[1]} for row in rows]


async def get_enrichment_status(handles: MusicBrainzHandles) -> dict[str, Any]:
    """Fetch source-table totals and Neo4j enrichment counts."""
    stats: dict[str, Any] = {"musicbrainz": {}}
    async with handles.relational.connection() as conn, conn.cursor() as cursor_cm:
        cursor = cast("Any", cursor_cm)
        for entity, discogs_col in (("artists", "discogs_artist_id"), ("labels", "discogs_label_id"), ("releases", "discogs_release_id")):
            total_query = sql.SQL("SELECT count(*) FROM musicbrainz.{table}").format(table=sql.Identifier(entity))
            matched_query = sql.SQL("SELECT count(*) FROM musicbrainz.{table} WHERE {column} IS NOT NULL").format(
                table=sql.Identifier(entity), column=sql.Identifier(discogs_col)
            )
            await cursor.execute(total_query)
            total_row = await cursor.fetchone()
            await cursor.execute(matched_query)
            matched_row = await cursor.fetchone()
            stats["musicbrainz"][entity] = {
                "total_mb": total_row[0] if total_row else 0,
                "matched_to_discogs": matched_row[0] if matched_row else 0,
            }

        await cursor.execute("SELECT count(*) FROM musicbrainz.relationships")
        relationship_row = await cursor.fetchone()
        stats["musicbrainz"]["relationships"] = {"total_in_mb": relationship_row[0] if relationship_row else 0}

    for entity, label in (("artists", "Artist"), ("labels", "Label"), ("releases", "Release")):
        row = await run_single(handles.graph, f"MATCH (n:{label}) WHERE n.mbid IS NOT NULL RETURN COUNT(n) AS total")  # nosemgrep
        stats["musicbrainz"][entity]["enriched_in_neo4j"] = row["total"] if row else 0

    row = await run_single(handles.graph, "MATCH ()-[r]->() WHERE r.source = 'musicbrainz' RETURN COUNT(r) AS total")
    stats["musicbrainz"]["relationships"]["created_in_neo4j"] = row["total"] if row else 0
    return stats
