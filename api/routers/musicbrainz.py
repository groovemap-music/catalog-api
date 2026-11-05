"""MusicBrainz enrichment API endpoints."""

import asyncio
import json
from typing import Any

import structlog
from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import JSONResponse

from api.limiter import limiter
from api.queries.musicbrainz_queries import (
    get_artist_external_links,
    get_artist_mb_relationships,
    get_artist_musicbrainz,
    get_enrichment_status,
)
from api.telemetry import CACHE_ENRICHMENT_STATUS, cache_get, record_cache_write_failure
from api.valkey_keys import valkey_key


logger = structlog.get_logger(__name__)
router = APIRouter()

_pool: Any = None
_neo4j_driver: Any = None
_valkey: Any = None

# Coverage moves when an enrichment batch lands, not per request, and a miss reads every MusicBrainz
# table, every Artist, Label and Release node, and the MusicBrainz-sourced relationships.
_ENRICHMENT_STATUS_CACHE_TTL = 3600
# One miss at a time: a second caller waits for the first one's answer instead of starting a second scan.
_enrichment_status_lock: asyncio.Lock | None = None  # lazy init to avoid binding to the wrong event loop


def configure(pool: Any, neo4j_driver: Any, valkey: Any = None) -> None:
    """Configure router dependencies."""
    global _pool, _neo4j_driver, _valkey
    _pool = pool
    _neo4j_driver = neo4j_driver
    _valkey = valkey


@router.get("/api/artist/{artist_id}/musicbrainz", status_code=status.HTTP_200_OK)
@limiter.limit("30/minute")
async def artist_musicbrainz(
    request: Request,  # noqa: ARG001 -- required by slowapi
    artist_id: int,
) -> JSONResponse:
    """Get MusicBrainz metadata for a Discogs artist."""
    if _neo4j_driver is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Service not ready")
    data = await get_artist_musicbrainz(_neo4j_driver, str(artist_id))
    if data is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No MusicBrainz data for this artist")
    return JSONResponse(content=data)


@router.get("/api/artist/{artist_id}/relationships", status_code=status.HTTP_200_OK)
@limiter.limit("30/minute")
async def artist_relationships(
    request: Request,  # noqa: ARG001 -- required by slowapi
    artist_id: int,
) -> JSONResponse:
    """Get MusicBrainz-sourced relationships for a Discogs artist."""
    if _neo4j_driver is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Service not ready")
    relationships = await get_artist_mb_relationships(_neo4j_driver, str(artist_id))
    return JSONResponse(content={"discogs_id": artist_id, "relationships": relationships})


@router.get("/api/artist/{artist_id}/external-links", status_code=status.HTTP_200_OK)
@limiter.limit("30/minute")
async def artist_external_links(
    request: Request,  # noqa: ARG001 -- required by slowapi
    artist_id: int,
) -> JSONResponse:
    """Get external links (Wikipedia, Wikidata, etc.) for a Discogs artist."""
    if _pool is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Service not ready")
    links = await get_artist_external_links(_pool, artist_id)
    return JSONResponse(content={"discogs_id": artist_id, "links": links})


@router.get("/api/enrichment/status", status_code=status.HTTP_200_OK)
@limiter.limit("10/minute")
async def enrichment_status_endpoint(
    request: Request,  # noqa: ARG001 -- required by slowapi
) -> JSONResponse:
    """Get MusicBrainz enrichment coverage statistics."""
    if _pool is None or _neo4j_driver is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Service not ready")
    cache_key = valkey_key("enrichment:status")
    cached = await _cached_enrichment_status(cache_key)
    if cached is not None:
        return JSONResponse(content=cached)

    global _enrichment_status_lock
    if _enrichment_status_lock is None:
        _enrichment_status_lock = asyncio.Lock()
    async with _enrichment_status_lock:
        # Whoever held the lock may have filled the cache while this request waited.
        cached = await _cached_enrichment_status(cache_key)
        if cached is not None:
            return JSONResponse(content=cached)
        stats = await get_enrichment_status(_pool, _neo4j_driver)
        if _valkey:
            try:
                await _valkey.setex(cache_key, _ENRICHMENT_STATUS_CACHE_TTL, json.dumps(stats))
            except Exception as exc:
                record_cache_write_failure(CACHE_ENRICHMENT_STATUS, exc)
    return JSONResponse(content=stats)


async def _cached_enrichment_status(cache_key: str) -> dict[str, Any] | None:
    """The cached status, or ``None`` on a miss, an unreadable entry, or no Valkey."""
    if not _valkey:
        return None
    try:
        cached = await cache_get(_valkey, cache_key, cache=CACHE_ENRICHMENT_STATUS)
        status_body: dict[str, Any] | None = json.loads(cached) if cached else None
    except Exception:
        logger.debug("⚠️ Enrichment status cache get failed", key=cache_key)
        return None
    return status_body
