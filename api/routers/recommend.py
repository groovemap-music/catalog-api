"""Recommend endpoints — artist similarity, explore from here, enhanced recommendations."""

import asyncio
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

import api.activity as activity
from api.cache import RecommendCache
from api.config import DEFAULT_FUSION_ALPHA
from api.dependencies import get_optional_user, require_user
from api.identity import NativeIdCache, catalog_ref, native_ids_for, native_ids_for_pairs
from api.limiter import limiter
from api.models import (
    DiscoveryNode,
    EntityRef,
    ExploreFromHereResponse,
    SimilarArtist,
    SimilarArtistsResponse,
)
from api.queries.recommend_queries import (
    MIN_ARTIST_RELEASES,
    compute_similar_artists,
    get_artist_identity,
    get_artist_profile,
    get_candidate_artists,
    get_explore_traversal,
    get_profiled_artists,
    score_discoveries,
)
from api.queries.similar_precomputed_queries import (
    PrecomputedList,
    fuse,
    get_current_list,
    get_embedding_cosines,
)
from api.queries.taste_queries import get_blind_spots, get_taste_heatmap


logger = structlog.get_logger(__name__)

router = APIRouter()

_neo4j_driver: Any = None
_cache: RecommendCache | None = None
_pg_pool: Any = None
_precomputed_enabled: bool = False
_fusion_alpha: float = DEFAULT_FUSION_ALPHA


def configure(
    neo4j: Any,
    jwt_secret: str | None,  # noqa: ARG001
    valkey: Any | None,
    *,
    pg_pool: Any = None,
    precomputed_enabled: bool = False,
    fusion_alpha: float = DEFAULT_FUSION_ALPHA,
) -> None:
    """Configure the recommend router with Neo4j driver, Valkey cache, and the precomputed-list switch.

    ``precomputed_enabled`` defaults to off: the similar-artist endpoint serves the heuristic
    until the maintainer flips it. ``fusion_alpha`` weights the embedding cosine.
    """
    global _neo4j_driver, _cache, _pg_pool, _precomputed_enabled, _fusion_alpha
    _neo4j_driver = neo4j
    _pg_pool = pg_pool
    _precomputed_enabled = precomputed_enabled
    _fusion_alpha = fusion_alpha
    if valkey is not None:
        _cache = RecommendCache(valkey=valkey, default_ttl=3600)


_VALID_ENTITY_TYPES = {"artist", "label", "genre", "style"}

_SIMILARITY_CACHE_TTL = 86400  # 24 hours
_EXPLORE_CACHE_TTL = 3600  # 1 hour


async def _record_similar_impressions(
    current_user: dict[str, Any] | None,
    similar: list[dict[str, Any]],
    precomputed: PrecomputedList | None = None,
) -> None:
    """Record the similar-artist list as shown, keyed on its own similarity score and ranking policy."""
    await activity.stamp_recommendation_impressions(
        (current_user or {}).get("sub", ""),
        activity.POLICY_SIMILAR_ARTIST if precomputed is None else activity.POLICY_SIMILAR_ARTIST_PRECOMPUTED,
        similar,
        score_key="similarity",
    )


def _similar_cache_key(artist_id: str, precomputed: PrecomputedList | None) -> str:
    if precomputed is None:
        return f"recommend:similar:artist:{artist_id}"
    return f"recommend:similar:artist:{artist_id}:precomputed:{precomputed.model_version}:{_fusion_alpha}"


async def _rank_precomputed(artist_id: str, precomputed: PrecomputedList) -> list[dict[str, Any]] | None:
    """Rank the artist's precomputed neighbours, fused with the heuristic unless alpha is 1.0.

    Returns ``None`` when the fusion cannot read the stored vectors it needs, so the caller
    serves the heuristic rather than a ranking built on half the inputs.
    """
    neighbours = {i: score for i, score in precomputed.neighbours if i != artist_id}
    alpha = _fusion_alpha

    if alpha >= 1.0:
        # The pure precomputed ranking: the batch's own order and cosine, names from the graph.
        profiled = {row["artist_id"]: row for row in await get_profiled_artists(_neo4j_driver, list(neighbours), with_profiles=False)}
        return [_ranked_row(profiled[i], score, cos=score, heuristic=None) for i, score in neighbours.items() if i in profiled]

    target_profile, candidates = await asyncio.gather(
        get_artist_profile(_neo4j_driver, artist_id),
        get_candidate_artists(_neo4j_driver, artist_id),
    )
    heuristic = {row["artist_id"]: row for row in compute_similar_artists(target_profile, candidates, limit=50)}

    # Precomputed neighbours the heuristic did not surface still need a heuristic score (and
    # a name), so they are profiled and scored the same way rather than counted as zero.
    extra = await get_profiled_artists(_neo4j_driver, [i for i in neighbours if i not in heuristic])
    profiled = {row["artist_id"]: row for row in extra}
    for row in compute_similar_artists(target_profile, extra, limit=len(extra)):
        heuristic[row["artist_id"]] = row

    heuristic_only = [i for i in heuristic if i not in neighbours]
    cosines = await get_embedding_cosines(_pg_pool, precomputed.model_version, artist_id, heuristic_only)
    if cosines is None:
        return None

    names = {**profiled, **heuristic}
    fused = fuse(neighbours, heuristic, cosines, alpha=alpha)
    return [_ranked_row(names[i], score, cos=cos, heuristic=heuristic.get(i)) for i, score, cos, _h in fused if i in names]


