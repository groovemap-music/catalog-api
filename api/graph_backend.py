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

from api.queries import (
    admin_pg_queries,
    admin_queries,
    autocomplete_pg_queries,
    autocomplete_queries,
    collaborator_pg_queries,
    collaborator_queries,
    gap_pg_queries,
    gap_queries,
    neo4j_pg_queries,
    neo4j_queries,
    network_pg_queries,
    network_queries,
)


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


# ── The "autocomplete" family ────────────────────────────────────────────────
class AutocompleteBackend(Protocol):
    """The five name searches the "autocomplete" family is made of.

    The one family that answers without traversing: the Cypher side calls Neo4j's Lucene
    full-text indexes and the PostgreSQL side reads the `graph` vertex relations through
    their trigram indexes, so neither spelling needs `graph.catalog` and both run on every
    server tier.

    As in `CollaboratorsBackend`, the handle is the backend's own — a Neo4j driver or a
    PostgreSQL pool — and is positional-only so each module can name it for what it takes.
    `query` is positional-only for the same reason the routers pass it that way; `limit` is
    named because it is the one argument a caller varies.
    """

    async def autocomplete_artist(self, handle: Any, query: str, /, limit: int = 10) -> list[dict[str, Any]]: ...

    async def autocomplete_label(self, handle: Any, query: str, /, limit: int = 10) -> list[dict[str, Any]]: ...

    async def autocomplete_genre(self, handle: Any, query: str, /, limit: int = 10) -> list[dict[str, Any]]: ...

    async def autocomplete_style(self, handle: Any, query: str, /, limit: int = 10) -> list[dict[str, Any]]: ...

    async def autocomplete_person(self, handle: Any, query: str, /, limit: int = 10) -> list[dict[str, Any]]: ...


# The signature-parity assertions. They exist only to be type-checked: each name binds a
# module to the protocol, and mypy verifies the module's functions against it. Keeping them
# here rather than in either query module means neither backend imports the other.
_NEO4J_COLLABORATORS: CollaboratorsBackend = network_queries
_POSTGRES_COLLABORATORS: CollaboratorsBackend = network_pg_queries
_NEO4J_AUTOCOMPLETE: AutocompleteBackend = autocomplete_queries
_POSTGRES_AUTOCOMPLETE: AutocompleteBackend = autocomplete_pg_queries


# ── Coverage spike family 1: vertex lookups and store statistics (gm-catalog-api-91a.2) ──
# Nine functions across four modules, none of which traverses an edge: single-vertex
# lookups, a min/max over `Release.year`, and node/edge counts. Each below is its own
# family — one Protocol, one pair of backend modules — because the functions live in
# different Cypher modules with different call signatures; grouping them here is what the
# coverage spike calls "family 1" even though `graph_backend.py` sees four families. None
# needs `graph.catalog`: every PostgreSQL statement is a plain SELECT over a phase 0 view,
# so all four are registered `requires_property_graph=False` in the parity harness and run
# on every integration tier.


class CollaboratorIdentityBackend(Protocol):
    """The single-vertex lookup behind the Explore endpoint's collaborators panel."""

    async def get_artist_identity(self, handle: Any, artist_id: str, /) -> dict[str, Any] | None: ...


class GapMetadataBackend(Protocol):
    """The three single-vertex lookups behind "Complete My Collection"'s gap endpoints."""

    async def get_label_metadata(self, handle: Any, label_id: str, /) -> dict[str, Any] | None: ...

    async def get_artist_metadata(self, handle: Any, artist_id: str, /) -> dict[str, Any] | None: ...

    async def get_master_metadata(self, handle: Any, master_id: str, /) -> dict[str, Any] | None: ...


class CatalogOverviewBackend(Protocol):
    """The catalog-wide year range and the six-label node-count summary."""

    async def get_year_range(self, handle: Any, /) -> dict[str, int] | None: ...

    async def get_graph_stats(self, handle: Any, /) -> dict[str, int]: ...


