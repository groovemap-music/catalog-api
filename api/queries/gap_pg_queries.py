"""SQL queries for the three gap-analysis metadata lookups — the PostgreSQL backend.

`gap_queries.get_label_metadata`, `get_artist_metadata`, and `get_master_metadata` (the
Cypher these replace) are single-vertex lookups: `MATCH (x {id: $id}) RETURN x.id, x.name`
(`x.title` for a master). Per the coverage spike's single-vertex-lookup rule, each is a plain
``SELECT`` over its phase 0 vertex view rather than a ``GRAPH_TABLE`` pattern — these
functions, along with the rest of coverage spike family 1 ("vertex lookups and store
statistics"), touch no edge and need no new relations, so they run on every integration
tier rather than only PostgreSQL 19.
"""

from __future__ import annotations

from typing import Any, cast

from common.query_debug import execute_sql

from api.queries.gap_queries import attach_gap_identity


_YEAR = "CASE WHEN btrim(release.year) ~ '^[0-9]{1,9}$' THEN btrim(release.year)::integer END"


def _media_predicate(*, families: bool, mediums: bool) -> str:
    """Return the closed-set media predicate used by each gap query."""
    family = "release.media_families && %(families)s::text[]"
    medium = "EXISTS (SELECT 1 FROM graph.issued_on media WHERE media.release_id = release.release_id AND media.medium_id = ANY(%(mediums)s::text[]))"
    if families and mediums:
        return f"AND ({family} OR {medium})"
    if families:
        return f"AND {family}"
    if mediums:
        return f"AND {medium}"
    return ""


def _gap_sql(edge: str, entity_column: str, projection: str, *, exclude_wantlist: bool, families: bool, mediums: bool) -> tuple[str, str]:
    """Build one of the three structurally identical, closed-set gap queries."""
    wanted = (
        "AND NOT EXISTS (SELECT 1 FROM graph.wants wanted WHERE wanted.user_id = %(user_id)s::uuid AND wanted.release_id::text = release.release_id)"
        if exclude_wantlist
        else ""
    )
    media = _media_predicate(families=families, mediums=mediums)
    common = f"""
FROM graph.{edge} subject
JOIN graph.release release ON release.release_id = subject.release_id
WHERE subject.{entity_column} = %(entity_id)s
  AND NOT EXISTS (
      SELECT 1 FROM graph.collected owned
      WHERE owned.user_id = %(user_id)s::uuid
        AND owned.release_id::text = release.release_id
  )
  {wanted}
  {media}
"""  # noqa: S608 -- every interpolated identifier is a module-owned closed-set literal
    page = f"""
SELECT release.release_id, release.title, {_YEAR} AS year, release.formats,
       {projection},
       ARRAY(SELECT DISTINCT genre_name FROM graph.in_genre genre
             WHERE genre.release_id = release.release_id ORDER BY genre_name) AS genres,
       EXISTS (SELECT 1 FROM graph.wants wanted
               WHERE wanted.user_id = %(user_id)s::uuid
                 AND wanted.release_id::text = release.release_id) AS on_wantlist
{common}
ORDER BY year DESC NULLS FIRST, release.title, release.release_id
OFFSET %(offset)s LIMIT %(limit)s
"""  # noqa: S608 -- composes only module-owned SQL
    count = f"SELECT count(DISTINCT release.release_id)::bigint\n{common}"
    return page, count


def _summary_sql(edge: str, entity_column: str) -> str:
    return f"""
WITH available AS (
    SELECT DISTINCT release_id FROM graph.{edge} WHERE {entity_column} = %(entity_id)s
)
SELECT count(*)::bigint AS total,
       count(*) FILTER (WHERE EXISTS (
           SELECT 1 FROM graph.collected owned
           WHERE owned.user_id = %(user_id)s::uuid
             AND owned.release_id::text = available.release_id
       ))::bigint AS owned
FROM available
"""  # noqa: S608 -- identifiers come only from the three static wrappers below


