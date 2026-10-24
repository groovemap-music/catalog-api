"""Metrics collector — path normalization, request buffer, background collection, and persistence.

Provides:
- ``normalize_path`` — replaces UUID/integer path segments with ``:id``
- ``MetricsBuffer`` — bounded in-memory ring buffer for request latency stats
- ``collect_queue_metrics`` — reads RabbitMQ Prometheus ``/metrics/detailed`` for queue depths and rates
- ``collect_service_health`` — concurrent health checks across services
- ``persist_metrics`` — batch INSERT into PostgreSQL metrics tables
- ``prune_old_metrics`` — retention-based DELETE from metrics tables
- ``run_collector`` — async loop orchestrating collection, persistence, and pruning
"""

from __future__ import annotations

import asyncio
import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import httpx
import structlog
from prometheus_client.parser import text_string_to_metric_families
from psycopg.types.json import Jsonb

from api.catalog_contract import DISCOGS_EXCHANGE_PREFIX, MUSICBRAINZ_EXCHANGE_PREFIX


logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Path normalization
# ---------------------------------------------------------------------------

_INT_SEGMENT = re.compile(r"/\d+(?=/|$)")
_UUID_SEGMENT = re.compile(r"/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}(?=/|$)")
_EXCLUDED_PREFIXES = ("/health", "/metrics", "/api/admin/")


def normalize_path(path: str) -> str:
    """Replace UUID and integer path segments with ``:id``."""
    path = _UUID_SEGMENT.sub("/:id", path)
    path = _INT_SEGMENT.sub("/:id", path)
    return path


# ---------------------------------------------------------------------------
# Percentile helper
# ---------------------------------------------------------------------------


def _percentile_index(n: int, p: int) -> int:
    """Return the 0-based index for the *p*-th percentile in a sorted list of *n* items.

    Uses the nearest-rank method (ceiling) to avoid returning index 0 for small datasets.
    """
    import math  # noqa: PLC0415

    idx = math.ceil(n * p / 100) - 1
    return max(idx, 0)


# ---------------------------------------------------------------------------
# MetricsBuffer
# ---------------------------------------------------------------------------


@dataclass
class _Entry:
    path: str
    status_code: int
    duration_ms: float


