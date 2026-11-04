"""One 504 for any Neo4j read that outlives its server-side budget.

Every helper in :mod:`api.queries.helpers` now carries a server timeout, so a slow read is terminated by
Neo4j and surfaces as ``Neo.ClientError.Transaction.TransactionTimedOut``. Routers that expected it
catch it themselves; this handler gives every other route the same 504 instead of a 500.
"""

from __future__ import annotations

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from neo4j.exceptions import ClientError as Neo4jClientError


logger = structlog.get_logger(__name__)

TIMEOUT_CODE_MARKER = "TransactionTimedOut"


def is_transaction_timeout(exc: BaseException) -> bool:
    """True when ``exc`` is Neo4j reporting that a transaction ran past its timeout."""
    return isinstance(exc, Neo4jClientError) and TIMEOUT_CODE_MARKER in (exc.code or str(exc))


async def neo4j_timeout_handler(request: Request, exc: Exception) -> JSONResponse:
    """Return 504 for a transaction timeout; any other Neo4j client error keeps its 500."""
    if not is_transaction_timeout(exc):
        raise exc
    logger.warning("⏱️ Neo4j read exceeded its server timeout", method=request.method, path=request.url.path)
    return JSONResponse(content={"error": "The query took too long and was stopped — narrow the request or retry"}, status_code=504)


def install_neo4j_timeout_handler(application: FastAPI) -> None:
    application.add_exception_handler(Neo4jClientError, neo4j_timeout_handler)
