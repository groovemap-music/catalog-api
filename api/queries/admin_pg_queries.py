"""SQL queries for the admin storage panel's graph-shape summary — the PostgreSQL backend.

`admin_queries.get_neo4j_storage` (the function this replaces on the PostgreSQL backend)
makes two Neo4j-only calls: ``CALL apoc.meta.stats()`` for label and relationship-type
counts, and ``CALL dbms.queryJmx(...)`` for on-disk store sizes, both best-effort. Neither
has a PostgreSQL equivalent as written — there is no `apoc`, no JMX, and a view (which is
what every `graph` schema relation is at this phase) has no size of its own — but the counts
themselves do: every count `apoc.meta.stats()` reports is answerable from the same phase 0
`graph` views the rest of coverage spike family 1 ("vertex lookups and store statistics")
reads, because counting rows in a view is ordinary SQL rather than a `GRAPH_TABLE` traversal.
This is why the family needs no new relations and runs on every integration tier rather than
only PostgreSQL 19.

Two differences from the Neo4j side are deliberate rather than gaps:

- **Store sizes come from the base tables, not the views.** A view has no size of its own,
  so ``store_sizes`` reports `pg_total_relation_size` for the four Discogs entity tables the
  vertex views project — the closest PostgreSQL equivalent to Neo4j's on-disk store sizes.
- **The relationship count only covers the edges phase 0 already exposes as views.** Neo4j's
  `relTypesCount` also includes the MusicBrainz relationship types (behind
  `MB_RELATIONSHIP_MAP`, coverage spike family 11) and the write-side `COLLECTED`/`WANTS`
  edges' provenance is already captured by ``collected``/``wants``; both are later families'
  scope, so this panel counts the Discogs-derived edges only. That is a strictly smaller
  total than `apoc.meta.stats()` would report today, not a wrong one — nothing here asserts
  parity with the Neo4j panel, because JMX store sizes and an admin snapshot's relationship
  vocabulary are not something a fake pool or a shared fixture can meaningfully compare row
  for row (see ``tests/test_admin_pg_queries.py`` for what *is* checked: the SQL shape and
  the response shape).
"""

from __future__ import annotations

from typing import Any, cast

from common.query_debug import execute_sql


# One row per label, in the same alphabetical order `get_neo4j_storage` sorts
# `record["labels"].items()` into (Neo4j label strings, not the lowercase view names).
NODE_COUNTS_SQL = """
SELECT 'Artist' AS label, count(*) AS count FROM graph.artist
UNION ALL
SELECT 'Genre', count(*) FROM graph.genre
UNION ALL
SELECT 'Label', count(*) FROM graph.label
UNION ALL
SELECT 'Master', count(*) FROM graph.master
UNION ALL
SELECT 'Release', count(*) FROM graph.release
UNION ALL
SELECT 'Style', count(*) FROM graph.style
ORDER BY label
"""

# One row per Discogs-derived edge view, bucketed under the Neo4j relationship type the
# coverage spike's edge-mapping table (Table 2) names for it. `in_genre` and `in_style` both
# fold into `IS`, exactly as Neo4j does: `graphinator` writes both patterns as a single
# `[:IS]` type, so `apoc.meta.stats()` reports one `IS` bucket rather than two.
EDGE_COUNTS_SQL = """
SELECT type, sum(count)::bigint AS count
FROM (
    SELECT 'BY' AS type, count(*) AS count FROM graph.by_artist
    UNION ALL
    SELECT 'ON', count(*) FROM graph.on_label
    UNION ALL
    SELECT 'IS', count(*) FROM graph.in_genre
    UNION ALL
    SELECT 'IS', count(*) FROM graph.in_style
    UNION ALL
    SELECT 'DERIVED_FROM', count(*) FROM graph.derived_from
    UNION ALL
    SELECT 'PART_OF', count(*) FROM graph.part_of
    UNION ALL
    SELECT 'MEMBER_OF', count(*) FROM graph.member_of
    UNION ALL
    SELECT 'ALIAS_OF', count(*) FROM graph.alias_of
    UNION ALL
    SELECT 'SUBLABEL_OF', count(*) FROM graph.sublabel_of
    UNION ALL
    SELECT 'CREDITED_ON', count(*) FROM graph.credited_on
    UNION ALL
    SELECT 'SAME_AS', count(*) FROM graph.same_as
    UNION ALL
    SELECT 'CREDITED_TO', count(*) FROM graph.credited_to
    UNION ALL
    SELECT 'ISSUED_ON', count(*) FROM graph.issued_on
    UNION ALL
    SELECT 'IN_FAMILY', count(*) FROM graph.in_family
    UNION ALL
    SELECT 'COLLECTED', count(*) FROM graph.collected
    UNION ALL
    SELECT 'WANTS', count(*) FROM graph.wants
) AS edges
GROUP BY type
ORDER BY type
"""

# `pg_total_relation_size` on the four Discogs entity tables the vertex views project —
# the store sizes a view-backed graph cannot report about itself. Formatted the same way
# `get_neo4j_storage` formats JMX byte counts, so the panel's units do not change with the
# backend.
STORE_SIZE_SQL = """
SELECT pg_total_relation_size('artists') + pg_total_relation_size('labels')
    + pg_total_relation_size('releases') + pg_total_relation_size('masters') AS total_bytes
"""


def _format_bytes(value: int) -> str:
    """Render a byte count the same way `get_neo4j_storage` renders JMX sizes."""
    if value >= 1_073_741_824:
        return f"{value / 1_073_741_824:.1f} GB"
    if value >= 1_048_576:
        return f"{value / 1_048_576:.0f} MB"
    return f"{value / 1024:.0f} kB"


async def get_neo4j_storage(pool: Any) -> dict[str, Any]:
    """Return the graph's label and edge counts, in the shape the admin panel renders.

    Same top-level shape as :func:`api.queries.admin_queries.get_neo4j_storage` —
    ``status``, ``nodes``, ``relationships``, ``store_sizes`` — so the panel does not need to
    branch on `GRAPH_BACKEND`. See the module docstring for what deliberately differs.
    """
    async with pool.connection() as conn, conn.cursor() as cursor_cm:
        cursor = cast("Any", cursor_cm)
        await execute_sql(cursor, NODE_COUNTS_SQL)
        node_rows = await cursor.fetchall()

        await execute_sql(cursor, EDGE_COUNTS_SQL)
        edge_rows = await cursor.fetchall()

        await execute_sql(cursor, STORE_SIZE_SQL)
        size_row = await cursor.fetchone()

    total_bytes = int(size_row[0]) if size_row and size_row[0] is not None else 0
    # PostgreSQL cannot decompose this the way Neo4j's node-store/relationship-store/
    # string-store split does — the four base tables hold both vertex and edge data
    # together as JSONB documents, so only the combined total means anything here.
    store_sizes = {
        "total": _format_bytes(total_bytes),
        "nodes": None,
        "relationships": None,
        "strings": None,
    }

    return {
        "status": "ok",
        "nodes": [{"label": row[0], "count": row[1]} for row in node_rows],
        "relationships": [{"type": row[0], "count": row[1]} for row in edge_rows],
        "store_sizes": store_sizes,
    }
