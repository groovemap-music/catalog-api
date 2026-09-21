"""User endpoints — migrated from explore service."""

import asyncio
import time
from collections import OrderedDict
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

import api.activity as activity
from api.dependencies import UnifiedAuth, get_optional_user, require_user, require_user_or_app_token
from api.graph_backend import RecommendationsBackend, UserCollectionBackend, get_recommendations_backend, get_user_collection_backend
from api.identity import native_ids_for
from api.limiter import bearer_token_key_func, limiter
from api.queries.recommend_queries import (  # noqa: F401 -- preserve legacy patch points
    get_blindspot_candidates,
    get_collector_counts,
    get_label_affinity_candidates,
    merge_recommendation_candidates,
)
from api.queries.user_queries import (  # noqa: F401 -- preserve legacy patch points
    check_releases_user_status,
    get_user_collection,
    get_user_collection_evolution,
    get_user_collection_stats,
    get_user_collection_timeline,
    get_user_recommendations,
    get_user_wantlist,
)


logger = structlog.get_logger(__name__)

router = APIRouter()

_neo4j_driver: Any = None
_pg_pool: Any = None
_graph_backend = "neo4j"
_user_backend: UserCollectionBackend = get_user_collection_backend("neo4j")
_recommendations_backend: RecommendationsBackend = get_recommendations_backend("neo4j")

# In-memory cache for timeline/evolution queries (keyed by user_id + params)
_timeline_cache: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()
_TIMELINE_CACHE_MAX = 128
_TIMELINE_CACHE_TTL = 300  # 5 minutes
_timeline_cache_lock: asyncio.Lock | None = None  # lazy init to avoid binding to wrong event loop


def configure(neo4j: Any, jwt_secret: str | None, graph_backend: str = "neo4j", pg_pool: Any = None) -> None:  # noqa: ARG001
    global _neo4j_driver, _pg_pool, _graph_backend, _user_backend, _recommendations_backend
    _neo4j_driver = neo4j
    _pg_pool = pg_pool
    _graph_backend = graph_backend
    _user_backend = get_user_collection_backend(graph_backend)
    _recommendations_backend = get_recommendations_backend(graph_backend)


def _handle() -> Any:
    return _pg_pool if _graph_backend == "postgres" else _neo4j_driver


def _query(name: str) -> Any:
    return getattr(_user_backend, name) if _graph_backend == "postgres" else globals()[name]


def _recommend_query(name: str) -> Any:
    return getattr(_recommendations_backend, name) if _graph_backend == "postgres" else globals()[name]


