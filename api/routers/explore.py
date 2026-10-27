"""Explore endpoints — migrated from explore service."""

import asyncio
import json
import time
from collections import OrderedDict
from collections.abc import Coroutine
from typing import Annotated, Any

import structlog
from common.media import legacy_format_names_to_media
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from neo4j.exceptions import ClientError as Neo4jClientError
from psycopg.errors import QueryCanceled

from api.dependencies import get_optional_user
from api.limiter import limiter
from api.models import PathNode, PathResponse
from api.queries import collaborator_queries, genre_tree_queries
from api.queries.neo4j_queries import (
    AUTOCOMPLETE_DISPATCH,
    COUNT_DISPATCH,
    DETAILS_DISPATCH,
    EXPAND_DISPATCH,
    EXPLORE_DISPATCH,
    TRENDS_DISPATCH,
    find_shortest_path,
    get_genre_emergence,
    get_graph_stats,
    get_year_range,
    node_label_to_type,
)
from api.queries.neo4j_queries import (
    DEFAULT_PATH_DEPTH as _DEFAULT_PATH_DEPTH,
)
from api.queries.neo4j_queries import (
    MAX_PATH_DEPTH as _MAX_PATH_DEPTH,
)
from api.queries.release_media_queries import get_release_catalog_blocks, get_release_media
from api.queries.strata_queries import get_shared_personnel, get_strata_catalog, get_strata_labels, get_strata_marks, get_strata_root
from api.strata import (
    DEFAULT_BUDGET,
    MAX_ROOT_RELEASES,
    Budget,
    BudgetError,
    Catalog,
    FocusNotFoundError,
    Mark,
    apply_labels,
    build_strata,
    decode_catalog,
    encode_catalog,
    label_targets,
)
from api.telemetry import CACHE_EXPLORE, CACHE_STRATA, CACHE_TRENDS, cache_get, record_cache_write_failure


logger = structlog.get_logger(__name__)

router = APIRouter()

_neo4j_driver: Any = None
_valkey: Any = None
_pg_pool: Any = None

# Valkey cache TTL for trends (genre/style/label) and explore (artist/label)
# 24 hours — data changes only on import
_TRENDS_CACHE_TTL = 86400
_EXPLORE_CACHE_TTL = 86400


def configure(neo4j: Any, jwt_secret: str | None, valkey: Any = None, pg_pool: Any = None) -> None:  # noqa: ARG001
    global _neo4j_driver, _valkey, _pg_pool
    _neo4j_driver = neo4j
    _valkey = valkey
    _pg_pool = pg_pool


_autocomplete_cache: OrderedDict[tuple[str, str, int], list[dict[str, Any]]] = OrderedDict()
_AUTOCOMPLETE_CACHE_MAX = 512
_autocomplete_lock: asyncio.Lock | None = None  # lazy init to avoid binding to wrong event loop

# Genre tree cache — refreshed every 5 minutes since the tree changes rarely
_genre_tree_cache: dict | None = None
_genre_tree_cache_time: float = 0
_GENRE_TREE_TTL = 300  # 5 minutes
_genre_tree_lock: asyncio.Lock | None = None  # lazy init to avoid binding to wrong event loop


def _get_cache_key(query: str, entity_type: str, limit: int) -> tuple[str, str, int]:
    return (query.lower().strip(), entity_type, limit)


def _build_categories(entity_type: str, result: dict[str, Any]) -> list[dict[str, Any]]:
    if entity_type == "artist":
        return [
            {"id": "cat-releases", "name": "Releases", "category": "releases", "count": result.get("release_count", 0)},
            {"id": "cat-labels", "name": "Labels", "category": "labels", "count": result.get("label_count", 0)},
            {"id": "cat-aliases", "name": "Aliases & Members", "category": "aliases", "count": result.get("alias_count", 0)},
        ]
    if entity_type == "genre":
        return [
            {"id": "cat-releases", "name": "Releases", "category": "releases", "count": result.get("release_count", 0)},
            {"id": "cat-artists", "name": "Artists", "category": "artists", "count": result.get("artist_count", 0)},
            {"id": "cat-labels", "name": "Labels", "category": "labels", "count": result.get("label_count", 0)},
            {"id": "cat-styles", "name": "Styles", "category": "styles", "count": result.get("style_count", 0)},
        ]
    if entity_type == "label":
        return [
            {"id": "cat-releases", "name": "Releases", "category": "releases", "count": result.get("release_count", 0)},
            {"id": "cat-artists", "name": "Artists", "category": "artists", "count": result.get("artist_count", 0)},
            {"id": "cat-genres", "name": "Genres", "category": "genres", "count": result.get("genre_count", 0)},
        ]
    if entity_type == "style":
        return [
            {"id": "cat-releases", "name": "Releases", "category": "releases", "count": result.get("release_count", 0)},
            {"id": "cat-artists", "name": "Artists", "category": "artists", "count": result.get("artist_count", 0)},
            {"id": "cat-labels", "name": "Labels", "category": "labels", "count": result.get("label_count", 0)},
            {"id": "cat-genres", "name": "Genres", "category": "genres", "count": result.get("genre_count", 0)},
        ]
    return []


