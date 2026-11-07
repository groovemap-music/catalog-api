"""Internal computation query endpoints for the insights service.

Exposes raw Neo4j and PostgreSQL query results as JSON so the insights
service can fetch data over HTTP instead of importing query modules directly.
"""

import asyncio
import json
import secrets
from collections.abc import AsyncIterator, Awaitable
from typing import Annotated, Any

import httpx
import structlog
from common import describe_exception
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, Response, StreamingResponse
from neo4j.exceptions import ClientError, Neo4jError, TransientError
from psycopg import Error as PsycopgError
from psycopg.rows import dict_row

from api.auth import decrypt_oauth_token, get_oauth_encryption_key
from api.config import DEFAULT_DISCOGS_USER_AGENT
from api.limiter import limiter
from api.models import RarityDistributions
from api.queries.insights_neo4j_queries import (
    query_artist_centrality,
    query_genre_trends,
    query_label_longevity,
    query_monthly_anniversaries,
    query_style_cotags,
    query_style_landmarks,
    query_style_year_counts,
)
from api.queries.insights_pg_queries import query_data_completeness
from api.queries.rarity_queries import (
    RARITY_PAGE_BUDGET_SECONDS,
    RARITY_PAGE_SIZE,
    fetch_rarity_page,
    fetch_rarity_quality_counts,
    iter_rarity_signals,
)
from api.syncer import DISCOGS_API_BASE, MAX_RATE_LIMIT_RETRIES, SYNC_DELAY_SECONDS, _auth_header
from api.telemetry import CACHE_INSIGHTS_COMPLETENESS, cache_get, record_cache_write_failure
from api.valkey_keys import valkey_key


logger = structlog.get_logger(__name__)

_neo4j: Any = None
_pool: Any = None
_valkey: Any = None
_config: Any = None


async def require_internal_secret(
    x_internal_secret: Annotated[str | None, Header()] = None,
) -> None:
    """Gate the internal insights router with a shared secret.

    These endpoints are mounted on the public app and reachable through the
    explore ``/api/{path:path}`` proxy, so the ``/api/internal`` prefix alone
    provides no isolation. The insights service presents the configured secret
    as the ``X-Internal-Secret`` header; any other caller is rejected.

    Fails closed: if no secret is configured the router is unreachable rather
    than anonymously open.
    """
    configured = getattr(_config, "insights_internal_secret", None) if _config is not None else None
    if not configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Internal insights API is not configured",
        )
    if not x_internal_secret or not secrets.compare_digest(x_internal_secret, configured):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing internal API credentials",
        )


router = APIRouter(
    prefix="/api/internal/insights",
    tags=["insights-compute"],
    dependencies=[Depends(require_internal_secret)],
)

# Reuse api.syncer's rate-limit pacing constant rather than forking a second copy
# — the two are explicit rate-limit siblings (both share MAX_RATE_LIMIT_RETRIES
# against the same Discogs 60 req/min budget) and a fork previously let this value
# drift out of sync with syncer.py's.
_ENRICHMENT_DELAY_SECONDS = SYNC_DELAY_SECONDS
_STALENESS_DAYS = 7

# Hard cap on releases enriched per invocation.
#
# The enrichment loop is rate-limited to one Discogs request per second, so its
# wall-clock cost is unbounded in the number of stale releases — a backlog of
# 10k releases would take ~3 hours and blow ANY client read timeout. That is why
# community_enrichment failed four times in production with an (empty-stringing)
# ReadTimeout: the endpoint simply never returned.
#
# Bounding the batch makes the endpoint's worst case predictable
# (~1500 * 1.5s ≈ 2250s, comfortably inside the insights client's 3600s read
# budget) and costs nothing in completeness: the daily scheduler drains the
# remaining backlog on subsequent cycles.
MAX_ENRICHMENT_RELEASES = 1500

# Cache TTL for data-completeness (6 hours — full table scans are very expensive)
_COMPLETENESS_CACHE_TTL = 21600


