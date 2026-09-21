"""Taste fingerprint endpoints — genre heatmap, obscurity, drift, blind spots."""

import asyncio
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse, Response

from api.dependencies import require_user
from api.graph_backend import GRAPH_BACKEND_ERROR_TYPES, TasteBackend, get_taste_backend, is_graph_query_timeout
from api.models import (
    BlindSpot,
    FingerprintResponse,
    HeatmapCell,
    HeatmapResponse,
    ObscurityScore,
    TasteDriftYear,
)
from api.queries.taste_queries import (  # noqa: F401 -- preserve legacy patch points
    get_blind_spots,
    get_collection_count,
    get_obscurity_score,
    get_taste_drift,
    get_taste_heatmap,
    get_top_labels,
)
from api.taste_card import render_taste_card


logger = structlog.get_logger(__name__)

router = APIRouter()

_neo4j_driver: Any = None
_pg_pool: Any = None
_graph_backend = "neo4j"
_taste_backend: TasteBackend = get_taste_backend("neo4j")

_MIN_COLLECTION_ITEMS = 10


def configure(neo4j: Any, jwt_secret: str | None, graph_backend: str = "neo4j", pg_pool: Any = None) -> None:  # noqa: ARG001
    """Configure the taste router with Neo4j driver and JWT secret."""
    global _neo4j_driver, _pg_pool, _graph_backend, _taste_backend
    _neo4j_driver = neo4j
    _pg_pool = pg_pool
    _graph_backend = graph_backend
    _taste_backend = get_taste_backend(graph_backend)


def _handle() -> Any:
    return _pg_pool if _graph_backend == "postgres" else _neo4j_driver


def _query(name: str) -> Any:
    return getattr(_taste_backend, name) if _graph_backend == "postgres" else globals()[name]


def _peak_decade(cells: list[dict[str, Any]]) -> int | None:
    """Return the decade with the most releases, or None if no data."""
    if not cells:
        return None
    decade_totals: dict[int, int] = {}
    for cell in cells:
        decade_totals[cell["decade"]] = decade_totals.get(cell["decade"], 0) + cell["count"]
    return max(decade_totals, key=lambda d: decade_totals[d])


async def _check_minimum(driver: Any, user_id: str) -> JSONResponse | None:
    """Return a 422 response if the user's collection is too small, else None."""
    count = await _query("get_collection_count")(driver, user_id)
    if count < _MIN_COLLECTION_ITEMS:
        return JSONResponse(
            content={"detail": f"Collection must have at least {_MIN_COLLECTION_ITEMS} items (currently {count})"},
            status_code=422,
        )
    return None


@router.get("/api/user/taste/heatmap")
async def taste_heatmap(
    current_user: Annotated[dict[str, Any], Depends(require_user)],
) -> JSONResponse:
    """Return genre x decade heatmap for the authenticated user."""
    if not _handle():
        return JSONResponse(content={"error": "Service not ready"}, status_code=503)
    user_id: str = current_user.get("sub", "")
    err = await _check_minimum(_handle(), user_id)
    if err:
        return err
    try:
        cells, total = await _query("get_taste_heatmap")(_handle(), user_id)
    except GRAPH_BACKEND_ERROR_TYPES as exc:
        if is_graph_query_timeout(exc):
            logger.warning("⏱️ Taste heatmap query timed out", user_id=user_id)
            return JSONResponse(
                content={"error": "Taste heatmap query timed out — collection may be too large"},
                status_code=504,
            )
        raise
    resp = HeatmapResponse(
        cells=[HeatmapCell(**c) for c in cells],
        total=total,
    )
    return JSONResponse(content=resp.model_dump())