@router.get("/api/autocomplete")
@limiter.limit("30/minute")
async def autocomplete(
    request: Request,  # noqa: ARG001
    q: str = Query(..., min_length=3),
    type: str = Query("artist"),
    limit: int = Query(10, ge=1, le=50),
) -> JSONResponse:
    if not _neo4j_driver:
        return JSONResponse(content={"error": "Service not ready"}, status_code=503)
    entity_type = type.lower()
    if entity_type not in AUTOCOMPLETE_DISPATCH:
        return JSONResponse(content={"error": f"Invalid type: {type}. Must be artist, genre, label, or style"}, status_code=400)
    global _autocomplete_lock
    if _autocomplete_lock is None:
        _autocomplete_lock = asyncio.Lock()
    cache_key = _get_cache_key(q, entity_type, limit)
    async with _autocomplete_lock:
        if cache_key in _autocomplete_cache:
            _autocomplete_cache.move_to_end(cache_key)
            return JSONResponse(content={"results": _autocomplete_cache[cache_key]})
    query_func = AUTOCOMPLETE_DISPATCH[entity_type]
    results = await query_func(_neo4j_driver, q, limit)
    async with _autocomplete_lock:
        if len(_autocomplete_cache) >= _AUTOCOMPLETE_CACHE_MAX:
            evict_count = _AUTOCOMPLETE_CACHE_MAX // 4
            for _ in range(evict_count):
                _autocomplete_cache.popitem(last=False)
        _autocomplete_cache[cache_key] = results
    return JSONResponse(content={"results": results})


@router.get("/api/explore")
async def explore(
    name: str = Query(...),
    type: str = Query("artist"),
) -> JSONResponse:
    if not _neo4j_driver:
        return JSONResponse(content={"error": "Service not ready"}, status_code=503)
    entity_type = type.lower()
    if entity_type not in EXPLORE_DISPATCH:
        return JSONResponse(content={"error": f"Invalid type: {type}. Must be artist, genre, label, or style"}, status_code=400)

    # Cache artist and label explore results in Valkey (expensive COUNT traversals)
    if _valkey and entity_type in ("artist", "label"):
        cache_key = f"explore:{entity_type}:{name}"
        try:
            cached = await cache_get(_valkey, cache_key, cache=CACHE_EXPLORE)
            if cached:
                return JSONResponse(content=json.loads(cached))
        except Exception:
            logger.debug("⚠️ Explore cache get failed", key=cache_key)

    query_func = EXPLORE_DISPATCH[entity_type]
    result = await query_func(_neo4j_driver, name)
    if not result:
        return JSONResponse(content={"error": f"{type.capitalize()} '{name}' not found"}, status_code=404)
    categories = _build_categories(entity_type, result)
    response = {"center": {"id": str(result["id"]), "name": result["name"], "type": entity_type}, "categories": categories}

    if _valkey and entity_type in ("artist", "label"):
        cache_key = f"explore:{entity_type}:{name}"
        try:
            await _valkey.setex(cache_key, _EXPLORE_CACHE_TTL, json.dumps(response))
        except Exception as exc:
            record_cache_write_failure(CACHE_EXPLORE, exc)

    return JSONResponse(content=response)


@router.get("/api/expand")
async def expand(
    node_id: str = Query(...),
    type: str = Query(...),
    category: str = Query(...),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    before_year: int | None = Query(default=None, ge=1900, le=2030),
) -> JSONResponse:
    if not _neo4j_driver:
        return JSONResponse(content={"error": "Service not ready"}, status_code=503)
    entity_type = type.lower()
    category_lower = category.lower()
    if entity_type not in EXPAND_DISPATCH:
        return JSONResponse(content={"error": f"Invalid type: {type}"}, status_code=400)
    type_categories = EXPAND_DISPATCH[entity_type]
    if category_lower not in type_categories:
        valid = ", ".join(type_categories.keys())
        return JSONResponse(content={"error": f"Invalid category '{category}' for type '{type}'. Valid: {valid}"}, status_code=400)
    query_func = type_categories[category_lower]
    count_func = COUNT_DISPATCH[entity_type][category_lower]
    results, total = await asyncio.gather(
        query_func(_neo4j_driver, node_id, limit, offset, before_year=before_year),
        count_func(_neo4j_driver, node_id, before_year=before_year),
    )
    return JSONResponse(content={"children": results, "total": total, "offset": offset, "limit": limit, "has_more": offset + len(results) < total})


