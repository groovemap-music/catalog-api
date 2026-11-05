"""Consolidated Neo4j Cypher query execution helpers.

Replaces duplicated ``_run_query`` / ``_run_single`` / ``_run_count`` across
multiple query modules with a single set of helpers that add:

- DEBUG-level query logging via :func:`common.query_debug.log_cypher_query`
- Optional ``PROFILE`` prefix when database profiling is enabled
- Best-effort ``EXPLAIN`` after query failure (when profiling is enabled)
"""

from __future__ import annotations

import logging
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from common.query_debug import (
    is_db_profiling,
    log_cypher_query,
    log_explain_result,
    log_profile_result,
)
from neo4j import Query

from api.telemetry import record_neo4j_query


if TYPE_CHECKING:
    from common import AsyncResilientNeo4jDriver

_logger = logging.getLogger(__name__)


class _DefaultTimeout:
    """Type of :data:`DEFAULT_TIMEOUT`, the "caller did not choose a budget" marker."""

    def __repr__(self) -> str:
        return "DEFAULT_TIMEOUT"


DEFAULT_TIMEOUT = _DefaultTimeout()

# Server-side budget for a read that names none. lux's db.transaction.timeout is 10 minutes, so before
# this default 119 of 150 request-path statements held Neo4j that long and the routers' 504 handlers
# could not fire in useful time (docs/cypher-performance-audit-2026-11-04.md, C1).
DEFAULT_READ_TIMEOUT_SECONDS = 30.0

Timeout = float | None | _DefaultTimeout


def resolve_timeout(timeout: Timeout) -> float | None:
    """Return the budget in seconds for ``timeout``; ``None`` means the caller opted out explicitly."""
    if isinstance(timeout, _DefaultTimeout):
        return DEFAULT_READ_TIMEOUT_SECONDS
    return timeout


_NAME_SEARCH_DEPTH = 4


def _statement_name(caller: Any) -> str:
    """Name a statement after the first public function that issued it, within a few frames.

    Query code often reaches Cypher through private helpers (``_count``, ``_fetch_batch``), so
    the direct caller would blend several public queries under one private name. Walk up at
    most ``_NAME_SEARCH_DEPTH`` frames to the first public function; if there is none, use the
    direct caller. The name is a code name, so no parameter value reaches a metric attribute.
    """
    frame = caller
    for _ in range(_NAME_SEARCH_DEPTH):
        if frame is None:
            break
        code_name = frame.f_code.co_name
        if not code_name.startswith(("_", "<")):
            return str(code_name)
        frame = frame.f_back
    return str(caller.f_code.co_name)


@contextmanager
def _timed_statement(name: str) -> Iterator[None]:
    """Record one statement's duration under its caller's name, with its error type if it fails.

    The name is the calling query function's code name, a fixed string, so no parameter value
    reaches a metric attribute.
    """
    start = time.perf_counter()
    try:
        yield
    except BaseException as exc:
        record_neo4j_query(name, time.perf_counter() - start, type(exc).__name__)
        raise
    record_neo4j_query(name, time.perf_counter() - start)


def _build_query(cypher: str, timeout: Timeout) -> Query | str:
    """Wrap ``cypher`` in a :class:`neo4j.Query` when a timeout is requested.

    ``session.run()`` has no ``timeout`` keyword argument — passing one merges
    it into the Cypher parameter map instead of applying a server-side
    transaction timeout (see neo4j._async.work.session.AsyncSession.run). A
    per-query timeout must be set via ``Query(cypher, timeout=...)``.
    """
    seconds = resolve_timeout(timeout)
    if seconds is not None:
        return Query(cypher, timeout=seconds)
    return cypher


async def _try_explain_on_error(
    driver: AsyncResilientNeo4jDriver,
    cypher: str,
    params: dict[str, Any] | None,
    error: BaseException,
    database: str | None = None,
) -> None:
    """Best-effort EXPLAIN after a query failure.

    Opens a new session, runs ``EXPLAIN {cypher}``, and logs the plan via
    :func:`log_explain_result`.  Any exception is silently swallowed because
    the database may be unreachable.
    """
    try:
        session_kwargs: dict[str, Any] = {}
        if database:
            session_kwargs["database"] = database
        async with driver.session(**session_kwargs) as session:
            result = await session.run(f"EXPLAIN {cypher}", params)
            summary = await result.consume()
            log_explain_result(cypher, params, summary, error)
    except Exception:  # noqa: S110
        pass  # nosec B110 — best-effort; DB may be unreachable


