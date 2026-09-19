"""Graph-backend selector — resolves a query family to its backend implementation.

Phase 0 of ADR 0012 (Neo4j -> PostgreSQL 19 property graph migration): this module is the
seam every later query-family rewrite plugs into. `ApiConfig.graph_backend` (read from
`GRAPH_BACKEND`) picks the backend by name; this module maps a *query family* — a named
group of query functions backing one feature, such as "collaborators" — to the module that
implements it for that backend.

Adding a family is one line in `_FAMILY_BACKENDS`. Registering a new backend implementation
for an existing family is one line in that family's mapping. Resolution returns the target
module itself (not a copy of its functions), so callers that reach a function through the
selector still see a `unittest.mock.patch` applied to that module's attribute, exactly as if
they had imported the module directly.

Each family also declares a `Protocol` and a typed accessor beside the mapping. The mapping
is typed `ModuleType`, which erases every signature it carries; the protocol is what puts
the signatures back, so mypy — not an integration test — is what catches a backend whose
implementation has drifted from its sibling's. See `CollaboratorsBackend`.
"""

from __future__ import annotations

from types import ModuleType
from typing import Any, Protocol, cast

import psycopg
from common.db_resilience import DatabaseUnavailableError
from neo4j.exceptions import ClientError as Neo4jClientError
from neo4j.exceptions import ServiceUnavailable as Neo4jServiceUnavailable
from neo4j.exceptions import SessionExpired as Neo4jSessionExpired

from api.queries import network_pg_queries, network_queries


class CollaboratorsBackend(Protocol):
    """The three query functions the "collaborators" family is made of.

    The first parameter is the backend's own connection handle — a Neo4j driver for
    `network_queries`, a PostgreSQL pool for `network_pg_queries` — so it is typed `Any`
    and is positional-only, which is what lets the two modules keep parameter names that
    say what they actually take. `depth` and `limit` are named in the protocol because the
    router passes them by keyword.

    A module satisfies a protocol structurally, so the two bindings below are the whole
    parity check: adding a backend whose `get_multi_hop_collaborators` returns the wrong
    type, drops `limit`, or renames `depth` fails `just typecheck` rather than a request.
    """

    async def get_artist_identity(self, handle: Any, artist_id: str, /) -> dict[str, Any] | None: ...

    async def get_multi_hop_collaborators(self, handle: Any, artist_id: str, /, depth: int = 2, limit: int = 50) -> list[dict[str, Any]]: ...

    async def count_multi_hop_collaborators(self, handle: Any, artist_id: str, /, depth: int = 2) -> int: ...


# The signature-parity assertions. They exist only to be type-checked: each name binds a
# module to the protocol, and mypy verifies the module's functions against it. Keeping them
# here rather than in either query module means neither backend imports the other.
_NEO4J_COLLABORATORS: CollaboratorsBackend = network_queries
_POSTGRES_COLLABORATORS: CollaboratorsBackend = network_pg_queries


# family name -> backend name -> module implementing that family's query functions.
_FAMILY_BACKENDS: dict[str, dict[str, ModuleType]] = {
    "collaborators": {
        "neo4j": network_queries,
        "postgres": network_pg_queries,
    },
}


def get_backend(family: str, backend: str) -> ModuleType:
    """Resolve *family* to the module implementing it for *backend*.

    Raises:
        KeyError: *family* is not registered, or has no implementation registered for
            *backend*.
    """
    try:
        family_backends = _FAMILY_BACKENDS[family]
    except KeyError:
        raise KeyError(f"Unknown graph query family: {family!r}") from None
    try:
        return family_backends[backend]
    except KeyError:
        raise KeyError(f"No {backend!r} implementation registered for graph query family {family!r}") from None


def get_collaborators_backend(backend: str) -> CollaboratorsBackend:
    """Resolve the "collaborators" family for *backend*, typed rather than as a module.

    The cast is sound because every module registered under that family is bound to
    `CollaboratorsBackend` above, which is where mypy checks it. Callers get a value whose
    three call sites are type-checked instead of an untyped `ModuleType`.
    """
    return cast("CollaboratorsBackend", get_backend("collaborators", backend))


