"""The backend-neutral half of the rarity signal batch.

ADR 0012 migrates the graph reads family by family, and the rarity signal batch is the one
family whose Cypher is not a single question but a *walk*: a keyset page of release ids, nine
page-scoped signal queries, a join, and a scoring pass over the result. Only the first three
of those touch a graph. Everything after them — the join, the composition through
:mod:`api.rarity`, the global percentile pass, and the coverage check — is arithmetic over
rows and is identical whichever engine produced them.

So it lives here, once, and each backend contributes only the four reads:

* ``fetch_release_id_page`` — the next keyset page of release ids.
* ``fetch_page_signals`` — every core and family-extension signal query, for one page.
* ``count_releases`` — the release total the coverage check compares the walk against.
* the community counts, which are PostgreSQL on *both* backends and are therefore read here
  rather than by either of them.

:func:`score_all_rarity_signals` takes those three reads as callables rather than importing
either backend, which is what keeps `api/queries/rarity_queries.py` and
`api/queries/rarity_pg_queries.py` from importing each other — the same rule the collaborators
pilot follows.

Two ordering rules make the two backends' results comparable at all, and both are here rather
than in a query:

* The scoring loop walks the **page's id list**, not the release query's row order. Neo4j
  returns an aggregated row set in whatever order its runtime produced it and PostgreSQL
  returns one in whatever order its planner produced it; the id page is ordered by both.
* :func:`rows_by_release_id` is what every backend's ``fetch_page_signals`` returns its rows
  through, so a page's signal rows are in ascending ``release_id`` order on both engines. The
  join below indexes by id and never cared about the order, so this costs a sort per page and
  changes no result.
"""

from __future__ import annotations

import bisect
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog
from psycopg.rows import dict_row

from api.rarity import (
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
)


logger = structlog.get_logger(__name__)


# Releases per page. Sized so a page's queries stay far inside the 600s server-side
# transaction timeout while keeping the number of round trips sane. It is the chunking
# contract's page — see the banner in `api/queries/rarity_queries.py`, which every backend's
# signal queries are bound by.
RARITY_PAGE_SIZE = 20_000

# Per-query server-side timeout. Must stay comfortably below Neo4j's db.transaction.timeout
# (600s in production) so a slow page surfaces as a fast, attributable failure rather than a
# 600s stall. The PostgreSQL backend applies the same budget as a `statement_timeout`, so a
# pathological page fails the same way on both engines.
RARITY_QUERY_TIMEOUT_SECONDS = 120.0


@dataclass(frozen=True)
class RarityHandles:
    """The two stores the rarity family reads, as one handle.

    ``graph`` answers the signal walk and the two id lookups: a Neo4j driver for
    :mod:`api.queries.rarity_queries`, a PostgreSQL pool for
    :mod:`api.queries.rarity_pg_queries`. ``insights`` is always a PostgreSQL pool, because
    ``insights.community_counts`` and ``insights.release_rarity`` are PostgreSQL tables on
    both backends — the migration moves the *graph* reads, not the results table.

    On the PostgreSQL backend the two are the same pool, which is the whole reason the two
    lookups collapse from four round trips to one: the graph and the results table are finally
    in the same database and a single statement can join them.

    The backends take one handle rather than two arguments because the graph-backend seam
    resolves a *family* to a module and then calls its functions uniformly; a family whose
    functions took different argument counts could not be bound to one `Protocol`.
    """

    graph: Any
    insights: Any = None


# One backend read, as the pipeline calls it. Each takes the family's whole handle rather
# than its graph half, because these are `RarityBackend` functions and the graph-backend seam
# calls every function of a family the same way. A read that only needs the graph simply
# reaches for `handles.graph` itself.
PageReader = Callable[["RarityHandles", str, int], Awaitable[list[str]]]
SignalReader = Callable[["RarityHandles", list[str]], Awaitable[dict[str, list[dict[str, Any]]]]]
CountReader = Callable[["RarityHandles"], Awaitable[int | None]]


