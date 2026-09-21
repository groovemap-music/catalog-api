"""PostgreSQL implementations of the Explore query family.

The edge relations are the materialized projection of the Discogs graph.  SQL is
used here because every Explore walk is a fixed one- or two-hop join; counters
are read from the counter-bearing vertex views, never recomputed on request.
"""

# ruff: noqa: S608 -- interpolated SQL fragments are module-owned allowlisted constants

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, cast

from common.query_debug import execute_sql


async def _rows(pool: Any, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    async with pool.connection() as conn, conn.cursor() as cursor_cm:
        cursor = cast("Any", cursor_cm)
        await execute_sql(cursor, sql, params or {})
        columns = [column.name for column in cursor.description]
        return [dict(zip(columns, row, strict=True)) for row in await cursor.fetchall()]


async def _one(pool: Any, sql: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
    rows = await _rows(pool, sql, params)
    return rows[0] if rows else None


async def _number(pool: Any, sql: str, params: dict[str, Any]) -> int:
    row = await _one(pool, sql, params)
    return int(row["total"]) if row else 0


_ALIASES = """
SELECT artist_id FROM graph.alias_of WHERE alias_artist_id = %(artist_id)s
UNION SELECT group_artist_id FROM graph.member_of WHERE member_artist_id = %(artist_id)s
UNION SELECT member_artist_id FROM graph.member_of WHERE group_artist_id = %(artist_id)s
"""


async def explore_artist(pool: Any, name: str) -> dict[str, Any] | None:
    return await _one(
        pool,
        f"""
        SELECT a.artist_id AS id, a.name,
               (SELECT count(DISTINCT b.release_id) FROM graph.by_artist b WHERE b.artist_id = a.artist_id) AS release_count,
               (SELECT count(DISTINCT o.label_id) FROM graph.by_artist b JOIN graph.on_label o USING (release_id)
                WHERE b.artist_id = a.artist_id) AS label_count,
               (SELECT count(*) FROM ({_ALIASES}) related) AS alias_count
        FROM graph.artist a WHERE a.name = %(name)s ORDER BY a.artist_id LIMIT 1
    """,
        {"name": name, "artist_id": await _artist_id(pool, name)},
    )


async def _artist_id(pool: Any, name: str) -> str | None:
    row = await _one(pool, "SELECT artist_id FROM graph.artist WHERE name = %(name)s ORDER BY artist_id LIMIT 1", {"name": name})
    return row["artist_id"] if row else None


async def explore_genre(pool: Any, name: str) -> dict[str, Any] | None:
    return await _one(
        pool,
        """SELECT name AS id, name, release_count, artist_count, label_count, style_count
        FROM graph.genre_vertex WHERE name = %(name)s""",
        {"name": name},
    )


async def explore_style(pool: Any, name: str) -> dict[str, Any] | None:
    return await _one(
        pool,
        """SELECT name AS id, name, release_count, artist_count, label_count, genre_count
        FROM graph.style_vertex WHERE name = %(name)s""",
        {"name": name},
    )


async def explore_label(pool: Any, name: str) -> dict[str, Any] | None:
    return await _one(
        pool,
        """SELECT label_id AS id, name, release_count, artist_count, genre_count
        FROM graph.label_vertex WHERE name = %(name)s ORDER BY label_id LIMIT 1""",
        {"name": name},
    )


# Fixed, allowlisted join fragments for the four center labels and four child labels.
_CENTER: dict[str, tuple[str, str, str]] = {
    "artist": ("graph.by_artist", "artist_id", "graph.artist"),
    "genre": ("graph.in_genre", "genre_name", "graph.genre"),
    "label": ("graph.on_label", "label_id", "graph.label"),
    "style": ("graph.in_style", "style_name", "graph.style"),
}
_CHILD: dict[str, tuple[str, str, str, str]] = {
    "artist": ("graph.by_artist", "artist_id", "graph.artist", "artist_id"),
    "genre": ("graph.in_genre", "genre_name", "graph.genre", "name"),
    "label": ("graph.on_label", "label_id", "graph.label", "label_id"),
    "style": ("graph.in_style", "style_name", "graph.style", "name"),
}
_YEAR = "CASE WHEN btrim(r.year) ~ '^[0-9]{4}$' THEN r.year::integer END"


def _params(name: str, before_year: int | None, limit: int = 50, offset: int = 0) -> dict[str, Any]:
    return {"name": name, "before_year": before_year if before_year else None, "limit": limit, "offset": offset}


def _base(center: str) -> str:
    edge, key, vertex = _CENTER[center]
    identity = "name" if center in {"genre", "style"} else key
    return f"FROM {vertex} center JOIN {edge} e ON e.{key} = center.{identity} JOIN graph.release r ON r.release_id = e.release_id"


def _where() -> str:
    return f"WHERE center.name = %(name)s AND (%(before_year)s::integer IS NULL OR ({_YEAR} > 0 AND {_YEAR} <= %(before_year)s))"


async def _expand_releases(pool: Any, center: str, name: str, limit: int, offset: int, before_year: int | None) -> list[dict[str, Any]]:
    sql = f"""SELECT DISTINCT r.release_id AS id, r.title AS name, 'release' AS type,
             CASE WHEN {_YEAR} > 0 THEN {_YEAR} END AS year
             {_base(center)} {_where()}
             ORDER BY year DESC NULLS LAST, id LIMIT %(limit)s OFFSET %(offset)s"""
    return await _rows(pool, sql, _params(name, before_year, limit, offset))


async def _expand_aggregate(pool: Any, center: str, child: str, name: str, limit: int, offset: int, before_year: int | None) -> list[dict[str, Any]]:
    edge, key, vertex, identity = _CHILD[child]
    sql = f"""SELECT c.{identity} AS id, c.name, '{child}' AS type, count(DISTINCT r.release_id) AS release_count
             {_base(center)} JOIN {edge} child_edge ON child_edge.release_id = r.release_id
             JOIN {vertex} c ON c.{identity} = child_edge.{key} {_where()}
             GROUP BY c.{identity}, c.name ORDER BY release_count DESC, id LIMIT %(limit)s OFFSET %(offset)s"""
    return await _rows(pool, sql, _params(name, before_year, limit, offset))


async def _count_releases(pool: Any, center: str, name: str, before_year: int | None) -> int:
    return await _number(pool, f"SELECT count(DISTINCT r.release_id) AS total {_base(center)} {_where()}", _params(name, before_year))


async def _count_aggregate(pool: Any, center: str, child: str, name: str, before_year: int | None) -> int:
    edge, key, vertex, identity = _CHILD[child]
    return await _number(
        pool,
        f"""SELECT count(DISTINCT c.{identity}) AS total {_base(center)}
        JOIN {edge} child_edge ON child_edge.release_id = r.release_id
        JOIN {vertex} c ON c.{identity} = child_edge.{key} {_where()}""",
        _params(name, before_year),
    )


async def expand_artist_aliases(
    pool: Any,
    artist_name: str,
    limit: int = 50,
    offset: int = 0,
    *,
    before_year: int | None = None,  # noqa: ARG001
) -> list[dict[str, Any]]:
    artist_id = await _artist_id(pool, artist_name)
    if artist_id is None:
        return []
    return await _rows(
        pool,
        f"""SELECT a.artist_id AS id, a.name, 'artist' AS type FROM ({_ALIASES}) related
        JOIN graph.artist a USING (artist_id) ORDER BY id LIMIT %(limit)s OFFSET %(offset)s""",
        {"artist_id": artist_id, "limit": limit, "offset": offset},
    )


async def count_artist_aliases(pool: Any, artist_name: str, *, before_year: int | None = None) -> int:  # noqa: ARG001
    artist_id = await _artist_id(pool, artist_name)
    if artist_id is None:
        return 0
    return await _number(pool, f"SELECT count(*) AS total FROM ({_ALIASES}) related", {"artist_id": artist_id})


# These public wrappers deliberately retain the Cypher functions' signatures.  The
# mappings above are constants, so no caller-supplied identifier enters SQL text.
def _make_expand(center: str, child: str) -> Callable[..., Awaitable[list[dict[str, Any]]]]:
    async def expand(pool: Any, name: str, limit: int = 50, offset: int = 0, *, before_year: int | None = None) -> list[dict[str, Any]]:
        if child == "release":
            return await _expand_releases(pool, center, name, limit, offset, before_year)
        return await _expand_aggregate(pool, center, child, name, limit, offset, before_year)

    return expand


def _make_count(center: str, child: str) -> Callable[..., Awaitable[int]]:
    async def count(pool: Any, name: str, *, before_year: int | None = None) -> int:
        if child == "release":
            return await _count_releases(pool, center, name, before_year)
        return await _count_aggregate(pool, center, child, name, before_year)

    return count


_EXPANSIONS = {
    "artist": ("releases", "labels"),
    "genre": ("releases", "artists", "labels", "styles"),
    "label": ("releases", "artists", "genres"),
    "style": ("releases", "artists", "labels", "genres"),
}
for _center, _children in _EXPANSIONS.items():
    for _plural in _children:
        _child = _plural[:-1] if _plural != "releases" else "release"
        _expand = _make_expand(_center, _child)
        _expand.__name__ = f"expand_{_center}_{_plural}"
        globals()[_expand.__name__] = _expand
        _count = _make_count(_center, _child)
        _count.__name__ = f"count_{_center}_{_plural}"
        globals()[_count.__name__] = _count


async def _trends(pool: Any, center: str, name: str) -> list[dict[str, Any]]:
    return await _rows(
        pool,
        f"""SELECT {_YEAR} AS year, count(DISTINCT r.release_id) AS count
        {_base(center)} WHERE center.name = %(name)s AND {_YEAR} > 0
        GROUP BY year ORDER BY year""",
        {"name": name},
    )


async def trends_artist(pool: Any, name: str) -> list[dict[str, Any]]:
    return await _trends(pool, "artist", name)


async def trends_genre(pool: Any, name: str) -> list[dict[str, Any]]:
    return await _trends(pool, "genre", name)


async def trends_label(pool: Any, name: str) -> list[dict[str, Any]]:
    return await _trends(pool, "label", name)


async def trends_style(pool: Any, name: str) -> list[dict[str, Any]]:
    return await _trends(pool, "style", name)


async def get_genre_emergence(pool: Any, before_year: int) -> dict[str, list[dict[str, Any]]]:
    params = {"before_year": before_year}
    sql = "SELECT name, first_year FROM graph.{} WHERE first_year IS NOT NULL AND first_year <= %(before_year)s ORDER BY first_year, name"
    return {"genres": await _rows(pool, sql.format("genre_vertex"), params), "styles": await _rows(pool, sql.format("style_vertex"), params)}


async def get_label_details(pool: Any, node_id: str) -> dict[str, Any] | None:
    return await _one(
        pool,
        """SELECT l.label_id AS id, l.name,
        (SELECT count(DISTINCT release_id) FROM graph.on_label WHERE label_id = l.label_id) AS release_count
        FROM graph.label l WHERE l.label_id = %(id)s""",
        {"id": node_id},
    )


async def get_genre_details(pool: Any, node_id: str) -> dict[str, Any] | None:
    return await _one(pool, "SELECT name AS id, name, artist_count FROM graph.genre_vertex WHERE name = %(id)s", {"id": node_id})


async def get_style_details(pool: Any, node_id: str) -> dict[str, Any] | None:
    return await _one(pool, "SELECT name AS id, name, artist_count FROM graph.style_vertex WHERE name = %(id)s", {"id": node_id})


async def get_artist_details(pool: Any, node_id: str) -> dict[str, Any] | None:
    return await _one(
        pool,
        """SELECT a.artist_id AS id, a.name,
        ARRAY(SELECT DISTINCT g.genre_name FROM graph.by_artist b JOIN graph.in_genre g USING (release_id)
              WHERE b.artist_id = a.artist_id ORDER BY g.genre_name) AS genres,
        ARRAY(SELECT DISTINCT s.style_name FROM graph.by_artist b JOIN graph.in_style s USING (release_id)
              WHERE b.artist_id = a.artist_id ORDER BY s.style_name) AS styles,
        (SELECT count(DISTINCT release_id) FROM graph.by_artist WHERE artist_id = a.artist_id) AS release_count,
        ARRAY(SELECT DISTINCT grp.name FROM graph.member_of m JOIN graph.artist grp ON grp.artist_id = m.group_artist_id
              WHERE m.member_artist_id = a.artist_id ORDER BY grp.name) AS groups
        FROM graph.artist a WHERE a.artist_id = %(id)s""",
        {"id": node_id},
    )


async def get_release_details(pool: Any, node_id: str) -> dict[str, Any] | None:
    return await _one(
        pool,
        f"""SELECT r.release_id AS id, r.title AS name,
        CASE WHEN {_YEAR} > 0 THEN {_YEAR} END AS year,
        ARRAY(SELECT DISTINCT a.name FROM graph.by_artist b JOIN graph.artist a USING (artist_id)
              WHERE b.release_id = r.release_id ORDER BY a.name) AS artists,
        ARRAY(SELECT DISTINCT l.name FROM graph.on_label o JOIN graph.label l USING (label_id)
              WHERE o.release_id = r.release_id ORDER BY l.name) AS labels,
        ARRAY(SELECT DISTINCT genre_name FROM graph.in_genre WHERE release_id = r.release_id ORDER BY genre_name) AS genres,
        ARRAY(SELECT DISTINCT style_name FROM graph.in_style WHERE release_id = r.release_id ORDER BY style_name) AS styles,
        r.formats FROM graph.release r WHERE r.release_id = %(id)s""",
        {"id": node_id},
    )
