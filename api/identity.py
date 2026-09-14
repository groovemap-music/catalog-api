"""Native identity on the read paths — resolve provider ids to native ids.

ADR 0009 in the ``design`` repository demotes every provider identifier to evidence and
keys each catalog entity by a native UUID version 7 that ``provider_aliases`` maps the
provider's id onto. The write side of that decision lives in
:mod:`common.identity`: ``resolve_aliases`` mints a catalog item for a provider id that
has none, and the SQL loaders and the collection sync call it inside their own
transaction.

A read path must not mint. A search hit, a recommendation, or a gap row is an entity the
caller is only looking at, and minting from a read would let a stale graph projection or
a caller-supplied identifier create catalog rows. So this module offers the lookup half
alone: one ``SELECT`` over the currently valid alias rows, joined against the batch's
keys through ``unnest``, and nothing else. A ref with no valid alias is simply absent
from the result and the response field comes back ``None``.

Every entry point here is read-only and non-raising. The native id is additive beside the
provider id the response already carried, so an unavailable alias table degrades one
field to ``None`` rather than failing a response that is otherwise fully served.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID

import structlog
from common.identity import AliasRef, catalog_kinds
from common.query_debug import execute_sql


if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Iterable


logger = structlog.get_logger(__name__)

__all__ = [
    "NativeIdCache",
    "catalog_ref",
    "configure",
    "lookup_native_ids",
    "lookup_owned_copy_ids",
    "native_ids_for",
    "native_ids_for_pairs",
    "resolve_native_ids",
]

# The provider every read path in this service resolves against. Discogs is the catalog
# the graph and the collection rows are keyed on; a second provider becomes an argument
# rather than a second function.
DEFAULT_PROVIDER = "discogs"

# One statement per batch. The join against `unnest` makes the batch a single lookup, the
# same shape `common.identity` uses on the write side, and the partial unique index on
# (provider, entity_kind, external_id) WHERE valid_to IS NULL answers it.
_SELECT_NATIVE_IDS = """
SELECT k.provider, k.entity_kind, k.external_id, alias.native_id
FROM unnest(%s::text[], %s::text[], %s::text[]) AS k(provider, entity_kind, external_id)
JOIN provider_aliases AS alias
  ON alias.provider = k.provider
 AND alias.entity_kind = k.entity_kind
 AND alias.external_id = k.external_id
WHERE alias.valid_to IS NULL
"""

# The owned copy is the native identity of a physical copy (ADR 0009); the collection row
# it came from carries the back-link. Reading it by (user_id, release_id) keeps the lookup
# owner-scoped, so one user's copy id can never reach another user's response.
_SELECT_OWNED_COPY_IDS = """
SELECT release_id, owned_copy_id
FROM user_collections
WHERE user_id = %s::uuid
  AND owned_copy_id IS NOT NULL
  AND release_id = ANY(%s::bigint[])