def configure(neo4j: Any, pool: Any, valkey: Any = None, config: Any = None) -> None:
    """Configure the insights compute router with database connections."""
    global _neo4j, _pool, _valkey, _config
    _neo4j = neo4j
    _pool = pool
    _valkey = valkey
    _config = config


@router.get("/artist-centrality")
@limiter.limit("5/minute")
async def artist_centrality(request: Request, limit: int = Query(100, ge=1, le=500)) -> JSONResponse:  # noqa: ARG001
    """Return raw artist centrality query results from Neo4j."""
    if not _neo4j:
        return JSONResponse(content={"error": "Service not ready"}, status_code=503)
    results = await query_artist_centrality(_neo4j, limit=limit)
    return JSONResponse(content={"items": results})


@router.get("/genre-trends")
@limiter.limit("5/minute")
async def genre_trends(request: Request) -> JSONResponse:  # noqa: ARG001
    """Return raw genre trends query results from Neo4j.

    The read scans every release-genre tag, so it runs under a long internal budget (see
    ``GENRE_TRENDS_QUERY_TIMEOUT_SECONDS``) published in the internal-insights contract. A Neo4j
    timeout is a retryable 503, never an empty list.
    """
    if not _neo4j:
        return JSONResponse(content={"error": "Service not ready"}, status_code=503)
    try:
        results = await query_genre_trends(_neo4j)
    except (TransientError, ClientError, BaseExceptionGroup) as exc:
        return _retryable_unavailable(exc, "Genre-trends computation")
    return JSONResponse(content={"items": results})


@router.get("/style-year-counts")
@limiter.limit("5/minute")
async def style_year_counts(request: Request) -> JSONResponse:  # noqa: ARG001
    """Return release and master counts per style and release year, with each style's genre shares.

    Also returns the release count per genre and release year, from which the consumer derives
    genre emergence years.

    Raw input for analytics-engine's style lifecycle and atlas layout. The read scans every
    release-style tag, so it runs under a long internal budget (see
    ``STYLE_YEAR_QUERY_TIMEOUT_SECONDS``) and is published with that processing budget in the
    internal-insights contract.
    """
    if not _neo4j:
        return JSONResponse(content={"error": "Service not ready"}, status_code=503)
    try:
        result = await query_style_year_counts(_neo4j)
    except (TransientError, ClientError, BaseExceptionGroup) as exc:
        return _retryable_unavailable(exc, "Style-year computation")
    return JSONResponse(content=result)


@router.get("/style-landmarks")
@limiter.limit("5/minute")
async def style_landmarks(request: Request) -> JSONResponse:  # noqa: ARG001
    """Return the landmark masters of every style: the top 25 by release count, ties by master id.

    Raw input for the master plane of analytics-engine's atlas. It visits every release-style tag
    one style at a time, so it is its own endpoint under the same long internal budget as the
    style-year read (see ``STYLE_LANDMARK_QUERY_TIMEOUT_SECONDS``), published in the
    internal-insights contract. A Neo4j timeout or memory failure is a retryable 503.
    """
    if not _neo4j:
        return JSONResponse(content={"error": "Service not ready"}, status_code=503)
    try:
        result = await query_style_landmarks(_neo4j)
    except (TransientError, ClientError, BaseExceptionGroup) as exc:
        return _retryable_unavailable(exc, "Style-landmarks computation")
    return JSONResponse(content=result)


@router.get("/style-cotags")
@limiter.limit("5/minute")
async def style_cotags(request: Request) -> JSONResponse:  # noqa: ARG001
    """Return the co-tagged styles of every style: the top 25 by shared releases, ties by name.

    Raw input for the lateral edges of analytics-engine's atlas. It counts every release's style
    pairs one style at a time, so it is its own endpoint under the same long internal budget as
    the style-year read (see ``STYLE_COTAG_QUERY_TIMEOUT_SECONDS``), published in the
    internal-insights contract. A Neo4j timeout or memory failure is a retryable 503.
    """
    if not _neo4j:
        return JSONResponse(content={"error": "Service not ready"}, status_code=503)
    try:
        result = await query_style_cotags(_neo4j)
    except (TransientError, ClientError, BaseExceptionGroup) as exc:
        return _retryable_unavailable(exc, "Style-cotags computation")
    return JSONResponse(content=result)


