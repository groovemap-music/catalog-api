"""Rarity scoring API endpoints.

Serves precomputed rarity scores from PostgreSQL, with Redis caching.
Artist and label endpoints also query Neo4j for release ID lookups.
"""

from typing import Any

import structlog
from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from api.limiter import limiter
from api.queries.rarity_queries import (
    get_rarity_by_artist,
    get_rarity_by_label,
    get_rarity_for_release,
    get_rarity_hidden_gems,
    get_rarity_leaderboard,
)
from api.rarity import CORE_SIGNAL_WEIGHTS, effective_weights, module_weights


logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/rarity", tags=["rarity"])

_neo4j_driver: Any = None
_pg_pool: Any = None


def configure(neo4j: Any, pg_pool: Any, *_args: Any, **_kwargs: Any) -> None:
    """Configure the rarity router with database connections."""
    global _neo4j_driver, _pg_pool
    _neo4j_driver = neo4j
    _pg_pool = pg_pool


def _family_signals(row: dict[str, Any]) -> dict[str, dict[str, float]]:
    """Read the family signals for a row: module id → signal → score.

    Prefers the stored ``family_signals`` JSONB. A row written before
    ``insights.release_rarity`` gained that column carries its family signals as flat columns
    instead, so those are attributed back to whichever installed module declares them. Rows
    from that era were scored with a pressing signal on every medium, so reporting it is
    faithful to how the stored score was actually composed.
    """
    stored = row.get("family_signals")
    if isinstance(stored, dict) and stored:
        return {
            str(module_id): {str(name): float(score) for name, score in signals.items() if isinstance(score, int | float)}
            for module_id, signals in stored.items()
            if isinstance(signals, dict)
        }

    recovered: dict[str, dict[str, float]] = {}
    for module_id, weights in module_weights().items():
        contributed = {name: float(row[name]) for name in weights if row.get(name) is not None}
        if contributed:
            recovered[module_id] = contributed
    return recovered


def _media_families(row: dict[str, Any]) -> list[str]:
    """Read the stored ``media_families`` JSONB into a list of family ids."""
    stored = row.get("media_families")
    if not isinstance(stored, list):
        return []
    return [family for family in stored if isinstance(family, str)]


def _format_breakdown(row: dict[str, Any]) -> dict[str, dict[str, float]]:
    """Build the breakdown dict from a flat database row.

    The weights reported are the *effective* ones for this release: renormalised over the
    signals it actually has. A release no family extension claims carries no
    ``pressing_scarcity`` entry at all, and its core signals each carry proportionally more
    weight. See :mod:`api.rarity.core`.

    For most rows this replays exactly how the stored score was composed. Legacy rows written
    before ``medium_rarity`` existed still carry it as ``NULL``, so it drops out of the signals
    considered here and the remaining weights renormalise to fill the gap instead — e.g. 0.90
    of the total (every core weight but ``medium_rarity``, plus a grooved release's
    ``pressing_scarcity``) renormalises up to 1.0. The reported breakdown for those rows is an
    approximation of the original composition, not a replay of it, until the next daily rarity
    table rebuild backfills ``medium_rarity`` and recomposes the stored score to match.

    ``format_rarity`` is reported with weight ``0.0``: it is deprecated and no longer scored,
    having been replaced by ``medium_rarity``.
    """
    signals = {name: float(row[name]) for name in CORE_SIGNAL_WEIGHTS if row.get(name) is not None}
    weights = dict(CORE_SIGNAL_WEIGHTS)

    declared = module_weights()
    for module_id, contributed in _family_signals(row).items():
        signals.update(contributed)
        weights.update(declared.get(module_id, {}))

    applied = effective_weights(signals.keys(), weights)
    breakdown = {name: {"score": score, "weight": applied.get(name, 0.0)} for name, score in signals.items()}

    if row.get("format_rarity") is not None:
        breakdown["format_rarity"] = {"score": float(row["format_rarity"]), "weight": 0.0}
    return breakdown