@dataclass
class MetricsBuffer:
    """Bounded ring buffer that accumulates per-request latency data."""

    max_size: int = 10_000
    _entries: deque[_Entry] = field(default_factory=deque)

    def record(self, path: str, status_code: int, duration_ms: float) -> None:
        """Record a request.  Excluded paths are silently dropped."""
        for prefix in _EXCLUDED_PREFIXES:
            if path.startswith(prefix):
                return
        if len(self._entries) >= self.max_size:
            self._entries.popleft()
        self._entries.append(_Entry(path=path, status_code=status_code, duration_ms=duration_ms))

    @staticmethod
    def _compute_stats(entries: deque[_Entry] | list[_Entry]) -> dict[str, dict[str, Any]]:
        """Group entries by path and compute count/percentile/error-count stats."""
        if not entries:
            return {}

        groups: dict[str, list[float]] = {}
        error_counts: dict[str, int] = {}
        for entry in entries:
            groups.setdefault(entry.path, []).append(entry.duration_ms)
            if entry.status_code >= 500:
                error_counts[entry.path] = error_counts.get(entry.path, 0) + 1

        result: dict[str, dict[str, Any]] = {}
        for path, durations in groups.items():
            durations.sort()
            n = len(durations)
            result[path] = {
                "count": n,
                "p50": durations[_percentile_index(n, 50)],
                "p95": durations[_percentile_index(n, 95)],
                "p99": durations[_percentile_index(n, 99)],
                "error_count": error_counts.get(path, 0),
            }
        return result

    def flush(self) -> dict[str, dict[str, Any]]:
        """Group buffered entries by path and compute stats, WITHOUT clearing the buffer.

        Non-destructive on purpose: the caller is responsible for calling
        :meth:`clear` only after the returned stats have been durably persisted.
        This keeps a failed persist from silently discarding the window's
        samples — they simply stay buffered (growth still bounded by
        ``max_size`` via ``record()``'s eviction) and are folded into the next
        cycle's stats until a persist finally succeeds.

        NOTE: pairing this with :meth:`clear` after an ``await`` (e.g. a
        persist call) is a TOCTOU — any entry ``record()``ed during that
        ``await`` is silently dropped by ``clear()``'s wholesale replacement,
        because it was never part of the snapshot ``flush()`` returned
        (groovemap-qtts). Callers that persist across an await should
        use :meth:`drain` / :meth:`restore` instead, which swap the buffer
        out atomically so newly-arrived entries can never be in the
        snapshot that gets dropped.
        """
        return self._compute_stats(self._entries)

    def clear(self) -> None:
        """Drop all buffered entries.  Call only after a successful persist of ``flush()``'s result."""
        self._entries = deque()

    def drain(self) -> tuple[dict[str, dict[str, Any]], deque[_Entry]]:
        """Atomically swap out the buffer and compute stats from the swapped-out copy.

        Returns ``(stats, samples)``. The swap (``samples = self._entries;
        self._entries = deque()``) is a single, non-yielding assignment, so
        any entry ``record()``ed after this call returns lands in the FRESH
        deque — never in ``samples`` — regardless of how long the caller then
        spends ``await``ing a persist. This is what makes drain/restore safe
        across an await where ``flush``/``clear`` is not: on persist failure,
        pass ``samples`` to :meth:`restore` to put them back ahead of
        whatever arrived in the meantime, instead of ``clear()`` wiping both
        the persisted snapshot AND everything recorded during the persist
        window (groovemap-qtts).
        """
        samples = self._entries
        self._entries = deque()
        try:
            stats = self._compute_stats(samples)
        except Exception:
            # Stats computation blew up on this batch — put the raw samples
            # back rather than losing them silently.
            self.restore(samples)
            raise
        return stats, samples

    def restore(self, samples: deque[_Entry]) -> None:
        """Re-prepend entries a failed persist could not durably store.

        Placed AHEAD of anything recorded since the matching :meth:`drain`
        call, then trimmed from the left (oldest-first) to ``max_size`` —
        the same eviction order :meth:`record` uses — so a restore can never
        transiently exceed the bound record() otherwise enforces.
        """
        if not samples:
            return
        self._entries.extendleft(reversed(samples))
        while len(self._entries) > self.max_size:
            self._entries.popleft()


# ---------------------------------------------------------------------------
# Task 4: Background collection
# ---------------------------------------------------------------------------

SERVICE_ENDPOINTS: dict[str, tuple[str, int]] = {
    "extractor-discogs": ("extractor-discogs", 8000),
    "extractor-musicbrainz": ("extractor-musicbrainz", 8000),
    "graphinator": ("graphinator", 8001),
    "tableinator": ("tableinator", 8002),
    "dashboard": ("dashboard", 8003),
    "api": ("api", 8005),
    "explore": ("explore", 8007),
    "insights": ("insights", 8009),
    "brainztableinator": ("brainztableinator", 8010),
    "brainzgraphinator": ("brainzgraphinator", 8011),
}


_QUEUE_FAMILIES = (
    "queue_coarse_metrics",
    "queue_consumer_count",
    "queue_delivery_metrics",
    "queue_exchange_metrics",
)

# metric name -> row field for gauges; counters are rate-converted below.
_GAUGE_METRICS = {
    "rabbitmq_detailed_queue_messages_ready": "messages_ready",
    "rabbitmq_detailed_queue_messages_unacked": "messages_unacknowledged",
    "rabbitmq_detailed_queue_consumers": "consumers",
}
_ACK_METRIC = "rabbitmq_detailed_queue_messages_acked_total"
_PUBLISH_METRIC = "rabbitmq_detailed_queue_exchange_messages_published_total"

# Previous counter sample per queue: (monotonic seconds, published_total, acked_total).
_previous_counters: dict[str, tuple[float, float, float]] = {}


def _queue_prefixes() -> tuple[str, ...]:
    return (DISCOGS_EXCHANGE_PREFIX, MUSICBRAINZ_EXCHANGE_PREFIX)


def _counter_rate(current: float, previous: float | None, elapsed: float) -> float:
    """Per-second rate from a counter delta; first sample, reset, or no elapsed time yields 0.0."""
    if previous is None or elapsed <= 0 or current < previous:
        return 0.0
    return (current - previous) / elapsed


