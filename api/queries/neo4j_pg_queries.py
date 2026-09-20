"""SQL queries for the catalog-overview lookups — the PostgreSQL backend.

`neo4j_queries.get_year_range` and `neo4j_queries.get_graph_stats` (the Cypher these
replace) touch no edge — one is a min/max over `Release.year`, the other six node counts —
so, per the coverage spike's classification, both are ``SQL-only (no graph)`` on the
PostgreSQL side: plain aggregates over the phase 0 vertex views rather than a
``GRAPH_TABLE`` pattern. This is coverage spike family 1 ("vertex lookups and store
statistics"); it needs no new relations and runs on every integration tier rather than only
PostgreSQL 19.
"""

from __future__ import annotations

from typing import Any, cast

from common.query_debug import execute_sql


# `graph.release.year` is `releases.data ->> 'year'`, a text column read straight off the
# Discogs document — unlike Neo4j's `r.year`, which `graphinator` writes as an integer
# property (or omits, for a document with no parseable year), nothing upstream of this view
# guarantees the text is numeric. Casting it unconditionally is what the first version of
# this query did, and it broke on the first non-numeric or whitespace-only value: PostgreSQL
# raises `invalid input syntax for type integer` mid-aggregate, which fails the whole
# catalog-wide query rather than excluding the one bad row the way Cypher's `r.year > 0`
# silently does by comparing a non-numeric value to `null`. The fix is the same guard
# `database-schema`'s `genre_stats`/`style_stats` counters already use for the identical
# cast (`postgres.py`'s `_COUNTER_BOOTSTRAP["genre_stats"]`): filter with
# `btrim(year) ~ '^[0-9]{4}$'` *before* the cast, in the `WHERE` of the subquery the cast
# runs in, so a row that fails the regex is excluded rather than reaching `::int` at all.
# `year_value > 0` then keeps parity with the Cypher's own guard against the `0` sentinel a
# malformed document could still carry. The outer `WHERE matched > 0` is what makes an empty
# result set behave like the Cypher's: `CALL { ... LIMIT 1 }` returns zero rows when nothing
# matches, so `run_single` returns `None` — a plain `min()`/`max()` over zero rows would
# otherwise still return one row of `NULL`s instead of no row at all.
YEAR_RANGE_SQL = """
SELECT min_year, max_year
FROM (
    SELECT min(year_value) AS min_year, max(year_value) AS max_year, count(*) AS matched
    FROM (
        SELECT NULLIF(btrim(year), '')::int AS year_value
        FROM graph.release
        WHERE btrim(year) ~ '^[0-9]{4}$'
    ) AS parsed
    WHERE year_value > 0
) AS bounds
WHERE matched > 0
"""

# One row, six scalar subqueries — the SQL analogue of the Cypher's `UNION ALL` of six
# `count()`s. Column order is the dict key order the Cypher side builds, which the parity
# harness checks along with the values.
GRAPH_STATS_SQL = """
SELECT
    (SELECT count(*) FROM graph.artist) AS artists,
    (SELECT count(*) FROM graph.label) AS labels,
    (SELECT count(*) FROM graph.release) AS releases,
    (SELECT count(*) FROM graph.master) AS masters,
    (SELECT count(*) FROM graph.genre) AS genres,
    (SELECT count(*) FROM graph.style) AS styles
"""


async def get_year_range(pool: Any) -> dict[str, int] | None:
    """Return the min and max release year across the catalog, or ``None`` if none qualify.

    Mirrors :func:`api.queries.neo4j_queries.get_year_range` column for column.
    """
    async with pool.connection() as conn, conn.cursor() as cursor_cm:
        cursor = cast("Any", cursor_cm)
        await execute_sql(cursor, YEAR_RANGE_SQL)
        row = await cursor.fetchone()

    if row is None:
        return None
    return {"min_year": row[0], "max_year": row[1]}


async def get_graph_stats(pool: Any) -> dict[str, int]:
    """Return aggregate vertex counts for each entity type in the graph.

    Mirrors :func:`api.queries.neo4j_queries.get_graph_stats` column for column — same six
    keys, same order, same `int` type. `count(*)` yields `bigint`, which psycopg already
    hands back as a Python `int` (unlike `sum()`/`avg()` over `numeric`, which come back as
    `Decimal`), so no cast is needed here the way the collaborators family needs one.
    """
    async with pool.connection() as conn, conn.cursor() as cursor_cm:
        cursor = cast("Any", cursor_cm)
        await execute_sql(cursor, GRAPH_STATS_SQL)
        row = await cursor.fetchone()

    if row is None:  # pragma: no cover - six scalar subqueries always return one row
        return {"artists": 0, "labels": 0, "releases": 0, "masters": 0, "genres": 0, "styles": 0}
    return {
        "artists": row[0],
        "labels": row[1],
        "releases": row[2],
        "masters": row[3],
        "genres": row[4],
        "styles": row[5],
    }
