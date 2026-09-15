"""Identifier lookup — ``GET /api/lookup/{provider}/{value}`` (ADR 0011).

The gesture this serves is the first one a collector performs in a shop: they have the
record in their hands, and the barcode on the sleeve, the catalogue number on the label, or
the inscription in the run-out groove is the only thing they can type. ADR 0011 mints those
three values as ``provider_aliases`` rows against the release's native id, so answering is
one indexed read rather than a search.

Three things about this surface are deliberate.

**The vocabulary is never spelled out here.** The addressable providers, and the
normalization each applies, are read off ``common.identifiers`` — the same vendored
vocabulary the loaders minted the rows from. Writing ``barcode``/``digits_only`` into this
module would let the reader normalize a value one way while the writer stored it another,
and a lookup that normalizes differently from the mint silently returns nothing. So the
provider list and the normalized value both come from
:func:`common.identifiers.alias_refs_for_release`, applied to a one-entry block, which is
the only normalization path the system has.

**Only namespaces that mint rows are addressable.** A label code or a rights society is
carried on the identifiers block and mints nothing, so asking for one is a rejected request
rather than an empty result — the caller learns the provider is not a lookup namespace
instead of concluding their record is not in the catalog.

**Not found is one answer.** A value no alias carries and a value whose alias points at no
loaded release row are both 404: a lookup that cannot show the caller a record found
nothing, and distinguishing the two would report an internal loading state as though it
were a fact about their record.

Public and rate limited exactly like search: the whole point is that somebody standing in a
shop can use it, and a sign-in is not something they can do with a record in one hand.
"""

from __future__ import annotations

from functools import cache
from typing import Annotated, Any, Final

import structlog
from common.identifiers import alias_identifier_types, alias_refs_for_release
from common.identity import new_id
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

import api.activity as activity
from api.dependencies import get_optional_user
from api.limiter import limiter
from api.models import LookupRelease, LookupResponse
from api.queries.lookup_queries import releases_for_native_id, resolve_alias_native_id


logger = structlog.get_logger(__name__)

router = APIRouter()

_pool: Any = None


def configure(pool: Any) -> None:
    """Wire the PostgreSQL pool into the lookup router."""
    global _pool
    _pool = pool


# The event this surface emits (ADR 0010). A lookup is a search with one term and one
# filter, and its published payload is the search payload, so it goes down as one rather
# than as a fourth event type nobody queries.
EVENT_SEARCH_QUERY: Final = "search.query"

# A value that survives all three normalizations to something non-empty, used once per
# process to learn which provider namespace each alias-bearing identifier type mints into.
# Digits-only reduces it to "1", the two whitespace rules leave it as it is; every rule
# yields a non-empty id, so every namespace reports itself.
_PROBE_VALUE: Final = "1A"


def _one_entry_block(identifier_type: str, value: str) -> dict[str, Any]:
    """Return a canonical identifiers block carrying exactly this one identifier.

    The block is the unit ``common.identifiers`` validates and extracts aliases from, so a
    single value is normalized by building the smallest valid block around it rather than by
    reaching for the private normalizer behind it.
    """
    return {
        "identifiers_version": "1",
        "items": [
            {
                "type": identifier_type,
                "value": value,
                "description": None,
                "source": {"provider": "discogs", "type": None, "field": "identifiers"},
            }
        ],
        "types": [identifier_type],
        "aliases": [],
        "unmapped": {"types": []},
    }


@cache
def _provider_identifier_types() -> dict[str, str]:
    """Map each addressable lookup provider onto the identifier type that mints it.

    Derived rather than declared: the vocabulary owns which types have an alias namespace
    and what each namespace is called, and asking it once per process is what keeps this
    module correct when the vocabulary gains a fourth namespace.
    """
    mapping: dict[str, str] = {}
    for identifier_type in alias_identifier_types():
        for ref in alias_refs_for_release(_one_entry_block(identifier_type, _PROBE_VALUE)):
            mapping[ref.provider] = identifier_type
    return mapping


def lookup_providers() -> tuple[str, ...]:
    """Return the provider namespaces this surface can resolve, sorted."""
    return tuple(sorted(_provider_identifier_types()))


def normalize_lookup_value(provider: str, value: str) -> str | None:
    """Return the value under ``provider``'s declared normalization, or ``None``.

    ``None`` means the value cannot key an alias: it normalized away to nothing, which is
    what a barcode field holding only punctuation does. The caller turns that into the same
    not-found answer an unknown value gets, because both describe a value that no row can
    carry.

    Raises:
        KeyError: If ``provider`` is not an addressable lookup namespace. Callers check
            :func:`lookup_providers` first.
    """
    identifier_type = _provider_identifier_types()[provider]
    for ref in alias_refs_for_release(_one_entry_block(identifier_type, value)):
        if ref.provider == provider:
            return ref.external_id
    return None


async def _record_lookup(user_id: str, provider: str, value: str, result_count: int) -> None:
    """Record the lookup as the ``search.query`` it is, filtered by its namespace.

    ADR 0010 records a query only for a caller the service can pseudonymise, so an anonymous
    lookup — the common case for this surface — leaves no behavioural record at all. A miss
    is recorded as readily as a hit: a barcode the catalog cannot resolve is the single most
    useful thing this surface can learn.
    """
    request_id = str(new_id())
    await activity.record_event(
        user_id,
        EVENT_SEARCH_QUERY,
        {"query": value, "filters": [f"lookup:{provider}"], "result_count": result_count, "request_id": request_id},
        idempotency_key=f"{EVENT_SEARCH_QUERY}:{request_id}",
    )


@router.get("/api/lookup/{provider}/{value}")
@limiter.limit("30/minute")
async def lookup_identifier(
    request: Request,  # noqa: ARG001 -- required by slowapi
    provider: str,
    value: str,
    current_user: Annotated[dict[str, Any] | None, Depends(get_optional_user)] = None,
) -> JSONResponse:
    """Resolve one catalogue identifier to the release or releases that carry it.

    ``provider`` is an ADR 0009 alias namespace: ``barcode``, ``catalog_number``, or
    ``matrix``. The value is normalized with that namespace's declared rule before it is
    looked up, so a barcode typed with its grouping spaces and one typed without resolve to
    the same row.

    Public, and rate limited to 30 requests a minute like search.
    """
    if _pool is None:
        return JSONResponse(content={"error": "Service not ready"}, status_code=503)

    providers = lookup_providers()
    if provider not in providers:
        return JSONResponse(
            content={"error": f"Invalid provider: {provider}. Valid: {', '.join(providers)}"},
            status_code=400,
        )

    normalized = normalize_lookup_value(provider, value)
    user_id = (current_user or {}).get("sub", "")

    native_id = await resolve_alias_native_id(_pool, provider, normalized) if normalized else None
    releases = await releases_for_native_id(_pool, native_id) if native_id is not None else []

    if user_id:
        await _record_lookup(user_id, provider, value, len(releases))

    if not releases:
        logger.debug("🔎 Identifier lookup found nothing", provider=provider)
        return JSONResponse(content={"error": f"No release found for {provider} '{value}'"}, status_code=404)

    body = LookupResponse(
        provider=provider,
        value=value,
        normalized=normalized or "",
        gm_id=str(native_id),
        releases=[LookupRelease(**release) for release in releases],
    )
    return JSONResponse(content=body.model_dump())