@router.get("/label-longevity")
@limiter.limit("5/minute")
async def label_longevity(
    request: Request,  # noqa: ARG001
    limit: int = Query(50, ge=1, le=500),
    include_placeholders: bool = Query(False),
) -> JSONResponse:
    """Return raw label longevity query results from Neo4j.

    The read takes the year of every release-label edge, so it runs under a long internal budget
    (see ``LABEL_LONGEVITY_QUERY_TIMEOUT_SECONDS``) published in the internal-insights contract. A
    Neo4j timeout or memory failure is a retryable 503, never an empty list.

    Placeholder labels such as Not On Label are left out unless ``include_placeholders`` is set,
    and then they take no place from a real label. Every row carries ``is_placeholder``.
    """
    if not _neo4j:
        return JSONResponse(content={"error": "Service not ready"}, status_code=503)
    try:
        results = await query_label_longevity(_neo4j, limit=limit, include_placeholders=include_placeholders)
    except (TransientError, ClientError, BaseExceptionGroup) as exc:
        return _retryable_unavailable(exc, "Label-longevity computation")
    return JSONResponse(content={"items": results})


@router.get("/anniversaries")
@limiter.limit("5/minute")
async def anniversaries(
    request: Request,  # noqa: ARG001
    year: int = Query(...),
    month: int = Query(..., ge=1, le=12),
    milestones: str = Query("25,30,40,50,75,100"),
) -> JSONResponse:
    """Return raw monthly anniversary query results from Neo4j."""
    if not _neo4j:
        return JSONResponse(content={"error": "Service not ready"}, status_code=503)
    try:
        milestone_years = [int(m.strip()) for m in milestones.split(",") if m.strip()]
    except ValueError:
        return JSONResponse(content={"error": "milestones must be comma-separated integers"}, status_code=422)
    results = await query_monthly_anniversaries(_neo4j, current_year=year, current_month=month, milestone_years=milestone_years)
    return JSONResponse(content={"items": results})


@router.get("/data-completeness")
@limiter.limit("5/minute")
async def data_completeness(request: Request) -> JSONResponse:  # noqa: ARG001
    """Return raw data completeness query results from PostgreSQL.

    Caches results in Valkey (6h TTL) because the underlying queries do
    full sequential scans — the releases table alone takes ~400s.
    """
    if not _pool:
        return JSONResponse(content={"error": "Service not ready"}, status_code=503)

    cache_key = valkey_key("insights:data-completeness")
    if _valkey:
        try:
            cached = await cache_get(_valkey, cache_key, cache=CACHE_INSIGHTS_COMPLETENESS)
            if cached:
                return JSONResponse(content=json.loads(cached))
        except Exception:
            logger.debug("⚠️ Data completeness cache get failed")

    results = await query_data_completeness(_pool)
    response = {"items": results}

    if _valkey:
        try:
            await _valkey.setex(cache_key, _COMPLETENESS_CACHE_TTL, json.dumps(response))
        except Exception as exc:
            record_cache_write_failure(CACHE_INSIGHTS_COMPLETENESS, exc)

    return JSONResponse(content=response)


# Neo4j error codes that mean "the database gave up on this transaction", not
# "the server has a bug". Both are retryable and must surface as 503, never 500.
#
# TransactionTimedOutClientConfiguration is a *ClientError*, not a TransientError,
# which is exactly why the previous handler missed it: rarity-scores blew
# db.transaction.timeout (600s) on every production run and the ClientError
# escaped as an unhandled 500.
TRANSACTION_TIMEOUT_CODES = frozenset(
    {
        "Neo.ClientError.Transaction.TransactionTimedOut",
        "Neo.ClientError.Transaction.TransactionTimedOutClientConfiguration",
    }
)


