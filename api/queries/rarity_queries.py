"""Rarity scoring queries and PostgreSQL lookups — the Neo4j backend.

Fetches the graph and community facts the rarity index needs, scores them through
:mod:`api.rarity`, and provides lookup functions for the precomputed results.

The scoring itself lives in :mod:`api.rarity`: a media-neutral core plus per-family extension
modules, per ADR 0007. The walk that drives it — paging, the join, the percentile pass, the
coverage check — lives in :mod:`api.queries.rarity_pipeline`, because none of it is Cypher and
the PostgreSQL backend (:mod:`api.queries.rarity_pg_queries`) runs the same walk. This module
owns the Cypher and nothing else. The pure scoring functions and the tier table are
re-exported here, so the historical import path ``api.queries.rarity_queries`` keeps working.

Graph model:
  (Release)-[:BY]->(Artist)
  (Release)-[:ON]->(Label)
  (Release)-[:IS]->(Genre)
  (Release)-[:IS]->(Style)
  (Release)-[:DERIVED_FROM]->(Master)
  (Release)-[:ISSUED_ON {qty, source}]->(Medium {id, family})
"""

from typing import Any

import structlog
from psycopg.rows import dict_row

from api.queries.helpers import run_query
from api.queries.rarity_pipeline import (
    RARITY_PAGE_SIZE,
    RARITY_QUERY_TIMEOUT_SECONDS,
    RarityHandles,
    rows_by_release_id,
    score_all_rarity_signals,
)
from api.queries.rarity_pipeline import (
    percentile_rank as _percentile_rank,
)
from api.rarity import (
    CORE_SIGNAL_WEIGHTS,
    FORMAT_RARITY_SCORES,
    MEDIUM_RARITY_SCORES,
    RARITY_TIERS,
    ReleaseContext,
    compute_collection_prevalence_score,
    compute_format_rarity_score,
    compute_graph_isolation_score,
    compute_label_catalog_score,
    compute_medium_rarity_score,
    compute_pressing_scarcity_score,
    compute_rarity_tier,
    compute_temporal_scarcity_score,
    family_queries,
    medium_rarity_score,
    resolve_media,
    score_release,
)
from api.rarity.families import grooved as _grooved


logger = structlog.get_logger(__name__)

# The grooved extension's sibling-count query, under its historical private name. Kept
# importable here because the chunking-contract regression tests pin it by this path.
_PRESSING_QUERY = _grooved.PRESSING_QUERY

# Re-exported for the historical import path; the definitions live in api.rarity and
# api.queries.rarity_pipeline.
__all__ = [
    "CORE_SIGNAL_WEIGHTS",
    "FORMAT_RARITY_SCORES",
    "MEDIUM_RARITY_SCORES",
    "RARITY_PAGE_SIZE",
    "RARITY_QUERY_TIMEOUT_SECONDS",
    "RARITY_TIERS",
    "RarityHandles",
    "compute_collection_prevalence_score",
    "compute_format_rarity_score",
    "compute_graph_isolation_score",
    "compute_label_catalog_score",
    "compute_medium_rarity_score",
    "compute_pressing_scarcity_score",
    "compute_rarity_tier",
    "compute_temporal_scarcity_score",
    "count_releases",
    "fetch_all_rarity_signals",
    "fetch_page_signals",
    "fetch_release_id_page",
    "get_rarity_by_artist",
    "get_rarity_by_label",
    "get_rarity_for_release",
    "get_rarity_hidden_gems",
    "get_rarity_leaderboard",
    "medium_rarity_score",
]

# Referenced so the re-exports above are not flagged as unused by the linter; every name is
# part of this module's historical public surface.
_REEXPORTED = (
    ReleaseContext,
    compute_collection_prevalence_score,
    compute_format_rarity_score,
    compute_graph_isolation_score,
    compute_label_catalog_score,
    compute_medium_rarity_score,
    compute_temporal_scarcity_score,
    family_queries,
    resolve_media,
    score_release,
    _percentile_rank,
)