def _parse_queue_samples(text: str, prefixes: tuple[str, ...]) -> dict[str, dict[str, float]]:
    """Parse ``/metrics/detailed`` text into per-queue values, keeping only contract-prefixed queues."""
    queues: dict[str, dict[str, float]] = {}
    for family in text_string_to_metric_families(text):
        for sample in family.samples:
            field_name = _GAUGE_METRICS.get(sample.name)
            if field_name is None and sample.name == _ACK_METRIC:
                field_name = "acked"
            elif field_name is None and sample.name == _PUBLISH_METRIC:
                field_name = "published"
            queue = sample.labels.get("queue", "")
            if field_name is None or not queue.startswith(prefixes):
                continue
            values = queues.setdefault(queue, {})
            # published is labelled per exchange; sum across exchanges for the queue.
            if field_name == "published":
                values[field_name] = values.get(field_name, 0.0) + sample.value
            else:
                values[field_name] = sample.value
    return queues


async def collect_queue_metrics(prometheus_url: str) -> list[dict[str, Any]]:
    """Read queue depths and counters from the RabbitMQ Prometheus ``/metrics/detailed`` endpoint.

    Publish/ack rates are derived from counter deltas against the previous
    collection (the first sample for a queue, or a counter reset, yields 0.0).
    Returns an empty list on any failure.
    """
    try:
        prefixes = _queue_prefixes()
        params: list[tuple[str, str | int | float | bool | None]] = [("family", f) for f in _QUEUE_FAMILIES]
        params.append(("queue", "(" + "|".join(re.escape(p) for p in prefixes) + ").*"))
        url = f"{prometheus_url.rstrip('/')}/metrics/detailed"
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, params=params)
        resp.raise_for_status()
        now = time.monotonic()
        queues = _parse_queue_samples(resp.text, prefixes)
        rows: list[dict[str, Any]] = []
        for name, values in queues.items():
            published = values.get("published", 0.0)
            acked = values.get("acked", 0.0)
            prev = _previous_counters.get(name)
            elapsed = now - prev[0] if prev else 0.0
            rows.append(
                {
                    "queue_name": name,
                    "messages_ready": int(values.get("messages_ready", 0)),
                    "messages_unacknowledged": int(values.get("messages_unacknowledged", 0)),
                    "consumers": int(values.get("consumers", 0)),
                    "publish_rate": _counter_rate(published, prev[1] if prev else None, elapsed),
                    "ack_rate": _counter_rate(acked, prev[2] if prev else None, elapsed),
                }
            )
            _previous_counters[name] = (now, published, acked)
        return rows
    except Exception:
        logger.warning("⚠️ Failed to collect queue metrics")
        return []


async def _check_one_service(
    client: httpx.AsyncClient,
    name: str,
    host: str,
    port: int,
) -> dict[str, Any]:
    """Check a single service health endpoint."""
    try:
        start = time.monotonic()
        resp = await client.get(f"http://{host}:{port}/health")
        elapsed = (time.monotonic() - start) * 1000
        status = "healthy" if resp.status_code == 200 else "unhealthy"
        return {
            "service_name": name,
            "status": status,
            "response_time_ms": round(elapsed, 2),
            "endpoint_stats": None,
        }
    except Exception:
        return {
            "service_name": name,
            "status": "unknown",
            "response_time_ms": 0.0,
            "endpoint_stats": None,
        }


async def collect_service_health(
    endpoints: dict[str, tuple[str, int]] | None = None,
) -> list[dict[str, Any]]:
    """Concurrent HTTP health checks across services.  Never raises."""
    eps = endpoints or SERVICE_ENDPOINTS
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            tasks = [_check_one_service(client, name, host, port) for name, (host, port) in eps.items()]
            results = await asyncio.gather(*tasks)
        return list(results)
    except Exception:
        logger.warning("⚠️ Failed to collect service health")
        return []


# ---------------------------------------------------------------------------
# Task 5: Persistence
# ---------------------------------------------------------------------------

_INSERT_QUEUE_SQL = """
INSERT INTO queue_metrics (recorded_at, queue_name, messages_ready, messages_unacknowledged, consumers, publish_rate, ack_rate)
VALUES (NOW(), %(queue_name)s, %(messages_ready)s, %(messages_unacknowledged)s, %(consumers)s, %(publish_rate)s, %(ack_rate)s)
"""