def _find_retryable_neo4j_error(exc: BaseException) -> Neo4jError | None:
    """Return the first retryable Neo4j error in ``exc``, or ``None``.

    Unwraps ``BaseExceptionGroup`` (the driver's internals can surface a
    transaction failure wrapped in one) and ``__cause__`` chains, so a retryable
    error is recognised no matter how it was re-raised.
    """
    if isinstance(exc, BaseExceptionGroup):
        for inner in exc.exceptions:
            found = _find_retryable_neo4j_error(inner)
            if found is not None:
                return found
        return None
    if isinstance(exc, TransientError):
        return exc
    if isinstance(exc, ClientError) and exc.code in TRANSACTION_TIMEOUT_CODES:
        return exc
    cause = exc.__cause__
    if cause is not None and cause is not exc:
        return _find_retryable_neo4j_error(cause)
    return None


def _retryable_unavailable(exc: BaseException, computation: str) -> JSONResponse:
    """Map a retryable Neo4j failure to a 503 response, re-raising anything else.

    TransientError (e.g. MemoryPoolOutOfMemoryError under DB pressure) and a transaction
    timeout (see TRANSACTION_TIMEOUT_CODES) are not server bugs. A 503 lets the caller log a
    meaningful, retryable failure instead of a bare 500.
    """
    retryable = _find_retryable_neo4j_error(exc)
    if retryable is None:
        raise exc
    logger.warning(f"⚠️ {computation} hit a retryable Neo4j error", code=retryable.code, exc_info=True)
    return JSONResponse(
        content={"error": "Neo4j temporarily unavailable (retryable error)", "code": retryable.code},
        status_code=503,
    )


# Rows encoded per chunk written to the client. The body is `{"items":[...]}`, byte for byte what
# `JSONResponse({"items": rows})` produced, written as the rows are scored.
_RARITY_CHUNK_ROWS = 500


def _encode_rarity_row(row: dict[str, Any]) -> str:
    """Encode one row exactly as `JSONResponse` encodes it (compact, UTF-8, no NaN)."""
    return json.dumps(row, ensure_ascii=False, allow_nan=False, indent=None, separators=(",", ":"))


async def _rarity_body(first: dict[str, Any], rest: AsyncIterator[dict[str, Any]]) -> AsyncIterator[bytes]:
    """Stream `{"items":[...]}` one chunk of rows at a time, so no response is ever held whole."""
    chunk = ['{"items":[', _encode_rarity_row(first)]
    rows = 1
    try:
        async for row in rest:
            chunk.append("," + _encode_rarity_row(row))
            rows += 1
            if rows % _RARITY_CHUNK_ROWS == 0:
                yield "".join(chunk).encode("utf-8")
                chunk = []
        chunk.append("]}")
        yield "".join(chunk).encode("utf-8")
    except Exception:
        # Headers are already sent. Ending the body early gives the client a broken document, which
        # its JSON parser rejects, rather than a complete-looking one with rows missing.
        logger.exception("⚠️ Rarity stream failed after it began", rows_sent=rows)
        raise


# How often a long read checks whether its client is still there.
_DISCONNECT_POLL_SECONDS = 1.0

# A paged walk over the catalog makes two requests per 20,000 releases, as fast as the database
# answers. The route is behind the internal secret, so this only guards against a runaway client.
_RARITY_PAGE_RATE_LIMIT = "600/minute"

# What a server that gave up on a request records, so the log tells it from a real response. The client
# never sees it: it has already gone.
_CLIENT_CLOSED_REQUEST = 499


class _ClientLeft(Exception):
    """The client closed the connection while its request was being computed."""