def rows_by_release_id(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return *rows* in ascending ``release_id`` order.

    Every backend's ``fetch_page_signals`` returns each fact's rows through this. Neither
    engine promises an order for an aggregated row set, and the parity harness compares two
    lists of rows, so without one the harness would be comparing the two planners. The join in
    :func:`score_all_rarity_signals` indexes by id and is indifferent to it.
    """
    return sorted(rows, key=lambda row: str(row["release_id"]))


def percentile_rank(value: float, sorted_values: list[float]) -> float:
    """Return percentile rank (0.0 to 1.0) of value in sorted list."""
    if not sorted_values or value <= 0:
        return 0.0
    return bisect.bisect_left(sorted_values, value) / len(sorted_values)


async def load_community_counts(pool: Any) -> dict[str, tuple[int, int]]:
    """Load community have/want counts from PostgreSQL (neutral fallback on failure).

    PostgreSQL on both backends: ``insights.community_counts`` is written by the community
    fetcher and was never in Neo4j, so this read is not part of what ADR 0012 migrates.
    """
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


def _index_by_release(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Index signal rows by their release_id."""
    return {row["release_id"]: row for row in rows}


async def score_all_rarity_signals(
    handles: RarityHandles,
    *,
    page: PageReader,
    signals: SignalReader,
    count: CountReader,
    page_size: int = RARITY_PAGE_SIZE,
) -> list[dict[str, Any]]:
    """Walk the release set in pages, score every release, and return the rows to store.

    Walks in keyset-paginated chunks of ``page_size``. For each page it asks the backend for
    the core signal rows plus those every installed family extension declares, scoped to that
    page's ids, joins them by release_id, and composes the rarity score through
    :func:`api.rarity.score_release`. Community counts (have/want) are loaded once from
    PostgreSQL when ``handles.insights`` is set.

    Hidden-gem scoring needs percentile ranks over the *global* quality-signal distributions,
    so those are accumulated while paging and applied in a final pass — no second trip to the
    graph.

    Args:
        handles: The graph and insights connections. See :class:`RarityHandles`.
        page: The backend's keyset page read.
        signals: The backend's per-page signal read.
        count: The backend's release-count read, for the coverage check.
        page_size: Releases per chunk. Bounds per-transaction working set.

    Returns:
        A list of dicts ready for PostgreSQL insertion, in ascending release id order. Each
        carries the historical keys plus ``media_families``, ``family_signals``, and
        ``medium_rarity``. ``pressing_scarcity`` is ``None`` for a release no family extension
        claims — a CD has no pressings to count.
    """
    current_year = datetime.now(UTC).year

    logger.info("🔍 Fetching rarity signals...", page_size=page_size)

    community_map = await load_community_counts(handles.insights)
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
        ids = await page(handles, cursor, page_size)
        if not ids:
            break
        cursor = ids[-1]
        pages += 1

        page_rows = await signals(handles, ids)

        release_map = _index_by_release(page_rows["release"])
        media_map = _index_by_release(page_rows["media"])
        label_map = {r["release_id"]: r["label_catalog_size"] for r in page_rows["label"]}
        temporal_map = _index_by_release(page_rows["temporal"])
        degree_map = {r["release_id"]: r["degree"] for r in page_rows["degree"]}
        artist_deg_map = {r["release_id"]: r["artist_max_degree"] for r in page_rows["artist_degree"]}
        label_size_map = {r["release_id"]: r["label_max_catalog"] for r in page_rows["label_size"]}
        genre_count_map = {r["release_id"]: r["genre_max_release_count"] for r in page_rows["genre_count"]}
        fact_maps = {fact: _index_by_release(page_rows.get(fact, [])) for fact in family_facts}

        all_artist_degrees.extend(r["artist_max_degree"] for r in page_rows["artist_degree"] if r["artist_max_degree"])
        all_label_sizes.extend(r["label_max_catalog"] for r in page_rows["label_size"] if r["label_max_catalog"])
        all_genre_counts.extend(r["genre_max_release_count"] for r in page_rows["genre_count"] if r["genre_max_release_count"])

        # Driven by the page's own id order rather than by the release query's row order: the
        # ids are ordered by both engines, an aggregated row set is ordered by neither, and
        # the order here is the order of the returned list. Every id on the page has a release
        # row — the two queries seek the same set — so `continue` is the defensive branch a
        # backend returning a short row set would take, not a normal one.
        for rid in ids:
            row = release_map.get(rid)
            if row is None:
                continue

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
            0.4 * percentile_rank(artist_deg, all_artist_degrees)
            + 0.3 * percentile_rank(label_sz, all_label_sizes)
            + 0.3 * percentile_rank(genre_ct, all_genre_counts)
        )
        entry["hidden_gem_score"] = round(rarity_score * quality_multiplier, 1)

    await warn_on_incomplete_coverage(handles, count, scored=len(results))

    logger.info("✅ Rarity scores computed", total=len(results), pages=pages)
    return results


async def warn_on_incomplete_coverage(handles: RarityHandles, count: CountReader, scored: int) -> None:
    """Log a warning when the paginated walk scored fewer releases than exist.

    The keyset walk compares ``release id > cursor`` against a string cursor; if the store
    ever held a non-string release id the comparison would yield null and silently truncate
    the walk. The count is O(1) on both backends — Neo4j serves it from the label count store
    and PostgreSQL from a single aggregate — so this costs nothing and turns a silent partial
    result into a loud one. A backend that cannot answer it at all is not a reason to fail the
    batch that has already been computed.
    """
    try:
        total = await count(handles)
    except Exception:
        logger.debug("⚠️ Release count check skipped", exc_info=True)
        return
    if isinstance(total, int) and scored < total:
        logger.warning(
            "⚠️ Rarity pagination covered fewer releases than the store holds",
            scored=scored,
            total=total,
            missing=total - scored,
        )