@router.get("/api/user/taste/fingerprint")
async def taste_fingerprint(
    current_user: Annotated[dict[str, Any], Depends(require_user)],
) -> JSONResponse:
    """Return full taste fingerprint combining all sub-queries."""
    if not _handle():
        return JSONResponse(content={"error": "Service not ready"}, status_code=503)
    user_id: str = current_user.get("sub", "")
    err = await _check_minimum(_handle(), user_id)
    if err:
        return err

    try:
        heatmap_result, obscurity_result, drift_result, blind_spots_result = await asyncio.gather(
            _query("get_taste_heatmap")(_handle(), user_id),
            _query("get_obscurity_score")(_handle(), user_id),
            _query("get_taste_drift")(_handle(), user_id),
            _query("get_blind_spots")(_handle(), user_id),
        )
    except GRAPH_BACKEND_ERROR_TYPES as exc:
        if is_graph_query_timeout(exc):
            logger.warning("⏱️ Taste fingerprint query timed out", user_id=user_id)
            return JSONResponse(
                content={"error": "Taste fingerprint query timed out — collection may be too large"},
                status_code=504,
            )
        raise

    cells, _total = heatmap_result
    resp = FingerprintResponse(
        heatmap=[HeatmapCell(**c) for c in cells],
        obscurity=ObscurityScore(**obscurity_result),
        drift=[TasteDriftYear(**d) for d in drift_result],
        blind_spots=[BlindSpot(**b) for b in blind_spots_result],
        peak_decade=_peak_decade(cells),
    )
    return JSONResponse(content=resp.model_dump())


@router.get("/api/user/taste/blindspots")
async def taste_blindspots(
    current_user: Annotated[dict[str, Any], Depends(require_user)],
    limit: int = Query(5, ge=1, le=20),
) -> JSONResponse:
    """Return genres the user's favourite artists release in but the user hasn't collected."""
    if not _handle():
        return JSONResponse(content={"error": "Service not ready"}, status_code=503)
    user_id: str = current_user.get("sub", "")
    err = await _check_minimum(_handle(), user_id)
    if err:
        return err
    try:
        spots = await _query("get_blind_spots")(_handle(), user_id, limit=limit)
    except GRAPH_BACKEND_ERROR_TYPES as exc:
        if is_graph_query_timeout(exc):
            logger.warning("⏱️ Blind spots query timed out", user_id=user_id)
            return JSONResponse(
                content={"error": "Blind spots query timed out — collection may be too large"},
                status_code=504,
            )
        raise
    return JSONResponse(content={"blind_spots": [BlindSpot(**b).model_dump() for b in spots]})


@router.get("/api/user/taste/card")
async def taste_card(
    current_user: Annotated[dict[str, Any], Depends(require_user)],
) -> Response:
    """Return an SVG taste card for the authenticated user."""
    if not _handle():
        return JSONResponse(content={"error": "Service not ready"}, status_code=503)
    user_id: str = current_user.get("sub", "")
    err = await _check_minimum(_handle(), user_id)
    if err:
        return err

    try:
        heatmap_result, obscurity_result, drift_result, labels_result = await asyncio.gather(
            _query("get_taste_heatmap")(_handle(), user_id),
            _query("get_obscurity_score")(_handle(), user_id),
            _query("get_taste_drift")(_handle(), user_id),
            _query("get_top_labels")(_handle(), user_id, limit=5),
        )
    except GRAPH_BACKEND_ERROR_TYPES as exc:
        if is_graph_query_timeout(exc):
            logger.warning("⏱️ Taste card query timed out", user_id=user_id)
            return JSONResponse(
                content={"error": "Taste card query timed out — collection may be too large"},
                status_code=504,
            )
        raise

    cells, _total = heatmap_result
    # Aggregate genre counts across all cells (decades) to find the true top genres
    genre_counts: dict[str, int] = {}
    for c in cells:
        g = c["genre"]
        genre_counts[g] = genre_counts.get(g, 0) + c.get("count", 1)
    top_genres = sorted(genre_counts.items(), key=lambda kv: kv[1], reverse=True)[:5]
    svg = render_taste_card(
        peak_decade=_peak_decade(cells),
        obscurity_score=obscurity_result["score"],
        top_genres=top_genres,
        top_labels=[(lb["label"], lb["count"]) for lb in labels_result],
        drift=[TasteDriftYear(**d) for d in drift_result],
    )
    return Response(content=svg, media_type="image/svg+xml", headers={"Cache-Control": "no-store"})