@router.get("/api/explore/year-range")
async def year_range() -> JSONResponse:
    if not _neo4j_driver:
        return JSONResponse(content={"error": "Service not ready"}, status_code=503)
    result = await get_year_range(_neo4j_driver)
    if result is None:
        return JSONResponse(content={"min_year": None, "max_year": None})
    # Clamp to valid bounds so the frontend slider stays within the
    # before_year validation range (ge=1900, le=2030).
    min_year = max(1900, min(2030, result.get("min_year", 1900)))
    max_year = max(1900, min(2030, result.get("max_year", 2030)))
    return JSONResponse(content={"min_year": min_year, "max_year": max_year})


@router.get("/api/explore/genre-emergence")
async def genre_emergence(
    before_year: int = Query(..., ge=1900, le=2030),
) -> JSONResponse:
    if not _neo4j_driver:
        return JSONResponse(content={"error": "Service not ready"}, status_code=503)
    result = await get_genre_emergence(_neo4j_driver, before_year)
    return JSONResponse(content=result)


async def _anonymous_marks() -> dict[str, Mark] | None:
    return None


# Shared-personnel edges returned per drawn master, at most: the renderer draws only
# neighbours within one credit and lists the rest in its panel, so the strongest few suffice.
_STRATA_EDGES_PER_MASTER = 4


def _strata_cache_key(artist_id: str) -> str:
    return f"explore:strata:{artist_id}"


async def _cached_strata_catalog(artist_id: str) -> Catalog | None:
    """The root's catalog from Valkey, or ``None`` on a miss, an unreadable entry, or no Valkey."""
    if not _valkey:
        return None
    cache_key = _strata_cache_key(artist_id)
    try:
        cached = await cache_get(_valkey, cache_key, cache=CACHE_STRATA)
    except Exception:
        logger.debug("⚠️ Strata cache get failed", key=cache_key)
        return None
    if not cached:
        return None
    return await asyncio.to_thread(decode_catalog, cached)


async def _store_strata_catalog(catalog: Catalog) -> None:
    if not _valkey:
        return
    try:
        await _valkey.setex(_strata_cache_key(catalog.root_id), _EXPLORE_CACHE_TTL, encode_catalog(catalog))
    except Exception as exc:
        record_cache_write_failure(CACHE_STRATA, exc)


