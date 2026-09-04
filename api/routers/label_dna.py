"""Label DNA endpoints — fingerprint and compare record labels."""

import asyncio
import json
from typing import Any

import structlog
from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from api.limiter import limiter
from api.models import (
    DecadeCount,
    FormatWeight,
    GenreWeight,
    LabelCompareEntry,
    LabelCompareResponse,
    LabelDNA,
    MediaFamilyWeight,
    MediumWeight,
    SimilarLabel,
    SimilarLabelsResponse,
    StyleWeight,
)
from api.queries.label_dna_queries import (
    MIN_RELEASES,
    compute_similar_labels,
    get_candidate_labels_genre_vectors,
    get_label_active_years,
    get_label_format_profile,
    get_label_full_profile,
    get_label_genre_profile,
    get_label_identity,
    get_label_media_profile,
)
from api.telemetry import CACHE_LABEL_DNA, CACHE_LABEL_SIMILAR, cache_get


logger = structlog.get_logger(__name__)

router = APIRouter()

_neo4j_driver: Any = None
_redis: Any = None

# Redis cache TTL for label DNA (24 hours — data changes only on import)
_LABEL_DNA_CACHE_TTL = 86400


def configure(neo4j: Any, redis: Any = None) -> None:
    global _neo4j_driver, _redis
    _neo4j_driver = neo4j
    _redis = redis


def _add_percentages(items: list[dict[str, Any]], total: int) -> list[dict[str, Any]]:
    """Add percentage field to each item based on total."""
    return [{**item, "percentage": round(item["count"] / total * 100, 1) if total else 0.0} for item in items]