# ── Backend-neutral error mapping ─────────────────────────────────────────────
# A query family's router must not care which backend raised a failure, and it must not
# conflate two different failure *kinds* that happen to come from the same backend. A
# query timeout ("this specific request took too long") and backend unavailability ("the
# database cannot be reached at all right now") are different situations with different
# retry advice, so they get two predicates, not one:
#
# `is_graph_query_timeout` — a statement/transaction-level timeout:
#   - Neo4j: a `ClientError` whose message contains "TransactionTimedOut" (or, more
#     rarely, "TransactionTimedOutClientConfiguration").
#   - PostgreSQL: `psycopg.errors.QueryCanceled` (SQLSTATE 57014), raised when the
#     session's `statement_timeout` fires and the server cancels the in-flight query.
#
# `is_graph_backend_unavailable` — the backend itself could not be reached or gave up
# before a query ever ran:
#   - Neo4j: `ServiceUnavailable` (no reachable server) or `SessionExpired` (the routing
#     table sent the driver to a server that is no longer serving this session) — both
#     `DriverError`, not `ClientError`, so they were never covered by the old
#     Neo4j-only `except ClientError` handlers and fell through to a bare 500; this gives
#     them the same 503 a not-configured backend already returns, instead of inventing a
#     third code.
#   - PostgreSQL: the connection pool's own `DatabaseUnavailableError` family —
#     `ConnectionEstablishmentError` after checkout has exhausted its retries,
#     `CircuitOpenError` once the breaker has tripped on repeated failures. This is the
#     pool's shape of "gave up waiting," raised in place of a bare `asyncio.TimeoutError`,
#     which `AsyncPostgreSQLPool` catches and retries internally rather than letting it
#     escape (see `common.postgres_resilient.AsyncPostgreSQLPool._pooled_connection`).
#     It reads like a timeout but it is not a *query* timeout — nothing about the request
#     itself was slow — so a router must not fold it into the 504/"try reducing depth or
#     limit" message; it belongs with 503, alongside every other "not ready" case.
#
# `GRAPH_BACKEND_ERROR_TYPES` is the tuple a router's `except` clause catches; the two
# predicates above are what it tests the caught exception against, in order (timeout,
# then unavailable) — a family that adds a third backend registers that backend's
# exception types in the tuple once, here, rather than teaching every router about them.
GRAPH_BACKEND_ERROR_TYPES: tuple[type[BaseException], ...] = (
    Neo4jClientError,
    Neo4jServiceUnavailable,
    Neo4jSessionExpired,
    psycopg.Error,
    DatabaseUnavailableError,
)

# The Neo4j and PostgreSQL shapes of "the backend could not be reached," tested by
# `is_graph_backend_unavailable`. Deliberately disjoint from `psycopg.errors.QueryCanceled`
# and the Neo4j `ClientError` timeout codes, which are query-level, not backend-level.
_BACKEND_UNAVAILABLE_ERRORS: tuple[type[BaseException], ...] = (
    Neo4jServiceUnavailable,
    Neo4jSessionExpired,
    DatabaseUnavailableError,
)


def is_graph_query_timeout(exc: BaseException) -> bool:
    """True when *exc* is a backend's way of saying a specific query ran out of time.

    Covers Neo4j's `TransactionTimedOut` / `TransactionTimedOutClientConfiguration`
    client errors (matched on message — the same substring check the pre-existing Neo4j
    handlers used, kept as-is so this is a refactor of that behavior, not a change to it)
    and PostgreSQL's statement-level cancellation (`QueryCanceled`).

    Deliberately excludes backend-unavailability — see `is_graph_backend_unavailable` —
    even though both eventually read as "the request didn't finish": only a genuine
    statement/transaction timeout belongs in the 504 this predicate drives a caller to
    return; a caller normally reacts to `False` by checking `is_graph_backend_unavailable`
    next, and re-raises only when neither predicate matches, so a real backend bug still
    surfaces as a 500 on both backends alike, exactly as it did before the PostgreSQL
    backend existed.
    """
    if isinstance(exc, Neo4jClientError):
        return "TransactionTimedOut" in str(exc)
    return isinstance(exc, psycopg.errors.QueryCanceled)