@router.get("/api/explore/strata")
@limiter.limit("60/minute")
async def strata(
    request: Request,  # noqa: ARG001 — required by slowapi rate limiter
    current_user: Annotated[dict[str, Any] | None, Depends(get_optional_user)],
    artist_id: str = Query(..., min_length=1, max_length=64),
    budget: str = Query(f"{DEFAULT_BUDGET.credits}/{DEFAULT_BUDGET.masters}/{DEFAULT_BUDGET.releases}", max_length=16),
    focus: str | None = Query(None, min_length=1, max_length=256),
    page: int = Query(0, ge=0, le=1_000_000),
) -> JSONResponse:
    """One artist's discography as credit, master, and release planes inside a node budget.

    The rule lives in :mod:`api.strata`. A signed-in caller also gets their collector state
    from the ``COLLECTED`` and ``WANTS`` edges the Discogs sync writes; an anonymous caller
    gets none.

    ``focus`` is an aggregate id from an earlier response, or a pressing: ``r:<release_id>``
    or ``mb:<mbid>`` returns the page of its master (or no-master lane) that holds it, with
    the pressing drawn; ``page`` is then ignored, and a pressing this artist does not hold is
    a 404. A signed-in caller's owned release nodes also carry their copy ids.

    The catalog side depends on the root alone, so it is cached in Valkey under the explore
    key family and TTL and shared by every caller. The caller's marks, and the owned copy ids
    that ride on them, are read on every request and never cached. A root with more than ``MAX_ROOT_RELEASES`` releases is
    refused with a 422 before its ladder is read.
    """
    if not _neo4j_driver:
        return JSONResponse(content={"error": "Service not ready"}, status_code=503)
    try:
        plane_budget = Budget.parse(budget)
    except BudgetError as exc:
        return JSONResponse(content={"error": str(exc)}, status_code=400)
    user_id = (current_user or {}).get("sub") or None

    def read_marks() -> Coroutine[Any, Any, dict[str, Mark] | None]:
        return get_strata_marks(_neo4j_driver, user_id, artist_id, _pg_pool) if user_id else _anonymous_marks()

    try:
        catalog = await _cached_strata_catalog(artist_id)
        if catalog is not None:
            marks = await read_marks()
        else:
            root = await get_strata_root(_neo4j_driver, artist_id)
            if root is None:
                return JSONResponse(content={"error": f"Artist '{artist_id}' not found"}, status_code=404)
            if root["releases"] > MAX_ROOT_RELEASES:
                logger.info("🚫 Strata root refused as too large", artist_id=artist_id, releases=root["releases"])
                return JSONResponse(
                    content={
                        "error": f"Artist '{artist_id}' has {root['releases']} releases; strata draws at most {MAX_ROOT_RELEASES}",
                        "releases": root["releases"],
                        "max_releases": MAX_ROOT_RELEASES,
                    },
                    status_code=422,
                )
            # The catalog side depends on the root alone; the caller's marks start from their
            # own edges. Neither waits on the other.
            catalog, marks = await asyncio.gather(get_strata_catalog(_neo4j_driver, _pg_pool, artist_id, root["name"]), read_marks())
            await _store_strata_catalog(catalog)
        try:
            # Folding a root near the ceiling is CPU work measured in hundreds of milliseconds,
            # so it runs off the event loop.
            payload = await asyncio.to_thread(build_strata, catalog, plane_budget, focus=focus, page=page, marks=marks)
        except FocusNotFoundError as exc:
            return JSONResponse(content={"error": str(exc)}, status_code=404)
        release_ids, master_ids, artist_ids = label_targets(payload)
        (release_titles, master_titles, artist_names), edges = await asyncio.gather(
            get_strata_labels(_neo4j_driver, release_ids, master_ids, artist_ids),
            get_shared_personnel(_neo4j_driver, master_ids, limit=_STRATA_EDGES_PER_MASTER * len(master_ids)),
        )
    except (Neo4jClientError, QueryCanceled) as exc:
        if isinstance(exc, QueryCanceled) or "TransactionTimedOut" in str(exc):
            logger.warning("⏱️ Strata query timed out", artist_id=artist_id)
            return JSONResponse(content={"error": "Strata query timed out — try again later"}, status_code=504)
        raise

    apply_labels(payload, release_titles=release_titles, master_titles=master_titles, artist_names=artist_names, edges=edges)
    return JSONResponse(content=payload)


@router.get("/api/collaborators/{artist_id}")
@limiter.limit("30/minute")
async def get_collaborators(
    request: Request,  # noqa: ARG001
    artist_id: str,
    limit: int = Query(20, ge=1, le=100),
) -> JSONResponse:
    if not _neo4j_driver:
        return JSONResponse(content={"error": "Service not ready"}, status_code=503)

    try:
        identity = await collaborator_queries.get_artist_identity(_neo4j_driver, artist_id)
        if not identity:
            return JSONResponse(content={"error": f"Artist '{artist_id}' not found"}, status_code=404)

        collaborators, total = await asyncio.gather(
            collaborator_queries.get_collaborators(_neo4j_driver, artist_id, limit=limit),
            collaborator_queries.count_collaborators(_neo4j_driver, artist_id),
        )
    except Neo4jClientError as exc:
        if "TransactionTimedOut" in str(exc):
            logger.warning("⏱️ Collaborators query timed out", artist_id=artist_id)
            return JSONResponse(
                content={"error": "Collaborators query timed out — try again later"},
                status_code=504,
            )
        raise

    return JSONResponse(
        content={
            "artist_id": identity["artist_id"],
            "artist_name": identity["artist_name"],
            "collaborators": collaborators,
            "total": total,
        }
    )