# ── Neo4j batch signal queries ──────────────────────────────────────
#
# CHUNKING CONTRACT — read before editing any query in this section, any query a family
# extension module contributes, or the PostgreSQL spelling of either in
# `api/queries/rarity_pg_queries.py`.
#
# These signal queries used to run as eight UNBOUNDED full-graph scans
# (`MATCH (r:Release) ...`). On the production graph that never completed:
# Neo4j killed the transaction at exactly db.transaction.timeout (600s) with
# Neo.ClientError.Transaction.TransactionTimedOutClientConfiguration, so
# release_rarity failed on 33 consecutive daily cycles (2026-06-22 → 2026-07-23)
# and the rarity data was permanently stale.
#
# Every signal query is now keyed off an explicit `$ids` page:
#
#   UNWIND $ids AS rid
#   MATCH (r:Release {id: rid})
#
# `UNWIND` + a property-map match forces an index seek per id against the
# `release_id` uniqueness constraint, so a page's working set is proportional to
# RARITY_PAGE_SIZE rather than to the whole graph. Each query also carries an
# explicit server-side timeout well under db.transaction.timeout, so a pathological
# page fails fast and loudly instead of silently burning the 600s budget.
#
# The PostgreSQL backend spells the same contract `WHERE release_id = ANY(%(ids)s)` over the
# same page, with the same timeout applied as a `statement_timeout`. Neither page may grow a
# scan of the whole release set.
#
# Pages are produced by keyset pagination over `r.id` (a string — the extractor
# parses ids as text), which is index-backed and, unlike SKIP/LIMIT, does not
# degrade as the offset grows.
#
# DO NOT reintroduce a bare `MATCH (r:Release)` here.

# Keyset pagination over the (uniqueness-constrained, therefore indexed) r.id.
# Ids are strings, so "" is a valid open-ended start cursor.
_RELEASE_ID_PAGE_QUERY = """
MATCH (r:Release)
WHERE r.id > $cursor
RETURN r.id AS release_id
ORDER BY r.id
LIMIT $limit
"""

# Label-count store lookup — O(1) in Neo4j, not a scan.
_RELEASE_COUNT_QUERY = """
MATCH (r:Release)
RETURN count(r) AS total
"""

# 1. The release itself: the display fields, and the row set the scoring loop walks.
#
# This is deliberately media-neutral and family-neutral. It used to be folded into the
# grooved pressing-scarcity query, which made the core's result set depend on a
# grooved-media concept; ADR 0007 requires the core to enumerate releases on its own.
#
# `artist_name` is `min(a.name)` rather than the historical `collect(DISTINCT a.name)[0]`.
# The `[0]` took an arbitrary element of an unordered collect, so a release with more than one
# credited artist had no defined display name — the value depended on the order Neo4j's expand
# happened to return the BY edges in, and no PostgreSQL spelling can reproduce that. `min`
# is the same "one of the credited names" with a rule attached, ignores nulls exactly as the
# comprehension's `[0]` would have skipped a missing name, and is what lets the two backends
# be compared at all. Releases with a single credit — the overwhelming majority — are
# unaffected.
_RELEASE_QUERY = """
UNWIND $ids AS rid
MATCH (r:Release {id: rid})
OPTIONAL MATCH (r)-[:BY]->(a:Artist)
WITH r, min(a.name) AS artist_name
RETURN r.id AS release_id, r.title AS title, artist_name, r.year AS year
"""

# 2. Media per release, best evidence first.
#
# ISSUED_ON edges are authoritative (canonical medium id and family on the Medium node).
# `media_families` is the cheap list property on the release node. `formats` is the
# deprecated raw Discogs name list, kept because `format_rarity` is still reported and
# because it is the only media evidence a release has until the enrichers backfill.
# The list comprehension drops the null row an OPTIONAL MATCH leaves when a release has
# no ISSUED_ON edge yet.
_MEDIA_QUERY = """
UNWIND $ids AS rid
MATCH (r:Release {id: rid})
OPTIONAL MATCH (r)-[:ISSUED_ON]->(m:Medium)
WITH r, [x IN collect(DISTINCT {id: m.id, family: m.family}) WHERE x.id IS NOT NULL] AS mediums
RETURN r.id AS release_id, mediums,
       r.media_families AS media_families,
       r.formats AS formats
"""

