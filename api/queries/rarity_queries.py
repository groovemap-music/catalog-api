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

import base64
import binascii
import bisect
import json
import math
from collections import Counter
from collections.abc import AsyncIterator, Iterable
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
    "RARITY_PAGE_BUDGET_SECONDS",
    "RARITY_PAGE_SIZE",
    "RARITY_QUERY_TIMEOUT_SECONDS",
    "RARITY_TIERS",
    "TIER_PAGE_MAX_LIMIT",
    "compute_collection_prevalence_score",
    "compute_format_rarity_score",
    "compute_graph_isolation_score",
    "compute_label_catalog_score",
    "compute_medium_rarity_score",
    "compute_pressing_scarcity_score",
    "compute_rarity_tier",
    "compute_temporal_scarcity_score",
    "decode_tier_cursor",
    "encode_tier_cursor",
    "fetch_all_rarity_signals",
    "fetch_rarity_page",
    "fetch_rarity_quality_counts",
    "get_rarity_by_artist",
    "get_rarity_by_label",
    "get_rarity_for_release",
    "get_rarity_hidden_gems",
    "get_rarity_leaderboard",
    "get_rarity_tier_page",
    "iter_rarity_signals",
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

# Whole-request budget for one page of the paged rarity routes. The API enforces it, so a client's
# read timeout above this always means the connection failed, never that a page is merely slow. A
# real 20,000-release page takes about 4 s of Neo4j time on lux, so this is headroom, not a target.
RARITY_PAGE_BUDGET_SECONDS = 300

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