_INSERT_HEALTH_SQL = """
INSERT INTO service_health_metrics (recorded_at, service_name, status, response_time_ms, endpoint_stats)
VALUES (NOW(), %(service_name)s, %(status)s, %(response_time_ms)s, %(endpoint_stats)s)
"""


async def persist_metrics(
    pool: Any,
    queue_rows: list[dict[str, Any]],
    health_rows: list[dict[str, Any]],
) -> None:
    """Batch INSERT queue and health metrics rows.  No-op if both lists are empty."""
    if not queue_rows and not health_rows:
        return

    for row in health_rows:
        if row.get("endpoint_stats") is not None:
            row["endpoint_stats"] = Jsonb(row["endpoint_stats"])

    async with pool.connection() as conn, conn.cursor() as cur:
        if queue_rows:
            await cur.executemany(_INSERT_QUEUE_SQL, queue_rows)
        if health_rows:
            await cur.executemany(_INSERT_HEALTH_SQL, health_rows)


_DELETE_QUEUE_SQL = "DELETE FROM queue_metrics WHERE recorded_at < NOW() - make_interval(days => %s)"
_DELETE_HEALTH_SQL = "DELETE FROM service_health_metrics WHERE recorded_at < NOW() - make_interval(days => %s)"


async def prune_old_metrics(pool: Any, retention_days: int) -> None:
    """Delete metrics older than *retention_days* from both tables."""
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(_DELETE_QUEUE_SQL, (retention_days,))
        await cur.execute(_DELETE_HEALTH_SQL, (retention_days,))


# ---------------------------------------------------------------------------
# Task 5: Collector loop
# ---------------------------------------------------------------------------


async def run_collector(
    pool: Any,
    config: Any,
    metrics_buffer: MetricsBuffer,
) -> None:
    """Async loop: collect, persist, prune, sleep.  Re-raises ``CancelledError``."""
    while True:
        queue_rows: list[dict[str, Any]] = []
        health_rows: list[dict[str, Any]] = []

        # 1. Collect queue metrics
        try:
            queue_rows = await collect_queue_metrics(config.rabbitmq_prometheus_url)
        except Exception:
            logger.error("❌ Error collecting queue metrics")

        # 2. Collect service health
        try:
            health_rows = await collect_service_health()
        except Exception:
            logger.error("❌ Error collecting service health")

        # 3. Drain metrics buffer — an ATOMIC swap (not the non-destructive
        # flush()), so any request recorded during the persist `await` below
        # lands in the buffer's fresh deque and is never part of the samples
        # a failed persist has to restore or a successful one discards
        # (groovemap-qtts). Attach endpoint_stats to the API health row.
        endpoint_stats: dict[str, dict[str, Any]] = {}
        buffer_samples: deque[Any] = deque()
        try:
            endpoint_stats, buffer_samples = metrics_buffer.drain()
            if endpoint_stats:
                # Find existing API row or create synthetic one
                api_row = next((r for r in health_rows if r["service_name"] == "api"), None)
                if api_row is not None:
                    api_row["endpoint_stats"] = endpoint_stats
                else:
                    health_rows.append(
                        {
                            "service_name": "api",
                            "status": "healthy",
                            "response_time_ms": 0.0,
                            "endpoint_stats": endpoint_stats,
                        }
                    )
                logger.info("📊 Flushed endpoint stats", paths=len(endpoint_stats))
        except Exception:
            logger.error("❌ Error flushing metrics buffer")

        # 4. Persist — only drop the drained endpoint stats once they're durably
        # written. On failure (including cancellation) they are restored
        # AHEAD of anything recorded during this await, instead of being lost
        # (drain() already removed them from the live buffer, so a `clear()`-
        # style "leave them buffered" is no longer available — restore() is
        # the equivalent for the atomic-swap design).
        try:
            await persist_metrics(pool, queue_rows, health_rows)
            logger.info("📊 Persisted metrics", queues=len(queue_rows), health=len(health_rows))
        except asyncio.CancelledError:
            metrics_buffer.restore(buffer_samples)
            raise
        except Exception:
            logger.error("❌ Error persisting metrics")
            metrics_buffer.restore(buffer_samples)

        # 5. Prune
        try:
            await prune_old_metrics(pool, config.metrics_retention_days)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error("❌ Error pruning old metrics")

        # 6. Sleep
        await asyncio.sleep(config.metrics_collection_interval)