LABEL_GAP_SUMMARY_SQL = _summary_sql("on_label", "label_id")
ARTIST_GAP_SUMMARY_SQL = _summary_sql("by_artist", "artist_id")
MASTER_GAP_SUMMARY_SQL = _summary_sql("derived_from", "master_id")


async def _rows(pool: Any, sql: str, params: dict[str, Any]) -> list[tuple[Any, ...]]:
    async with pool.connection() as conn, conn.cursor() as cursor_cm:
        cursor = cast("Any", cursor_cm)
        await execute_sql(cursor, sql, params)
        return cast("list[tuple[Any, ...]]", await cursor.fetchall())


async def _gaps(
    pool: Any,
    *,
    edge: str,
    entity_column: str,
    projection: str,
    user_id: str,
    entity_id: str,
    limit: int,
    offset: int,
    exclude_wantlist: bool,
    families: list[str] | None,
    mediums: list[str] | None,
) -> tuple[list[dict[str, Any]], int]:
    page_sql, count_sql = _gap_sql(
        edge,
        entity_column,
        projection,
        exclude_wantlist=exclude_wantlist,
        families=bool(families),
        mediums=bool(mediums),
    )
    params: dict[str, Any] = {"user_id": user_id, "entity_id": entity_id, "limit": limit, "offset": offset}
    if families:
        params["families"] = families
    if mediums:
        params["mediums"] = mediums
    rows = await _rows(pool, page_sql, params)
    totals = await _rows(pool, count_sql, params)
    results: list[dict[str, Any]] = []
    for row in rows:
        result = {"id": row[0], "title": row[1], "year": row[2], "formats": row[3]}
        if edge == "on_label":
            result.update({"artist": row[4], "genres": row[5], "on_wantlist": row[6]})
        elif edge == "by_artist":
            result.update({"label": row[4], "genres": row[5], "on_wantlist": row[6]})
        else:
            result.update({"artist": row[4], "label": row[5], "genres": row[6], "on_wantlist": row[7]})
        results.append(result)
    return await attach_gap_identity(results), totals[0][0] if totals else 0


async def _summary(pool: Any, sql: str, user_id: str, entity_id: str) -> dict[str, Any]:
    rows = await _rows(pool, sql, {"user_id": user_id, "entity_id": entity_id})
    if not rows:
        return {"total": 0, "owned": 0, "missing": 0}
    total, owned = rows[0]
    return {"total": total, "owned": owned, "missing": total - owned}


async def get_label_gaps(
    pool: Any,
    user_id: str,
    label_id: str,
    limit: int = 50,
    offset: int = 0,
    exclude_wantlist: bool = False,
    families: list[str] | None = None,
    mediums: list[str] | None = None,
) -> tuple[list[dict[str, Any]], int]:
    return await _gaps(
        pool,
        edge="on_label",
        entity_column="label_id",
        projection="(SELECT min(artist.name) FROM graph.by_artist artist_edge JOIN graph.artist artist USING (artist_id) WHERE artist_edge.release_id = release.release_id) AS artist",
        user_id=user_id,
        entity_id=label_id,
        limit=limit,
        offset=offset,
        exclude_wantlist=exclude_wantlist,
        families=families,
        mediums=mediums,
    )


async def get_label_gap_summary(pool: Any, user_id: str, label_id: str) -> dict[str, Any]:
    return await _summary(pool, LABEL_GAP_SUMMARY_SQL, user_id, label_id)


async def get_artist_gaps(
    pool: Any,
    user_id: str,
    artist_id: str,
    limit: int = 50,
    offset: int = 0,
    exclude_wantlist: bool = False,
    families: list[str] | None = None,
    mediums: list[str] | None = None,
) -> tuple[list[dict[str, Any]], int]:
    return await _gaps(
        pool,
        edge="by_artist",
        entity_column="artist_id",
        projection="(SELECT min(label.name) FROM graph.on_label label_edge JOIN graph.label label USING (label_id) WHERE label_edge.release_id = release.release_id) AS label",
        user_id=user_id,
        entity_id=artist_id,
        limit=limit,
        offset=offset,
        exclude_wantlist=exclude_wantlist,
        families=families,
        mediums=mediums,
    )


