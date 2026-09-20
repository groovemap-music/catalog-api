"""SQL/PGQ queries for the rarity signal batch — the PostgreSQL backend.

The Cypher this replaces is :mod:`api.queries.rarity_queries`, and the walk both backends run
is :mod:`api.queries.rarity_pipeline`. This module is only the eleven statements and the four
reads that issue them.

CHUNKING CONTRACT
-----------------
Read the banner in `api/queries/rarity_queries.py` before editing anything here. Eight
unbounded `MATCH (r:Release)` scans is what took the rarity pipeline down for 33 consecutive
daily cycles, and every statement below is bound to one explicit page of release ids for
exactly that reason. `UNWIND $ids AS rid / MATCH (r:Release {id: rid})` becomes::

    MATCH (r IS release WHERE r.release_id = ANY(%(ids)s))

inside the element pattern — **not** a filter applied to a finished `GRAPH_TABLE`, which would
traverse the whole catalog and then throw most of it away. The page is
:data:`api.queries.rarity_pipeline.RARITY_PAGE_SIZE` releases on both backends, and the same
per-query budget is applied here as a server-side `statement_timeout` so a pathological page
fails fast and attributably rather than stalling. `tests/test_rarity_pg_queries.py` pins both.

Where the counters come from
----------------------------
Four of the Cypher signal queries read a *node property* `graphinator` writes in a post-import
pass — `Label.release_count`, `Genre.release_count`, and the degrees `COUNT { (a)--() }` and
`COUNT { (r)--() }`. Re-aggregating those on request is the failure the chunking contract
exists to prevent, so they stay property reads here too. The phase 2 schema revision makes
four labels bind a `<label>_vertex` projection that joins the storage relation to its counter
relation, which is what lets `l.release_count` and `a.degree` read exactly as the Cypher reads
them. A counter the loader has never computed reads `0`, not null, because the projection
COALESCEs it — the same rule the Cypher's `COALESCE(l.release_count, 0)` states.

`graph.release_degree` is the one counter that is **not** folded onto its label. Its live half
is a pair of lateral counts over `user_collections` and `user_wantlists` that no unique key
makes removable, so folding it onto the `release` vertex would make every traversal that binds
a release count collection rows it never reads. It is its own label, and
`MATCH (d IS release_degree WHERE d.release_id = ...)` is the one spelling this rewrite has to
carry forward. Its value is the loader's catalog-edge count plus those two live counts, which
is what Neo4j's `COUNT { (r)--() }` counts: catalog edges plus `COLLECTED` and `WANTS`.

One bound follows from that and is worth stating: `graph.release_degree_base` is grouped over
the edge tables, so a release with no catalog edge at all has no row in it and therefore none
in `graph.release_degree`. Its degree reads 0 here, where Neo4j would still count a `COLLECTED`
edge. Every release a loader ingests has at least a `by_artist` or an `issued_on` edge, so the
case is an empty catalog entry rather than a live one.

Year is text here
-----------------
`graph.release.year` is `releases.data ->> 'year'` — text read straight off the Discogs
document — while Neo4j's `r.year` is an integer `graphinator` wrote or omitted. Casting it
unconditionally breaks the whole aggregate on the first non-numeric value, so every read of it
filters with `btrim(year) ~ '^[0-9]{4}$'` *before* the cast, exactly as
`api/queries/neo4j_pg_queries.py` and the producer's own `genre_stats` counter do.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast

import structlog
from common.query_debug import execute_sql

from api.queries.rarity_pipeline import (
    RARITY_PAGE_SIZE,
    RARITY_QUERY_TIMEOUT_SECONDS,
    RarityHandles,
    rows_by_release_id,
    score_all_rarity_signals,
)
from api.rarity import family_sql_queries


logger = structlog.get_logger(__name__)


# ── The keyset walk ──────────────────────────────────────────────────────────

# Keyset pagination over `release_id`, which is the Cypher's `r.id > $cursor ORDER BY r.id`.
#
# `COLLATE "C"` on both the comparison and the sort is not decoration. Neo4j orders strings by
# code point; PostgreSQL orders them by the database's collation, which for a non-`C` locale
# can ignore punctuation and case and would give a different page boundary for the same cursor.
# Discogs ids are digit strings, where every collation agrees — which is exactly why an
# implicit dependency on that would go unnoticed until it did not.
RELEASE_ID_PAGE_SQL = """
SELECT release_id
FROM graph.release
WHERE release_id COLLATE "C" > %(cursor)s
ORDER BY release_id COLLATE "C"
LIMIT %(limit)s
"""

# The walk's coverage check. One aggregate over the release view, which is the SQL analogue of
# Neo4j serving `count(r)` from its label count store.
RELEASE_COUNT_SQL = """
SELECT count(*)::bigint AS total
FROM graph.release
"""


# ── The eight core signal statements ─────────────────────────────────────────

# 1. The release itself: the display fields, and the row set the scoring loop walks.
#
# `min(name COLLATE "C")` is the SQL of the Cypher's `min(a.name)`, with the collation pinned
# for the same reason the page boundary is: two engines cannot agree on "the alphabetically
# first credit" unless they agree on the alphabet. A release with no credit gets a null
# `artist_name` from the LEFT JOIN, which is what `min` over no rows returns on the other side.
RELEASE_SQL = """
SELECT page.release_id AS release_id,
       page.title      AS title,
       credited.artist_name AS artist_name,
       CASE WHEN btrim(page.year) ~ '^[0-9]{4}$' THEN btrim(page.year)::int END AS year
