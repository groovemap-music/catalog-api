"""PostgreSQL implementation of the MusicBrainz enrichment read family."""

from typing import Any, cast

from common.query_debug import execute_sql
from psycopg import sql

from api.queries.musicbrainz_pipeline import MusicBrainzHandles


ARTIST_MUSICBRAINZ_SQL = """
SELECT artist.mbid::text,
       artist.type,
       artist.gender,
       artist.begin_date,
       artist.end_date,
       artist.area,
       artist.begin_area,
       artist.disambiguation
FROM graph.mb_artist AS artist
WHERE artist.discogs_artist_id = %(discogs_id)s
"""

ARTIST_RELATIONSHIPS_SQL = """
WITH artist_relationships AS (
    SELECT relationship.relationship_type AS type,
           target.discogs_artist_id::text AS target_id,
           target.name AS target_name,
           'outgoing'::text AS direction,
           relationship.begin_date,
           relationship.end_date,
           relationship.attributes
    FROM graph.mb_artist AS anchor
    JOIN graph.mb_rel_artist_artist AS relationship
      ON relationship.source_mbid = anchor.mbid
    JOIN graph.mb_artist AS target
      ON target.mbid = relationship.target_mbid
    WHERE anchor.discogs_artist_id = %(discogs_id)s
      AND target.discogs_artist_id IS NOT NULL
      AND relationship.relationship_type IS NOT NULL
    UNION ALL
    SELECT relationship.relationship_type AS type,
           source.discogs_artist_id::text AS target_id,
           source.name AS target_name,
           'incoming'::text AS direction,
           relationship.begin_date,
           relationship.end_date,
           relationship.attributes
    FROM graph.mb_artist AS anchor
    JOIN graph.mb_rel_artist_artist AS relationship
      ON relationship.target_mbid = anchor.mbid
    JOIN graph.mb_artist AS source
      ON source.mbid = relationship.source_mbid
    WHERE anchor.discogs_artist_id = %(discogs_id)s
      AND source.discogs_artist_id IS NOT NULL
      AND relationship.relationship_type IS NOT NULL
)
SELECT type, target_id, target_name, direction, begin_date, end_date, attributes
FROM artist_relationships
ORDER BY type COLLATE "C", target_id COLLATE "C", direction COLLATE "C",
         begin_date COLLATE "C" NULLS FIRST, end_date COLLATE "C" NULLS FIRST,
         attributes::text COLLATE "C" NULLS FIRST
"""

EXTERNAL_LINKS_SQL = """
SELECT link.service_name AS service, link.url
FROM musicbrainz.external_links AS link
JOIN musicbrainz.artists AS artist ON artist.mbid = link.mbid
WHERE artist.discogs_artist_id = %(discogs_id)s
  AND link.entity_type = 'artist'
ORDER BY link.service_name COLLATE "C", link.url COLLATE "C"
"""

_ARTIST_COLUMNS = ("mbid", "type", "gender", "begin_date", "end_date", "area", "begin_area", "disambiguation")
_RELATIONSHIP_COLUMNS = ("type", "target_id", "target_name", "direction", "begin_date", "end_date", "attributes")
_LINK_COLUMNS = ("service", "url")


async def _fetch_all(pool: Any, statement: str, params: dict[str, Any], columns: tuple[str, ...]) -> list[dict[str, Any]]:
    async with pool.connection() as conn, conn.cursor() as cursor_cm:
        cursor = cast("Any", cursor_cm)
        await execute_sql(cursor, statement, params)
        rows = await cursor.fetchall()
    return [dict(zip(columns, row, strict=True)) for row in rows]


async def get_artist_musicbrainz(handles: MusicBrainzHandles, discogs_id: int | str) -> dict[str, Any] | None:
    """Fetch mapped MusicBrainz metadata from ``graph.mb_artist``."""
    rows = await _fetch_all(handles.graph, ARTIST_MUSICBRAINZ_SQL, {"discogs_id": discogs_id}, _ARTIST_COLUMNS)
    if not rows:
        return None
    return {"discogs_id": discogs_id, **rows[0]}


async def get_artist_mb_relationships(handles: MusicBrainzHandles, discogs_id: int | str) -> list[dict[str, Any]]:
    """Fetch mapped artist relationships, excluding raw types Neo4j never writes."""
    return await _fetch_all(handles.graph, ARTIST_RELATIONSHIPS_SQL, {"discogs_id": discogs_id}, _RELATIONSHIP_COLUMNS)


