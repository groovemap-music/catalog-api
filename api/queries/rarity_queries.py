"""Rarity scoring queries and PostgreSQL lookups.

Fetches the graph and community facts the rarity index needs, scores them through
:mod:`api.rarity`, and provides lookup functions for the precomputed results.

The scoring itself lives in :mod:`api.rarity`: a media-neutral core plus per-family extension
modules, per ADR 0007. This module owns only the data access and the paging around it. The
pure scoring functions and the tier table are re-exported here, so the historical import path
``api.queries.rarity_queries`` keeps working.

Graph model:
  (Release)-[:BY]->(Artist)
  (Release)-[:ON]->(Label)
  (Release)-[:IS]->(Genre)
  (Release)-[:IS]->(Style)
  (Release)-[:DERIVED_FROM]->(Master)
  (Release)-[:ISSUED_ON {qty, source}]->(Medium {id, family})
"""

import bisect
from datetime import UTC, datetime
from typing import Any

import structlog
from psycopg.rows import dict_row

from api.queries.helpers import run_query
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

# Re-exported for the historical import path; the definitions live in api.rarity.
__all__ = [
    "CORE_SIGNAL_WEIGHTS",
    "FORMAT_RARITY_SCORES",
    "MEDIUM_RARITY_SCORES",
    "RARITY_PAGE_SIZE",
    "RARITY_QUERY_TIMEOUT_SECONDS",
    "RARITY_TIERS",
    "compute_collection_prevalence_score",
    "compute_format_rarity_score",
    "compute_graph_isolation_score",
    "compute_label_catalog_score",
    "compute_medium_rarity_score",
    "compute_pressing_scarcity_score",
    "compute_rarity_tier",
    "compute_temporal_scarcity_score",
    "fetch_all_rarity_signals",
    "get_rarity_by_artist",
    "get_rarity_by_label",
    "get_rarity_for_release",
    "get_rarity_hidden_gems",
    "get_rarity_leaderboard",
    "medium_rarity_score",
]


# ── Neo4j batch signal queries ──────────────────────────────────────
#
# CHUNKING CONTRACT — read before editing any query in this section, or any query a family
# extension module contributes.
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
# Pages are produced by keyset pagination over `r.id` (a string — the extractor
# parses ids as text), which is index-backed and, unlike SKIP/LIMIT, does not
# degrade as the offset grows.
#
# DO NOT reintroduce a bare `MATCH (r:Release)` here.

# Releases per page. Sized so a page's queries stay far inside the 600s
# server-side transaction timeout while keeping the number of round trips sane.
RARITY_PAGE_SIZE = 20_000

# Per-query server-side timeout. Must stay comfortably below Neo4j's
# db.transaction.timeout (600s in production) so a slow page surfaces as a fast,
# attributable failure rather than a 600s stall.
RARITY_QUERY_TIMEOUT_SECONDS = 120.0

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
_RELEASE_QUERY = """
UNWIND $ids AS rid
MATCH (r:Release {id: rid})
OPTIONAL MATCH (r)-[:BY]->(a:Artist)
WITH r, collect(DISTINCT a.name)[0] AS artist_name
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


def _percentile_rank(value: float, sorted_values: list[float]) -> float:
    """Return percentile rank (0.0 to 1.0) of value in sorted list."""
    if not sorted_values or value <= 0:
        return 0.0
    return bisect.bisect_left(sorted_values, value) / len(sorted_values)


async def _fetch_release_id_page(driver: Any, cursor: str, limit: int) -> list[str]:
    """Return the next page of release ids strictly after ``cursor``."""
    rows = await run_query(
        driver,
        _RELEASE_ID_PAGE_QUERY,
        database="neo4j",
        timeout=RARITY_QUERY_TIMEOUT_SECONDS,
        cursor=cursor,
        limit=limit,
    )
    return [row["release_id"] for row in rows]


async def _load_community_counts(pool: Any) -> dict[str, tuple[int, int]]:
    """Load community have/want counts from PostgreSQL (neutral fallback on failure)."""
    if pool is None:
        return {}
    try:
        async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT release_id, have_count, want_count FROM insights.community_counts")
            community_rows = await cur.fetchall()
        community_map = {str(r["release_id"]): (r["have_count"], r["want_count"]) for r in community_rows}
        logger.info("📊 Community counts loaded", count=len(community_map))
    except Exception:
        logger.warning("⚠️ Failed to load community counts, using neutral fallback", exc_info=True)
        return {}
    return community_map


async def _fetch_page_signals(driver: Any, ids: list[str]) -> dict[str, list[dict[str, Any]]]:
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
        signals[fact] = await _run(cypher)
    return signals


def _index_by_release(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Index signal rows by their release_id."""
    return {row["release_id"]: row for row in rows}