# 3. Label catalog size per release
_LABEL_QUERY = """
UNWIND $ids AS rid
MATCH (r:Release {id: rid})-[:ON]->(l:Label)
WITH r.id AS release_id, min(COALESCE(l.release_count, 0)) AS label_catalog_size
RETURN release_id, label_catalog_size
"""

# 4. Temporal: release year + latest sibling year
_TEMPORAL_QUERY = """
UNWIND $ids AS rid
MATCH (r:Release {id: rid})
OPTIONAL MATCH (r)-[:DERIVED_FROM]->(m:Master)<-[:DERIVED_FROM]-(sibling:Release)
WHERE sibling.year IS NOT NULL AND sibling <> r
WITH r.id AS release_id, r.year AS year,
     max(sibling.year) AS latest_sibling_year
RETURN release_id, year, latest_sibling_year
"""

# 5. Graph degree per release.
# Use COUNT {} rather than size([(r)-[]-() | 1]): the list comprehension
# materialises a list element per relationship for every Release, which over
# the full graph exhausts the Neo4j transaction memory pool. COUNT {} counts
# without building the intermediate list.
_DEGREE_QUERY = """
UNWIND $ids AS rid
MATCH (r:Release {id: rid})
WITH r, COUNT { (r)--() } AS degree
RETURN r.id AS release_id, degree
"""

# Quality signals for hidden gem scoring
# 6. Max artist degree per release
_ARTIST_DEGREE_QUERY = """
UNWIND $ids AS rid
MATCH (r:Release {id: rid})-[:BY]->(a:Artist)
WITH r.id AS release_id, max(COUNT { (a)--() }) AS artist_max_degree
RETURN release_id, artist_max_degree
"""

# 7. Max label catalog size per release
_LABEL_SIZE_QUERY = """
UNWIND $ids AS rid
MATCH (r:Release {id: rid})-[:ON]->(l:Label)
WITH r.id AS release_id, max(COALESCE(l.release_count, 0)) AS label_max_catalog
RETURN release_id, label_max_catalog
"""

# 8. Max genre release count per release
_GENRE_COUNT_QUERY = """
UNWIND $ids AS rid
MATCH (r:Release {id: rid})-[:IS]->(g:Genre)
WITH r.id AS release_id, max(COALESCE(g.release_count, 0)) AS genre_max_release_count
RETURN release_id, genre_max_release_count
"""

# The media-neutral queries, keyed by the fact name their rows are indexed under. Family
# extension modules contribute further queries through the registry; see
# api/rarity/families/registry.py. Fact names are unique across the core and every module.
_CORE_QUERIES: dict[str, str] = {
    "release": _RELEASE_QUERY,
    "media": _MEDIA_QUERY,
    "label": _LABEL_QUERY,
    "temporal": _TEMPORAL_QUERY,
    "degree": _DEGREE_QUERY,
    "artist_degree": _ARTIST_DEGREE_QUERY,
    "label_size": _LABEL_SIZE_QUERY,
    "genre_count": _GENRE_COUNT_QUERY,
}


# ── The four graph reads the rarity family's Neo4j backend contributes ───────


async def fetch_release_id_page(driver: Any, cursor: str, limit: int) -> list[str]:
    """Return the next page of release ids strictly after ``cursor``, in ascending order."""
    rows = await run_query(
        driver,
        _RELEASE_ID_PAGE_QUERY,
        database="neo4j",
        timeout=RARITY_QUERY_TIMEOUT_SECONDS,
        cursor=cursor,
        limit=limit,
    )
    return [row["release_id"] for row in rows]


async def fetch_page_signals(driver: Any, ids: list[str]) -> dict[str, list[dict[str, Any]]]:
    """Run the core and family-extension signal queries for one page of release ids.

    Run sequentially, not via asyncio.gather: running them concurrently sums
    their working sets against the single dbms.memory.transaction.total.max
    pool, which tips it into a TransientError MemoryPoolOutOfMemoryError.
    Sequential execution caps peak transaction memory at one query at a time.
    This is a daily background computation, so the wall-clock cost is acceptable.

    Every installed family module's queries run for every page, regardless of which
    releases on the page its family covers: applicability is decided per release at scoring
    time, and a per-release query would defeat the chunking contract above.
    """

    async def _run(cypher: str) -> list[dict[str, Any]]:
        return await run_query(
            driver,
            cypher,
            database="neo4j",
            timeout=RARITY_QUERY_TIMEOUT_SECONDS,
            ids=ids,
        )

    signals: dict[str, list[dict[str, Any]]] = {}
    for fact, cypher in (*_CORE_QUERIES.items(), *family_queries().items()):
        signals[fact] = rows_by_release_id(await _run(cypher))
    return signals