async def _unless_client_left[T](request: Request, work: Awaitable[T]) -> T:
    """Await ``work``, cancelling it and raising :class:`_ClientLeft` if the client disconnects.

    FastAPI lets a handler run on after its client has gone. Cancelling here also ends the read in
    flight, because the Neo4j driver drops the connection of a cancelled query.
    """
    task = asyncio.ensure_future(work)
    try:
        while True:
            done, _pending = await asyncio.wait({task}, timeout=_DISCONNECT_POLL_SECONDS)
            if done:
                return task.result()
            if await request.is_disconnected():
                logger.info("🔌 Rarity client left, stopping its read")
                raise _ClientLeft
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def _rarity_page_response(request: Request, work: Awaitable[dict[str, Any]], computation: str) -> Response:
    """Answer with one whole page or an error status: a page is never sent in part.

    Every failure a client can retry is a 503, including a page that overran the budget the contract
    publishes, so the client's read timeout only ever fires on a broken connection.
    """
    try:
        async with asyncio.timeout(RARITY_PAGE_BUDGET_SECONDS):
            page = await _unless_client_left(request, work)
    except _ClientLeft:
        return Response(status_code=_CLIENT_CLOSED_REQUEST)
    except TimeoutError:
        logger.warning(f"⚠️ {computation} overran its page budget", budget_seconds=RARITY_PAGE_BUDGET_SECONDS)
        return JSONResponse(content={"error": "Rarity page overran its budget"}, status_code=503)
    except (TransientError, ClientError, BaseExceptionGroup) as exc:
        return _retryable_unavailable(exc, computation)
    except PsycopgError:
        logger.warning(f"⚠️ {computation} hit a PostgreSQL error", exc_info=True)
        return JSONResponse(content={"error": "PostgreSQL temporarily unavailable"}, status_code=503)
    return JSONResponse(content=page)


@router.get("/rarity-scores/quality-counts")
@limiter.limit(_RARITY_PAGE_RATE_LIMIT)
async def rarity_quality_counts(
    request: Request,
    after: str = Query(""),
    limit: int = Query(RARITY_PAGE_SIZE, ge=1, le=RARITY_PAGE_SIZE),
) -> Response:
    """Return one page of the first pass: how often each quality-signal value occurs.

    The caller follows ``next_after`` to the end and sums the counts, which is what hidden-gem scoring
    ranks each release against. See :func:`rarity_page`.
    """
    if not _neo4j:
        return JSONResponse(content={"error": "Service not ready"}, status_code=503)
    return await _rarity_page_response(request, fetch_rarity_quality_counts(_neo4j, after=after, limit=limit), "Rarity quality counts")


@router.post("/rarity-scores/page")
@limiter.limit(_RARITY_PAGE_RATE_LIMIT)
async def rarity_page(
    request: Request,
    distributions: RarityDistributions,
    after: str = Query(""),
    limit: int = Query(RARITY_PAGE_SIZE, ge=1, le=RARITY_PAGE_SIZE),
) -> Response:
    """Return one page of scored rows, ranked against the summed quality counts in the body.

    The counts travel with each request so the API keeps nothing between pages: any worker can answer
    any page, a restart loses nothing, and a dropped request retries one page.
    """
    if not _neo4j or not _pool:
        return JSONResponse(content={"error": "Service not ready"}, status_code=503)
    work = fetch_rarity_page(_neo4j, _pool, after=after, limit=limit, distributions=distributions.model_dump())
    return await _rarity_page_response(request, work, "Rarity page")


@router.get("/rarity-scores", response_model=None)
@limiter.limit("5/minute")
async def rarity_scores(request: Request) -> Response:
    """Return computed rarity scores for all releases from Neo4j.

    Deprecated for :func:`rarity_quality_counts` and :func:`rarity_page`: this is one response for the
    whole catalog, so it cannot be retried, and a client has to hold all of it.

    The body is `{"items": [...]}` with every release, as before, but it is streamed: the API process
    holds one page of rows rather than one row per release in the graph (about 1.7 KB each, so about
    17 GB at 10M releases). The first row is read before the response starts, which covers the first
    pass over the quality signals and the first page, so a Neo4j error there is still a 503. Starlette
    ends the stream when the client leaves; before that first row, :func:`_unless_client_left` does.
    """
    if not _neo4j:
        return JSONResponse(content={"error": "Service not ready"}, status_code=503)
    rows = iter_rarity_signals(_neo4j, _pool)
    try:
        first = await _unless_client_left(request, anext(rows))
    except _ClientLeft:
        return Response(status_code=_CLIENT_CLOSED_REQUEST)
    except StopAsyncIteration:
        return JSONResponse(content={"items": []})
    except (TransientError, ClientError, BaseExceptionGroup) as exc:
        return _retryable_unavailable(exc, "Rarity computation")
    return StreamingResponse(_rarity_body(first, rows), media_type="application/json")