FROM (
    SELECT release_id, title, year
    FROM graph.release
    WHERE release_id = ANY(%(ids)s)
) AS page
LEFT JOIN (
    SELECT release_id, min(name COLLATE "C") AS artist_name
    FROM GRAPH_TABLE (graph.catalog
        MATCH (r IS release WHERE r.release_id = ANY(%(ids)s))-[IS by_artist]->(a IS artist)
        COLUMNS (r.release_id AS release_id, a.name AS name)
    ) AS credit
    GROUP BY release_id
) AS credited ON credited.release_id = page.release_id
"""

# 2. Media per release, best evidence first.
#
# `issued_on` is authoritative — the canonical medium id, with the family on the medium vertex
# — and is aggregated into the same `[{id, family}, ...]` shape the Cypher's `collect(DISTINCT
# {id: m.id, family: m.family})` builds; psycopg hands the `jsonb` back as a list of dicts. The
# `COALESCE(..., '[]')` is the Cypher's list comprehension dropping the null row an OPTIONAL
# MATCH leaves behind: a release with no `issued_on` edge gets `[]`, never null.
#
# `media_families` and `formats` are list properties of the release vertex on both engines, and
# `graph.release` projects each as an empty `text[]` rather than null when the document carries
# none.
MEDIA_SQL = """
SELECT page.release_id AS release_id,
       COALESCE(issued.mediums, '[]'::jsonb) AS mediums,
       page.media_families AS media_families,
       page.formats AS formats
FROM (
    SELECT release_id, media_families, formats
    FROM graph.release
    WHERE release_id = ANY(%(ids)s)
) AS page
LEFT JOIN (
    SELECT release_id, jsonb_agg(DISTINCT jsonb_build_object('id', medium_id, 'family', family)) AS mediums
    FROM GRAPH_TABLE (graph.catalog
        MATCH (r IS release WHERE r.release_id = ANY(%(ids)s))-[IS issued_on]->(m IS medium)
        COLUMNS (r.release_id AS release_id, m.medium_id AS medium_id, m.family AS family)
    ) AS issue
    GROUP BY release_id
) AS issued ON issued.release_id = page.release_id
"""

# 3. Label catalog size per release. A row only for a release that is on a label, exactly as
# the Cypher's non-optional `(r)-[:ON]->(l:Label)` produces one.
LABEL_SQL = """
SELECT release_id, min(release_count)::bigint AS label_catalog_size
FROM GRAPH_TABLE (graph.catalog
    MATCH (r IS release WHERE r.release_id = ANY(%(ids)s))-[IS on_label]->(l IS label)
    COLUMNS (r.release_id AS release_id, l.release_count AS release_count)
) AS hop
GROUP BY release_id
"""

# 4. Temporal: release year + latest sibling year.
#
# `sibling.release_id <> r.release_id` is the Cypher's `sibling <> r`. Neo4j derives it from
# relationship isomorphism — the same `DERIVED_FROM` edge may not bind twice — and SQL/PGQ's
# walk semantics do not, so without it every mastered release is its own latest sibling.
TEMPORAL_SQL = """
SELECT page.release_id AS release_id,
       CASE WHEN btrim(page.year) ~ '^[0-9]{4}$' THEN btrim(page.year)::int END AS year,
       sibling.latest_sibling_year AS latest_sibling_year