async def count_releases(driver: Any) -> int | None:
    """Return how many releases the graph holds, for the walk's coverage check.

    Served from Neo4j's label count store (O(1), not a scan), so the check costs nothing.
    """
    rows = await run_query(
        driver,
        _RELEASE_COUNT_QUERY,
        database="neo4j",
        timeout=RARITY_QUERY_TIMEOUT_SECONDS,
    )
    if not rows:
        return None
    total = rows[0].get("total")
    return total if isinstance(total, int) else None


async def fetch_all_rarity_signals(
    handles: RarityHandles,
    *,
    page_size: int = RARITY_PAGE_SIZE,
) -> list[dict[str, Any]]:
    """Fetch all rarity signals from Neo4j and compute scores.

    The walk, the join, and the scoring are :func:`api.queries.rarity_pipeline.score_all_rarity_signals`;
    this binds it to the three Cypher reads above. See that function for what comes back.
    """
    return await score_all_rarity_signals(
        handles,
        page=fetch_release_id_page,
        signals=fetch_page_signals,
        count=count_releases,
        page_size=page_size,
    )


# ── PostgreSQL lookup functions ─────────────────────────────────────
#
# These three read `insights.release_rarity` directly and never touch a graph, so they are
# not part of what ADR 0012 migrates and are not in the rarity family's `Protocol`: there is
# one implementation and both backends call it.


async def get_rarity_for_release(pool: Any, release_id: int) -> dict[str, Any] | None:
    """Get precomputed rarity breakdown for a single release."""
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT release_id, title, artist_name, year, rarity_score, tier,
                   hidden_gem_score, pressing_scarcity, label_catalog,
                   format_rarity, temporal_scarcity, graph_isolation,
                   collection_prevalence, medium_rarity, media_families,
                   family_signals
            FROM insights.release_rarity
            WHERE release_id = %s
            """,
            (release_id,),
        )
        row: dict[str, Any] | None = await cur.fetchone()
        return row


async def get_rarity_leaderboard(
    pool: Any,
    page: int = 1,
    page_size: int = 20,
    tier: str | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Get paginated global rarity leaderboard."""
    offset = (page - 1) * page_size

    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        if tier:
            await cur.execute(
                """
                SELECT release_id, title, artist_name, year, rarity_score, tier, hidden_gem_score
                FROM insights.release_rarity
                WHERE tier = %s
                ORDER BY rarity_score DESC, release_id
                LIMIT %s OFFSET %s
                """,
                (tier, page_size, offset),
            )
            items = await cur.fetchall()

            await cur.execute(
                "SELECT count(*) AS total FROM insights.release_rarity WHERE tier = %s",
                (tier,),
            )
        else:
            await cur.execute(
                """
                SELECT release_id, title, artist_name, year, rarity_score, tier, hidden_gem_score
                FROM insights.release_rarity
                ORDER BY rarity_score DESC, release_id
                LIMIT %s OFFSET %s
                """,
                (page_size, offset),
            )
            items = await cur.fetchall()

            await cur.execute(
                "SELECT count(*) AS total FROM insights.release_rarity",
            )

        count_row = await cur.fetchone()
        total = count_row["total"] if count_row else 0

    return items, total


