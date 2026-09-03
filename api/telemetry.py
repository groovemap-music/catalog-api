"""OpenTelemetry instruments owned by catalog-api.

The transport, resource, and provider come from ``common.telemetry``; this module owns
only the instruments this service records and the small helpers that record them. Every
instrument is created lazily from a single meter so a process that never configures
``OTEL_EXPORTER_OTLP_ENDPOINT`` pays for one no-op instrument per metric and nothing else.

Attribute sets are closed and low-cardinality by construction: the ``cache`` and ``outcome``
values are the module constants below, never a key, an id, or free text.

This is separate from :mod:`api.metrics_collector`, which keeps the homegrown Postgres-backed
endpoint history that operations-console reads. The two do not interact.
"""

from __future__ import annotations

import inspect
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

import structlog
from common import get_meter


if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    import redis.asyncio as aioredis


logger = structlog.get_logger(__name__)

# One meter for the whole service, named after the package the instruments belong to.
METER_NAME = "groovemap.api"

# Closed attribute vocabularies. Every recorded value comes from one of these sets.
CACHE_HIT = "hit"
CACHE_MISS = "miss"

# Logical Redis caches. The value names the cache, never the key inside it.
CACHE_CREDITS_LEADERBOARD = "credits_leaderboard"
CACHE_CREDITS_PERSON = "credits_person"
CACHE_EXPLORE = "explore"
CACHE_INSIGHTS_COMPLETENESS = "insights_completeness"
CACHE_LABEL_DNA = "label_dna"
CACHE_LABEL_SIMILAR = "label_similar"
CACHE_NETWORK_CENTRALITY = "network_centrality"
CACHE_NETWORK_CLUSTER = "network_cluster"
CACHE_NLQ_QUERY = "nlq_query"
CACHE_NLQ_SUGGESTIONS = "nlq_suggestions"
CACHE_RECOMMEND = "recommend"
CACHE_SEARCH = "search"
CACHE_TRENDS = "trends"

# NLQ request outcomes.
NLQ_CACHED = "cached"
NLQ_ERROR = "error"
NLQ_INVALID = "invalid"
NLQ_SUCCESS = "success"
NLQ_UNAVAILABLE = "unavailable"

# `db.system.name` for the Redis client, which is not one of the shared resilient wrappers
# and therefore reports `db.client.operation.duration` itself.
REDIS_SYSTEM = "redis"

# Redis client methods that manage the connection rather than issue a command. Timing them
# would put lifecycle latency into the database-operation histogram.
_UNTIMED_REDIS_METHODS = frozenset({"aclose", "close", "disconnect", "initialize", "reset"})


@dataclass(frozen=True)
class _Instruments:
    """The service's instruments, built once against the installed MeterProvider."""

    cache: Any
    db_operation_duration: Any
    nlq_requests: Any
    sync_duration: Any


# Built on first use, after `setup_telemetry` has installed the real provider. Tests reset
# this to rebuild against an in-memory reader.
_instruments: _Instruments | None = None


def _build_instruments() -> _Instruments:
    """Create every instrument from one meter."""
    meter = get_meter(METER_NAME)
    return _Instruments(
        cache=meter.create_counter(
            "groovemap.api.cache",
            unit="{event}",
            description="Redis cache-aside lookups by logical cache and outcome",
        ),
        db_operation_duration=meter.create_histogram(
            "db.client.operation.duration",
            unit="s",
            description="Duration of a database client operation",
        ),
        nlq_requests=meter.create_counter(
            "groovemap.api.nlq.requests",
            unit="{request}",
            description="Natural-language query requests by outcome",
        ),
        sync_duration=meter.create_histogram(
            "groovemap.api.sync.duration",
            unit="s",
            description="Duration of a full Discogs collection and wantlist sync",
        ),
    )


def instruments() -> _Instruments:
    """Return the service's instruments, building them on first use."""
    global _instruments

    if _instruments is None:
        _instruments = _build_instruments()
    return _instruments