async def _enrich_community_counts(
    pool: Any,
    neo4j: Any,
    encryption_key: str | None,
) -> dict[str, Any]:
    """Fetch community have/want counts from Discogs API for releases in user collections."""
    # 1. Find releases needing enrichment (new or stale)
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT DISTINCT release_id FROM (
                SELECT release_id FROM user_collections
                UNION
                SELECT release_id FROM user_wantlists
            ) AS combined
            WHERE release_id NOT IN (
                SELECT release_id FROM insights.community_counts
                WHERE fetched_at > NOW() - make_interval(days => %s)
            )
            """,
            (_STALENESS_DAYS,),
        )
        rows = await cur.fetchall()
        all_release_ids = [r["release_id"] for r in rows]

    if not all_release_ids:
        logger.info("📊 No releases need community enrichment")
        return {"enriched": 0, "skipped": 0, "errors": 0, "remaining": 0}

    # Bound the batch so this endpoint always returns inside the caller's read
    # timeout — the leftover backlog is picked up by the next scheduled cycle.
    release_ids = all_release_ids[:MAX_ENRICHMENT_RELEASES]
    remaining = len(all_release_ids) - len(release_ids)
    if remaining:
        logger.info(
            "📊 Community enrichment backlog capped for this cycle",
            batch_size=len(release_ids),
            backlog=len(all_release_ids),
            remaining=remaining,
        )

    logger.info("📊 Releases needing community enrichment", count=len(release_ids))

    # 2. Get OAuth credentials (any user with valid Discogs OAuth)
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT ot.access_token, ot.access_secret, ot.provider_username
            FROM oauth_tokens ot
            WHERE ot.provider = 'discogs'
            LIMIT 1
            """
        )
        token = await cur.fetchone()

        if not token:
            logger.warning("⚠️ No Discogs OAuth credentials for community enrichment")
            return {"enriched": 0, "skipped": len(release_ids), "errors": 0, "remaining": remaining, "error": "no_credentials"}

        access_token = decrypt_oauth_token(token["access_token"], encryption_key)
        access_secret = decrypt_oauth_token(token["access_secret"], encryption_key)

        await cur.execute("SELECT key, value FROM app_config WHERE key IN ('discogs_consumer_key', 'discogs_consumer_secret')")
        config_rows = await cur.fetchall()
        app_config = {r["key"]: r["value"] for r in config_rows}
        if "discogs_consumer_key" not in app_config or "discogs_consumer_secret" not in app_config:
            logger.warning("⚠️ Discogs app credentials not configured")
            return {"enriched": 0, "skipped": len(release_ids), "errors": 0, "remaining": remaining, "error": "no_credentials"}
        consumer_key = decrypt_oauth_token(app_config["discogs_consumer_key"], encryption_key)
        consumer_secret = decrypt_oauth_token(app_config["discogs_consumer_secret"], encryption_key)

    # 3. Fetch community counts from Discogs API
    enriched = 0
    errors = 0
    batch: list[dict[str, Any]] = []
    rate_limit_retries = 0

    exhausted = False
    async with httpx.AsyncClient(timeout=30.0) as client:
        for release_id in release_ids:
            if exhausted:
                break

            url = f"{DISCOGS_API_BASE}/releases/{release_id}"
            auth = _auth_header(
                "GET",
                url,
                consumer_key,
                consumer_secret,
                access_token,
                access_secret,
            )
            headers = {
                "Authorization": auth,
                "User-Agent": DEFAULT_DISCOGS_USER_AGENT,
                "Accept": "application/json",
            }

            # Retry loop for rate limiting — ensures the same release is retried.
            # A transient transport error (timeout, reset, truncated body) must
            # count as an error for THIS release and move on, not abort the whole
            # run — mirroring the non-200 branch below.
            fetch_failed = False
            while True:
                try:
                    response = await client.get(url, headers=headers)
                except httpx.HTTPError as exc:
                    logger.warning("⚠️ Discogs API request failed for release", release_id=release_id, error=describe_exception(exc))
                    fetch_failed = True
                    break

                if response.status_code == 429:
                    rate_limit_retries += 1
                    if rate_limit_retries > MAX_RATE_LIMIT_RETRIES:
                        logger.error("❌ Rate limit retries exhausted during enrichment")
                        exhausted = True
                        break
                    logger.warning("⚠️ Rate limited, waiting 60s...", retry=rate_limit_retries)
                    await asyncio.sleep(60)
                    continue

                rate_limit_retries = 0
                break  # Got a non-429 response

            if exhausted:
                break

            if fetch_failed:
                errors += 1
                await asyncio.sleep(_ENRICHMENT_DELAY_SECONDS)
                continue

            if response.status_code != 200:
                logger.warning("⚠️ Discogs API error for release", release_id=release_id, status=response.status_code)
                errors += 1
                await asyncio.sleep(_ENRICHMENT_DELAY_SECONDS)
                continue

            try:
                data = response.json()
            except (json.JSONDecodeError, ValueError) as exc:
                logger.warning("⚠️ Discogs API returned a non-JSON body for release", release_id=release_id, error=str(exc))
                errors += 1
                await asyncio.sleep(_ENRICHMENT_DELAY_SECONDS)
                continue
            community = data.get("community", {})
            have = community.get("have", 0)
            want = community.get("want", 0)

            # Upsert to PostgreSQL
            async with pool.connection() as conn, conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO insights.community_counts (release_id, have_count, want_count, fetched_at)
                    VALUES (%s, %s, %s, NOW())
                    ON CONFLICT (release_id) DO UPDATE SET
                        have_count = EXCLUDED.have_count,
                        want_count = EXCLUDED.want_count,
                        fetched_at = NOW()
                    """,
                    (release_id, have, want),
                )

            batch.append({"release_id": release_id, "have": have, "want": want})
            enriched += 1
            await asyncio.sleep(_ENRICHMENT_DELAY_SECONDS)

    # 4. Batch update Neo4j Release nodes
    if batch and neo4j is not None:
        cypher = """
        UNWIND $batch AS item
        MATCH (r:Release {id: toString(item.release_id)})
        SET r.community_have = item.have,
            r.community_want = item.want
        """
        try:
            async with neo4j.session() as session:
                result = await session.run(cypher, {"batch": batch})
                await result.consume()
            logger.info("✅ Neo4j Release nodes updated with community counts", count=len(batch))
        except Exception as e:
            logger.error("❌ Failed to update Neo4j community counts", error=str(e))

    logger.info("✅ Community enrichment complete", enriched=enriched, errors=errors, remaining=remaining)
    return {
        "enriched": enriched,
        "skipped": len(release_ids) - enriched - errors,
        "errors": errors,
        "remaining": remaining,
    }


@router.get("/community-enrichment")
@limiter.limit("1/minute")
async def community_enrichment(request: Request) -> JSONResponse:  # noqa: ARG001
    """Enrich releases in user collections with Discogs community have/want counts."""
    if not _pool:
        return JSONResponse(content={"error": "Service not ready"}, status_code=503)

    encryption_key = get_oauth_encryption_key(_config.encryption_master_key) if _config else None
    result = await _enrich_community_counts(_pool, _neo4j, encryption_key)
    return JSONResponse(content=result)