def is_graph_backend_unavailable(exc: BaseException) -> bool:
    """True when *exc* is a backend's way of saying it could not be reached at all.

    Covers Neo4j's `ServiceUnavailable` / `SessionExpired` driver errors and PostgreSQL's
    connection-pool `DatabaseUnavailableError` family (`ConnectionEstablishmentError`,
    `CircuitOpenError`). A caller normally reacts to `True` with the same 503 a
    not-configured backend already returns — never the query-timeout 504, since no query
    ran: `is_graph_query_timeout` is `False` for every case here.
    """
    return isinstance(exc, _BACKEND_UNAVAILABLE_ERRORS)


# ── Startup readiness for the PostgreSQL backend ─────────────────────────────
# `graph.catalog` is conditional: the `groovemap-database-schema` initializer declares it
# only on a PostgreSQL 19 server and only with its `SCHEMA_PROPERTY_GRAPH` switch on, and
# the persistence contract tells consumers to probe rather than assume. These constants
# restate that producer's `PROPERTY_GRAPH_MINIMUM_SERVER_VERSION`, `PROPERTY_GRAPH_SCHEMA`,
# and `PROPERTY_GRAPH_RELATION`; they are not imported because `groovemap-database-schema`
# is a dev dependency and nothing under `api/` may depend on it at runtime.
POSTGRES_GRAPH_MINIMUM_SERVER_VERSION = 190000
POSTGRES_GRAPH_SCHEMA = "graph"
POSTGRES_GRAPH_RELATION = "catalog"
POSTGRES_GRAPH_NAME = f"{POSTGRES_GRAPH_SCHEMA}.{POSTGRES_GRAPH_RELATION}"

_SERVER_VERSION_SQL = "SELECT current_setting('server_version_num')::int"

# A property graph is a relation, so it has a row in `pg_class` under its own relkind.
# Matching on the name alone is the same check the initializer makes before it creates one.
_PROPERTY_GRAPH_EXISTS_SQL = """
SELECT EXISTS (
    SELECT 1
    FROM pg_class AS relation
    JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
    WHERE namespace.nspname = %s AND relation.relname = %s
)
"""


class GraphBackendUnavailableError(RuntimeError):
    """The configured graph backend cannot be served by the connected database."""


async def verify_postgres_graph_backend(pool: Any) -> None:
    """Fail startup when `GRAPH_BACKEND=postgres` cannot be served by this server.

    Both gates are the ones the schema producer applies when it decides whether to declare
    the graph, checked here from the consumer's side: the server must speak SQL/PGQ, and
    the graph must actually be there. Failing here turns a misconfiguration into one clear
    message at boot instead of a `relation "graph.catalog" does not exist` on the first
    request that reaches the collaborators endpoint.

    Raises:
        GraphBackendUnavailableError: the server predates PostgreSQL 19, or `graph.catalog`
            has not been declared on it.
    """
    async with pool.connection() as conn, conn.cursor() as cursor_cm:
        cursor = cast("Any", cursor_cm)
        await cursor.execute(_SERVER_VERSION_SQL)
        version_row = await cursor.fetchone()
        server_version_num = int(version_row[0]) if version_row else 0
        if server_version_num < POSTGRES_GRAPH_MINIMUM_SERVER_VERSION:
            raise GraphBackendUnavailableError(
                f"GRAPH_BACKEND=postgres needs SQL/PGQ, which arrived in PostgreSQL 19 "
                f"(server_version_num {POSTGRES_GRAPH_MINIMUM_SERVER_VERSION}); this server reports "
                f"server_version_num {server_version_num}. Upgrade the server or set GRAPH_BACKEND=neo4j."
            )

        await cursor.execute(_PROPERTY_GRAPH_EXISTS_SQL, (POSTGRES_GRAPH_SCHEMA, POSTGRES_GRAPH_RELATION))
        exists_row = await cursor.fetchone()
        if not (exists_row and exists_row[0]):
            raise GraphBackendUnavailableError(
                f"GRAPH_BACKEND=postgres needs the {POSTGRES_GRAPH_NAME} property graph, which this server "
                f"does not have. Apply the groovemap-database-schema initializer with SCHEMA_PROPERTY_GRAPH "
                f"enabled, or set GRAPH_BACKEND=neo4j."
            )