async def get_artist_external_links(handles: MusicBrainzHandles, discogs_id: int | str) -> list[dict[str, Any]]:
    """Fetch external links from the shared relational MusicBrainz store."""
    return await _fetch_all(handles.relational, EXTERNAL_LINKS_SQL, {"discogs_id": discogs_id}, _LINK_COLUMNS)


_ENTITY_COUNTS = (
    ("artists", "discogs_artist_id", "mb_artist"),
    ("labels", "discogs_label_id", "mb_label"),
    ("releases", "discogs_release_id", "mb_release"),
)

_MB_VERTEX_KEYS = {
    "artist": ("mb_artist", "discogs_artist_id"),
    "label": ("mb_label", "discogs_label_id"),
    "release": ("mb_release", "discogs_release_id"),
    "release_group": ("mb_release_group", "discogs_master_id"),
}


def _projected_relationship_count_sql() -> str:
    """Count the distinct mapped edges Neo4j can bind and ``MERGE``.

    The relational source deliberately retains separate relationship instances that differ
    by dates or attributes.  The graph enricher does not: it merges by endpoint pair and
    mapped relationship type.  The status field is historically named
    ``created_in_neo4j``, so it must count that projected edge identity rather than source
    instances.
    """
    branches: list[str] = []
    for source, (source_view, source_key) in _MB_VERTEX_KEYS.items():
        for target, (target_view, target_key) in _MB_VERTEX_KEYS.items():
            branches.append(
                f"""SELECT '{source}'::text AS source_type,
       source_vertex.{source_key}::text AS source_key,
       '{target}'::text AS target_type,
       target_vertex.{target_key}::text AS target_key,
       relationship.relationship_type
FROM graph.mb_rel_{source}_{target} AS relationship
JOIN graph.{source_view} AS source_vertex ON source_vertex.mbid = relationship.source_mbid
JOIN graph.{target_view} AS target_vertex ON target_vertex.mbid = relationship.target_mbid
WHERE relationship.relationship_type IS NOT NULL
  AND source_vertex.{source_key} IS NOT NULL
  AND target_vertex.{target_key} IS NOT NULL"""  # noqa: S608 -- all names come from the fixed mapping above
            )
    return "SELECT count(*)::bigint FROM (\n" + "\nUNION\n".join(branches) + "\n) AS projected"  # noqa: S608


PROJECTED_RELATIONSHIP_COUNT_SQL = _projected_relationship_count_sql()


async def get_enrichment_status(handles: MusicBrainzHandles) -> dict[str, Any]:
    """Return source totals and the rows projected into the selected graph backend."""
    stats: dict[str, Any] = {"musicbrainz": {}}
    async with handles.relational.connection() as conn, conn.cursor() as cursor_cm:
        cursor = cast("Any", cursor_cm)
        for entity, discogs_col, graph_view in _ENTITY_COUNTS:
            total_query = sql.SQL("SELECT count(*) FROM musicbrainz.{table}").format(table=sql.Identifier(entity))
            matched_query = sql.SQL("SELECT count(*) FROM musicbrainz.{table} WHERE {column} IS NOT NULL").format(
                table=sql.Identifier(entity), column=sql.Identifier(discogs_col)
            )
            projected_query = sql.SQL("SELECT count(*) FROM graph.{view} WHERE {column} IS NOT NULL").format(
                view=sql.Identifier(graph_view), column=sql.Identifier(discogs_col)
            )
            await cursor.execute(total_query)
            total_row = await cursor.fetchone()
            await cursor.execute(matched_query)
            matched_row = await cursor.fetchone()
            await cursor.execute(projected_query)
            projected_row = await cursor.fetchone()
            stats["musicbrainz"][entity] = {
                "total_mb": total_row[0] if total_row else 0,
                "matched_to_discogs": matched_row[0] if matched_row else 0,
                "enriched_in_neo4j": projected_row[0] if projected_row else 0,
            }

        await cursor.execute("SELECT count(*) FROM musicbrainz.relationships")
        total_relationships = await cursor.fetchone()
        await cursor.execute(PROJECTED_RELATIONSHIP_COUNT_SQL)
        projected_relationships = await cursor.fetchone()
    stats["musicbrainz"]["relationships"] = {
        "total_in_mb": total_relationships[0] if total_relationships else 0,
        "created_in_neo4j": projected_relationships[0] if projected_relationships else 0,
    }
    return stats