FROM (
    SELECT release_id, year
    FROM graph.release
    WHERE release_id = ANY(%(ids)s)
) AS page
LEFT JOIN (
    SELECT release_id, max(btrim(sibling_year)::int) AS latest_sibling_year
    FROM GRAPH_TABLE (graph.catalog
        MATCH (r IS release WHERE r.release_id = ANY(%(ids)s))
              -[IS derived_from]->(m IS master)<-[IS derived_from]-(sibling IS release)
        WHERE sibling.release_id <> r.release_id
        COLUMNS (r.release_id AS release_id, sibling.year AS sibling_year)
    ) AS walk
    WHERE btrim(sibling_year) ~ '^[0-9]{4}$'
    GROUP BY release_id
) AS sibling ON sibling.release_id = page.release_id
"""

# 5. Graph degree per release — the counter read, not a re-aggregation. See the module
# docstring for why `release_degree` is a label of its own and what its two halves are. The
# LEFT JOIN is what gives every id on the page a row, as the Cypher's plain
# `MATCH (r:Release {id: rid})` does; a release with no row in the base relation reads 0.
DEGREE_SQL = """
SELECT page.release_id AS release_id,
       COALESCE(counted.degree, 0)::bigint AS degree
FROM (
    SELECT release_id
    FROM graph.release
    WHERE release_id = ANY(%(ids)s)
) AS page
LEFT JOIN (
    SELECT release_id, degree
    FROM GRAPH_TABLE (graph.catalog
        MATCH (d IS release_degree WHERE d.release_id = ANY(%(ids)s))
        COLUMNS (d.release_id AS release_id, d.degree AS degree)
    ) AS walk
) AS counted ON counted.release_id = page.release_id
"""

# 6. Max artist degree per release. `a.degree` is the `artist_degree` counter joined onto the
# artist vertex, which is what the Cypher's `COUNT { (a)--() }` counts: every edge incident to
# the artist, in either direction.
ARTIST_DEGREE_SQL = """
SELECT release_id, max(degree)::bigint AS artist_max_degree
FROM GRAPH_TABLE (graph.catalog
    MATCH (r IS release WHERE r.release_id = ANY(%(ids)s))-[IS by_artist]->(a IS artist)
    COLUMNS (r.release_id AS release_id, a.degree AS degree)
) AS hop
GROUP BY release_id
"""

# 7. Max label catalog size per release.
LABEL_SIZE_SQL = """
SELECT release_id, max(release_count)::bigint AS label_max_catalog
FROM GRAPH_TABLE (graph.catalog
    MATCH (r IS release WHERE r.release_id = ANY(%(ids)s))-[IS on_label]->(l IS label)
    COLUMNS (r.release_id AS release_id, l.release_count AS release_count)
) AS hop
GROUP BY release_id
"""

# 8. Max genre release count per release. The overloaded Cypher `[:IS]` to a `:Genre` is
# `in_genre` here; the `[:IS]` to a `:Style` is `in_style` and this signal does not read it,
# exactly as the Cypher does not.
GENRE_COUNT_SQL = """
SELECT release_id, max(release_count)::bigint AS genre_max_release_count
FROM GRAPH_TABLE (graph.catalog
    MATCH (r IS release WHERE r.release_id = ANY(%(ids)s))-[IS in_genre]->(g IS genre)
    COLUMNS (r.release_id AS release_id, g.release_count AS release_count)
) AS hop
GROUP BY release_id
"""

# The media-neutral statements, keyed by the fact name their rows are indexed under — the same
# keys `api/queries/rarity_queries.py` uses, because the pipeline joins on them. Family
# extension modules contribute further statements through the registry's `sql_queries`.
_CORE_SQL: dict[str, str] = {
    "release": RELEASE_SQL,
    "media": MEDIA_SQL,
    "label": LABEL_SQL,
    "temporal": TEMPORAL_SQL,
    "degree": DEGREE_SQL,
    "artist_degree": ARTIST_DEGREE_SQL,
    "label_size": LABEL_SIZE_SQL,
    "genre_count": GENRE_COUNT_SQL,
}

# The column each fact's rows carry, in the order the Cypher returns them. The rows the
# pipeline joins are dicts, so this is what turns a tuple row back into one — and keeping it
# beside the statements is what makes a renamed column a two-line change rather than a silent
# `KeyError` three modules away.
_FACT_COLUMNS: dict[str, tuple[str, ...]] = {
    "release": ("release_id", "title", "artist_name", "year"),
    "media": ("release_id", "mediums", "media_families", "formats"),
    "label": ("release_id", "label_catalog_size"),
    "temporal": ("release_id", "year", "latest_sibling_year"),
    "degree": ("release_id", "degree"),
    "artist_degree": ("release_id", "artist_max_degree"),
    "label_size": ("release_id", "label_max_catalog"),
    "genre_count": ("release_id", "genre_max_release_count"),
    "grooved_pressing": ("release_id", "pressing_count"),
}


# ── The two graph-keyed lookups, each collapsed to one statement ─────────────
#
# On the Neo4j backend each of these is four round trips: does the vertex exist, which
# releases hang off it, one page of `insights.release_rarity`, and the count. Nothing could
# join the first two to the last two, because they were in different databases. Here they are
# in the same one, so each is a single statement.
#
# The shape is what keeps the three answers distinguishable when the page comes back empty.
# `summary` is one row and always produced; the page is a LEFT JOIN LATERAL onto it, so the
# statement returns at least one row whatever the page holds. `vertex_exists` false is the
# caller's `None` (404); true with `total` 0 is an existing vertex with no scored release;
# true with rows is a page. A plain `SELECT ... LIMIT` could not tell the first two apart.
#
# `release_id ~ '^[0-9]+$'` is the Cypher path's `str(...).isdigit()` filter, and it is needed
# for the same reason: `graph` keys releases on text and `insights.release_rarity` keys them on
# `bigint`, so a non-numeric graph id has no row to join to and must not reach the cast.

_RARITY_BY_VERTEX_SQL = """
WITH anchor AS (
    SELECT {key} AS vertex_id
    FROM GRAPH_TABLE (graph.catalog
        MATCH (v IS {label} WHERE v.{key} = %(vertex_id)s)
        COLUMNS (v.{key} AS {key})
    ) AS found
),
credited AS (
    SELECT DISTINCT release_id
    FROM GRAPH_TABLE (graph.catalog
        MATCH (v IS {label} WHERE v.{key} = %(vertex_id)s)<-[IS {edge}]-(r IS release)
        COLUMNS (r.release_id AS release_id)
    ) AS walk
    WHERE release_id ~ '^[0-9]+$'
),
matched AS (
    SELECT rarity.release_id, rarity.title, rarity.artist_name, rarity.year,
           rarity.rarity_score, rarity.tier, rarity.hidden_gem_score
    FROM insights.release_rarity AS rarity
    JOIN credited ON credited.release_id::bigint = rarity.release_id
)
SELECT summary.vertex_exists,
       summary.total,
       page.release_id, page.title, page.artist_name, page.year,
       page.rarity_score, page.tier, page.hidden_gem_score