class AdminStorageBackend(Protocol):
    """The admin storage panel's graph-shape summary.

    `get_neo4j_storage` keeps its name across both backends even though the PostgreSQL side
    reads no Neo4j store — see `api/queries/admin_pg_queries.py` for why, and for why this
    one function is proven by unit tests rather than the live parity harness.
    """

    async def get_neo4j_storage(self, handle: Any, /) -> dict[str, Any]: ...


_NEO4J_COLLABORATOR_IDENTITY: CollaboratorIdentityBackend = collaborator_queries
_POSTGRES_COLLABORATOR_IDENTITY: CollaboratorIdentityBackend = collaborator_pg_queries

_NEO4J_GAP_METADATA: GapMetadataBackend = gap_queries
_POSTGRES_GAP_METADATA: GapMetadataBackend = gap_pg_queries

_NEO4J_CATALOG_OVERVIEW: CatalogOverviewBackend = neo4j_queries
_POSTGRES_CATALOG_OVERVIEW: CatalogOverviewBackend = neo4j_pg_queries

_NEO4J_ADMIN_STORAGE: AdminStorageBackend = admin_queries
_POSTGRES_ADMIN_STORAGE: AdminStorageBackend = admin_pg_queries


# family name -> backend name -> module implementing that family's query functions.
_FAMILY_BACKENDS: dict[str, dict[str, ModuleType]] = {
    "collaborators": {
        "neo4j": network_queries,
        "postgres": network_pg_queries,
    },
    "autocomplete": {
        "neo4j": autocomplete_queries,
        "postgres": autocomplete_pg_queries,
    },
    "collaborator_identity": {
        "neo4j": collaborator_queries,
        "postgres": collaborator_pg_queries,
    },
    "gap_metadata": {
        "neo4j": gap_queries,
        "postgres": gap_pg_queries,
    },
    "catalog_overview": {
        "neo4j": neo4j_queries,
        "postgres": neo4j_pg_queries,
    },
    "admin_storage": {
        "neo4j": admin_queries,
        "postgres": admin_pg_queries,
    },
}


def registered_families() -> frozenset[str]:
    """Return every family name registered in `_FAMILY_BACKENDS`.

    What `tests/test_real_databases.py`'s coverage guard reads to check that a *family*, not
    just a function within one, was not silently left off the parity harness: registering a
    family and forgetting to also register it with the harness is otherwise invisible, the
    same gap `test_every_function_of_a_registered_family_is_covered_by_a_parity_call`'s own
    docstring warns a forgotten function is — one level up.
    """
    return frozenset(_FAMILY_BACKENDS)


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


def get_autocomplete_backend(backend: str) -> AutocompleteBackend:
    """Resolve the "autocomplete" family for *backend*, typed rather than as a module.

    Sound for the same reason `get_collaborators_backend` is: both registered modules are
    bound to `AutocompleteBackend` above, which is where mypy checks them.
    """
    return cast("AutocompleteBackend", get_backend("autocomplete", backend))


def get_collaborator_identity_backend(backend: str) -> CollaboratorIdentityBackend:
    """Resolve the "collaborator_identity" family for *backend*, typed rather than as a module."""
    return cast("CollaboratorIdentityBackend", get_backend("collaborator_identity", backend))


def get_gap_metadata_backend(backend: str) -> GapMetadataBackend:
    """Resolve the "gap_metadata" family for *backend*, typed rather than as a module."""
    return cast("GapMetadataBackend", get_backend("gap_metadata", backend))


def get_catalog_overview_backend(backend: str) -> CatalogOverviewBackend:
    """Resolve the "catalog_overview" family for *backend*, typed rather than as a module."""
    return cast("CatalogOverviewBackend", get_backend("catalog_overview", backend))


def get_admin_storage_backend(backend: str) -> AdminStorageBackend:
    """Resolve the "admin_storage" family for *backend*, typed rather than as a module."""
    return cast("AdminStorageBackend", get_backend("admin_storage", backend))


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