async def get_rarity_hidden_gems(
    pool: Any,
    page: int = 1,
    page_size: int = 20,
    min_rarity: float = 41.0,
) -> tuple[list[dict[str, Any]], int]:
    """Get paginated hidden gems sorted by hidden_gem_score."""
    offset = (page - 1) * page_size

    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT release_id, title, artist_name, year, rarity_score, tier, hidden_gem_score
            FROM insights.release_rarity
            WHERE rarity_score >= %s AND hidden_gem_score IS NOT NULL
            ORDER BY hidden_gem_score DESC, release_id
            LIMIT %s OFFSET %s
            """,
            (min_rarity, page_size, offset),
        )
        items = await cur.fetchall()

        await cur.execute(
            "SELECT count(*) AS total FROM insights.release_rarity WHERE rarity_score >= %s AND hidden_gem_score IS NOT NULL",
            (min_rarity,),
        )
        count_row = await cur.fetchone()
        total = count_row["total"] if count_row else 0

    return items, total


# ── The two graph-keyed lookups ─────────────────────────────────────
#
# Two round trips each on this backend, because the ids live in Neo4j and the scores live in
# PostgreSQL and nothing can join across the two. The PostgreSQL backend collapses each of
# these to one statement; see `api/queries/rarity_pg_queries.py`.

_ARTIST_IDENTITY_QUERY = "MATCH (a:Artist {id: $artist_id}) RETURN a.id AS id, a.name AS name LIMIT 1"
_ARTIST_RELEASES_QUERY = "MATCH (a:Artist {id: $artist_id})<-[:BY]-(r:Release) RETURN r.id AS release_id"
_LABEL_IDENTITY_QUERY = "MATCH (l:Label {id: $label_id}) RETURN l.id AS id, l.name AS name LIMIT 1"
_LABEL_RELEASES_QUERY = "MATCH (l:Label {id: $label_id})<-[:ON]-(r:Release) RETURN r.id AS release_id"

_RARITY_PAGE_SQL = """
            SELECT release_id, title, artist_name, year, rarity_score, tier, hidden_gem_score
            FROM insights.release_rarity
            WHERE release_id = ANY(%s)
            ORDER BY rarity_score DESC, release_id
            LIMIT %s OFFSET %s
            """

_RARITY_TOTAL_SQL = "SELECT count(*) AS total FROM insights.release_rarity WHERE release_id = ANY(%s)"


async def _rarity_page(pool: Any, release_ids: list[int], page: int, page_size: int) -> tuple[list[dict[str, Any]], int]:
    """Return one page of stored rarity rows for *release_ids*, and how many there are."""
    offset = (page - 1) * page_size

    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(_RARITY_PAGE_SQL, (release_ids, page_size, offset))
        items = await cur.fetchall()

        await cur.execute(_RARITY_TOTAL_SQL, (release_ids,))
        count_row = await cur.fetchone()
        total = count_row["total"] if count_row else 0

    return items, total


def _numeric_release_ids(rows: list[dict[str, Any]]) -> list[int]:
    """Return the stored-table ids for *rows*, dropping any release id that is not numeric."""
    return [int(r["release_id"]) for r in rows if str(r["release_id"]).isdigit()]


async def get_rarity_by_artist(
    handles: RarityHandles,
    artist_id: str,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[dict[str, Any]], int] | None:
    """Get rarest releases by a specific artist.

    First queries Neo4j for release_ids, then fetches from PostgreSQL.
    Returns None if artist not found.
    """
    artist_rows = await run_query(
        handles.graph,
        _ARTIST_IDENTITY_QUERY,
        database="neo4j",
        artist_id=artist_id,
    )
    if not artist_rows:
        return None

    release_rows = await run_query(
        handles.graph,
        _ARTIST_RELEASES_QUERY,
        database="neo4j",
        artist_id=artist_id,
    )
    if not release_rows:
        return [], 0

    release_ids = _numeric_release_ids(release_rows)
    if not release_ids:
        return [], 0

    return await _rarity_page(handles.insights, release_ids, page, page_size)


async def get_rarity_by_label(
    handles: RarityHandles,
    label_id: str,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[dict[str, Any]], int] | None:
    """Get rarest releases on a specific label.

    First queries Neo4j for release_ids, then fetches from PostgreSQL.
    Returns None if label not found.
    """
    label_rows = await run_query(
        handles.graph,
        _LABEL_IDENTITY_QUERY,
        database="neo4j",
        label_id=label_id,
    )
    if not label_rows:
        return None

    release_rows = await run_query(
        handles.graph,
        _LABEL_RELEASES_QUERY,
        database="neo4j",
        label_id=label_id,
    )
    if not release_rows:
        return [], 0

    release_ids = _numeric_release_ids(release_rows)
    if not release_ids:
        return [], 0

    return await _rarity_page(handles.insights, release_ids, page, page_size)