def _ranked_row(source: dict[str, Any], score: float, *, cos: float, heuristic: dict[str, Any] | None) -> dict[str, Any]:
    """A ``SimilarArtist``-shaped row; ``breakdown`` keeps the heuristic dimensions and adds ``embedding``."""
    dims = dict(heuristic["breakdown"]) if heuristic else dict.fromkeys(("genre", "style", "label", "collaborator"), 0.0)
    return {
        "artist_id": source["artist_id"],
        "artist_name": source["artist_name"],
        "similarity": round(score, 4),
        "breakdown": {**dims, "embedding": round(cos, 4)},
        "release_count": source.get("release_count", 0),
        "shared_genres": heuristic["shared_genres"] if heuristic else [],
        "shared_labels": heuristic["shared_labels"] if heuristic else [],
    }


@router.get("/api/recommend/similar/artist/{artist_id}")
@limiter.limit("30/minute")
async def similar_artists(
    request: Request,  # noqa: ARG001 -- required by slowapi
    artist_id: str,
    current_user: Annotated[dict[str, Any] | None, Depends(get_optional_user)] = None,
    limit: int = Query(20, ge=1, le=50),
) -> JSONResponse:
    """Find artists with the closest multi-dimensional similarity to the given artist.

    Open to anonymous callers, so the impression is recorded only when the caller carries
    a token: a showing the service cannot pseudonymise leaves no behavioural record.
    """
    if not _neo4j_driver:
        return JSONResponse(content={"error": "Service not ready"}, status_code=503)

    # The precomputed list is a primary-key read, so it is looked up before the cache: the
    # cache key must name the model version and alpha that produced a body, or flipping the
    # switch (or a monthly release) would keep serving the other path's cached answer.
    precomputed = await get_current_list(_pg_pool, artist_id) if _precomputed_enabled else None
    cache_key = _similar_cache_key(artist_id, precomputed)
    if _cache:
        cached = await _cache.get(cache_key)
        if cached is not None:
            cached["similar"] = cached["similar"][:limit]
            await _record_similar_impressions(current_user, cached["similar"], precomputed)
            return JSONResponse(content=cached)

    identity = await get_artist_identity(_neo4j_driver, artist_id)
    if not identity:
        return JSONResponse(content={"error": f"Artist '{artist_id}' not found"}, status_code=404)

    if identity["release_count"] < MIN_ARTIST_RELEASES:
        return JSONResponse(
            content={"error": f"Artist '{artist_id}' has fewer than {MIN_ARTIST_RELEASES} releases"},
            status_code=422,
        )

    ranked = await _rank_precomputed(artist_id, precomputed) if precomputed else None
    if ranked is None:
        # No list to serve (or the fusion could not read the vectors it needs): the
        # heuristic path, under its own cache key.
        precomputed = None
        cache_key = _similar_cache_key(artist_id, None)
        target_profile, candidates = await asyncio.gather(
            get_artist_profile(_neo4j_driver, artist_id),
            get_candidate_artists(_neo4j_driver, artist_id),
        )
        ranked = compute_similar_artists(target_profile, candidates, limit=50)

    # One alias lookup for the ranked page. The similarity itself is computed on the graph,
    # where identity is only a projection, so the native id comes from the alias table.
    gm_ids = await native_ids_for("artist", [r["artist_id"] for r in ranked if r.get("artist_id")])

    response = SimilarArtistsResponse(
        artist_id=identity["artist_id"],
        artist_name=identity["artist_name"],
        similar=[SimilarArtist(**r, gm_id=gm_ids.get(str(r.get("artist_id")))) for r in ranked],
    )
    response_data = response.model_dump()

    # Cached before the ids are stamped, so the body in Valkey never carries one: an
    # impression records a list having been shown, and the request that filled the cache
    # is not the request that shows it to the next caller.
    if _cache:
        await _cache.set(cache_key, response_data, ttl=_SIMILARITY_CACHE_TTL)

    response_data["similar"] = response_data["similar"][:limit]
    await _record_similar_impressions(current_user, response_data["similar"], precomputed)
    return JSONResponse(content=response_data)