@router.get("/api/genre-tree")
@limiter.limit("30/minute")
async def genre_tree(
    request: Request,  # noqa: ARG001
) -> JSONResponse:
    """Return the full genre/style hierarchy derived from release co-occurrence."""
    global _genre_tree_cache, _genre_tree_cache_time, _genre_tree_lock

    if not _neo4j_driver:
        return JSONResponse(content={"error": "Service not ready"}, status_code=503)

    now = time.monotonic()
    if _genre_tree_cache is not None and (now - _genre_tree_cache_time) < _GENRE_TREE_TTL:
        return JSONResponse(content=_genre_tree_cache)

    # Serialize cache refresh to prevent thundering herd on expiry
    if _genre_tree_lock is None:
        _genre_tree_lock = asyncio.Lock()
    async with _genre_tree_lock:
        # Re-check after acquiring lock — another coroutine may have refreshed
        now = time.monotonic()
        if _genre_tree_cache is not None and (now - _genre_tree_cache_time) < _GENRE_TREE_TTL:
            return JSONResponse(content=_genre_tree_cache)

        try:
            genres = await genre_tree_queries.get_genre_tree(_neo4j_driver)
        except Neo4jClientError as exc:
            if "TransactionTimedOut" in str(exc):
                logger.warning("⏱️ Genre tree query timed out")
                return JSONResponse(
                    content={"error": "Genre tree query timed out — try again later"},
                    status_code=504,
                )
            raise

        _genre_tree_cache = {"genres": genres}
        _genre_tree_cache_time = time.monotonic()
        return JSONResponse(content=_genre_tree_cache)


async def _resolve_release_media(release_id: str, formats: list[Any] | None) -> dict[str, Any]:
    """Resolve the ADR 0007 canonical ``media`` block for a release node response.

    Prefers ``releases.media`` in PostgreSQL (computed at load time by the
    Discogs SQL loader); when the row is missing or the column is NULL —
    or PostgreSQL is not configured — derives a best-effort block from the
    release's raw, deprecated ``formats`` name list so the ``media`` key is
    never omitted for a release.
    """
    media = await get_release_media(_pg_pool, release_id) if _pg_pool is not None else None
    if media is not None:
        return media
    return legacy_format_names_to_media(formats or [])


# What a release detail response carries when PostgreSQL is not configured: the three keys
# are additive and always present, so a consumer renders "no markings recorded" rather than
# branching on whether the field exists.
_NO_CATALOG_BLOCKS: dict[str, Any] = {"identifiers": [], "companies": [], "country": None}


async def _resolve_release_blocks(release_id: str) -> dict[str, Any]:
    """Resolve the ADR 0011 ``identifiers``, ``companies``, and ``country`` for a release.

    Read from ``releases.data`` in PostgreSQL exactly as :func:`_resolve_release_media`
    reads the media block, and deliberately *not* from the Neo4j ``CREDITED_TO`` edges the
    graph enricher writes: the edges are a projection of this same block, so reading the
    block is one indexed row rather than a traversal, and the two cannot disagree.
    """
    if _pg_pool is None:
        return dict(_NO_CATALOG_BLOCKS)
    return await get_release_catalog_blocks(_pg_pool, release_id)


@router.get("/api/node/{node_id}")
async def get_node_details(
    node_id: str,
    type: str = Query("artist"),
) -> JSONResponse:
    if not _neo4j_driver:
        return JSONResponse(content={"error": "Service not ready"}, status_code=503)
    entity_type = type.lower()
    if entity_type not in DETAILS_DISPATCH:
        return JSONResponse(content={"error": f"Invalid type: {type}"}, status_code=400)
    query_func = DETAILS_DISPATCH[entity_type]
    result = await query_func(_neo4j_driver, node_id)
    if not result:
        return JSONResponse(content={"error": f"{type.capitalize()} '{node_id}' not found"}, status_code=404)
    if entity_type == "release":
        media, blocks = await asyncio.gather(
            _resolve_release_media(node_id, result.get("formats")),
            _resolve_release_blocks(node_id),
        )
        result["media"] = media
        result.update(blocks)
    return JSONResponse(content=result)


@router.get("/api/trends")
async def get_trends(
    name: str = Query(...),
    type: str = Query("artist"),
) -> JSONResponse:
    if not _neo4j_driver:
        return JSONResponse(content={"error": "Service not ready"}, status_code=503)
    entity_type = type.lower()
    if entity_type not in TRENDS_DISPATCH:
        return JSONResponse(content={"error": f"Invalid type: {type}. Must be artist, genre, label, or style"}, status_code=400)

    # Cache genre, style, and label trends in Valkey (data changes only on import)
    if _valkey and entity_type in ("genre", "style", "label"):
        cache_key = f"trends:{entity_type}:{name}"
        try:
            cached = await cache_get(_valkey, cache_key, cache=CACHE_TRENDS)
            if cached:
                return JSONResponse(content=json.loads(cached))
        except Exception:
            logger.debug("⚠️ Trends cache get failed", key=cache_key)

    query_func = TRENDS_DISPATCH[entity_type]
    results = await query_func(_neo4j_driver, name)
    response = {"name": name, "type": entity_type, "data": results}

    if _valkey and entity_type in ("genre", "style", "label"):
        cache_key = f"trends:{entity_type}:{name}"
        try:
            await _valkey.setex(cache_key, _TRENDS_CACHE_TTL, json.dumps(response))
        except Exception as exc:
            record_cache_write_failure(CACHE_TRENDS, exc)

    return JSONResponse(content=response)


