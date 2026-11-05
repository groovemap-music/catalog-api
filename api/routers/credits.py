"""Credits & Provenance endpoints — the people behind the music."""

import asyncio
import json
from typing import Any

import structlog
from common.credit_roles import ALL_CATEGORIES
from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from api.limiter import limiter
from api.models import (
    ConnectionEntry,
    CreditEntry,
    LeaderboardEntry,
    PersonAutocompleteEntry,
    PersonConnectionsResponse,
    PersonCreditsResponse,
    PersonProfileResponse,
    PersonTimelineResponse,
    ReleaseCreditEntry,
    ReleaseCreditsResponse,
    RoleLeaderboardResponse,
    SecondHopEntry,
    SharedCreditEntry,
    SharedCreditsResponse,
    TimelineEntry,
)
from api.queries.credits_queries import (
    MAX_CONNECTION_DEPTH,
    autocomplete_person,
    count_person_credits,
    get_person_connections,
    get_person_credits,
    get_person_profile,
    get_person_role_breakdown,
    get_person_timeline,
    get_release_credits,
    get_role_leaderboard,
    get_shared_credits,
)
from api.telemetry import CACHE_CREDITS_LEADERBOARD, CACHE_CREDITS_PERSON, cache_get, record_cache_write_failure
from api.valkey_keys import valkey_key


logger = structlog.get_logger(__name__)

router = APIRouter()

_neo4j_driver: Any = None
_valkey: Any = None

# Valkey cache TTL for credits (24 hours — data changes only on import)
_CREDITS_CACHE_TTL = 86400


def configure(neo4j: Any, valkey: Any = None) -> None:
    global _neo4j_driver, _valkey
    _neo4j_driver = neo4j
    _valkey = valkey


# ── Person sub-routes MUST be declared before the catch-all {name} route ──


@router.get("/api/credits/person/{name}/timeline")
@limiter.limit("60/minute")
async def person_timeline(
    request: Request,  # noqa: ARG001 -- required by slowapi
    name: str,
) -> JSONResponse:
    """Credits over time — year-by-year activity."""
    if not _neo4j_driver:
        return JSONResponse(content={"error": "Service not ready"}, status_code=503)

    records = await get_person_timeline(_neo4j_driver, name)
    if not records:
        return JSONResponse(content={"error": f"No timeline data for '{name}'"}, status_code=404)

    timeline = [TimelineEntry(year=r["year"], category=r["category"], count=r["count"]) for r in records]
    response = PersonTimelineResponse(name=name, timeline=timeline)
    return JSONResponse(content=response.model_dump())


@router.get("/api/credits/person/{name}/profile")
@limiter.limit("60/minute")
async def person_profile(
    request: Request,  # noqa: ARG001 -- required by slowapi
    name: str,
) -> JSONResponse:
    """Summary profile for a credited person."""
    if not _neo4j_driver:
        return JSONResponse(content={"error": "Service not ready"}, status_code=503)

    profile = await get_person_profile(_neo4j_driver, name)
    if not profile:
        return JSONResponse(content={"error": f"Person '{name}' not found"}, status_code=404)

    role_breakdown = await get_person_role_breakdown(_neo4j_driver, name)

    response = PersonProfileResponse(
        name=profile["name"],
        total_credits=profile["total_credits"],
        categories=profile["categories"] or [],
        first_year=profile["first_year"],
        last_year=profile["last_year"],
        artist_id=profile["artist_id"],
        artist_name=profile["artist_name"],
        role_breakdown=[{"category": r["category"], "count": r["count"]} for r in role_breakdown],
    )
    return JSONResponse(content=response.model_dump())


# A person's credit pages are cached only when they are worth it and their number is bounded: persons with at
# least this many credits, the default page size, and the first pages. Every other request is answered the same,
# just uncached (the Valkey instance is shared with auth state and is noeviction).
_CREDITS_PAGE_DEFAULT = 100
_CREDITS_CACHE_MIN_TOTAL = 2000
_CREDITS_CACHE_MAX_PAGES = 20


