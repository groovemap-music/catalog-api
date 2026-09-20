"""Credits & Provenance endpoints — the people behind the music."""

import json
from typing import Any

import structlog
from common.credit_roles import ALL_CATEGORIES
from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from api.graph_backend import (
    GRAPH_BACKEND_ERROR_TYPES,
    AutocompleteBackend,
    CreditsBackend,
    get_autocomplete_backend,
    get_credits_backend,
    is_graph_backend_unavailable,
    is_graph_query_timeout,
)
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
    SharedCreditEntry,
    SharedCreditsResponse,
    TimelineEntry,
)
from api.queries import autocomplete_queries, credits_queries
from api.telemetry import CACHE_CREDITS_LEADERBOARD, CACHE_CREDITS_PERSON, cache_get


logger = structlog.get_logger(__name__)

router = APIRouter()

_neo4j_driver: Any = None
_redis: Any = None
# The PostgreSQL pool, held alongside the Neo4j driver because every route on this router
# is now served by whichever backend `GRAPH_BACKEND` selects: the person search through the
# "autocomplete" family, the eight traversals through the "credits" family
# (`gm-catalog-api-dl8.1`).
_pg_pool: Any = None
_graph_backend: str = "neo4j"
# Resolved through the graph-backend selector; both default to the Neo4j implementation so
# an unconfigured router behaves exactly as it did before the seam existed.
_autocomplete_backend: AutocompleteBackend = autocomplete_queries
_credits_backend: CreditsBackend = credits_queries

# Every credits row's `category` column reads `CREDITED_ON.category` on Neo4j and
# `graph.credited_on.role_category` on PostgreSQL — one rename, absorbed entirely inside
# the two backends (see `api/queries/credits_pg_queries.py`). The column the handlers below
# read, the response models, and the cached payloads are all still spelled `category`.

# Redis cache TTL for credits (24 hours — data changes only on import)
_CREDITS_CACHE_TTL = 86400


def configure(neo4j: Any, redis: Any = None, graph_backend: str = "neo4j", pg_pool: Any = None) -> None:
    global _neo4j_driver, _redis, _pg_pool, _graph_backend, _autocomplete_backend, _credits_backend
    _neo4j_driver = neo4j
    _redis = redis
    _pg_pool = pg_pool
    _graph_backend = graph_backend
    _autocomplete_backend = get_autocomplete_backend(graph_backend)
    _credits_backend = get_credits_backend(graph_backend)


def _handle() -> Any:
    """Return the connection handle the resolved backends expect.

    Read at call time rather than frozen in `configure`, so the handle always tracks the
    module-level connection the rest of this router uses. Both families this router
    resolves take the same handle, because both are selected by the same `GRAPH_BACKEND`.
    """
    return _pg_pool if _graph_backend == "postgres" else _neo4j_driver


def _autocomplete_handle() -> Any:
    """Return the connection handle the resolved autocomplete backend expects."""
    return _handle()


def _backend_failure(exc: BaseException, what: str, **context: Any) -> JSONResponse:
    """Map a backend failure to a response, or re-raise it.

    Backend-neutral, the way `api/routers/network.py` is: a query that ran out of time is
    a 504 whichever engine timed it out, a backend that could not be reached at all is the
    same 503 a not-configured backend returns, and anything else is a genuine bug that
    still reaches the 500 both backends always produced.
    """
    if is_graph_query_timeout(exc):
        logger.warning("⏱️ Credits query timed out", query=what, **context)
        return JSONResponse(content={"error": f"{what} query timed out — try again with a narrower request"}, status_code=504)
    if is_graph_backend_unavailable(exc):
        logger.warning("🔌 Credits graph backend unavailable", query=what, **context)
        return JSONResponse(content={"error": "Graph backend unavailable — try again later"}, status_code=503)
    raise exc


# ── Person sub-routes MUST be declared before the catch-all {name} route ──


@router.get("/api/credits/person/{name}/timeline")
@limiter.limit("60/minute")
async def person_timeline(
    request: Request,  # noqa: ARG001 -- required by slowapi
    name: str,
) -> JSONResponse:
    """Credits over time — year-by-year activity."""
    handle = _handle()
    if not handle:
        return JSONResponse(content={"error": "Service not ready"}, status_code=503)

    try:
        records = await _credits_backend.get_person_timeline(handle, name)
    except GRAPH_BACKEND_ERROR_TYPES as exc:
        return _backend_failure(exc, "Person timeline", person=name)
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
    handle = _handle()
    if not handle:
        return JSONResponse(content={"error": "Service not ready"}, status_code=503)

    try:
        profile = await _credits_backend.get_person_profile(handle, name)
        if not profile:
            return JSONResponse(content={"error": f"Person '{name}' not found"}, status_code=404)

        role_breakdown = await _credits_backend.get_person_role_breakdown(handle, name)
    except GRAPH_BACKEND_ERROR_TYPES as exc:
        return _backend_failure(exc, "Person profile", person=name)

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