def _add_media_percentages(families: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add percentage fields to a media profile.

    Each family's percentage is its share of the label's total media-tagged
    release count (families sum to ~100%). Each medium's percentage is its
    share within its own family (mediums within one family sum to ~100%),
    so the nested detail reads as "of this label's vinyl, X% is 12-inch".
    """
    family_total = sum(f["count"] for f in families)
    result = []
    for family in families:
        medium_total = sum(m["count"] for m in family["mediums"])
        result.append(
            {
                "name": family["family"],
                "count": family["count"],
                "percentage": round(family["count"] / family_total * 100, 1) if family_total else 0.0,
                "mediums": [
                    {
                        "id": medium["id"],
                        "label": medium["label"],
                        "count": medium["count"],
                        "percentage": round(medium["count"] / medium_total * 100, 1) if medium_total else 0.0,
                    }
                    for medium in family["mediums"]
                ],
            }
        )
    return result


async def _build_dna(label_id: str) -> tuple[LabelDNA | None, str]:
    """Build a full LabelDNA fingerprint for a label.

    Returns (dna, reason) — reason is "ok", "not_found", or "too_few".

    Checks Redis cache first (same key as ``/api/label/{id}/dna``).
    On miss, runs profile queries and caches the result so that
    subsequent calls (e.g. from ``/api/label/dna/compare``) are instant.
    """
    # Check cache first — reuses the same key as the /dna endpoint
    cache_key = f"label-dna:{label_id}"
    if _redis:
        try:
            cached = await cache_get(_redis, cache_key, cache=CACHE_LABEL_DNA)
            if cached:
                return LabelDNA(**json.loads(cached)), "ok"
        except Exception:
            logger.debug("⚠️ Label DNA _build_dna cache get failed", key=cache_key)

    profile = await get_label_full_profile(_neo4j_driver, label_id)
    if not profile:
        return None, "not_found"

    release_count = profile["release_count"]
    if release_count < MIN_RELEASES:
        return None, "too_few"

    artist_count = profile["artist_count"]
    genres = profile["genres"]
    styles = profile["styles"]
    decades = profile["decades"]

    active_years, formats, media = await asyncio.gather(
        get_label_active_years(_neo4j_driver, label_id),
        get_label_format_profile(_neo4j_driver, label_id),
        get_label_media_profile(_neo4j_driver, label_id),
    )

    # Artist diversity: unique artists / total releases (capped at 1.0)
    artist_diversity = round(min(artist_count / release_count, 1.0), 4) if release_count else 0.0

    # Peak decade
    peak_decade = max(decades, key=lambda d: d["count"])["decade"] if decades else None

    # Prolificacy: releases per active year
    num_active_years = len(active_years)
    prolificacy = round(release_count / num_active_years, 2) if num_active_years else 0.0

    # Total counts for percentage calculation
    genre_total = sum(g["count"] for g in genres)
    style_total = sum(s["count"] for s in styles)
    decade_total = sum(d["count"] for d in decades)
    format_total = sum(f["count"] for f in formats)

    dna = LabelDNA(
        label_id=profile["label_id"],
        label_name=profile["label_name"],
        release_count=release_count,
        artist_count=artist_count,
        artist_diversity=artist_diversity,
        active_years=active_years,
        peak_decade=peak_decade,
        prolificacy=prolificacy,
        genres=[GenreWeight(**g) for g in _add_percentages(genres, genre_total)],
        styles=[StyleWeight(**s) for s in _add_percentages(styles, style_total)],
        formats=[FormatWeight(**f) for f in _add_percentages(formats, format_total)],
        media=[
            MediaFamilyWeight(name=f["name"], count=f["count"], percentage=f["percentage"], mediums=[MediumWeight(**m) for m in f["mediums"]])
            for f in _add_media_percentages(media)
        ],
        decades=[DecadeCount(**d) for d in _add_percentages(decades, decade_total)],
    )

    # Cache the result so compare and subsequent /dna calls are instant
    if _redis:
        try:
            await _redis.setex(cache_key, _LABEL_DNA_CACHE_TTL, json.dumps(dna.model_dump(), default=str))
        except Exception:
            logger.debug("⚠️ Label DNA _build_dna cache set failed", key=cache_key)

    return dna, "ok"


@router.get("/api/label/{label_id}/dna")
@limiter.limit("30/minute")
async def label_dna(
    request: Request,  # noqa: ARG001 -- required by slowapi
    label_id: str,
) -> JSONResponse:
    """Get the full DNA fingerprint for a label."""
    if not _neo4j_driver:
        return JSONResponse(content={"error": "Service not ready"}, status_code=503)

    # _build_dna already checks and populates the Redis cache — no redundant lookup here
    dna, reason = await _build_dna(label_id)
    if dna is None:
        if reason == "not_found":
            return JSONResponse(content={"error": f"Label '{label_id}' not found"}, status_code=404)
        return JSONResponse(
            content={"error": f"Label '{label_id}' has fewer than {MIN_RELEASES} releases"},
            status_code=422,
        )

    response = dna.model_dump()

    # _build_dna already caches the result — no redundant cache write needed

    return JSONResponse(content=response)


@router.get("/api/label/{label_id}/similar")
@limiter.limit("30/minute")
async def similar_labels(
    request: Request,  # noqa: ARG001 -- required by slowapi
    label_id: str,
    limit: int = Query(10, ge=1, le=50),
) -> JSONResponse:
    """Find labels with the closest DNA fingerprint to the given label."""
    if not _neo4j_driver:
        return JSONResponse(content={"error": "Service not ready"}, status_code=503)

    # Check Redis cache first (keyed by label_id + limit)
    cache_key = f"label-similar:{label_id}:{limit}"
    if _redis:
        try:
            cached = await cache_get(_redis, cache_key, cache=CACHE_LABEL_SIMILAR)
            if cached:
                return JSONResponse(content=json.loads(cached))
        except Exception:
            logger.debug("⚠️ Label similar cache get failed", key=cache_key)

    identity = await get_label_identity(_neo4j_driver, label_id)
    if not identity:
        return JSONResponse(content={"error": f"Label '{label_id}' not found"}, status_code=404)

    if identity["release_count"] < MIN_RELEASES:
        return JSONResponse(
            content={"error": f"Label '{label_id}' has fewer than {MIN_RELEASES} releases"},
            status_code=422,
        )

    target_genres, candidates = await asyncio.gather(
        get_label_genre_profile(_neo4j_driver, label_id),
        get_candidate_labels_genre_vectors(_neo4j_driver, label_id),
    )

    ranked = compute_similar_labels(target_genres, candidates, limit=limit)

    response = SimilarLabelsResponse(
        label_id=identity["label_id"],
        label_name=identity["label_name"],
        similar=[SimilarLabel(**r) for r in ranked],
    )
    response_data = response.model_dump()

    # Cache the result
    if _redis:
        try:
            await _redis.setex(cache_key, _LABEL_DNA_CACHE_TTL, json.dumps(response_data, default=str))
        except Exception:
            logger.debug("⚠️ Label similar cache set failed", key=cache_key)

    return JSONResponse(content=response_data)


@router.get("/api/label/dna/compare")
@limiter.limit("30/minute")
async def compare_labels(
    request: Request,  # noqa: ARG001 -- required by slowapi
    ids: str = Query(..., description="Comma-separated label IDs (2-5)"),
) -> JSONResponse:
    """Side-by-side DNA comparison of multiple labels."""
    if not _neo4j_driver:
        return JSONResponse(content={"error": "Service not ready"}, status_code=503)

    label_ids = [lid.strip() for lid in ids.split(",") if lid.strip()]
    if len(label_ids) < 2:
        return JSONResponse(content={"error": "At least 2 label IDs required"}, status_code=400)
    if len(label_ids) > 5:
        return JSONResponse(content={"error": "At most 5 label IDs allowed"}, status_code=400)

    dna_results = await asyncio.gather(*[_build_dna(lid) for lid in label_ids])

    entries = []
    for lid, (dna, reason) in zip(label_ids, dna_results, strict=True):
        if dna is None:
            if reason == "not_found":
                return JSONResponse(content={"error": f"Label '{lid}' not found"}, status_code=404)
            return JSONResponse(
                content={"error": f"Label '{lid}' has fewer than {MIN_RELEASES} releases"},
                status_code=422,
            )
        entries.append(LabelCompareEntry(dna=dna))

    response = LabelCompareResponse(labels=entries)
    return JSONResponse(content=response.model_dump())