FROM (
    SELECT EXISTS (SELECT 1 FROM anchor) AS vertex_exists,
           (SELECT count(*)::bigint FROM matched) AS total
) AS summary
LEFT JOIN LATERAL (
    SELECT * FROM matched
    ORDER BY rarity_score DESC, release_id
    LIMIT %(limit)s OFFSET %(offset)s
) AS page ON TRUE
"""

# The anchor is an artist and the edge into it is `by_artist` — the Cypher's
# `(a:Artist {id: $artist_id})<-[:BY]-(r:Release)`.
RARITY_BY_ARTIST_SQL = _RARITY_BY_VERTEX_SQL.format(label="artist", key="artist_id", edge="by_artist")

# The anchor is a label and the edge into it is `on_label` — the Cypher's
# `(l:Label {id: $label_id})<-[:ON]-(r:Release)`.
RARITY_BY_LABEL_SQL = _RARITY_BY_VERTEX_SQL.format(label="label", key="label_id", edge="on_label")

# The columns `RARITY_BY_ARTIST_SQL` and `RARITY_BY_LABEL_SQL` project, in order: the two
# summary columns, then the stored row the Cypher path's `dict_row` cursor returns.
_LOOKUP_SUMMARY_COLUMNS = 2
_LOOKUP_ROW_COLUMNS: tuple[str, ...] = (
    "release_id",
    "title",
    "artist_name",
    "year",
    "rarity_score",
    "tier",
    "hidden_gem_score",
)


@asynccontextmanager
async def _page_cursor(pool: Any) -> AsyncIterator[Any]:
    """Yield a cursor whose statements carry the chunking contract's server-side budget.

    The Cypher side passes `timeout=RARITY_QUERY_TIMEOUT_SECONDS` on every signal query so a
    pathological page fails fast rather than burning the 600s transaction budget. This is the
    same budget, applied the way PostgreSQL takes it. The pool hands out autocommit
    connections, so `SET LOCAL` would be a no-op and the setting has to be reset explicitly
    before the connection goes back — a leaked `statement_timeout` would apply to every
    unrelated query the next borrower runs.
    """
    timeout_ms = int(RARITY_QUERY_TIMEOUT_SECONDS * 1000)
    async with pool.connection() as conn, conn.cursor() as cursor_cm:
        cursor = cast("Any", cursor_cm)
        await cursor.execute("SET statement_timeout = %s", (timeout_ms,))
        try:
            yield cursor
        finally:
            await cursor.execute("RESET statement_timeout")


# ── The four graph reads the rarity family's PostgreSQL backend contributes ──


async def fetch_release_id_page(pool: Any, cursor: str, limit: int) -> list[str]:
    """Return the next page of release ids strictly after ``cursor``, in ascending order.

    Mirrors :func:`api.queries.rarity_queries.fetch_release_id_page` column for column.
    """
    async with _page_cursor(pool) as db:
        await execute_sql(db, RELEASE_ID_PAGE_SQL, {"cursor": cursor, "limit": limit})
        rows = await db.fetchall()
    return [row[0] for row in rows]


async def fetch_page_signals(pool: Any, ids: list[str]) -> dict[str, list[dict[str, Any]]]:
    """Run the core and family-extension signal statements for one page of release ids.

    Run sequentially, on one connection, for the same reason the Cypher side does: this is a
    daily background computation whose whole point is a bounded working set, and nine
    concurrent page-wide traversals is the opposite of that. Every installed family module's
    statements run for every page regardless of which releases on the page its family covers —
    applicability is decided per release at scoring time, and a per-release statement would
    defeat the chunking contract.
    """
    signals: dict[str, list[dict[str, Any]]] = {}
    async with _page_cursor(pool) as db:
        for fact, sql in (*_CORE_SQL.items(), *family_sql_queries().items()):
            await execute_sql(db, sql, {"ids": ids})
            rows = await db.fetchall()
            columns = _FACT_COLUMNS[fact]
            signals[fact] = rows_by_release_id([dict(zip(columns, row, strict=True)) for row in rows])
    return signals


async def count_releases(pool: Any) -> int | None:
    """Return how many releases the catalog holds, for the walk's coverage check."""
    async with _page_cursor(pool) as db:
        await execute_sql(db, RELEASE_COUNT_SQL)
        row = await db.fetchone()
    return int(row[0]) if row else None


