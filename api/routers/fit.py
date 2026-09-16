"""CrateFit — the item-in-hand fit profile for one candidate release.

``GET /api/fit/release/{release_id}`` answers the question a collector asks with a record
in their hands: *given everything I already own, is this one for me?* The answer is the
decomposition :mod:`api.fit` computes — five named components, each with evidence drawn
from the caller's own shelves — and this module is only the plumbing around it: two graph
reads, one precomputed rarity read, the cache, and the impression.

Two orderings here are load-bearing and both are inherited from the recommendation
surfaces:

* **Cache, then stamp.** The body cached in Redis never carries an ``impression_id``. An
  impression records a list having been *shown*, and the request that filled the cache is
  not the request that shows it to the next caller; reusing its id would report one
  showing where there were many and would hand every later viewer an id belonging to
  somebody else's impression. So every request — hit or miss — mints its own.
* **No native id is counted, never raised.** ADR 0010 keys an impression on the ADR 0009
  native id, and a release the alias table does not carry has none. The profile is still
  returned, with a null ``impression_id``, and the gap is counted through
  :func:`api.activity.count_unidentified_candidate` exactly as the recommendation surfaces
  count theirs.

Delegated access is on its own scope. ``fit:read`` is not folded into ``collection:read``
because the two authorise very different things: a kiosk scoring a record in a shop needs
to *use* the collection to answer, and has no business listing what is in it.
"""

from __future__ import annotations

from typing import Annotated, Any

import structlog
from common.identity import new_id
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

import api.activity as activity
from api.cache import RecommendCache
from api.dependencies import UnifiedAuth, require_user_or_app_token
from api.fit import compute_fit
from api.identity import native_ids_for
from api.limiter import limiter
from api.models import FitProfile
from api.queries.fit_queries import get_collection_ids, get_release_context, get_release_rarity


logger = structlog.get_logger(__name__)

router = APIRouter()

_neo4j_driver: Any = None
_pool: Any = None
_cache: RecommendCache | None = None

# Ten minutes, matching the collection read the profile is computed from: caching the body
# for longer than its inputs would serve a profile whose evidence the collector could
# already see was stale.
_FIT_CACHE_TTL = 600


def configure(neo4j: Any, pool: Any, redis: Any | None) -> None:
    """Configure the fit router with the Neo4j driver, the PostgreSQL pool, and Redis."""
    global _neo4j_driver, _pool, _cache
    _neo4j_driver = neo4j
    _pool = pool
    if redis is not None:
        _cache = RecommendCache(redis=redis, default_ttl=_FIT_CACHE_TTL)


def _cache_key(user_id: str, release_id: str) -> str:
    """The key one collector's profile of one release is cached under.

    Under the ``recommend:explore:{user_id}:…`` prefix on purpose: that is what
    :meth:`api.cache.RecommendCache.invalidate_user` sweeps, and a fit profile is a
    statement about a collection, so the sync that changes the collection has to be able
    to drop it.
    """
    return f"recommend:explore:{user_id}:fit:release:{release_id}"


async def _stamp_impression(user_id: str, body: dict[str, Any]) -> None:
    """Record this showing of this profile and stamp the body with its id.

    Runs per request served rather than per profile computed — see the module docstring.
    One item, at position one, scored by the fit itself, with the deterministic propensity
    the policy actually assigns: CrateFit ranks nothing and samples nothing, it answers
    about the one record the caller named.
    """
    body["impression_id"] = None
    native_id = body["release"].get("gm_id")
    if not native_id:
        activity.count_unidentified_candidate()
        return

    identifiers = await activity.record_impressions(
        user_id,
        activity.SURFACE_FIT,
        activity.POLICY_CRATEFIT,
        new_id(),
        [(1, native_id, float(body["fit"]), activity.DETERMINISTIC_PROPENSITY)],
    )
    body["impression_id"] = identifiers[0] if identifiers else None


@router.get("/api/fit/release/{release_id}")
@limiter.limit("30/minute")
async def release_fit(
    request: Request,  # noqa: ARG001 -- required by slowapi
    release_id: str,
    auth: Annotated[UnifiedAuth, Depends(require_user_or_app_token(["fit:read"]))],
) -> JSONResponse:
    """Return the decomposed fit of one candidate release for the calling collector.

    Reachable with a first-party session or a ``fit:read`` app token; either way the
    profile is computed against the caller's own collection, because a fit answer about
    somebody else's shelves is not an answer.
    """
    if not _neo4j_driver:
        return JSONResponse(content={"error": "Service not ready"}, status_code=503)

    user_id = auth.user_id
    cache_key = _cache_key(user_id, release_id)
    if _cache:
        cached = await _cache.get(cache_key)
        if cached is not None:
            await _stamp_impression(user_id, cached)
            return JSONResponse(content=cached)

    context = await get_release_context(_neo4j_driver, release_id)
    if context is None:
        return JSONResponse(content={"error": f"Release '{release_id}' not found"}, status_code=404)

    collection = await get_collection_ids(_neo4j_driver, user_id, cache=_cache)
    profile = compute_fit(collection, context)

    rarity = await get_release_rarity(_pool, release_id)
    gm_ids = await native_ids_for("release", [release_id])
    artists = context.get("artists") or []

    response = FitProfile.model_validate(
        {
            "release": {
                "id": context["id"],
                "gm_id": gm_ids.get(str(release_id)),
                "title": context.get("title"),
                "artist": (artists[0].get("name") if artists else None),
                "year": context.get("year"),
                "media_families": context.get("media_families") or [],
                "rarity": rarity,
            },
            "fit": profile["fit"],
            "components": profile["components"],
            "confidence": profile["confidence"],
            "policy_id": activity.POLICY_CRATEFIT,
            "fit_version": profile["fit_version"],
        }
    )
    body = response.model_dump()
    for component in body["components"].values():
        # Trim the unset fields Pydantic fills in as null: `api.fit` only ever sets the
        # keys an entry's kind actually uses, and the wire shape should read the same way.
        component["evidence_items"] = [{key: value for key, value in item.items() if value is not None} for item in component["evidence_items"]]

    # Cached before the impression is stamped, so the body in Redis never carries one.
    if _cache:
        await _cache.set(cache_key, body, ttl=_FIT_CACHE_TTL)

    await _stamp_impression(user_id, body)
    logger.info("🎯 Fit profile served", release_id=release_id, fit=body["fit"], confidence=body["confidence"], via=auth.via)
    return JSONResponse(content=body)
