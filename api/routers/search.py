"""Search endpoint -- unified full-text search across all entity types."""

from typing import Annotated, Any

import structlog
from common.identity import new_id
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

import api.activity as activity
from api.dependencies import get_optional_user
from api.limiter import limiter
from api.queries.search_queries import ALL_TYPES, execute_search, split_media_filter


logger = structlog.get_logger(__name__)

router = APIRouter()

_pool: Any = None
_redis: Any = None


def configure(pool: Any, redis: Any) -> None:
    """Wire database pool and Redis client into the search router."""
    global _pool, _redis
    _pool = pool
    _redis = redis


_VALID_TYPES = set(ALL_TYPES)

# The two event types this surface emits (ADR 0010).
EVENT_SEARCH_QUERY = "search.query"
EVENT_SEARCH_RESULT_IMPRESSION = "search.result_impression"


def _search_filters(
    requested_types: list[str],
    genres: list[str],
    media: list[str],
    year_min: int | None,
    year_max: int | None,
) -> list[str]:
    """Render everything that narrowed the search as the schema's flat string array.

    The published `search.query` payload is closed over `query`, `filters`,
    `result_count`, and `request_id`, so the entity-type restriction the epic design named
    as its own `types` field has nowhere of its own to go. It is a filter on the query in
    every sense that matters to an analysis, so it travels in `filters` under a `type:`
    prefix beside the genre, media, and year bounds rather than being dropped.
    """
    applied = {f"type:{entity_type}" for entity_type in requested_types}
    applied.update(f"genre:{genre}" for genre in genres)
    applied.update(f"media:{medium}" for medium in media)
    if year_min is not None:
        applied.add(f"year_min:{year_min}")
    if year_max is not None:
        applied.add(f"year_max:{year_max}")
    return sorted(applied)


async def _record_search(user_id: str, request_id: str, q: str, filters: list[str], results: list[dict[str, Any]]) -> None:
    """Record the query and one result impression per hit shown.

    `search.result_impression` is per result rather than one event carrying the ordered
    ids: the published payload is closed over `impression_id`, `item_id`, `position`, and
    `request_id`, and its description names a single result at a single position. The
    whole page therefore goes down as one batch, which keeps a twenty-hit page at one
    round trip.

    The `impression_id` identifies the shown-result event itself. Search is not a ranked
    recommendation surface, so it has no row in `activity.impressions` for the id to name,
    and it is stamped onto the hit so a client can report an outcome against it.

    A hit whose provider id has no native id yet is counted and skipped: the payload
    requires `item_id` and carries no provider-id field.
    """
    await activity.record_event(
        user_id,
        EVENT_SEARCH_QUERY,
        {"query": q, "filters": filters, "result_count": len(results), "request_id": request_id},
        idempotency_key=f"{EVENT_SEARCH_QUERY}:{request_id}",
    )

    impressions = []
    for position, hit in enumerate(results, start=1):
        native_id = hit.get("gm_id")
        if not native_id:
            hit["impression_id"] = None
            activity.count_unidentified_candidate()
            continue
        impression_id = str(new_id())
        hit["impression_id"] = impression_id
        impressions.append(
            (
                EVENT_SEARCH_RESULT_IMPRESSION,
                {"impression_id": impression_id, "item_id": native_id, "position": position, "request_id": request_id},
                f"{EVENT_SEARCH_RESULT_IMPRESSION}:{request_id}:{position}",
            )
        )
    await activity.record_events(user_id, impressions)


@router.get("/api/search")
@limiter.limit("30/minute")
async def search(
    request: Request,  # noqa: ARG001 -- required by slowapi
    current_user: Annotated[dict[str, Any] | None, Depends(get_optional_user)] = None,
    q: str = Query(..., min_length=3, description="Search query (minimum 3 characters)"),
    types: str = Query(
        default="artist,label,master,release",
        description="Comma-separated entity types to search",
    ),
    genres: str = Query(default="", description="Comma-separated genre filter"),
    media: list[str] = Query(
        default=[],
        description="Repeated media family or medium id (ADR 0007) to filter release results",
    ),
    year_min: int | None = Query(default=None, ge=1000, le=9999, description="Minimum release year"),
    year_max: int | None = Query(default=None, ge=1000, le=9999, description="Maximum release year"),
    limit: int = Query(default=20, ge=1, le=100, description="Results per page"),
    offset: int = Query(default=0, ge=0, description="Pagination offset"),
) -> JSONResponse:
    """Search across artists, labels, masters, and releases using PostgreSQL full-text search.

    Returns relevance-ranked results with facet counts and result highlighting.
    Results are cached in Redis for 5 minutes.
    Rate limited to 30 requests/minute.
    """
    if _pool is None:
        return JSONResponse(content={"error": "Service not ready"}, status_code=503)

    # Parse and validate types
    requested_types = [t.strip().lower() for t in types.split(",") if t.strip()]
    if not requested_types:
        requested_types = list(ALL_TYPES)
    invalid = [t for t in requested_types if t not in _VALID_TYPES]
    if invalid:
        return JSONResponse(
            content={"error": f"Invalid type(s): {', '.join(invalid)}. Valid: {', '.join(sorted(_VALID_TYPES))}"},
            status_code=400,
        )

    # Parse genre filter
    genre_list = [g.strip() for g in genres.split(",") if g.strip()] if genres else []

    # Parse and validate media filter (ADR 0007 family/medium ids)
    media_list = [m.strip() for m in media if m and m.strip()]
    media_families, media_mediums, unknown_media = split_media_filter(media_list)
    if unknown_media:
        return JSONResponse(
            content={"error": f"Invalid media id(s): {', '.join(unknown_media)}"},
            status_code=400,
        )

    logger.debug(
        "🔍 Search request",
        q=q,
        types=requested_types,
        genres=genre_list,
        media=media_list,
        year_min=year_min,
        year_max=year_max,
    )

    result = await execute_search(
        pool=_pool,
        redis=_redis,
        q=q,
        types=requested_types,
        genres=genre_list,
        year_min=year_min,
        year_max=year_max,
        limit=limit,
        offset=offset,
        media_families=media_families,
        media_mediums=media_mediums,
    )

    # ADR 0010 records the search only for a caller the service can pseudonymise. An
    # anonymous search has no subject, so it leaves no behavioural record at all rather
    # than an unattributable one.
    user_id = (current_user or {}).get("sub", "")
    if user_id:
        await _record_search(
            user_id,
            str(new_id()),
            q,
            _search_filters(requested_types, genre_list, media_list, year_min, year_max),
            result.get("results", []),
        )

    return JSONResponse(content=result)