# 4. Temporal: release year + latest sibling year. Exclude the masterless sentinel
# before sibling expansion, just as the grooved pressing query does.
_TEMPORAL_QUERY = """
UNWIND $ids AS rid
MATCH (r:Release {id: rid})
OPTIONAL MATCH (r)-[:DERIVED_FROM]->(m:Master)
WHERE NOT m.id IN ['0', 0]
OPTIONAL MATCH (m)<-[:DERIVED_FROM]-(sibling:Release)
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


async def _load_page_community_counts(pool: Any, ids: list[str]) -> dict[str, tuple[int, int]]:
    """Load the have/want counts of one page of releases.

    Unlike :func:`_load_community_counts` this has no neutral fallback: a page scored without its
    counts would be stored as if they were known, so a failure here fails the page and the client
    retries it.
    """
    release_ids = [int(release_id) for release_id in ids if release_id.isdecimal()]
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT release_id, have_count, want_count FROM insights.community_counts WHERE release_id = ANY(%s)",
            (release_ids,),
        )
        community_rows = await cur.fetchall()
    return {str(r["release_id"]): (r["have_count"], r["want_count"]) for r in community_rows}


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


class _Distribution:
    """The positive values of one quality signal across the catalog, held as value counts.

    The signals are integer degrees and catalog sizes, so the number of distinct values stays small
    however many releases there are. ``percentile_rank`` returns exactly what :func:`_percentile_rank`
    returns over the full sorted list of the same values: the share of values strictly below ``value``.
    """

    def __init__(self) -> None:
        self._counts: Counter[float] = Counter()
        self._values: list[float] = []
        self._below: list[int] = []
        self._total = 0

    @classmethod
    def from_counts(cls, counts: Iterable[tuple[float, int]]) -> _Distribution:
        """Build a frozen distribution from ``(value, count)`` pairs, as :meth:`counts` gives them."""
        distribution = cls()
        for value, count in counts:
            distribution._counts[value] += count
        distribution.freeze()
        return distribution

    def add(self, value: float | None) -> None:
        """Count ``value`` if it is truthy, as the sorted list always did."""
        if value:
            self._counts[value] += 1

    def counts(self) -> list[tuple[float, int]]:
        """Return the ``(value, count)`` pairs, smallest value first."""
        return sorted(self._counts.items())

    def freeze(self) -> None:
        """Order the distinct values and precompute how many values lie below each."""
        self._values = sorted(self._counts)
        self._below = []
        running = 0
        for value in self._values:
            self._below.append(running)
            running += self._counts[value]
        self._total = running

    def percentile_rank(self, value: float) -> float:
        """Return the percentile rank (0.0 to 1.0) of ``value`` in the frozen distribution."""
        if not self._total or value <= 0:
            return 0.0
        index = bisect.bisect_left(self._values, value)
        below = self._below[index] if index < len(self._values) else self._total
        return below / self._total


async def _release_id_pages(driver: Any, page_size: int) -> AsyncIterator[list[str]]:
    """Walk the Release ids in keyset-paginated pages, in id order."""
    cursor = ""
    while True:
        ids = await _fetch_release_id_page(driver, cursor, page_size)
        if not ids:
            return
        cursor = ids[-1]
        yield ids


# Quality fact to (distribution name, row column).
_QUALITY_SIGNALS = {
    "artist_degree": ("artist", "artist_max_degree"),
    "label_size": ("label", "label_max_catalog"),
    "genre_count": ("genre", "genre_max_release_count"),
}


def _empty_distributions() -> dict[str, _Distribution]:
    """Return one empty distribution per quality signal."""
    return {name: _Distribution() for name, _column in _QUALITY_SIGNALS.values()}


async def _count_quality_signals(driver: Any, ids: list[str], distributions: dict[str, _Distribution]) -> None:
    """Add one page of releases to the quality-signal distributions."""
    for fact, (name, column) in _QUALITY_SIGNALS.items():
        rows = await run_query(driver, _CORE_QUERIES[fact], database="neo4j", timeout=RARITY_QUERY_TIMEOUT_SECONDS, ids=ids)
        for row in rows:
            distributions[name].add(row[column])


async def _quality_distributions(driver: Any, page_size: int) -> dict[str, _Distribution]:
    """First pass: the global quality-signal distributions, from the three quality queries alone.

    Hidden-gem scoring ranks each release against the whole catalog, so no row can be finished before
    every page has been seen. Holding every finished row until then is what grew the API process by
    about 1.7 KB per release. Counting values instead keeps this pass at the size of the number of
    distinct values, and the scoring pass below then finishes one page at a time.
    """
    distributions = _empty_distributions()
    async for ids in _release_id_pages(driver, page_size):
        await _count_quality_signals(driver, ids, distributions)
    for distribution in distributions.values():
        distribution.freeze()
    return distributions


def _score_page(
    signals: dict[str, list[dict[str, Any]]],
    community_map: dict[str, tuple[int, int]],
    distributions: dict[str, _Distribution],
    current_year: int,
) -> list[dict[str, Any]]:
    """Score one page of releases from its signal rows, in the order the release query returned them."""
    family_facts = tuple(family_queries())
    media_map = _index_by_release(signals["media"])
    label_map = {r["release_id"]: r["label_catalog_size"] for r in signals["label"]}
    temporal_map = _index_by_release(signals["temporal"])
    degree_map = {r["release_id"]: r["degree"] for r in signals["degree"]}
    artist_deg_map = {r["release_id"]: r["artist_max_degree"] for r in signals["artist_degree"]}
    label_size_map = {r["release_id"]: r["label_max_catalog"] for r in signals["label_size"]}
    genre_count_map = {r["release_id"]: r["genre_max_release_count"] for r in signals["genre_count"]}
    fact_maps = {fact: _index_by_release(signals.get(fact, [])) for fact in family_facts}

    scored_rows: list[dict[str, Any]] = []
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

        quality_multiplier = (
            0.4 * distributions["artist"].percentile_rank(artist_deg_map.get(rid, 0) or 0)
            + 0.3 * distributions["label"].percentile_rank(label_size_map.get(rid, 0) or 0)
            + 0.3 * distributions["genre"].percentile_rank(genre_count_map.get(rid, 0) or 0)
        )

        scored_rows.append(
            {
                "release_id": rid,
                "title": row.get("title") or "",
                "artist_name": row.get("artist_name") or "",
                "year": row.get("year"),
                "rarity_score": round(scored.score, 1),
                "tier": scored.tier,
                # Derived from the UNROUNDED score, as it always has been.
                "hidden_gem_score": round(scored.score * quality_multiplier, 1),
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
    return scored_rows


async def iter_rarity_signals(
    driver: Any,
    pool: Any = None,
    *,
    page_size: int = RARITY_PAGE_SIZE,
) -> AsyncIterator[dict[str, Any]]:
    """Yield every release's rarity row in release-id order, holding one page of rows at a time.

    Same rows, same order and same values as :func:`fetch_all_rarity_signals` returns, without
    the list: the process holds one page of signals instead of one row per release in the graph.

    Hidden-gem scoring needs percentile ranks over the *global* quality-signal distributions,
    so a first pass reads only the three quality signals to build them (see
    :func:`_quality_distributions`), and a second pass walks the Release set in keyset-paginated chunks
    of ``page_size``. For each page it runs the core signal queries plus those every installed family
    extension declares, scoped to that page's ids, joins them by release_id, and composes the rarity
    score through :func:`api.rarity.score_release`. Community counts (have/want) are loaded once from
    PostgreSQL when a pool is provided.

    Both passes read the live graph, so a release added between them is ranked against distributions
    that did not include it; the effect is one release's weight in a percentile.

    Args:
        driver: The async Neo4j driver.
        pool: Optional PostgreSQL pool for community counts.
        page_size: Releases per chunk. Bounds per-transaction working set.
    """
    current_year = datetime.now(UTC).year

    logger.info("🔍 Fetching rarity signals from Neo4j...", page_size=page_size)

    community_map = await _load_community_counts(pool)
    distributions = await _quality_distributions(driver, page_size)

    scored_count = 0
    pages = 0
    async for ids in _release_id_pages(driver, page_size):
        pages += 1
        signals = await _fetch_page_signals(driver, ids)
        for scored_row in _score_page(signals, community_map, distributions, current_year):
            scored_count += 1
            yield scored_row

        logger.debug("📄 Rarity page scored", page=pages, ids=len(ids), scored=scored_count)

    # Coverage check. The keyset walk compares `r.id > $cursor` against a string
    # cursor; if the graph ever held non-string Release ids the comparison would
    # yield null and silently truncate the walk. count(r) is served from Neo4j's
    # label count store (O(1), not a scan), so this costs nothing and turns a
    # silent partial result into a loud one.
    await _warn_on_incomplete_coverage(driver, scored=scored_count)

    logger.info("✅ Rarity scores computed", total=scored_count, pages=pages)


async def fetch_all_rarity_signals(
    driver: Any,
    pool: Any = None,
    *,
    page_size: int = RARITY_PAGE_SIZE,
) -> list[dict[str, Any]]:
    """Return every rarity row as a list.

    Holds one dict per release in memory, so it is for fixtures and small graphs. A request path
    over the real catalog iterates :func:`iter_rarity_signals` instead.
    """
    return [row async for row in iter_rarity_signals(driver, pool, page_size=page_size)]


def _next_after(ids: list[str], limit: int) -> str | None:
    """Return the cursor of the page after ``ids``, or ``None`` if ``ids`` was the last page."""
    return ids[-1] if len(ids) == limit else None


async def fetch_rarity_quality_counts(driver: Any, *, after: str, limit: int) -> dict[str, Any]:
    """Count the quality signals of the ``limit`` releases after ``after``, the first pass, one page at a time.

    The caller sums these across pages and passes the totals to :func:`fetch_rarity_page`, so the
    catalog-wide distributions are computed once and a failed request costs one page. ``release_total``
    is what the label count store holds now, so a caller can tell a complete walk from a truncated one.
    """
    ids = await _fetch_release_id_page(driver, after, limit)
    distributions = _empty_distributions()
    if ids:
        await _count_quality_signals(driver, ids, distributions)
    count_rows = await run_query(driver, _RELEASE_COUNT_QUERY, database="neo4j", timeout=RARITY_QUERY_TIMEOUT_SECONDS)
    return {
        **{name: distribution.counts() for name, distribution in distributions.items()},
        "releases": len(ids),
        "release_total": count_rows[0]["total"],
        "next_after": _next_after(ids, limit),
    }


async def fetch_rarity_page(
    driver: Any,
    pool: Any,
    *,
    after: str,
    limit: int,
    distributions: dict[str, list[tuple[float, int]]],
) -> dict[str, Any]:
    """Score the ``limit`` releases after ``after`` against catalog-wide quality-signal counts.

    The rows are the ones :func:`iter_rarity_signals` yields for the same releases. Nothing is kept
    between calls, so any process can serve any page, and a retry repeats one page.
    """
    ids = await _fetch_release_id_page(driver, after, limit)
    if not ids:
        return {"items": [], "next_after": None}
    community_map = await _load_page_community_counts(pool, ids)
    signals = await _fetch_page_signals(driver, ids)
    frozen = {name: _Distribution.from_counts(counts) for name, counts in distributions.items()}
    return {
        "items": _score_page(signals, community_map, frozen, datetime.now(UTC).year),
        "next_after": _next_after(ids, limit),
    }


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


# One page of a tier's releases in leaderboard order, by keyset. The order is score descending
# then release id ascending, the same order get_rarity_leaderboard pages with OFFSET, and it
# matches the (tier, rarity_score DESC, release_id) index. The cursor is the last row's score and
# id. rarity_score is REAL, so the score is bound ::real: a float8 cursor compares against a value
# that a text round trip has rounded, and admits none of a tie group whose score it rounded. That
# drops rows, which is worse than a repeat.
_TIER_KEYSET_PAGE = """
SELECT release_id, title, artist_name, year, rarity_score, tier, hidden_gem_score
FROM insights.release_rarity
WHERE tier = %(tier)s
  AND (%(after_score)s::real IS NULL
       OR (rarity_score <= %(after_score)s::real
           AND (rarity_score < %(after_score)s::real OR release_id > %(after_id)s::bigint)))