_VALID_PATH_TYPES = frozenset(EXPLORE_DISPATCH.keys())
# _MAX_PATH_DEPTH / _DEFAULT_PATH_DEPTH are re-exported (aliased on import,
# above) from neo4j_queries — the single source of truth for shortestPath
# depth bounds, also used by the NLQ find_path tool handler.
# node_label_to_type is likewise re-exported from neo4j_queries — the single
# source of truth for normalizing raw Neo4j labels, also used by the NLQ
# find_path tool handler (api/nlq/tools.py) so the two mirror sites cannot
# diverge again.


@router.get("/api/path")
async def find_path(
    from_name: str = Query(...),
    from_type: str = Query("artist"),
    to_name: str = Query(...),
    to_type: str = Query("artist"),
    max_depth: int = Query(_DEFAULT_PATH_DEPTH, ge=1, le=_MAX_PATH_DEPTH),
) -> JSONResponse:
    if not _neo4j_driver:
        return JSONResponse(content={"error": "Service not ready"}, status_code=503)

    from_type_lower = from_type.lower()
    to_type_lower = to_type.lower()

    if from_type_lower not in _VALID_PATH_TYPES:
        return JSONResponse(
            content={"error": f"Invalid from_type: {from_type}. Must be one of: {', '.join(sorted(_VALID_PATH_TYPES))}"},
            status_code=400,
        )
    if to_type_lower not in _VALID_PATH_TYPES:
        return JSONResponse(
            content={"error": f"Invalid to_type: {to_type}. Must be one of: {', '.join(sorted(_VALID_PATH_TYPES))}"},
            status_code=400,
        )

    from_explore = EXPLORE_DISPATCH[from_type_lower]
    to_explore = EXPLORE_DISPATCH[to_type_lower]

    from_node, to_node = await asyncio.gather(
        from_explore(_neo4j_driver, from_name),
        to_explore(_neo4j_driver, to_name),
    )

    if not from_node:
        return JSONResponse(
            content={"error": f"{from_type.capitalize()} '{from_name}' not found"},
            status_code=404,
        )
    if not to_node:
        return JSONResponse(
            content={"error": f"{to_type.capitalize()} '{to_name}' not found"},
            status_code=404,
        )

    try:
        raw = await find_shortest_path(
            _neo4j_driver,
            str(from_node["id"]),
            str(to_node["id"]),
            max_depth=max_depth,
            from_type=from_type_lower,
            to_type=to_type_lower,
        )
    except Neo4jClientError as exc:
        if "TransactionTimedOut" in str(exc):
            logger.warning("⏱️ Path query timed out", from_name=from_name, to_name=to_name, max_depth=max_depth)
            return JSONResponse(
                content={"error": "Path query timed out — try reducing max_depth or searching closer nodes"},
                status_code=504,
            )
        raise

    if raw is None:
        return JSONResponse(content=PathResponse(found=False, length=None, path=[]).model_dump())

    raw_nodes: list[dict[str, Any]] = raw["nodes"]
    raw_rels: list[str] = raw["rels"]

    path_nodes = [
        PathNode(
            id=str(n["id"]),
            name=str(n["name"]),
            type=node_label_to_type(n["labels"]),
            rel=raw_rels[i - 1] if i > 0 else None,
        )
        for i, n in enumerate(raw_nodes)
    ]

    return JSONResponse(
        content=PathResponse(
            found=True,
            length=len(raw_rels),
            path=path_nodes,
        ).model_dump()
    )


@router.get("/api/graph/stats")
async def graph_stats() -> JSONResponse:
    """Return aggregate node counts for each entity type in the knowledge graph."""
    if not _neo4j_driver:
        return JSONResponse(content={"error": "Service not ready"}, status_code=503)
    counts = await get_graph_stats(_neo4j_driver)
    return JSONResponse(content={"total_entities": sum(counts.values()), "counts": counts})
