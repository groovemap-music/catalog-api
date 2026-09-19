"""SQL/PGQ queries for the one-hop Collaborators endpoint — the PostgreSQL backend.

This is the one-hop sibling of the pilot's ``network_pg_queries`` (ADR 0012): the same
``by_artist`` edge over ``graph.catalog``, walked once instead of chained to two hops, and
grouped by shared-release year the way :mod:`api.queries.collaborator_queries` groups it in
Cypher. See :mod:`api.queries.network_pg_queries` for the four rules this follows (pattern
matching replaces Cypher, not SQL; one constant per query; every value is a parameter;
parity is column-for-column) and `docs/graph-table-migration-template.md` for the worked
example.

This module covers ``get_collaborators`` and ``count_collaborators`` only.
``collaborator_queries.get_artist_identity`` is not part of this family: the coverage spike
groups every plain vertex lookup — including this one — into a separate identity/statistics
family migrated by its own bead, so it is not duplicated or registered here.

Mapping the Cypher onto the graph
----------------------------------
``:Artist`` is ``graph.artist`` is ``(a IS artist)``, ``:Release`` is ``graph.release`` is
``(r IS release)``, and the overloaded ``[:BY]`` out of a release is ``graph.by_artist`` is
``-[IS by_artist]->``, directed release -> artist. ``graph.release.year`` restates the
producer's ``releases.data ->> 'year'`` view column, which is ``text`` — the Cypher's
``r.year`` is an integer property on the projected node, so every comparison and cast here
is explicit, following the ``year ~ '^[0-9]{4}$'`` guard :mod:`api.queries.search_queries`
already uses for the same JSONB column before casting it.

Where a one-hop pattern still needs a walk-semantics guard
------------------------------------------------------------
Neo4j's relationship isomorphism forbids binding the same relationship to the pattern's two
edge variables, which is what already keeps ``(a)<-[:BY]-(:Release)-[:BY]->(other)`` from
reporting the anchor as its own collaborator when a release credits the anchor alongside
itself. SQL/PGQ has no such rule — the same edge may bind to both edge patterns — so the
Cypher's explicit ``WHERE other.id <> $artist_id`` is not merely mirrored here, it is the
only thing standing between a release the anchor shares with nobody and a phantom
self-collaboration. The parity harness's three-credit release (`tests/graph_fixture.py`,
``THREE_CREDIT_RELEASE_ID``) is what makes dropping the guard observable: without it, a walk
from the probe anchor can turn around on that release and report the anchor as its own
one-hop collaborator.
"""

from __future__ import annotations

from typing import Any, cast

import structlog
from common.query_debug import execute_sql


logger = structlog.get_logger(__name__)


# One row per (collaborator, shared release), with the release's year alongside so the
# grouping query below can reproduce the Cypher's per-year breakdown. `peer.artist_id <>
# anchor.artist_id` is the walk-semantics guard described above; the year guard mirrors
# `r.year > 0` by first ruling out anything that is not four digits before the cast, the
# same defensive order `api.queries.search_queries._run_decade_facets` uses on the same
# JSONB-backed column.
_ONE_HOP_COLLABORATORS_HOP = """
    SELECT collaborator_id, collaborator_name, release_id, year
    FROM GRAPH_TABLE (graph.catalog
        MATCH (anchor IS artist WHERE anchor.artist_id = %(artist_id)s)
              <-[IS by_artist]-(credit IS release)-[IS by_artist]->(peer IS artist)
        WHERE peer.artist_id <> anchor.artist_id
          AND credit.year ~ '^[0-9]{4}$'
        COLUMNS (
            peer.artist_id AS collaborator_id,
            peer.name AS collaborator_name,
            credit.release_id AS release_id,
            (credit.year)::int AS year
        )
    ) AS hop
    WHERE year > 0
"""

# Depth-1 collaborators grouped by shared year, then rolled up to the Cypher's shape:
# `release_count` is the total distinct shared releases, `first_year`/`last_year` bound the
# years they were credited together, and `yearly_counts` is the same
# `collect({year: year, count: year_count})` the Cypher builds, ordered ascending by year
# because that is the order the Cypher's `ORDER BY other.id, year` feeds into its own
# `collect`. `json_agg` returns `json`, which psycopg decodes to a Python list of dicts
# without any adapter registration on this project's part — `psycopg.types.json.
# register_default_adapters` wires both the `json` and `jsonb` loaders for every connection.
ONE_HOP_COLLABORATORS_SQL = f"""
WITH hop AS ({_ONE_HOP_COLLABORATORS_HOP.rstrip()}
),
by_year AS (
    SELECT collaborator_id,
           collaborator_name,
           year,
           count(DISTINCT release_id) AS year_count
    FROM hop
    GROUP BY collaborator_id, collaborator_name, year
)
SELECT collaborator_id AS artist_id,
       collaborator_name AS artist_name,
       sum(year_count)::bigint AS release_count,
       min(year) AS first_year,
       max(year) AS last_year,
       json_agg(json_build_object('year', year, 'count', year_count) ORDER BY year) AS yearly_counts
FROM by_year
GROUP BY collaborator_id, collaborator_name
ORDER BY release_count DESC
LIMIT %(limit)s
"""  # noqa: S608 — every interpolated part is a module-level constant above; no caller input reaches it

# The same predicate the depth-1 rows above are filtered by, counted rather than grouped:
# the Cypher's `count(DISTINCT other)` over the identical `WHERE other.id <> $artist_id AND
# r.year > 0` match.
COUNT_ONE_HOP_COLLABORATORS_SQL = f"""
SELECT count(DISTINCT collaborator_id)::bigint AS total
FROM ({_ONE_HOP_COLLABORATORS_HOP.rstrip()}
) AS hop
"""  # noqa: S608 — as above: the composition is of module constants only


async def get_collaborators(pool: Any, artist_id: str, limit: int = 20) -> list[dict[str, Any]]:
    """Return one-hop collaborators with release counts and yearly breakdown.

    Mirrors :func:`api.queries.collaborator_queries.get_collaborators` column for column,
    including its default ``limit``.
    """
    async with pool.connection() as conn, conn.cursor() as cursor_cm:
        cursor = cast("Any", cursor_cm)
        await execute_sql(cursor, ONE_HOP_COLLABORATORS_SQL, {"artist_id": artist_id, "limit": limit})
        rows = await cursor.fetchall()

    collaborators = [
        {
            "artist_id": row[0],
            "artist_name": row[1],
            "release_count": row[2],
            "first_year": row[3],
            "last_year": row[4],
            "yearly_counts": row[5],
        }
        for row in rows
    ]
    logger.debug("🔍 One-hop collaborators resolved", artist_id=artist_id, count=len(collaborators))
    return collaborators


async def count_collaborators(pool: Any, artist_id: str) -> int:
    """Return how many distinct one-hop collaborators the artist has."""
    async with pool.connection() as conn, conn.cursor() as cursor_cm:
        cursor = cast("Any", cursor_cm)
        await execute_sql(cursor, COUNT_ONE_HOP_COLLABORATORS_SQL, {"artist_id": artist_id})
        row = await cursor.fetchone()

    return int(row[0]) if row else 0