async def fetch_all_rarity_signals(
    driver: Any,
    pool: Any = None,
    *,
    page_size: int = RARITY_PAGE_SIZE,
) -> list[dict[str, Any]]:
    """Fetch all rarity signals from Neo4j and compute scores.

    Walks the Release set in keyset-paginated chunks of ``page_size``. For each page it runs
    the core signal queries plus those every installed family extension declares, scoped to
    that page's ids, joins them by release_id, and composes the rarity score through
    :func:`api.rarity.score_release`. Community counts (have/want) are loaded once from
    PostgreSQL when a pool is provided.

    Hidden-gem scoring needs percentile ranks over the *global* quality-signal
    distributions, so those are accumulated while paging and applied in a final
    pass — no second trip to Neo4j.

    Args:
        driver: The async Neo4j driver.
        pool: Optional PostgreSQL pool for community counts.
        page_size: Releases per chunk. Bounds per-transaction working set.

    Returns:
        A list of dicts ready for PostgreSQL insertion. Each carries the historical keys plus
        ``media_families``, ``family_signals``, and ``medium_rarity``. ``pressing_scarcity`` is
        ``None`` for a release no family extension claims — a CD has no pressings to count.
    """
    current_year = datetime.now(UTC).year

    logger.info("🔍 Fetching rarity signals from Neo4j...", page_size=page_size)

    community_map = await _load_community_counts(pool)
    family_facts = tuple(family_queries())

    results: list[dict[str, Any]] = []
    # Deferred hidden-gem inputs, positionally parallel to `results`:
    # (unrounded rarity_score, artist_max_degree, label_max_catalog,
    # genre_max_release_count). The UNROUNDED score is kept deliberately —
    # hidden_gem_score has always been derived from it, not from the rounded
    # value stored in the result dict.
    quality_inputs: list[tuple[float, float, float, float]] = []
    all_artist_degrees: list[float] = []
    all_label_sizes: list[float] = []
    all_genre_counts: list[float] = []

    cursor = ""
    pages = 0
    while True:
        ids = await _fetch_release_id_page(driver, cursor, page_size)
        if not ids:
            break
        cursor = ids[-1]
        pages += 1

        signals = await _fetch_page_signals(driver, ids)

        media_map = _index_by_release(signals["media"])
        label_map = {r["release_id"]: r["label_catalog_size"] for r in signals["label"]}
        temporal_map = _index_by_release(signals["temporal"])
        degree_map = {r["release_id"]: r["degree"] for r in signals["degree"]}
        artist_deg_map = {r["release_id"]: r["artist_max_degree"] for r in signals["artist_degree"]}
        label_size_map = {r["release_id"]: r["label_max_catalog"] for r in signals["label_size"]}
        genre_count_map = {r["release_id"]: r["genre_max_release_count"] for r in signals["genre_count"]}
        fact_maps = {fact: _index_by_release(signals.get(fact, [])) for fact in family_facts}

        all_artist_degrees.extend(r["artist_max_degree"] for r in signals["artist_degree"] if r["artist_max_degree"])
        all_label_sizes.extend(r["label_max_catalog"] for r in signals["label_size"] if r["label_max_catalog"])
        all_genre_counts.extend(r["genre_max_release_count"] for r in signals["genre_count"] if r["genre_max_release_count"])

        for row in signals["release"]:
            rid = row["release_id"]

            media_row = media_map.get(rid, {})
            formats = media_row.get("formats") or []
            media = resolve_media(
                mediums=media_row.get("mediums"),
                media_families=media_row.get("media_families"),
                formats=formats,
            )

            temporal_info = temporal_map.get(rid, {})
            have, want = community_map.get(rid, (None, None))

            core_signals = {
                "label_catalog": compute_label_catalog_score(label_map.get(rid, 0)),
                "medium_rarity": compute_medium_rarity_score(media),
                "temporal_scarcity": compute_temporal_scarcity_score(
                    temporal_info.get("year"),
                    temporal_info.get("latest_sibling_year"),
                    current_year,
                ),
                "graph_isolation": compute_graph_isolation_score(degree_map.get(rid, 0)),
                # Neutral fallback when the community counts are unavailable.
                "collection_prevalence": compute_collection_prevalence_score(have, want or 0) if have is not None else 50.0,
            }

            scored = score_release(
                ReleaseContext(
                    release_id=rid,
                    media=media,
                    year=row.get("year"),
                    facts={fact: fact_maps[fact].get(rid, {}) for fact in family_facts},
                ),
                core_signals,
            )

            quality_inputs.append(
                (
                    scored.score,
                    artist_deg_map.get(rid, 0) or 0,
                    label_size_map.get(rid, 0) or 0,
                    genre_count_map.get(rid, 0) or 0,
                )
            )

            results.append(
                {
                    "release_id": rid,
                    "title": row.get("title") or "",
                    "artist_name": row.get("artist_name") or "",
                    "year": row.get("year"),
                    "rarity_score": round(scored.score, 1),
                    "tier": scored.tier,
                    # Filled in below, once the global distributions are known.
                    "hidden_gem_score": 0.0,
                    # None when no family extension claimed this release.
                    "pressing_scarcity": scored.signals.get("pressing_scarcity"),
                    "label_catalog": core_signals["label_catalog"],
                    # Deprecated, unscored, retained for one minor version.
                    "format_rarity": compute_format_rarity_score(formats),
                    "temporal_scarcity": core_signals["temporal_scarcity"],
                    "graph_isolation": core_signals["graph_isolation"],
                    "collection_prevalence": core_signals["collection_prevalence"],
                    "medium_rarity": core_signals["medium_rarity"],
                    "media_families": list(media.families),
                    "family_signals": scored.family_signals,
                }
            )

        logger.debug("📄 Rarity page scored", page=pages, ids=len(ids), scored=len(results))

    # Percentile normalization for quality signals, over the global distributions.
    all_artist_degrees.sort()
    all_label_sizes.sort()
    all_genre_counts.sort()

    for entry, (rarity_score, artist_deg, label_sz, genre_ct) in zip(results, quality_inputs, strict=True):
        quality_multiplier = (
            0.4 * _percentile_rank(artist_deg, all_artist_degrees)
            + 0.3 * _percentile_rank(label_sz, all_label_sizes)
            + 0.3 * _percentile_rank(genre_ct, all_genre_counts)
        )
        entry["hidden_gem_score"] = round(rarity_score * quality_multiplier, 1)

    # Coverage check. The keyset walk compares `r.id > $cursor` against a string
    # cursor; if the graph ever held non-string Release ids the comparison would
    # yield null and silently truncate the walk. count(r) is served from Neo4j's
    # label count store (O(1), not a scan), so this costs nothing and turns a
    # silent partial result into a loud one.
    await _warn_on_incomplete_coverage(driver, scored=len(results))

    logger.info("✅ Rarity scores computed", total=len(results), pages=pages)
    return results