async def _attach_recommendation_identity(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add the ADR 0009 native id to each recommended release.

    One alias lookup for the whole page. A candidate whose Discogs id has no valid alias
    yet keeps its provider `id` and carries `gm_id: None`, so a consumer can adopt native
    identity incrementally instead of waiting for the projection to be complete.
    """
    if not items:
        return items
    gm_ids = await native_ids_for("release", [item["id"] for item in items if item.get("id")])
    for item in items:
        item["gm_id"] = gm_ids.get(str(item.get("id")))
    return items


def _get_cached(key: str) -> dict[str, Any] | None:
    """Get from cache. Caller must hold _timeline_cache_lock."""
    entry = _timeline_cache.get(key)
    if entry is None:
        return None
    ts, data = entry
    if time.monotonic() - ts > _TIMELINE_CACHE_TTL:
        _timeline_cache.pop(key, None)
        return None
    _timeline_cache.move_to_end(key)
    return data


def _set_cached(key: str, data: dict[str, Any]) -> None:
    """Write to cache. Caller must hold _timeline_cache_lock."""
    _timeline_cache[key] = (time.monotonic(), data)
    _timeline_cache.move_to_end(key)
    while len(_timeline_cache) > _TIMELINE_CACHE_MAX:
        _timeline_cache.popitem(last=False)


@router.get("/api/user/collection")
@limiter.limit("60/minute", key_func=bearer_token_key_func)
@limiter.limit("600/hour", key_func=bearer_token_key_func)
async def user_collection(
    request: Request,  # noqa: ARG001 — required by slowapi rate limiter
    auth: Annotated[UnifiedAuth, Depends(require_user_or_app_token(["collection:read"]))],
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> JSONResponse:
    if not _handle():
        return JSONResponse(content={"error": "Service not ready"}, status_code=503)
    user_id = auth.user_id
    results, total = await _query("get_user_collection")(_handle(), user_id, limit, offset)
    return JSONResponse(
        content={
            "user_id": user_id,
            "releases": results,
            "total": total,
            "offset": offset,
            "limit": limit,
            "has_more": offset + len(results) < total,
        }
    )


@router.get("/api/user/wantlist")
async def user_wantlist(
    current_user: Annotated[dict[str, Any], Depends(require_user)],
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> JSONResponse:
    if not _handle():
        return JSONResponse(content={"error": "Service not ready"}, status_code=503)
    user_id: str = current_user.get("sub", "")
    results, total = await _query("get_user_wantlist")(_handle(), user_id, limit, offset)
    return JSONResponse(content={"releases": results, "total": total, "offset": offset, "limit": limit, "has_more": offset + len(results) < total})


@router.get("/api/user/recommendations")
async def user_recommendations(
    current_user: Annotated[dict[str, Any], Depends(require_user)],
    limit: int = Query(20, ge=1, le=100),
    strategy: str = Query("artist", pattern="^(artist|multi)$"),
) -> JSONResponse:
    """Recommend releases from the caller's collection, by one of two strategies.

    Each strategy is its own ranking policy under ADR 0010, so the impressions it writes
    carry their own policy id and every returned item carries the `impression_id` a client
    reports an outcome against. This response is not cached, so the ids are minted on the
    one request that shows the list.
    """
    if not _handle():
        return JSONResponse(content={"error": "Service not ready"}, status_code=503)
    user_id: str = current_user.get("sub", "")

    if strategy == "artist":
        results = await _query("get_user_recommendations")(_handle(), user_id, limit)
        # Normalize raw count scores to 0-1 range
        if results:
            max_score = max(r.get("score", 0) for r in results)
            if max_score > 0:
                for r in results:
                    r["score"] = round(r.get("score", 0) / max_score, 4)
        await _attach_recommendation_identity(results)
        await activity.stamp_recommendation_impressions(user_id, activity.POLICY_USER_RECOMMENDATIONS_ARTIST, results)
        return JSONResponse(content={"recommendations": results, "total": len(results)})

    # Multi-signal strategy
    artist_results, label_results, blindspot_results = await asyncio.gather(
        _query("get_user_recommendations")(_handle(), user_id, limit=50),
        _recommend_query("get_label_affinity_candidates")(_handle(), user_id, limit=50),
        _recommend_query("get_blindspot_candidates")(_handle(), user_id, limit=50),
    )

    # Normalize artist results to candidate format
    artist_candidates = [
        {
            "id": r["id"],
            "title": r.get("title"),
            "artist": r.get("artist"),
            "label": r.get("label"),
            "year": r.get("year"),
            "genres": r.get("genres", []),
            "score": r.get("score", 0),
            "source": f"artist: collected {r.get('score', 0)} releases",
        }
        for r in artist_results
    ]

    # Collect all unique release IDs for obscurity scoring
    all_ids = list({c["id"] for candidates in [artist_candidates, label_results, blindspot_results] for c in candidates if c.get("id")})
    collector_counts = await _recommend_query("get_collector_counts")(_handle(), all_ids) if all_ids else {}

    merged = merge_recommendation_candidates(
        artist_candidates,
        label_results,
        blindspot_results,
        collector_counts=collector_counts,
        limit=limit,
    )

    await _attach_recommendation_identity(merged)
    await activity.stamp_recommendation_impressions(user_id, activity.POLICY_USER_RECOMMENDATIONS_MULTI, merged)

    return JSONResponse(
        content={
            "recommendations": merged,
            "total": len(merged),
            "strategy": "multi",
        }
    )


@router.get("/api/user/collection/stats")
@limiter.limit("60/minute", key_func=bearer_token_key_func)
@limiter.limit("600/hour", key_func=bearer_token_key_func)
async def user_collection_stats(
    request: Request,  # noqa: ARG001 — required by slowapi rate limiter
    auth: Annotated[UnifiedAuth, Depends(require_user_or_app_token(["collection:read"]))],
) -> JSONResponse:
    if not _handle():
        return JSONResponse(content={"error": "Service not ready"}, status_code=503)
    user_id = auth.user_id
    stats = await _query("get_user_collection_stats")(_handle(), user_id)
    # stats is a dict from the query layer (contract); merge user_id at the top level.
    return JSONResponse(content={"user_id": user_id, **stats})


@router.get("/api/user/collection/timeline")
@limiter.limit("60/minute", key_func=bearer_token_key_func)
@limiter.limit("600/hour", key_func=bearer_token_key_func)
async def user_collection_timeline(
    request: Request,  # noqa: ARG001 — required by slowapi rate limiter
    auth: Annotated[UnifiedAuth, Depends(require_user_or_app_token(["collection:read"]))],
    bucket: str = Query("year", pattern="^(year|decade)$"),
) -> JSONResponse:
    if not _handle():
        return JSONResponse(content={"error": "Service not ready"}, status_code=503)
    user_id = auth.user_id
    global _timeline_cache_lock
    if _timeline_cache_lock is None:
        _timeline_cache_lock = asyncio.Lock()
    cache_key = f"timeline:{user_id}:{bucket}"
    async with _timeline_cache_lock:
        cached = _get_cached(cache_key)
    if cached is not None:
        # cached value already includes user_id (we put it there on first write).
        return JSONResponse(content=cached)
    result = await _query("get_user_collection_timeline")(_handle(), user_id, bucket)
    payload: dict[str, Any] = {"user_id": user_id, **result}
    async with _timeline_cache_lock:
        _set_cached(cache_key, payload)
    return JSONResponse(content=payload)


@router.get("/api/user/collection/evolution")
async def user_collection_evolution(
    current_user: Annotated[dict[str, Any], Depends(require_user)],
    metric: str = Query("genre", pattern="^(genre|style|label)$"),
) -> JSONResponse:
    if not _handle():
        return JSONResponse(content={"error": "Service not ready"}, status_code=503)
    user_id: str = current_user.get("sub", "")
    global _timeline_cache_lock
    if _timeline_cache_lock is None:
        _timeline_cache_lock = asyncio.Lock()
    cache_key = f"evolution:{user_id}:{metric}"
    async with _timeline_cache_lock:
        cached = _get_cached(cache_key)
    if cached is not None:
        return JSONResponse(content=cached)
    result = await _query("get_user_collection_evolution")(_handle(), user_id, metric)
    async with _timeline_cache_lock:
        _set_cached(cache_key, result)
    return JSONResponse(content=result)


@router.get("/api/user/status")
async def user_release_status(
    ids: str = Query(...),
    current_user: Annotated[dict[str, Any] | None, Depends(get_optional_user)] = None,
) -> JSONResponse:
    release_ids = [rid.strip() for rid in ids.split(",") if rid.strip()]
    if not release_ids:
        return JSONResponse(content={"status": {}})
    if len(release_ids) > 100:
        return JSONResponse(content={"error": "Too many IDs: maximum is 100"}, status_code=422)
    if not _handle() or current_user is None:
        return JSONResponse(content={"status": {rid: {"in_collection": False, "in_wantlist": False} for rid in release_ids}})
    user_id: str = current_user.get("sub", "")
    status_map = await _query("check_releases_user_status")(_handle(), user_id, release_ids)
    result = {rid: status_map.get(rid, {"in_collection": False, "in_wantlist": False}) for rid in release_ids}
    return JSONResponse(content={"status": result})