ORDER BY rarity_score DESC, release_id
LIMIT %(limit)s
"""


# The page size the leaderboard route already allows (Query le=100); the query clamps to it too, so a caller that skips the
# route cannot ask Postgres for an unbounded page.
TIER_PAGE_MAX_LIMIT = 100


_BIGINT_MAX = 2**63 - 1
_REAL_MAX = 3.4028234663852886e38
_REAL_MIN_NORMAL = 1.1754943508222875e-38


def encode_tier_cursor(tier: str, score: float, release_id: int) -> str:
    """The opaque cursor for the page after the row (``score``, ``release_id``) of ``tier``.

    The server makes it and the client only hands it back, so the score keeps every digit of the REAL it was read as
    (``repr`` of the float is exact) and a client cannot round it to a neighbouring REAL. The tier is in the token so a cursor
    from one tier is refused on another.
    """
    raw = json.dumps([tier, repr(float(score)), int(release_id)]).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_tier_cursor(token: str, tier: str) -> tuple[float, int]:
    """Read a cursor made by :func:`encode_tier_cursor` for ``tier``; ``ValueError`` for anything else.

    A token that decodes cleanly can still carry values Postgres refuses (a release id past BIGINT, a score outside REAL's range
    or one that underflows it), which would surface as a 500 from the query, so those are rejected here.
    """
    try:
        cursor_tier, score_text, release_id = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
        if not (isinstance(cursor_tier, str) and isinstance(score_text, str)) or isinstance(release_id, bool) or not isinstance(release_id, int):
            raise ValueError
        score = float(score_text)
    except (ValueError, TypeError, binascii.Error, UnicodeDecodeError) as exc:
        raise ValueError("invalid cursor") from exc
    in_real_range = math.isfinite(score) and abs(score) <= _REAL_MAX and (score == 0 or abs(score) >= _REAL_MIN_NORMAL)
    if cursor_tier != tier or not in_real_range or not 0 <= release_id <= _BIGINT_MAX:
        raise ValueError("invalid cursor")
    return score, release_id


async def get_rarity_tier_page(
    pool: Any,
    tier: str,
    after: tuple[float, int] | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """The next page of a tier's releases after the cursor ``after`` = (rarity_score, release_id).

    Pass ``None`` for the first page. Pages are consistent under score ties: every row is returned
    exactly once in a full walk, provided the caller sends the cursor score as text with at least
    9 significant digits (FLT_DECIMAL_DIG). Fewer digits move the score to a neighbouring REAL, and
    the walk then repeats a tie group, which on real Postgres ran without ending.
    """
    after_score, after_id = after if after is not None else (None, None)
    limit = max(1, min(limit, TIER_PAGE_MAX_LIMIT + 1))  # one more than a page lets the route see whether another follows
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            _TIER_KEYSET_PAGE,
            {"tier": tier, "after_score": after_score, "after_id": after_id, "limit": limit},
        )
        rows: list[dict[str, Any]] = await cur.fetchall()
    return rows


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


async def _release_rarity_has_rows(pool: Any) -> bool:
    """Whether ``insights.release_rarity`` holds any row at all (a primary-key probe, not a count).

    The by-label and by-artist reads filter this table by a list of release ids, so on an empty table
    they can only return nothing; this lets them say so without first reading that list out of Neo4j,
    which for a large label is hundreds of thousands of ids. The table is empty until the first
    rarity computation has stored its rows.
    """
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT EXISTS (SELECT 1 FROM insights.release_rarity) AS present")
        row = await cur.fetchone()
    return bool(row and row["present"])


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

    if not await _release_rarity_has_rows(pool):
        return [], 0

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

    if not await _release_rarity_has_rows(pool):
        return [], 0

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
