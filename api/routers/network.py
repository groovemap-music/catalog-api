"""Collaboration Network endpoints — artist connection graph and centrality.

Provides multi-hop collaborator traversal, betweenness centrality scores,
and community/cluster detection around artists.
"""

import json
from typing import Any

import structlog
from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from neo4j.exceptions import ClientError as Neo4jClientError

from api.graph_backend import (
    GRAPH_BACKEND_ERROR_TYPES,
    CollaboratorsBackend,
    get_collaborators_backend,
    is_graph_backend_unavailable,
    is_graph_query_timeout,
)
from api.limiter import limiter
from api.queries import network_queries
from api.telemetry import CACHE_NETWORK_CENTRALITY, CACHE_NETWORK_CLUSTER, cache_get


logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/network", tags=["network"])

_neo4j: Any = None
_redis: Any = None
# The PostgreSQL pool, held alongside the Neo4j driver because the collaborators family is
# the one part of this router that can be served by either engine. Every other endpoint here
# is Neo4j-only (GDS centrality, cluster detection) and reads `_neo4j` directly.
_pg_pool: Any = None
_graph_backend: str = "neo4j"
# Resolved via the graph-backend selector; defaults to the Neo4j implementation so an
# unconfigured router (e.g. in tests) behaves exactly as it did before the seam existed.
_collaborators_backend: CollaboratorsBackend = network_queries

# Cache TTL for centrality and cluster results (1 hour — moderately expensive)
_NETWORK_CACHE_TTL = 3600


def configure(neo4j: Any, redis: Any = None, graph_backend: str = "neo4j", pg_pool: Any = None) -> None:
    """Configure the network router with database connections."""
    global _neo4j, _redis, _pg_pool, _graph_backend, _collaborators_backend
    _neo4j = neo4j
    _redis = redis
    _pg_pool = pg_pool
    _graph_backend = graph_backend
    _collaborators_backend = get_collaborators_backend(graph_backend)


def _collaborators_handle() -> Any:
    """Return the connection handle the resolved collaborators backend expects.

    Read at call time rather than frozen in `configure`, so the handle always tracks the
    module-level connection the rest of this router uses.
    """
    return _pg_pool if _graph_backend == "postgres" else _neo4j


@router.get("/artist/{artist_id}/collaborators")
@limiter.limit("30/minute")
async def artist_collaborators(
    request: Request,  # noqa: ARG001
    artist_id: str,
    depth: int = Query(2, ge=1, le=3),
    limit: int = Query(50, ge=1, le=200),
) -> JSONResponse:
    """Return direct and indirect collaborators via shared releases.

    Depth controls how many hops to traverse:
    - depth=1: direct collaborators only
    - depth=2: collaborators of collaborators (default)
    - depth=3: three hops out
    """
    handle = _collaborators_handle()
    if not handle:
        return JSONResponse(content={"error": "Service not ready"}, status_code=503)

    try:
        identity = await _collaborators_backend.get_artist_identity(handle, artist_id)
        if not identity:
            return JSONResponse(content={"error": f"Artist '{artist_id}' not found"}, status_code=404)

        collaborators = await _collaborators_backend.get_multi_hop_collaborators(
            handle,
            artist_id,
            depth=depth,
            limit=limit,
        )
        total = await _collaborators_backend.count_multi_hop_collaborators(
            handle,
            artist_id,
            depth=depth,
        )
    except GRAPH_BACKEND_ERROR_TYPES as exc:
        # Backend-neutral: `GRAPH_BACKEND_ERROR_TYPES` covers both the Neo4j driver's and
        # the PostgreSQL pool's exception hierarchies. `is_graph_query_timeout` and
        # `is_graph_backend_unavailable` tell the two failure kinds apart — a timed-out
        # query versus a backend that could not be reached at all — from a genuine backend
        # bug, which still re-raises to the same 500 both backends always produced.
        if is_graph_query_timeout(exc):
            logger.warning("⏱️ Network collaborators query timed out", artist_id=artist_id, depth=depth)
            return JSONResponse(
                content={"error": "Network collaborators query timed out — try reducing depth or limit"},
                status_code=504,
            )
        if is_graph_backend_unavailable(exc):
            logger.warning("🔌 Network collaborators graph backend unavailable", artist_id=artist_id, depth=depth)
            return JSONResponse(
                content={"error": "Graph backend unavailable — try again later"},
                status_code=503,
            )
        raise

    return JSONResponse(
        content={
            "artist_id": identity["artist_id"],
            "artist_name": identity["artist_name"],
            "depth": depth,
            "collaborators": collaborators,
            "total": total,
        }
    )