async def get_artist_gap_summary(pool: Any, user_id: str, artist_id: str) -> dict[str, Any]:
    return await _summary(pool, ARTIST_GAP_SUMMARY_SQL, user_id, artist_id)


async def get_master_gaps(
    pool: Any,
    user_id: str,
    master_id: str,
    limit: int = 50,
    offset: int = 0,
    exclude_wantlist: bool = False,
    families: list[str] | None = None,
    mediums: list[str] | None = None,
) -> tuple[list[dict[str, Any]], int]:
    projection = "(SELECT min(artist.name) FROM graph.by_artist artist_edge JOIN graph.artist artist USING (artist_id) WHERE artist_edge.release_id = release.release_id) AS artist, (SELECT min(label.name) FROM graph.on_label label_edge JOIN graph.label label USING (label_id) WHERE label_edge.release_id = release.release_id) AS label"
    return await _gaps(
        pool,
        edge="derived_from",
        entity_column="master_id",
        projection=projection,
        user_id=user_id,
        entity_id=master_id,
        limit=limit,
        offset=offset,
        exclude_wantlist=exclude_wantlist,
        families=families,
        mediums=mediums,
    )


async def get_master_gap_summary(pool: Any, user_id: str, master_id: str) -> dict[str, Any]:
    return await _summary(pool, MASTER_GAP_SUMMARY_SQL, user_id, master_id)


LABEL_METADATA_SQL = """
SELECT label_id, name
FROM graph.label
WHERE label_id = %(label_id)s
"""

ARTIST_METADATA_SQL = """
SELECT artist_id, name
FROM graph.artist
WHERE artist_id = %(artist_id)s
"""

# `graph.master` names the title column `title`, not `name`; the Cypher projects it as
# `m.title AS name`, and the response dict below keeps that same key.
MASTER_METADATA_SQL = """
SELECT master_id, title
FROM graph.master
WHERE master_id = %(master_id)s
"""


async def get_label_metadata(pool: Any, label_id: str) -> dict[str, Any] | None:
    """Return the label's id and name, or ``None`` when no such label exists.

    Mirrors :func:`api.queries.gap_queries.get_label_metadata` column for column.
    """
    async with pool.connection() as conn, conn.cursor() as cursor_cm:
        cursor = cast("Any", cursor_cm)
        await execute_sql(cursor, LABEL_METADATA_SQL, {"label_id": label_id})
        row = await cursor.fetchone()

    if row is None:
        return None
    return {"id": row[0], "name": row[1]}


async def get_artist_metadata(pool: Any, artist_id: str) -> dict[str, Any] | None:
    """Return the artist's id and name, or ``None`` when no such artist exists.

    Mirrors :func:`api.queries.gap_queries.get_artist_metadata` column for column.
    """
    async with pool.connection() as conn, conn.cursor() as cursor_cm:
        cursor = cast("Any", cursor_cm)
        await execute_sql(cursor, ARTIST_METADATA_SQL, {"artist_id": artist_id})
        row = await cursor.fetchone()

    if row is None:
        return None
    return {"id": row[0], "name": row[1]}


async def get_master_metadata(pool: Any, master_id: str) -> dict[str, Any] | None:
    """Return the master's id and title (as ``name``), or ``None`` when it does not exist.

    Mirrors :func:`api.queries.gap_queries.get_master_metadata` column for column.
    """
    async with pool.connection() as conn, conn.cursor() as cursor_cm:
        cursor = cast("Any", cursor_cm)
        await execute_sql(cursor, MASTER_METADATA_SQL, {"master_id": master_id})
        row = await cursor.fetchone()

    if row is None:
        return None
    return {"id": row[0], "name": row[1]}