def reset_instruments() -> None:
    """Drop the cached instruments so the next record rebinds to the current provider.

    Needed only by tests, which install an in-memory reader after import time.
    """
    global _instruments

    _instruments = None


def record_cache(cache: str, *, hit: bool) -> None:
    """Count one cache-aside lookup against a named Redis cache."""
    instruments().cache.add(1, {"outcome": CACHE_HIT if hit else CACHE_MISS, "cache": cache})


def record_nlq_request(outcome: str) -> None:
    """Count one natural-language query request."""
    instruments().nlq_requests.add(1, {"outcome": outcome})


def record_sync_duration(seconds: float, outcome: str) -> None:
    """Record how long a full Discogs sync ran and how it ended."""
    instruments().sync_duration.record(seconds, {"outcome": outcome})


def record_redis_operation(operation: str, seconds: float, error_type: str | None = None) -> None:
    """Record one Redis command as `db.client.operation.duration`."""
    attributes: dict[str, str] = {"db.system.name": REDIS_SYSTEM, "db.operation.name": operation}
    if error_type is not None:
        attributes["error.type"] = error_type
    instruments().db_operation_duration.record(seconds, attributes)


async def cache_get(redis: Any, key: str, *, cache: str) -> Any:
    """Read one cache-aside key and count the hit or the miss.

    Returns whatever the client returns, so a caller keeps its own deserialization and its
    own error handling: a failing read counts as a miss and the exception is re-raised for
    the caller's existing ``except`` block to swallow.
    """
    if redis is None:
        return None
    try:
        value = await redis.get(key)
    except Exception:
        record_cache(cache, hit=False)
        raise
    record_cache(cache, hit=value is not None)
    return value


async def _timed_redis_call(operation: str, awaitable: Awaitable[Any]) -> Any:
    """Await one Redis command, recording its duration and any error type."""
    start = time.perf_counter()
    try:
        result = await awaitable
    except Exception as exc:
        record_redis_operation(operation, time.perf_counter() - start, type(exc).__name__)
        raise
    record_redis_operation(operation, time.perf_counter() - start)
    return result


class _InstrumentedRedis:
    """Transparent proxy that times every Redis command the service issues.

    Redis is the one backing store catalog-api reaches without a `groovemap-runtime`
    resilient wrapper, so `db.client.operation.duration` has to come from somewhere. Wrapping
    the single client the service builds records every call site at once, including the ones
    inside third-party code, instead of asking each caller to remember.

    Commands that return a coroutine are timed; everything else (attributes, pipelines,
    connection lifecycle) is passed straight through untouched.
    """

    def __init__(self, client: Any) -> None:
        self._client = client

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self._client, name)
        if name.startswith("_") or name in _UNTIMED_REDIS_METHODS or not callable(attribute):
            return attribute

        def call(*args: Any, **kwargs: Any) -> Any:
            result = attribute(*args, **kwargs)
            # A command is a coroutine function, so the call itself does no work and the
            # timer belongs around the await. The test is deliberately `iscoroutine` and not
            # "is awaitable": `pipeline()` returns an awaitable Pipeline that callers use as
            # an async context manager, and wrapping that would break `async with`.
            if inspect.iscoroutine(result):
                return _timed_redis_call(name, result)
            return result

        # Cache the wrapper on the instance so later lookups skip __getattr__ entirely.
        self.__dict__[name] = call
        return call


def instrument_redis(client: aioredis.Redis) -> aioredis.Redis:
    """Wrap a Redis client so its commands report `db.client.operation.duration`.

    The returned proxy forwards every attribute to the client, so it stands in for it
    everywhere the real client is used. It is typed as the client it wraps because that is
    what every caller sees; the cast is the one place that fact is asserted.
    """
    return cast("aioredis.Redis", _InstrumentedRedis(client))


def timer() -> Callable[[], float]:
    """Start a monotonic timer; the returned callable gives elapsed seconds."""
    start = time.perf_counter()

    def elapsed() -> float:
        return time.perf_counter() - start

    return elapsed