async def fetch_all_rarity_signals(
    handles: RarityHandles,
    *,
    page_size: int = RARITY_PAGE_SIZE,
) -> list[dict[str, Any]]:
    """Fetch all rarity signals from PostgreSQL and compute scores.

    The walk, the join, and the scoring are
    :func:`api.queries.rarity_pipeline.score_all_rarity_signals`; this binds it to the three
    statements above. Mirrors :func:`api.queries.rarity_queries.fetch_all_rarity_signals` row
    for row.
    """
    return await score_all_rarity_signals(
        handles,
        page=fetch_release_id_page,
        signals=fetch_page_signals,
        count=count_releases,
        page_size=page_size,
    )


# ── The two graph-keyed lookups ─────────────────────────────────────────────


async def _rarity_by_vertex(
    handles: RarityHandles,
    sql: str,
    vertex_id: str,
    page: int,
    page_size: int,
) -> tuple[list[dict[str, Any]], int] | None:
    """Run one collapsed lookup and unpack its summary row plus its page."""
    offset = (page - 1) * page_size

    async with handles.graph.connection() as conn, conn.cursor() as cursor_cm:
        cursor = cast("Any", cursor_cm)
        await execute_sql(cursor, sql, {"vertex_id": vertex_id, "limit": page_size, "offset": offset})
        rows = await cursor.fetchall()

    if not rows or not rows[0][0]:
        return None

    total = int(rows[0][1])
    items = [dict(zip(_LOOKUP_ROW_COLUMNS, row[_LOOKUP_SUMMARY_COLUMNS:], strict=True)) for row in rows if row[_LOOKUP_SUMMARY_COLUMNS] is not None]
    return items, total


async def get_rarity_by_artist(
    handles: RarityHandles,
    artist_id: str,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[dict[str, Any]], int] | None:
    """Get rarest releases by a specific artist.

    One statement: the artist's existence, the releases credited to it, the stored rarity rows
    for those releases, and the total, joined in the database the scores already live in.
    Returns None if the artist is not found.
    """
    result = await _rarity_by_vertex(handles, RARITY_BY_ARTIST_SQL, artist_id, page, page_size)
    if result is not None:
        logger.debug("🔍 Artist rarity resolved", artist_id=artist_id, total=result[1])
    return result


async def get_rarity_by_label(
    handles: RarityHandles,
    label_id: str,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[dict[str, Any]], int] | None:
    """Get rarest releases on a specific label.

    One statement, exactly as :func:`get_rarity_by_artist` is. Returns None if the label is
    not found.
    """
    result = await _rarity_by_vertex(handles, RARITY_BY_LABEL_SQL, label_id, page, page_size)
    if result is not None:
        logger.debug("🔍 Label rarity resolved", label_id=label_id, total=result[1])
    return result