async def run_query(
    driver: AsyncResilientNeo4jDriver,
    cypher: str,
    *,
    timeout: Timeout = DEFAULT_TIMEOUT,
    database: str | None = None,
    **params: Any,
) -> list[dict[str, Any]]:
    """Execute a Cypher query and return all results as a list of dicts.

    Args:
        driver: The async Neo4j driver.
        cypher: The Cypher query string.
        timeout: Server-side budget in seconds. Omitted, it is ``DEFAULT_READ_TIMEOUT_SECONDS``; pass
            ``None`` only for an internal batch read or write that must outlive a request.
        database: Optional Neo4j database name.
        **params: Query parameters forwarded to ``session.run()``.

    Returns:
        A list of dictionaries, one per result record.
    """
    log_cypher_query(cypher, params)

    profiling = is_db_profiling()
    actual_cypher = f"PROFILE {cypher}" if profiling else cypher
    query = _build_query(actual_cypher, timeout)

    session_kwargs: dict[str, Any] = {}
    if database:
        session_kwargs["database"] = database

    with _timed_statement(_statement_name(sys._getframe(1))):
        try:
            async with driver.session(**session_kwargs) as session:
                result = await session.run(query, params)
                records = [dict(record) async for record in result]
                summary = await result.consume()
                if profiling:
                    log_profile_result(cypher, params, summary)
                return records
        except Exception as exc:
            if profiling:
                await _try_explain_on_error(driver, cypher, params, exc, database)
            raise


async def run_single(
    driver: AsyncResilientNeo4jDriver,
    cypher: str,
    *,
    timeout: Timeout = DEFAULT_TIMEOUT,
    database: str | None = None,
    **params: Any,
) -> dict[str, Any] | None:
    """Execute a Cypher query and return a single result, or ``None``.

    Args:
        driver: The async Neo4j driver.
        cypher: The Cypher query string.
        timeout: Server-side budget in seconds. Omitted, it is ``DEFAULT_READ_TIMEOUT_SECONDS``; pass
            ``None`` only for an internal batch read or write that must outlive a request.
        database: Optional Neo4j database name.
        **params: Query parameters forwarded to ``session.run()``.

    Returns:
        A dictionary for the single record, or ``None`` if not found.
    """
    log_cypher_query(cypher, params)

    profiling = is_db_profiling()
    actual_cypher = f"PROFILE {cypher}" if profiling else cypher
    query = _build_query(actual_cypher, timeout)

    session_kwargs: dict[str, Any] = {}
    if database:
        session_kwargs["database"] = database

    with _timed_statement(_statement_name(sys._getframe(1))):
        try:
            async with driver.session(**session_kwargs) as session:
                result = await session.run(query, params)
                record = await result.single()
                summary = await result.consume()
                if profiling:
                    log_profile_result(cypher, params, summary)
                return dict(record) if record else None
        except Exception as exc:
            if profiling:
                await _try_explain_on_error(driver, cypher, params, exc, database)
            raise


async def run_count(
    driver: AsyncResilientNeo4jDriver,
    cypher: str,
    *,
    timeout: Timeout = DEFAULT_TIMEOUT,
    database: str | None = None,
    **params: Any,
) -> int:
    """Execute a count Cypher query and return the integer result.

    Expects the query to return a single record with a ``total`` field.

    Args:
        driver: The async Neo4j driver.
        cypher: The Cypher query string.
        timeout: Server-side budget in seconds. Omitted, it is ``DEFAULT_READ_TIMEOUT_SECONDS``; pass
            ``None`` only for an internal batch read or write that must outlive a request.
        database: Optional Neo4j database name.
        **params: Query parameters forwarded to ``session.run()``.

    Returns:
        The count as an integer, or ``0`` if no record is returned.
    """
    log_cypher_query(cypher, params)

    profiling = is_db_profiling()
    actual_cypher = f"PROFILE {cypher}" if profiling else cypher
    query = _build_query(actual_cypher, timeout)

    session_kwargs: dict[str, Any] = {}
    if database:
        session_kwargs["database"] = database

    with _timed_statement(_statement_name(sys._getframe(1))):
        try:
            async with driver.session(**session_kwargs) as session:
                result = await session.run(query, params)
                record = await result.single()
                summary = await result.consume()
                if profiling:
                    log_profile_result(cypher, params, summary)
                return int(record["total"]) if record else 0
        except Exception as exc:
            if profiling:
                await _try_explain_on_error(driver, cypher, params, exc, database)
            raise