def _format_list_item(row: dict[str, Any]) -> dict[str, Any]:
    """Format a database row as a list item."""
    return {
        "release_id": row["release_id"],
        "title": row.get("title") or "",
        "artist": row.get("artist_name") or "",
        "year": row.get("year"),
        "rarity_score": row["rarity_score"],
        "tier": row["tier"],
        "hidden_gem_score": row.get("hidden_gem_score"),
    }


# ── Static path endpoints FIRST (before /{release_id}) ─────────────


@router.get("/leaderboard")
@limiter.limit("30/minute")
async def rarity_leaderboard(
    request: Request,  # noqa: ARG001
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    tier: str | None = Query(None),
) -> JSONResponse:
    """Get global rarity leaderboard, paginated."""
    if not _pg_pool:
        return JSONResponse(content={"error": "Service not ready"}, status_code=503)

    items, total = await get_rarity_leaderboard(_pg_pool, page, page_size, tier)
    return JSONResponse(
        content={
            "items": [_format_list_item(r) for r in items],
            "total": total,
            "page": page,
            "page_size": page_size,
        }
    )


@router.get("/hidden-gems")
@limiter.limit("30/minute")
async def hidden_gems(
    request: Request,  # noqa: ARG001
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    min_rarity: float = Query(41.0, ge=0, le=100),
) -> JSONResponse:
    """Get top hidden gems sorted by hidden gem score."""
    if not _pg_pool:
        return JSONResponse(content={"error": "Service not ready"}, status_code=503)

    items, total = await get_rarity_hidden_gems(_pg_pool, page, page_size, min_rarity)
    return JSONResponse(
        content={
            "items": [_format_list_item(r) for r in items],
            "total": total,
            "page": page,
            "page_size": page_size,
        }
    )


@router.get("/artist/{artist_id}")
@limiter.limit("30/minute")
async def artist_rarity(
    request: Request,  # noqa: ARG001
    artist_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> JSONResponse:
    """Get rarest releases by a specific artist."""
    if not _pg_pool or not _neo4j_driver:
        return JSONResponse(content={"error": "Service not ready"}, status_code=503)

    result = await get_rarity_by_artist(_neo4j_driver, _pg_pool, artist_id, page, page_size)
    if result is None:
        return JSONResponse(content={"error": "Artist not found"}, status_code=404)

    items, total = result
    return JSONResponse(
        content={
            "items": [_format_list_item(r) for r in items],
            "total": total,
            "page": page,
            "page_size": page_size,
        }
    )


@router.get("/label/{label_id}")
@limiter.limit("30/minute")
async def label_rarity(
    request: Request,  # noqa: ARG001
    label_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> JSONResponse:
    """Get rarest releases on a specific label."""
    if not _pg_pool or not _neo4j_driver:
        return JSONResponse(content={"error": "Service not ready"}, status_code=503)

    result = await get_rarity_by_label(_neo4j_driver, _pg_pool, label_id, page, page_size)
    if result is None:
        return JSONResponse(content={"error": "Label not found"}, status_code=404)

    items, total = result
    return JSONResponse(
        content={
            "items": [_format_list_item(r) for r in items],
            "total": total,
            "page": page,
            "page_size": page_size,
        }
    )


# ── Parameterized path endpoint LAST ───────────────────────────────


@router.get("/{release_id}")
@limiter.limit("30/minute")
async def get_release_rarity(request: Request, release_id: int) -> JSONResponse:  # noqa: ARG001
    """Get full rarity breakdown for a single release."""
    if not _pg_pool:
        return JSONResponse(content={"error": "Service not ready"}, status_code=503)

    row = await get_rarity_for_release(_pg_pool, release_id)
    if row is None:
        return JSONResponse(content={"error": "Release rarity not found"}, status_code=404)

    return JSONResponse(
        content={
            "release_id": row["release_id"],
            "title": row.get("title") or "",
            "artist": row.get("artist_name") or "",
            "year": row.get("year"),
            "rarity_score": row["rarity_score"],
            "tier": row["tier"],
            "hidden_gem_score": row.get("hidden_gem_score"),
            "media_families": _media_families(row),
            "family_signals": _family_signals(row),
            "breakdown": _format_breakdown(row),
        }
    )