@router.get("/artist/{artist_id}/centrality")
@limiter.limit("30/minute")
async def artist_centrality(
    request: Request,  # noqa: ARG001
    artist_id: str,
) -> JSONResponse:
    """Return degree and collaboration centrality scores for an artist."""
    if not _neo4j:
        return JSONResponse(content={"error": "Service not ready"}, status_code=503)

    # Check Redis cache
    cache_key = f"network:centrality:{artist_id}"
    if _redis:
        try:
            cached = await cache_get(_redis, cache_key, cache=CACHE_NETWORK_CENTRALITY)
            if cached:
                return JSONResponse(content=json.loads(cached))
        except Exception:
            logger.debug("⚠️ Network centrality cache get failed", key=cache_key)

    try:
        result = await network_queries.get_artist_centrality(_neo4j, artist_id)
    except Neo4jClientError as exc:
        if "TransactionTimedOut" in str(exc):
            logger.warning("⏱️ Network centrality query timed out", artist_id=artist_id)
            return JSONResponse(
                content={"error": "Centrality query timed out — try again later"},
                status_code=504,
            )
        raise

    if not result:
        return JSONResponse(content={"error": f"Artist '{artist_id}' not found"}, status_code=404)

    response = {
        "artist_id": result["artist_id"],
        "artist_name": result["artist_name"],
        "centrality": {
            "degree": result["degree"],
            "collaborator_count": result["collaborator_count"],
            "collaboration_releases": result["collaboration_releases"],
            "group_count": result["group_count"],
            "alias_count": result["alias_count"],
        },
    }

    if _redis:
        try:
            await _redis.setex(cache_key, _NETWORK_CACHE_TTL, json.dumps(response))
        except Exception:
            logger.debug("⚠️ Network centrality cache set failed", key=cache_key)

    return JSONResponse(content=response)


@router.get("/cluster/{artist_id}")
@limiter.limit("30/minute")
async def artist_cluster(
    request: Request,  # noqa: ARG001
    artist_id: str,
    limit: int = Query(50, ge=1, le=200),
) -> JSONResponse:
    """Detect community/cluster around an artist.

    Groups connected artists by their primary genre to approximate
    community structure via shared-release co-occurrence.
    """
    if not _neo4j:
        return JSONResponse(content={"error": "Service not ready"}, status_code=503)

    # Check Redis cache
    cache_key = f"network:cluster:{artist_id}:{limit}"
    if _redis:
        try:
            cached = await cache_get(_redis, cache_key, cache=CACHE_NETWORK_CLUSTER)
            if cached:
                return JSONResponse(content=json.loads(cached))
        except Exception:
            logger.debug("⚠️ Network cluster cache get failed", key=cache_key)

    try:
        identity = await network_queries.get_artist_identity(_neo4j, artist_id)
        if not identity:
            return JSONResponse(content={"error": f"Artist '{artist_id}' not found"}, status_code=404)

        clusters = await network_queries.get_artist_cluster(_neo4j, artist_id, limit=limit)
    except Neo4jClientError as exc:
        if "TransactionTimedOut" in str(exc):
            logger.warning("⏱️ Network cluster query timed out", artist_id=artist_id)
            return JSONResponse(
                content={"error": "Cluster detection query timed out — try again later"},
                status_code=504,
            )
        raise

    response = {
        "artist_id": identity["artist_id"],
        "artist_name": identity["artist_name"],
        "clusters": clusters,
        "total_clusters": len(clusters),
        "total_members": sum(c["size"] for c in clusters),
    }

    if _redis:
        try:
            await _redis.setex(cache_key, _NETWORK_CACHE_TTL, json.dumps(response))
        except Exception:
            logger.debug("⚠️ Network cluster cache set failed", key=cache_key)

    return JSONResponse(content=response)