"""

_pool: Any = None


def configure(pool: Any) -> None:
    """Wire the PostgreSQL pool from ``api.api`` startup."""
    global _pool
    _pool = pool


def catalog_ref(entity_kind: str, external_id: Any, *, provider: str = DEFAULT_PROVIDER) -> AliasRef | None:
    """Build an alias ref for a catalog entity, or ``None`` when there can be none.

    Read paths carry kinds the alias table never keys — a graph traversal surfaces genre
    and style nodes beside artists and labels, and those are name-keyed rather than
    catalog entities. Returning ``None`` for them keeps the filtering in one place
    instead of at every call site, and keeps :class:`AliasRef`'s constructor validation
    (which raises on an unknown kind) off the response path.
    """
    if entity_kind not in catalog_kinds():
        return None
    external = str(external_id) if external_id is not None else ""
    if not external:
        return None
    try:
        return AliasRef(provider, entity_kind, external)
    except ValueError:
        logger.debug("🔎 Skipping unresolvable alias ref", provider=provider, entity_kind=entity_kind)
        return None


async def lookup_native_ids(conn: Any, refs: Iterable[AliasRef]) -> dict[AliasRef, UUID]:
    """Return the native id of every ref that currently has a valid alias.

    One ``SELECT`` for the whole batch and no write of any kind: unlike
    :func:`common.identity.resolve_aliases` this never mints, so a ref the alias table
    does not carry is returned absent rather than created.

    Runs on the caller's connection and opens no transaction of its own.

    Args:
        conn: A psycopg ``AsyncConnection``.
        refs: The provider identifiers to resolve. Duplicates collapse and the batch is
            ordered deterministically, so two callers submitting the same set send the
            same array values.

    Returns:
        A dict from ref to native id, holding only the refs that resolved.
    """
    ordered = sorted(dict.fromkeys(refs), key=lambda ref: (ref.provider, ref.entity_kind, ref.external_id))
    if not ordered:
        return {}

    async with conn.cursor() as cursor:
        await execute_sql(
            cursor,
            _SELECT_NATIVE_IDS,
            (
                [ref.provider for ref in ordered],
                [ref.entity_kind for ref in ordered],
                [ref.external_id for ref in ordered],
            ),
        )
        rows = await cursor.fetchall()

    return {AliasRef(provider, entity_kind, external_id): native_id for provider, entity_kind, external_id, native_id in rows}


async def resolve_native_ids(refs: Iterable[AliasRef], *, pool: Any = None) -> dict[AliasRef, UUID]:
    """Resolve refs through the configured pool, degrading to ``{}`` on any failure.

    The native id is additive beside a provider id the response already carries, so a
    pool that is not configured yet, or an alias table that is briefly unreachable, must
    cost the caller one ``None`` field and not the whole response.
    """
    active = pool if pool is not None else _pool
    if active is None:
        return {}
    ordered = list(dict.fromkeys(refs))
    if not ordered:
        return {}
    try:
        async with active.connection() as conn:
            return await lookup_native_ids(conn, ordered)
    except Exception:
        logger.warning("⚠️ Native id lookup failed; responses degrade to no native id", ref_count=len(ordered), exc_info=True)
        return {}


async def native_ids_for_pairs(
    pairs: Iterable[tuple[str, Any]],
    *,
    provider: str = DEFAULT_PROVIDER,
    pool: Any = None,
    cache: NativeIdCache | None = None,
) -> dict[tuple[str, str], str]:
    """Map ``(entity_kind, external_id)`` onto the native id, stringified for JSON.

    The read paths carry mixed kinds in one response — a search page holds artists,
    labels, masters, and releases — so the key is the pair rather than the id alone.
    Pairs whose kind is not a catalog kind are dropped before any statement runs.
    """
    refs: dict[tuple[str, str], AliasRef] = {}
    for entity_kind, external_id in pairs:
        ref = catalog_ref(entity_kind, external_id, provider=provider)
        if ref is not None:
            refs[(entity_kind, ref.external_id)] = ref
    if not refs:
        return {}

    resolved = await cache.resolve(refs.values()) if cache is not None else await resolve_native_ids(refs.values(), pool=pool)
    return {key: str(resolved[ref]) for key, ref in refs.items() if ref in resolved}


async def native_ids_for(
    entity_kind: str,
    external_ids: Iterable[Any],
    *,
    provider: str = DEFAULT_PROVIDER,
    pool: Any = None,
    cache: NativeIdCache | None = None,
) -> dict[str, str]:
    """Map the external ids of one entity kind onto their native ids, stringified."""
    by_pair = await native_ids_for_pairs(((entity_kind, external_id) for external_id in external_ids), provider=provider, pool=pool, cache=cache)
    return {external_id: native_id for (_kind, external_id), native_id in by_pair.items()}


async def lookup_owned_copy_ids(user_id: str, release_ids: Iterable[Any], *, pool: Any = None) -> dict[str, str]:
    """Map a user's Discogs release ids onto the native owned copy they hold.

    The owned copy is minted by the collection sync and back-linked from the collection
    row, so this reads ``user_collections.owned_copy_id`` rather than the alias table:
    the copy identifies one user's physical object, and no provider ever named it.

    Never raises — a failed lookup leaves the response without the field.
    """
    active = pool if pool is not None else _pool
    if active is None or not user_id:
        return {}
    wanted: list[int] = []
    for release_id in dict.fromkeys(release_ids):
        try:
            wanted.append(int(release_id))
        except TypeError, ValueError:
            continue
    if not wanted:
        return {}

    try:
        async with active.connection() as conn, conn.cursor() as cursor:
            await execute_sql(cursor, _SELECT_OWNED_COPY_IDS, (user_id, wanted))
            rows = await cursor.fetchall()
    except Exception:
        logger.warning("⚠️ Owned copy lookup failed; collection items degrade to no copy id", release_count=len(wanted), exc_info=True)
        return {}

    return {str(release_id): str(owned_copy_id) for release_id, owned_copy_id in rows if owned_copy_id is not None}


class NativeIdCache:
    """Memoize native id lookups for the life of one request.

    A single response can ask for the same entity more than once — a recommendation set
    and the gap rows behind it overlap, and an explore traversal names the entity it
    started from among its discoveries. The cache holds both halves of the answer: the
    refs that resolved, and the refs that are known to have no valid alias, so a miss is
    asked for once rather than on every pass.

    Scope it to one request. Alias validity changes under it, and a process-lifetime cache
    would keep serving the id of an alias that has since been closed.
    """

    __slots__ = ("_missing", "_pool", "_resolved")

    def __init__(self, pool: Any = None) -> None:
        self._pool = pool
        self._resolved: dict[AliasRef, UUID] = {}
        self._missing: set[AliasRef] = set()

    async def resolve(self, refs: Iterable[AliasRef]) -> dict[AliasRef, UUID]:
        """Return the native id of every ref that resolves, querying only what is new."""
        wanted = list(dict.fromkeys(refs))
        unknown = [ref for ref in wanted if ref not in self._resolved and ref not in self._missing]
        if unknown:
            found = await resolve_native_ids(unknown, pool=self._pool)
            self._resolved.update(found)
            self._missing.update(ref for ref in unknown if ref not in found)
        return {ref: self._resolved[ref] for ref in wanted if ref in self._resolved}

    def native_id(self, ref: AliasRef) -> str | None:
        """Return the already-resolved native id for one ref, stringified for JSON."""
        native = self._resolved.get(ref)
        return str(native) if native is not None else None