def _credits_page_cacheable(limit: int, offset: int) -> bool:
    return limit == _CREDITS_PAGE_DEFAULT and offset % limit == 0 and offset < limit * _CREDITS_CACHE_MAX_PAGES


@router.get("/api/credits/person/{name}")
@limiter.limit("60/minute")
async def person_credits(
    request: Request,  # noqa: ARG001 -- required by slowapi
    name: str,
    limit: int = Query(_CREDITS_PAGE_DEFAULT, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> JSONResponse:
    """One page of the releases a person is credited on, newest first.

    ``total_credits`` is the number of credits in all, so a client reaches every credit by raising ``offset``
    by ``limit`` until ``has_more`` is false. Before this was paginated the whole list came back at once
    (60,758 rows for the busiest mastering engineer).
    """
    if not _neo4j_driver:
        return JSONResponse(content={"error": "Service not ready"}, status_code=503)

    cacheable = _credits_page_cacheable(limit, offset)
    cache_key = valkey_key(f"credits:person:{name}:{limit}:{offset}")
    if _valkey and cacheable:
        try:
            cached = await cache_get(_valkey, cache_key, cache=CACHE_CREDITS_PERSON)
            if cached:
                return JSONResponse(content=json.loads(cached))
        except Exception:
            logger.debug("⚠️ Credits person cache get failed", key=cache_key)

    records, total = await asyncio.gather(
        get_person_credits(_neo4j_driver, name, limit, offset),
        count_person_credits(_neo4j_driver, name),
    )
    if not total:
        return JSONResponse(content={"error": f"No credits found for '{name}'"}, status_code=404)

    credits = [
        CreditEntry(
            release_id=r["release_id"],
            title=r["title"] or "Unknown",
            year=r["year"],
            role=r["role"],
            category=r["category"],
            artists=r["artists"] or [],
            labels=r["labels"] or [],
        )
        for r in records
    ]
    response = PersonCreditsResponse(
        name=name, total_credits=total, credits=credits, limit=limit, offset=offset, has_more=offset + len(credits) < total
    )
    response_data = response.model_dump()

    if _valkey and cacheable and total >= _CREDITS_CACHE_MIN_TOTAL:
        try:
            await _valkey.setex(cache_key, _CREDITS_CACHE_TTL, json.dumps(response_data, default=str))
        except Exception as exc:
            record_cache_write_failure(CACHE_CREDITS_PERSON, exc)

    return JSONResponse(content=response_data)


@router.get("/api/credits/release/{release_id}")
@limiter.limit("60/minute")
async def release_credits(
    request: Request,  # noqa: ARG001 -- required by slowapi
    release_id: str,
) -> JSONResponse:
    """Full credits breakdown for a release."""
    if not _neo4j_driver:
        return JSONResponse(content={"error": "Service not ready"}, status_code=503)

    records = await get_release_credits(_neo4j_driver, release_id)
    if not records:
        return JSONResponse(content={"error": f"No credits found for release '{release_id}'"}, status_code=404)

    credits = [
        ReleaseCreditEntry(
            name=r["name"],
            role=r["role"],
            category=r["category"],
            artist_id=r["artist_id"],
            artist_name=r["artist_name"],
        )
        for r in records
    ]
    response = ReleaseCreditsResponse(release_id=release_id, credits=credits)
    return JSONResponse(content=response.model_dump())


@router.get("/api/credits/role/{role}/top")
@limiter.limit("30/minute")
async def role_leaderboard(
    request: Request,  # noqa: ARG001 -- required by slowapi
    role: str,
    limit: int = Query(20, ge=1, le=100),
) -> JSONResponse:
    """Most prolific people in a given role category."""
    if not _neo4j_driver:
        return JSONResponse(content={"error": "Service not ready"}, status_code=503)

    if role not in ALL_CATEGORIES:
        return JSONResponse(
            content={"error": f"Invalid role category '{role}'. Valid: {', '.join(ALL_CATEGORIES)}"},
            status_code=400,
        )

    cache_key = valkey_key(f"credits:leaderboard:{role}:{limit}")
    if _valkey:
        try:
            cached = await cache_get(_valkey, cache_key, cache=CACHE_CREDITS_LEADERBOARD)
            if cached:
                return JSONResponse(content=json.loads(cached))
        except Exception:
            logger.debug("⚠️ Credits leaderboard cache get failed", key=cache_key)

    records = await get_role_leaderboard(_neo4j_driver, role, limit)
    entries = [LeaderboardEntry(name=r["name"], credit_count=r["credit_count"]) for r in records]
    response = RoleLeaderboardResponse(category=role, entries=entries)
    response_data = response.model_dump()

    if _valkey:
        try:
            await _valkey.setex(cache_key, _CREDITS_CACHE_TTL, json.dumps(response_data, default=str))
        except Exception as exc:
            record_cache_write_failure(CACHE_CREDITS_LEADERBOARD, exc)

    return JSONResponse(content=response_data)


@router.get("/api/credits/shared")
@limiter.limit("30/minute")
async def shared_credits(
    request: Request,  # noqa: ARG001 -- required by slowapi
    person1: str = Query(..., description="First person name"),
    person2: str = Query(..., description="Second person name"),
) -> JSONResponse:
    """Releases where two people are both credited."""
    if not _neo4j_driver:
        return JSONResponse(content={"error": "Service not ready"}, status_code=503)

    records = await get_shared_credits(_neo4j_driver, person1, person2)
    shared = [
        SharedCreditEntry(
            release_id=r["release_id"],
            title=r["title"] or "Unknown",
            year=r["year"],
            person1_role=r["person1_role"],
            person2_role=r["person2_role"],
            artists=r["artists"] or [],
        )
        for r in records
    ]
    response = SharedCreditsResponse(person1=person1, person2=person2, shared_releases=shared)
    return JSONResponse(content=response.model_dump())


@router.get("/api/credits/connections/{name}")
@limiter.limit("30/minute")
async def person_connections(
    request: Request,  # noqa: ARG001 -- required by slowapi
    name: str,
    depth: int = Query(2, ge=1, le=MAX_CONNECTION_DEPTH),
    limit: int = Query(50, ge=1, le=200),
) -> JSONResponse:
    """People connected through shared releases (collaboration graph).

    ``depth=1`` returns the connections. ``depth=2`` (the default) also returns each connection's ten best second
    hops, ranked by shared releases. There are no third hops, so ``depth=3`` is rejected with 422 rather than
    answered as 2.
    """
    if not _neo4j_driver:
        return JSONResponse(content={"error": "Service not ready"}, status_code=503)

    records = await get_person_connections(_neo4j_driver, name, depth, limit)
    connections = [
        ConnectionEntry(
            name=r["name"],
            shared_count=r["shared_count"],
            second_hops=[SecondHopEntry(**hop) for hop in r.get("second_hops") or []],
        )
        for r in records
    ]
    response = PersonConnectionsResponse(name=name, connections=connections)
    return JSONResponse(content=response.model_dump())


@router.get("/api/credits/autocomplete")
@limiter.limit("120/minute")
async def credits_autocomplete(
    request: Request,  # noqa: ARG001 -- required by slowapi
    q: str = Query(..., min_length=2, description="Search query"),
    limit: int = Query(10, ge=1, le=50),
) -> JSONResponse:
    """Search credits by person name."""
    if not _neo4j_driver:
        return JSONResponse(content={"error": "Service not ready"}, status_code=503)

    records = await autocomplete_person(_neo4j_driver, q, limit)
    results = [PersonAutocompleteEntry(name=r["name"], score=r["score"]) for r in records]
    return JSONResponse(content={"results": [r.model_dump() for r in results]})