@router.get("/api/recommend/explore/{entity_type}/{entity_id}")
@limiter.limit("30/minute")
async def explore_from_here(
    request: Request,  # noqa: ARG001 -- required by slowapi
    entity_type: str,
    entity_id: str,
    current_user: Annotated[dict[str, Any], Depends(require_user)],
    hops: int = Query(2, ge=1, le=3),
    limit: int = Query(10, ge=1, le=50),
) -> JSONResponse:
    """Personalized multi-hop traversal from an entity, ranked by user taste."""
    if not _neo4j_driver:
        return JSONResponse(content={"error": "Service not ready"}, status_code=503)

    if entity_type not in _VALID_ENTITY_TYPES:
        return JSONResponse(
            content={"error": f"Invalid entity_type '{entity_type}'. Must be one of: {', '.join(sorted(_VALID_ENTITY_TYPES))}"},
            status_code=400,
        )

    user_id: str = current_user.get("sub", "")

    # Check cache — hops changes the traversal depth (not merely a truncation
    # like limit), so it MUST be part of the key or callers get results computed
    # for a different hop depth.
    cache_key = f"recommend:explore:{user_id}:{entity_type}:{entity_id}:{hops}"
    if _cache:
        cached = await _cache.get(cache_key)
        if cached is not None:
            cached["discoveries"] = cached["discoveries"][:limit]
            await activity.stamp_recommendation_impressions(user_id, activity.POLICY_EXPLORE, cached["discoveries"])
            return JSONResponse(content=cached)

    # Run traversal and user taste queries in parallel
    traversal_results, heatmap_result, blind_spots_raw = await asyncio.gather(
        get_explore_traversal(_neo4j_driver, entity_type, entity_id, hops=hops),
        get_taste_heatmap(_neo4j_driver, user_id),
        get_blind_spots(_neo4j_driver, user_id),
    )

    # Build flat genre vector from heatmap (aggregate across decades)
    cells, _total = heatmap_result
    genre_counts: dict[str, int] = {}
    for cell in cells:
        genre_counts[cell["genre"]] = genre_counts.get(cell["genre"], 0) + cell["count"]
    total_genre = sum(genre_counts.values())
    user_genre_vector = {g: c / total_genre for g, c in genre_counts.items()} if total_genre else {}

    blind_spot_genres = {bs["genre"] for bs in blind_spots_raw}

    scored = score_discoveries(traversal_results, user_genre_vector, blind_spot_genres, limit=50)

    # Determine the starting entity name from the first path element or use entity_id
    from_name = entity_id
    if traversal_results and traversal_results[0].get("path_names"):
        from_name = str(traversal_results[0]["path_names"][0])

    # The traversal's starting entity is frequently named again among the discoveries, and
    # a request-scoped cache is what keeps that from costing a second lookup.
    identity_cache = NativeIdCache()
    gm_ids = await native_ids_for_pairs(
        [(entity_type, entity_id), *((s["type"], s["id"]) for s in scored)],
        cache=identity_cache,
    )
    # Genre and style nodes are name-keyed rather than catalog entities, so `catalog_ref`
    # returns None for them and their `gm_id` stays None by construction.
    from_ref = catalog_ref(entity_type, entity_id)

    response = ExploreFromHereResponse.model_validate(
        {
            "from": EntityRef(
                id=entity_id,
                name=from_name,
                type=entity_type,
                gm_id=gm_ids.get((entity_type, from_ref.external_id)) if from_ref else None,
            ),
            "discoveries": [DiscoveryNode(**s, gm_id=gm_ids.get((s["type"], str(s["id"])))) for s in scored],
        }
    )
    response_data = response.model_dump(by_alias=True)

    # Cached before the ids are stamped, for the same reason as the similarity body above.
    if _cache:
        await _cache.set(cache_key, response_data, ttl=_EXPLORE_CACHE_TTL)

    response_data["discoveries"] = response_data["discoveries"][:limit]
    await activity.stamp_recommendation_impressions(user_id, activity.POLICY_EXPLORE, response_data["discoveries"])
    return JSONResponse(content=response_data)