async def _warn_on_incomplete_coverage(driver: Any, scored: int) -> None:
    """Log a warning when the paginated walk scored fewer releases than exist."""
    try:
        count_rows = await run_query(
            driver,
            _RELEASE_COUNT_QUERY,
            database="neo4j",
            timeout=RARITY_QUERY_TIMEOUT_SECONDS,
        )
    except Exception:
        logger.debug("⚠️ Release count check skipped", exc_info=True)
        return
    if not count_rows:
        return
    total = count_rows[0].get("total")
    if isinstance(total, int) and scored < total:
        logger.warning(
            "⚠️ Rarity pagination covered fewer releases than the graph holds",
            scored=scored,
            total=total,
            missing=total - scored,
        )


# ── PostgreSQL lookup functions ─────────────────────────────────────


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


async def get_rarity_by_artist(
    driver: Any,
    pool: Any,
    artist_id: str,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[dict[str, Any]], int] | None:
    """Get rarest releases by a specific artist.

    First queries Neo4j for release_ids, then fetches from PostgreSQL.
    Returns None if artist not found.
    """
    artist_rows = await run_query(
        driver,
        "MATCH (a:Artist {id: $artist_id}) RETURN a.id AS id, a.name AS name LIMIT 1",
        database="neo4j",
        artist_id=artist_id,
    )
    if not artist_rows:
        return None

    release_rows = await run_query(
        driver,
        "MATCH (a:Artist {id: $artist_id})<-[:BY]-(r:Release) RETURN r.id AS release_id",
        database="neo4j",
        artist_id=artist_id,
    )
    if not release_rows:
        return [], 0

    release_ids = [int(r["release_id"]) for r in release_rows if str(r["release_id"]).isdigit()]
    if not release_ids:
        return [], 0
    offset = (page - 1) * page_size

    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT release_id, title, artist_name, year, rarity_score, tier, hidden_gem_score
            FROM insights.release_rarity
            WHERE release_id = ANY(%s)
            ORDER BY rarity_score DESC, release_id
            LIMIT %s OFFSET %s
            """,
            (release_ids, page_size, offset),
        )
        items = await cur.fetchall()

        await cur.execute(
            "SELECT count(*) AS total FROM insights.release_rarity WHERE release_id = ANY(%s)",
            (release_ids,),
        )
        count_row = await cur.fetchone()
        total = count_row["total"] if count_row else 0

    return items, total


async def get_rarity_by_label(
    driver: Any,
    pool: Any,
    label_id: str,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[dict[str, Any]], int] | None:
    """Get rarest releases on a specific label.

    First queries Neo4j for release_ids, then fetches from PostgreSQL.
    Returns None if label not found.
    """
    label_rows = await run_query(
        driver,
        "MATCH (l:Label {id: $label_id}) RETURN l.id AS id, l.name AS name LIMIT 1",
        database="neo4j",
        label_id=label_id,
    )
    if not label_rows:
        return None

    release_rows = await run_query(
        driver,
        "MATCH (l:Label {id: $label_id})<-[:ON]-(r:Release) RETURN r.id AS release_id",
        database="neo4j",
        label_id=label_id,
    )
    if not release_rows:
        return [], 0

    release_ids = [int(r["release_id"]) for r in release_rows if str(r["release_id"]).isdigit()]
    if not release_ids:
        return [], 0
    offset = (page - 1) * page_size

    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT release_id, title, artist_name, year, rarity_score, tier, hidden_gem_score
            FROM insights.release_rarity
            WHERE release_id = ANY(%s)
            ORDER BY rarity_score DESC, release_id
            LIMIT %s OFFSET %s
            """,
            (release_ids, page_size, offset),
        )
        items = await cur.fetchall()

        await cur.execute(
            "SELECT count(*) AS total FROM insights.release_rarity WHERE release_id = ANY(%s)",
            (release_ids,),
        )
        count_row = await cur.fetchone()
        total = count_row["total"] if count_row else 0

    return items, total