@router.get("/api/credits/person/{name}")
@limiter.limit("60/minute")
async def person_credits(
    request: Request,  # noqa: ARG001 -- required by slowapi
    name: str,
) -> JSONResponse:
    """All releases a person is credited on, grouped by role."""
    handle = _handle()
    if not handle:
        return JSONResponse(content={"error": "Service not ready"}, status_code=503)

    cache_key = f"credits:person:{name}"
    if _redis:
        try:
            cached = await cache_get(_redis, cache_key, cache=CACHE_CREDITS_PERSON)
            if cached:
                return JSONResponse(content=json.loads(cached))
        except Exception:
            logger.debug("⚠️ Credits person cache get failed", key=cache_key)

    try:
        records = await _credits_backend.get_person_credits(handle, name)
    except GRAPH_BACKEND_ERROR_TYPES as exc:
        return _backend_failure(exc, "Person credits", person=name)
    if not records:
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
    response = PersonCreditsResponse(name=name, total_credits=len(credits), credits=credits)
    response_data = response.model_dump()

    if _redis:
        try:
            await _redis.setex(cache_key, _CREDITS_CACHE_TTL, json.dumps(response_data, default=str))
        except Exception:
            logger.debug("⚠️ Credits person cache set failed", key=cache_key)

    return JSONResponse(content=response_data)


@router.get("/api/credits/release/{release_id}")
@limiter.limit("60/minute")
async def release_credits(
    request: Request,  # noqa: ARG001 -- required by slowapi
    release_id: str,
) -> JSONResponse:
    """Full credits breakdown for a release."""
    handle = _handle()
    if not handle:
        return JSONResponse(content={"error": "Service not ready"}, status_code=503)

    try:
        records = await _credits_backend.get_release_credits(handle, release_id)
    except GRAPH_BACKEND_ERROR_TYPES as exc:
        return _backend_failure(exc, "Release credits", release_id=release_id)
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
    handle = _handle()
    if not handle:
        return JSONResponse(content={"error": "Service not ready"}, status_code=503)

    if role not in ALL_CATEGORIES:
        return JSONResponse(
            content={"error": f"Invalid role category '{role}'. Valid: {', '.join(ALL_CATEGORIES)}"},
            status_code=400,
        )

    cache_key = f"credits:leaderboard:{role}:{limit}"
    if _redis:
        try:
            cached = await cache_get(_redis, cache_key, cache=CACHE_CREDITS_LEADERBOARD)
            if cached:
                return JSONResponse(content=json.loads(cached))
        except Exception:
            logger.debug("⚠️ Credits leaderboard cache get failed", key=cache_key)

    try:
        records = await _credits_backend.get_role_leaderboard(handle, role, limit)
    except GRAPH_BACKEND_ERROR_TYPES as exc:
        return _backend_failure(exc, "Role leaderboard", category=role)
    entries = [LeaderboardEntry(name=r["name"], credit_count=r["credit_count"]) for r in records]
    response = RoleLeaderboardResponse(category=role, entries=entries)
    response_data = response.model_dump()

    if _redis:
        try:
            await _redis.setex(cache_key, _CREDITS_CACHE_TTL, json.dumps(response_data, default=str))
        except Exception:
            logger.debug("⚠️ Credits leaderboard cache set failed", key=cache_key)

    return JSONResponse(content=response_data)


@router.get("/api/credits/shared")
@limiter.limit("30/minute")
async def shared_credits(
    request: Request,  # noqa: ARG001 -- required by slowapi
    person1: str = Query(..., description="First person name"),
    person2: str = Query(..., description="Second person name"),
) -> JSONResponse:
    """Releases where two people are both credited."""
    handle = _handle()
    if not handle:
        return JSONResponse(content={"error": "Service not ready"}, status_code=503)

    try:
        records = await _credits_backend.get_shared_credits(handle, person1, person2)
    except GRAPH_BACKEND_ERROR_TYPES as exc:
        return _backend_failure(exc, "Shared credits", person1=person1, person2=person2)
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
    depth: int = Query(2, ge=1, le=3),
    limit: int = Query(50, ge=1, le=200),
) -> JSONResponse:
    """People connected through shared releases (collaboration graph)."""
    handle = _handle()
    if not handle:
        return JSONResponse(content={"error": "Service not ready"}, status_code=503)

    try:
        records = await _credits_backend.get_person_connections(handle, name, depth, limit)
    except GRAPH_BACKEND_ERROR_TYPES as exc:
        return _backend_failure(exc, "Person connections", person=name, depth=depth)
    connections = [ConnectionEntry(name=r["name"], shared_count=r["shared_count"]) for r in records]
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
    handle = _autocomplete_handle()
    if not handle:
        return JSONResponse(content={"error": "Service not ready"}, status_code=503)

    try:
        records = await _autocomplete_backend.autocomplete_person(handle, q, limit)
    except GRAPH_BACKEND_ERROR_TYPES as exc:
        return _backend_failure(exc, "Person autocomplete", query=q)
    results = [PersonAutocompleteEntry(name=r["name"], score=r["score"]) for r in records]
    return JSONResponse(content={"results": [r.model_dump() for r in results]})
