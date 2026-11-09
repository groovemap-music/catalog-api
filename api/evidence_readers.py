"""Authority-separated projections over already-read catalog evidence.

This is a pure, read-only prototype. Callers provide records fetched through the existing
catalog, owner-scoped observation, or admin ledger path. It adds no endpoint and cannot
authenticate a request. Public catalog facts, one authenticated owner's observations, and
admin identity history are projected by separate functions with allowlisted output fields.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal


DEFAULT_LIMIT = 50
MAX_LIMIT = 100

_RELEASE_FIELDS = ("id", "name", "year", "artists", "labels", "genres", "styles", "formats")
_CREDIT_FIELDS = ("name", "role", "category", "artist_id", "artist_name")
_ALIAS_FIELDS = ("provider", "external_id", "source", "held_by", "valid_from", "valid_to", "opened_by", "closed_by")
_EVENT_FIELDS = (
    "at",
    "type",
    "item",
    "providers",
    "provider",
    "external_id",
    "source",
    "source_ref",
    "decision_ref",
    "dated_from",
    "rule",
    "superseded",
    "survivor",
    "compressed",
    "moves",
    "discogs_id_before",
    "discogs_id_after",
)


@dataclass(frozen=True, slots=True)
class CatalogFact:
    """A record from an existing public catalog query.

    ``kind`` identifies the query result shape, not a caller-selected visibility flag.
    ``fields`` may contain extra columns, but the projection only emits query fields listed
    in this module. ``source_ref`` is the record identifier available from the existing
    result; ``subject_ref`` is a separately supplied trusted subject context, not a match
    resolved by this module.
    """

    kind: Literal["release_details", "release_credit"]
    source_ref: str | None
    fields: Mapping[str, Any]
    subject_ref: str | None = None


@dataclass(frozen=True, slots=True)
class OwnerObservation:
    """An observation row paired with its owner from trusted server-side read context.

    ``owner_id`` must be bound by the existing owner-scoped query, using the authenticated
    principal. It must never be copied from a request parameter or an observation payload.
    """

    owner_id: str
    fields: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.owner_id, str) or not self.owner_id.strip():
            raise ValueError("owner_id must be bound by a trusted server-side read")


@dataclass(frozen=True, slots=True)
class IdentityLedger:
    """One payload returned by the existing admin-only identity ledger reader."""

    payload: Mapping[str, Any]


EvidenceRecord = CatalogFact | OwnerObservation | IdentityLedger


@dataclass(frozen=True, slots=True)
class EvidencePage:
    """A bounded projection and whether additional evidence was omitted."""

    items: tuple[dict[str, Any], ...]
    truncated: bool
    limit: int


def _limit(limit: int) -> int:
    if not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_LIMIT}")
    return limit


def _present(fields: Mapping[str, Any], allowlist: tuple[str, ...], *, preserve_nulls: bool = False) -> dict[str, Any]:
    """Copy allowlisted fields, optionally retaining explicit nulls from ledger rows."""
    return {key: fields[key] for key in allowlist if key in fields and (preserve_nulls or fields[key] is not None)}


def _rows(value: Any) -> tuple[Any, ...]:
    """Treat only materialized list-like source fields as row collections."""
    return tuple(value) if isinstance(value, (list, tuple)) else ()


def _alias_ref(fields: Mapping[str, Any]) -> str | None:
    provider = str(fields.get("provider") or "")
    external_id = str(fields.get("external_id") or "")
    return f"{provider}:{external_id}" if provider and external_id else None


def _entry(
    *,
    authority: str,
    subject_ref: str | None,
    field_ref: str,
    value: Any,
    source: str,
    source_ref: str | None,
    source_span: str | None,
    time_value: Any,
    time_meaning: str,
    method: str,
    review_state: str = "unknown",
    owner_asserted_confidence: float | None = None,
) -> dict[str, Any]:
    """Use one explicit shape while keeping asserted and reviewed confidence distinct."""
    return {
        "authority": authority,
        "subject_ref": subject_ref,
        "field_ref": field_ref,
        "value": value,
        "source": source,
        "source_ref": source_ref,
        "source_span": source_span,
        "time": {"value": time_value, "meaning": time_meaning},
        "method": method,
        "review_state": review_state,
        "owner_asserted_confidence": owner_asserted_confidence,
        "review_confidence": None,
    }


def _catalog_entries(record: CatalogFact) -> list[dict[str, Any]]:
    field_names: tuple[str, ...]
    if record.kind == "release_details":
        fields = _present(record.fields, _RELEASE_FIELDS)
        source = "underlying provider/source attribution unavailable in release-details result"
        method = "existing Neo4j release-details query projection"
        field_names = _RELEASE_FIELDS
    else:
        fields = _present(record.fields, _CREDIT_FIELDS)
        source = "underlying provider/source attribution unavailable in release-credits result"
        method = "existing Neo4j release-credits query projection"
        field_names = _CREDIT_FIELDS

    return [
        _entry(
            authority="public_catalog",
            subject_ref=record.subject_ref,
            field_ref=f"{record.kind}.{key}",
            value=fields[key],
            source=source,
            source_ref=record.source_ref,
            source_span=None,
            time_value=None,
            time_meaning="unknown; source query returns no fact timestamp",
            method=method,
        )
        for key in field_names
        if key in fields
    ]


def _observation_entries(record: OwnerObservation) -> list[dict[str, Any]]:
    fields = _present(record.fields, ("id", "owned_copy_id", "kind", "value", "source", "confidence", "observed_at", "created_at"))
    observation_id = str(fields.get("id") or "") or None
    source = str(fields.get("source") or "unknown")
    result: list[dict[str, Any]] = []
    for key in ("kind", "value"):
        if key not in fields:
            continue
        result.append(
            _entry(
                authority="owner_copy",
                subject_ref=str(fields.get("owned_copy_id") or "") or None,
                field_ref=f"observation.{key}",
                value=fields[key],
                source=source,
                source_ref=observation_id,
                source_span=None,
                time_value=fields.get("observed_at"),
                time_meaning="owner/delegate-asserted time the evidence applied; null if not recorded",
                method="stored observation row projection",
                owner_asserted_confidence=fields.get("confidence"),
            )
        )
    if observation_id is not None:
        result.append(
            _entry(
                authority="owner_copy",
                subject_ref=str(fields.get("owned_copy_id") or "") or None,
                field_ref="observation.created_at",
                value=fields.get("created_at"),
                source=source,
                source_ref=observation_id,
                source_span=None,
                time_value=fields.get("created_at"),
                time_meaning="row creation time; distinct from observed_at",
                method="stored observation row projection",
                owner_asserted_confidence=fields.get("confidence"),
            )
        )
    return result


def _ledger_entries(record: IdentityLedger) -> list[dict[str, Any]]:
    payload = record.payload
    result: list[dict[str, Any]] = []
    work = payload.get("work")
    if isinstance(work, Mapping):
        work_ref = str(work.get("gm_id") or "") or None
        for provider in _rows(work.get("providers", ())):
            if not isinstance(provider, Mapping):
                continue
            fields = _present(provider, ("provider", "external_id", "valid_from", "valid_to"), preserve_nulls=True)
            if fields:
                result.append(
                    _entry(
                        authority="admin_identity",
                        subject_ref=work_ref,
                        field_ref="identity_ledger.work.provider_alias",
                        value=fields,
                        source="provider_aliases via identity ledger",
                        source_ref=_alias_ref(fields),
                        source_span=None,
                        time_value=fields.get("valid_from"),
                        time_meaning="alias validity start; valid_to is the recorded validity end",
                        method="existing admin identity ledger projection",
                    )
                )
        result.extend(_event_entries(work.get("events", ()), work_ref, "work"))

    for edition in _rows(payload.get("editions", ())):
        if not isinstance(edition, Mapping):
            continue
        edition_ref = str(edition.get("gm_id") or "") or None
        for alias in _rows(edition.get("evidence", ())):
            if not isinstance(alias, Mapping):
                continue
            fields = _present(alias, _ALIAS_FIELDS, preserve_nulls=True)
            if fields:
                result.append(
                    _entry(
                        authority="admin_identity",
                        subject_ref=edition_ref,
                        field_ref="identity_ledger.edition.provider_alias",
                        value=fields,
                        source=str(fields.get("source") or "provider_aliases"),
                        source_ref=_alias_ref(fields),
                        source_span=None,
                        time_value=fields.get("valid_from"),
                        time_meaning="alias validity start; valid_to is the recorded validity end",
                        method="existing admin identity ledger projection",
                    )
                )
        result.extend(_event_entries(edition.get("events", ()), edition_ref, "edition"))
    return result


def _event_entries(events: Any, subject_ref: str | None, plane: str) -> list[dict[str, Any]]:
    if not isinstance(events, (list, tuple)):
        return []
    result: list[dict[str, Any]] = []
    for event in events:
        if not isinstance(event, Mapping):
            continue
        fields = _present(event, _EVENT_FIELDS, preserve_nulls=True)
        if not fields:
            continue
        event_type = str(fields.get("type") or "unknown")
        dated_from = str(fields.get("dated_from") or "unknown")
        result.append(
            _entry(
                authority="admin_identity",
                subject_ref=subject_ref,
                field_ref=f"identity_ledger.{plane}.event.{event_type}",
                value=fields,
                source=dated_from,
                source_ref=str(fields.get("source_ref") or fields.get("decision_ref") or "") or None,
                source_span=None,
                time_value=fields.get("at"),
                time_meaning=f"ledger event date attributed to {dated_from}",
                method="existing admin identity ledger event projection",
            )
        )
    return result


def _page(entries: list[dict[str, Any]], limit: int) -> EvidencePage:
    bounded = _limit(limit)
    return EvidencePage(tuple(entries[:bounded]), len(entries) > bounded, bounded)


def read_public_evidence(records: Iterable[EvidenceRecord], *, limit: int = DEFAULT_LIMIT) -> EvidencePage:
    """Project only public catalog query records, ignoring owner and admin records."""
    entries: list[dict[str, Any]] = []
    for record in records:
        if isinstance(record, CatalogFact):
            entries.extend(_catalog_entries(record))
    return _page(entries, limit)


def read_owner_evidence(records: Iterable[EvidenceRecord], *, trusted_owner_id: str, limit: int = DEFAULT_LIMIT) -> EvidencePage:
    """Project public catalog records and observations owned by the authenticated principal.

    ``trusted_owner_id`` must come from existing server auth state (for example
    ``UnifiedAuth.user_id``), never a client-declared owner identifier. Ownership is matched
    against the row-owner value attached by the existing owner-scoped reader. Ledger records
    are always omitted, including for a logged-in user.
    """
    if not isinstance(trusted_owner_id, str) or not trusted_owner_id.strip():
        raise ValueError("trusted_owner_id must be a non-empty authenticated principal id")
    entries: list[dict[str, Any]] = []
    for record in records:
        if isinstance(record, CatalogFact):
            entries.extend(_catalog_entries(record))
        elif isinstance(record, OwnerObservation) and record.owner_id == trusted_owner_id:
            entries.extend(_observation_entries(record))
    return _page(entries, limit)


def read_admin_identity_evidence(
    records: Iterable[EvidenceRecord], *, require_admin_payload: Mapping[str, Any], limit: int = DEFAULT_LIMIT
) -> EvidencePage:
    """Project only identity-ledger records after the existing admin dependency succeeds.

    ``require_admin_payload`` is the server-verified payload returned by
    ``api.dependencies.require_admin``; that dependency validates the admin JWT and, when a
    database pool is configured, checks that the active database user has ``is_admin=True``.
    This helper is not an authentication boundary and must not be wired to client-supplied
    claims.
    """
    if require_admin_payload.get("type") != "admin" or not require_admin_payload.get("sub"):
        raise PermissionError("a server-verified admin principal is required")
    entries: list[dict[str, Any]] = []
    for record in records:
        if isinstance(record, IdentityLedger):
            entries.extend(_ledger_entries(record))
    return _page(entries, limit)
