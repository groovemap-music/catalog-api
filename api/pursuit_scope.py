"""Pure scope eligibility and denominator accounting for transient Missing previews.

The caller supplies one catalog snapshot and an already-authorized folded collection. This
module performs no reads, writes, identity resolution, or network work. Unknown scope fields
and uncertain identities remain explicit evidence rather than being treated as a match.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum


class TargetKind(StrEnum):
    ARTIST = "artist"
    LABEL = "label"


class UnitKind(StrEnum):
    EDITION = "edition"
    WORK = "work"


class IdentityState(StrEnum):
    CONFIRMED = "confirmed"
    AMBIGUOUS = "ambiguous"
    UNKNOWN = "unknown"


class UnknownPolicy(StrEnum):
    NEEDS_REVIEW = "needs_review"
    INCLUDE = "include"
    EXCLUDE = "exclude"


class CoReleasePolicy(StrEnum):
    INCLUDE = "include"
    NEEDS_REVIEW = "needs_review"
    EXCLUDE = "exclude"


class UnitStatus(StrEnum):
    OWNED = "owned"
    MISSING = "missing"
    NEEDS_REVIEW = "needs_review"
    OWNERSHIP_REVIEW = "ownership_review"
    EXCLUDED = "excluded"


@dataclass(frozen=True, slots=True)
class ReleaseFact:
    """One provider release row with separate source and native edition identities.

    ``media_keys`` may contain canonical media-family and medium IDs. Country values are
    compared exactly as supplied by the catalog. ``None`` means unknown; an empty tuple
    means known to have no value.
    """

    release_id: str
    edition_id: str | None
    artist_ids: tuple[str, ...] | None
    label_ids: tuple[str, ...] | None
    media_keys: tuple[str, ...] | None
    year: int | None
    countries: tuple[str, ...] | None
    release_identity: IdentityState = IdentityState.CONFIRMED
    work_id: str | None = None
    work_identity: IdentityState = IdentityState.UNKNOWN
    source_provider: str = "discogs"

    def __post_init__(self) -> None:
        if not self.release_id:
            raise ValueError("release_id must be non-empty")
        if not self.source_provider:
            raise ValueError("source_provider must be non-empty")
        if self.edition_id == "":
            raise ValueError("edition_id must be non-empty when supplied")
        if self.work_id == "":
            raise ValueError("work_id must be non-empty when supplied")


@dataclass(frozen=True, slots=True)
class CatalogSnapshot:
    """Identified, caller-materialized catalog facts for one preview calculation."""

    snapshot_id: str
    releases: tuple[ReleaseFact, ...]
    # Keys absent from this map, or set to None, have unknown ancestry. Empty tuples are
    # known roots; values list all known ancestors in parent-to-root order.
    label_ancestors: Mapping[str, tuple[str, ...] | None] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.snapshot_id:
            raise ValueError("snapshot_id must be non-empty")
        seen: dict[tuple[str, str], ReleaseFact] = {}
        for release in self.releases:
            source_key = (release.source_provider, release.release_id)
            prior = seen.get(source_key)
            if prior is not None and prior != release:
                raise ValueError(f"catalog snapshot has conflicting rows for source release {source_key!r}")
            seen[source_key] = release


@dataclass(frozen=True, slots=True)
class AuthorizedCollection:
    """The authorized, folded collection IDs consumed by a preview.

    Use :meth:`from_folded` with the existing ``fit_queries.fold_collection`` result. The
    ``release_ids`` are provider IDs (Discogs in the existing fit fold). The caller supplies
    provider-to-native-edition candidates; only a single candidate confirms ownership.
    """

    snapshot_id: str
    source_provider: str
    release_ids: tuple[str, ...]
    provider_to_edition_candidates: Mapping[str, tuple[str, ...]]

    def __post_init__(self) -> None:
        if not self.snapshot_id:
            raise ValueError("collection snapshot_id must be non-empty")
        if not self.source_provider:
            raise ValueError("collection source_provider must be non-empty")

    @classmethod
    def from_folded(
        cls,
        snapshot_id: str,
        folded: Mapping[str, object],
        *,
        source_provider: str,
        provider_to_edition_candidates: Mapping[str, Sequence[str]],
    ) -> AuthorizedCollection:
        """Adapt the existing folded source IDs using caller-supplied native identity evidence."""
        raw_ids = folded.get("release_ids")
        if not isinstance(raw_ids, Sequence) or isinstance(raw_ids, str) or any(not isinstance(value, str) for value in raw_ids):
            raise ValueError("folded collection release_ids must be a sequence of strings")
        if any(
            not isinstance(key, str) or not isinstance(candidates, Sequence) or isinstance(candidates, str)
            for key, candidates in provider_to_edition_candidates.items()
        ):
            raise ValueError("provider-to-edition mappings must map source IDs to sequences of native IDs")
        if any(any(not isinstance(value, str) for value in candidates) for candidates in provider_to_edition_candidates.values()):
            raise ValueError("provider-to-edition candidates must contain strings")
        if any(not candidate for candidates in provider_to_edition_candidates.values() for candidate in candidates):
            raise ValueError("provider-to-edition candidates must be non-empty IDs")
        return cls(
            snapshot_id=snapshot_id,
            source_provider=source_provider,
            release_ids=tuple(sorted(set(raw_ids))),
            provider_to_edition_candidates={key: tuple(sorted(set(candidates))) for key, candidates in provider_to_edition_candidates.items()},
        )

    @property
    def confirmed_edition_ids(self) -> frozenset[str]:
        """Native editions reached by exactly one candidate for an owned provider ID."""
        return frozenset(
            self.provider_to_edition_candidates[release_id][0]
            for release_id in self.release_ids
            if len(self.provider_to_edition_candidates.get(release_id, ())) == 1
        )

    @property
    def unresolved_provider_ids(self) -> tuple[str, ...]:
        """Owned source IDs with no mapping or multiple possible native editions."""
        return tuple(release_id for release_id in self.release_ids if len(self.provider_to_edition_candidates.get(release_id, ())) != 1)

    @property
    def ambiguous_provider_candidates(self) -> Mapping[str, tuple[str, ...]]:
        return {
            source_id: candidates for source_id in self.release_ids if len(candidates := self.provider_to_edition_candidates.get(source_id, ())) > 1
        }


@dataclass(frozen=True, slots=True)
class PursuitScope:
    """A bounded artist or label scope and its explicit eligibility policies."""

    target_kind: TargetKind
    target_id: str
    unit_kind: UnitKind = UnitKind.EDITION
    media_any: tuple[str, ...] = ()
    year_from: int | None = None
    year_to: int | None = None
    countries_any: tuple[str, ...] = ()
    unknown_policy: UnknownPolicy = UnknownPolicy.NEEDS_REVIEW
    co_release_policy: CoReleasePolicy = CoReleasePolicy.NEEDS_REVIEW
    include_sublabels: bool = False
    target_identity: IdentityState = IdentityState.CONFIRMED

    def __post_init__(self) -> None:
        if not self.target_id:
            raise ValueError("target_id must be non-empty")
        if self.year_from is not None and self.year_to is not None and self.year_from > self.year_to:
            raise ValueError("year_from must not be after year_to")
        if self.target_kind is TargetKind.ARTIST and self.include_sublabels:
            raise ValueError("include_sublabels applies only to label scopes")

    @property
    def scope_key(self) -> tuple[object, ...]:
        """Stable, hashable scope identity for downstream crate comparisons."""
        return (
            self.target_kind.value,
            self.target_id,
            self.unit_kind.value,
            tuple(sorted(set(self.media_any))),
            self.year_from,
            self.year_to,
            tuple(sorted(set(self.countries_any))),
            self.unknown_policy.value,
            self.co_release_policy.value,
            self.include_sublabels,
            self.target_identity.value,
        )


@dataclass(frozen=True, slots=True)
class UnitEvidence:
    """Why one scope unit was counted, withheld, or excluded."""

    unit_id: str | None
    unit_kind: UnitKind
    unit_key: tuple[str, str]
    release_ids: tuple[str, ...]
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DenominatorEvidence:
    """Checked known eligible-unit denominator and separate unresolved evidence counts."""

    denominator: int
    eligible: int
    owned: int
    missing: int
    ownership_review: int
    needs_review: int
    excluded: int
    unknown_excluded: int
    denominator_complete: bool
    ownership_complete: bool

    def __post_init__(self) -> None:
        if (
            min(
                self.denominator,
                self.eligible,
                self.owned,
                self.missing,
                self.ownership_review,
                self.needs_review,
                self.excluded,
                self.unknown_excluded,
            )
            < 0
        ):
            raise ValueError("denominator counts must be non-negative")
        if self.denominator != self.owned + self.missing + self.ownership_review or self.eligible != self.denominator:
            raise ValueError("denominator must equal known eligible units: owned + missing + ownership_review")
        if self.denominator_complete and (self.needs_review != 0 or self.unknown_excluded != 0):
            raise ValueError("denominator cannot be complete while unresolved or unknown-excluded evidence remains")
        if self.ownership_complete and self.ownership_review:
            raise ValueError("ownership cannot be complete while ownership review remains")

    @property
    def completion_fraction(self) -> float | None:
        if not self.denominator_complete or not self.ownership_complete or not self.denominator:
            return None
        return self.owned / self.denominator


@dataclass(frozen=True, slots=True)
class ScopePreview:
    """Transient preview result with snapshot, scope, units, and denominator evidence."""

    catalog_snapshot_id: str
    collection_snapshot_id: str
    scope: PursuitScope
    owned: tuple[UnitEvidence, ...]
    missing: tuple[UnitEvidence, ...]
    ownership_review: tuple[UnitEvidence, ...]
    needs_review: tuple[UnitEvidence, ...]
    excluded: tuple[UnitEvidence, ...]
    denominator_evidence: DenominatorEvidence

    def __post_init__(self) -> None:
        counts = self.denominator_evidence
        if (len(self.owned), len(self.missing), len(self.ownership_review), len(self.needs_review), len(self.excluded)) != (
            counts.owned,
            counts.missing,
            counts.ownership_review,
            counts.needs_review,
            counts.excluded,
        ):
            raise ValueError("preview evidence lists do not match denominator counts")

    @property
    def scope_key(self) -> tuple[object, ...]:
        """Stable scope identity for a downstream comparison consumer."""
        return self.scope.scope_key


class _Eligibility(StrEnum):
    ELIGIBLE = "eligible"
    NEEDS_REVIEW = "needs_review"
    EXCLUDED = "excluded"


@dataclass(frozen=True, slots=True)
class _ReleaseDecision:
    eligibility: _Eligibility
    reasons: tuple[str, ...]


@dataclass(slots=True)
class _UnitBucket:
    unit_id: str | None
    unit_key: tuple[str, str]
    release_ids: set[tuple[str, str]] = field(default_factory=set)
    eligible_release_ids: set[tuple[str, str]] = field(default_factory=set)
    confirmed_owned_release_ids: set[tuple[str, str]] = field(default_factory=set)
    ownership_uncertain: bool = False
    reasons: set[str] = field(default_factory=set)
    needs_review: bool = False
    excluded_release_ids: set[tuple[str, str]] = field(default_factory=set)
    excluded_reasons: set[str] = field(default_factory=set)


def _values(values: tuple[str, ...] | None) -> set[str]:
    return set(values or ())


def _scope_membership(
    release: ReleaseFact,
    scope: PursuitScope,
    snapshot: CatalogSnapshot,
) -> tuple[bool, bool, tuple[str, ...]]:
    """Return matched, unknown, and reasons for artist/label ownership of a release."""
    credits = release.artist_ids if scope.target_kind is TargetKind.ARTIST else release.label_ids
    if credits is None:
        return False, True, (f"unknown_{scope.target_kind.value}_credits",)
    target_match = scope.target_id in credits
    unknown_ancestry = False
    if scope.target_kind is TargetKind.LABEL and scope.include_sublabels and not target_match:
        for label_id in credits:
            if label_id not in snapshot.label_ancestors or snapshot.label_ancestors[label_id] is None:
                unknown_ancestry = True
            elif scope.target_id in (snapshot.label_ancestors[label_id] or ()):
                target_match = True
                break
    if target_match:
        if scope.target_identity is not IdentityState.CONFIRMED:
            return True, True, (f"{scope.target_identity.value}_target_identity",)
        return True, False, ()
    if unknown_ancestry:
        return False, True, ("unknown_sublabel_ancestry",)
    return False, False, ()


def _apply_unknown_policy(scope: PursuitScope, reasons: Sequence[str]) -> _ReleaseDecision:
    detail = tuple(sorted(set(reasons)))
    if scope.unknown_policy is UnknownPolicy.INCLUDE:
        return _ReleaseDecision(_Eligibility.ELIGIBLE, (*detail, "unknown_fields_included_by_policy"))
    if scope.unknown_policy is UnknownPolicy.EXCLUDE:
        return _ReleaseDecision(_Eligibility.EXCLUDED, (*detail, "unknown_fields_excluded_by_policy"))
    return _ReleaseDecision(_Eligibility.NEEDS_REVIEW, detail)


def _decide_release(release: ReleaseFact, scope: PursuitScope, snapshot: CatalogSnapshot) -> _ReleaseDecision:
    matched, unknown_membership, membership_reasons = _scope_membership(release, scope, snapshot)
    if not matched and not unknown_membership:
        return _ReleaseDecision(_Eligibility.EXCLUDED, ("outside_target_scope",))

    unknown_reasons = list(membership_reasons)
    credits = release.artist_ids if scope.target_kind is TargetKind.ARTIST else release.label_ids
    if (matched or unknown_membership) and scope.target_identity is not IdentityState.CONFIRMED:
        unknown_reasons.append(f"{scope.target_identity.value}_target_identity")
    co_release_review = False
    co_release_excluded = False
    if matched and credits is not None and len(set(credits)) > 1:
        if scope.co_release_policy is CoReleasePolicy.EXCLUDE:
            co_release_excluded = True
        if scope.co_release_policy is CoReleasePolicy.NEEDS_REVIEW:
            co_release_review = True

    if scope.media_any:
        if release.media_keys is None:
            unknown_reasons.append("unknown_media")
        elif not (_values(scope.media_any) & _values(release.media_keys)):
            return _ReleaseDecision(_Eligibility.EXCLUDED, ("media_filter_mismatch",))
    if scope.year_from is not None or scope.year_to is not None:
        if release.year is None:
            unknown_reasons.append("unknown_year")
        elif (scope.year_from is not None and release.year < scope.year_from) or (scope.year_to is not None and release.year > scope.year_to):
            return _ReleaseDecision(_Eligibility.EXCLUDED, ("year_filter_mismatch",))
    if scope.countries_any:
        if release.countries is None:
            unknown_reasons.append("unknown_country")
        elif not (_values(scope.countries_any) & _values(release.countries)):
            return _ReleaseDecision(_Eligibility.EXCLUDED, ("country_filter_mismatch",))
    if co_release_excluded:
        return _ReleaseDecision(_Eligibility.EXCLUDED, ("co_release_excluded_by_policy",))
    if co_release_review:
        return _ReleaseDecision(_Eligibility.NEEDS_REVIEW, (*unknown_reasons, "co_release_requires_review"))
    if (matched or unknown_membership) and scope.target_identity is not IdentityState.CONFIRMED:
        return _ReleaseDecision(_Eligibility.NEEDS_REVIEW, (*unknown_reasons, "target_identity_requires_review"))
    if unknown_reasons:
        return _apply_unknown_policy(scope, unknown_reasons)
    return _ReleaseDecision(_Eligibility.ELIGIBLE, ("scope_match",))


def _unit_reference(release: ReleaseFact, scope: PursuitScope) -> tuple[str | None, str | None]:
    """Return the stable grouping id, or an unresolved identity reason."""
    if scope.unit_kind is UnitKind.EDITION:
        if release.edition_id is None:
            return None, "unknown_native_edition_id"
        if release.release_identity is not IdentityState.CONFIRMED:
            return None, f"{release.release_identity.value}_release_identity"
        return release.edition_id, None
    if release.release_identity is not IdentityState.CONFIRMED:
        return None, f"{release.release_identity.value}_release_identity"
    if release.work_id is None:
        return None, "unknown_work_identity"
    if release.work_identity is not IdentityState.CONFIRMED:
        return None, f"{release.work_identity.value}_work_identity"
    return release.work_id, None


def _unit_key(unit_id: str | None, source_key: tuple[str, str], scope: PursuitScope) -> tuple[str, str]:
    """Return a canonical unit key or a per-record review key without inventing identity."""
    if unit_id is None:
        return (f"needs_review_{scope.unit_kind.value}", f"{source_key[0]}:{source_key[1]}")
    return (scope.unit_kind.value, unit_id)


def _source_key(release: ReleaseFact) -> tuple[str, str]:
    return (release.source_provider, release.release_id)


def preview_scope(snapshot: CatalogSnapshot, scope: PursuitScope, collection: AuthorizedCollection) -> ScopePreview:
    """Count owned, missing, and uncertain units in a caller-defined catalog snapshot.

    The denominator includes known eligible units even where collection ownership needs review.
    Unknown eligibility and unresolved identities are reported outside that known denominator.
    Work ownership requires a confirmed held native edition that qualifies for all scope filters.
    """
    releases_by_source: dict[tuple[str, str], ReleaseFact] = {}
    for release in snapshot.releases:
        releases_by_source.setdefault(_source_key(release), release)
    confirmed_collection_ids = set(collection.confirmed_edition_ids)
    buckets: dict[tuple[str, str], _UnitBucket] = {}
    work_owned: set[str] = set()
    if scope.unit_kind is UnitKind.WORK:
        work_owned = {
            release.work_id
            for release in releases_by_source.values()
            if release.edition_id in confirmed_collection_ids
            and release.release_identity is IdentityState.CONFIRMED
            and release.work_id is not None
            and release.work_identity is IdentityState.CONFIRMED
            and _decide_release(release, scope, snapshot).eligibility is _Eligibility.ELIGIBLE
        }

    for release in sorted(releases_by_source.values(), key=lambda item: (item.source_provider, item.release_id)):
        decision = _decide_release(release, scope, snapshot)
        unit_id, identity_reason = _unit_reference(release, scope)
        source_key = _source_key(release)
        key = _unit_key(unit_id, source_key, scope)
        bucket = buckets.setdefault(key, _UnitBucket(unit_id, key))
        if decision.eligibility is _Eligibility.EXCLUDED:
            bucket.excluded_release_ids.add(source_key)
            bucket.excluded_reasons.update(decision.reasons)
            continue

        bucket.release_ids.add(source_key)
        bucket.reasons.update(decision.reasons)
        if identity_reason:
            bucket.reasons.add(identity_reason)
            bucket.reasons.add("identity_requires_review")
            bucket.needs_review = True
            continue
        if decision.eligibility is _Eligibility.NEEDS_REVIEW:
            bucket.reasons.add("eligibility_requires_review")
            bucket.needs_review = True
            if scope.unit_kind is UnitKind.WORK and release.edition_id in confirmed_collection_ids and unit_id is not None:
                bucket.ownership_uncertain = True
            continue
        bucket.eligible_release_ids.add(source_key)
        if (scope.unit_kind is UnitKind.EDITION and release.edition_id in confirmed_collection_ids) or (
            scope.unit_kind is UnitKind.WORK and release.work_id in work_owned
        ):
            bucket.confirmed_owned_release_ids.add(source_key)

    for provider_id in collection.unresolved_provider_ids:
        candidates = collection.provider_to_edition_candidates.get(provider_id, ())
        possible_ids = (
            candidates
            if len(candidates) > 1
            else tuple(
                release.edition_id
                for release in snapshot.releases
                if release.source_provider == collection.source_provider and release.release_id == provider_id and release.edition_id is not None
            )
        )
        possible_units: set[tuple[str, str]] = set()
        for edition_id in possible_ids:
            for release in snapshot.releases:
                if release.edition_id != edition_id:
                    continue
                unit_id, identity_reason = _unit_reference(release, scope)
                if unit_id is not None:
                    possible_units.add(_unit_key(unit_id, _source_key(release), scope))
        for key in possible_units:
            bucket = buckets.setdefault(key, _UnitBucket(key[1], key))
            bucket.ownership_uncertain = True
            bucket.reasons.add("uncertain_collection_identity")
        if not possible_units:
            key = ("needs_review_collection_identity", f"{collection.source_provider}:{provider_id}")
            bucket = buckets.setdefault(key, _UnitBucket(None, key))
            bucket.needs_review = True
            bucket.reasons.add("unmapped_collection_identity" if not candidates else "ambiguous_collection_identity")

    owned: list[UnitEvidence] = []
    missing: list[UnitEvidence] = []
    ownership_review: list[UnitEvidence] = []
    needs_review: list[UnitEvidence] = []
    excluded: list[UnitEvidence] = []
    for key, bucket in sorted(buckets.items()):
        reasons = set(bucket.reasons)
        if bucket.confirmed_owned_release_ids:
            status = UnitStatus.OWNED
            reasons.add("confirmed_owned_release")
            release_ids = bucket.eligible_release_ids
        elif bucket.eligible_release_ids and bucket.ownership_uncertain:
            status = UnitStatus.OWNERSHIP_REVIEW
            reasons.add("ownership_requires_review")
            release_ids = bucket.eligible_release_ids
        elif bucket.eligible_release_ids:
            status = UnitStatus.MISSING
            reasons.add("not_in_authorized_collection")
            if bucket.needs_review:
                reasons.add("other_edition_eligibility_requires_review")
            release_ids = bucket.eligible_release_ids
        elif bucket.needs_review:
            status = UnitStatus.NEEDS_REVIEW
            release_ids = bucket.release_ids - bucket.excluded_release_ids
        elif bucket.excluded_release_ids:
            excluded.append(
                UnitEvidence(
                    bucket.unit_id,
                    scope.unit_kind,
                    bucket.unit_key,
                    tuple(sorted(f"{provider}:{source_id}" for provider, source_id in bucket.excluded_release_ids)),
                    tuple(sorted(bucket.excluded_reasons)),
                )
            )
            continue
        else:
            status = UnitStatus.NEEDS_REVIEW
            reasons.add("no_confirmed_eligible_release")
            release_ids = bucket.release_ids
        evidence = UnitEvidence(
            bucket.unit_id,
            scope.unit_kind,
            key,
            tuple(sorted(f"{provider}:{source_id}" for provider, source_id in release_ids)),
            tuple(sorted(reasons)),
        )
        if status is UnitStatus.OWNED:
            owned.append(evidence)
        elif status is UnitStatus.MISSING:
            missing.append(evidence)
        elif status is UnitStatus.OWNERSHIP_REVIEW:
            ownership_review.append(evidence)
        else:
            needs_review.append(evidence)

    excluded_evidence = tuple(excluded)
    owned_evidence = tuple(owned)
    missing_evidence = tuple(missing)
    ownership_review_evidence = tuple(ownership_review)
    review_evidence = tuple(needs_review)
    denominator = len(owned_evidence) + len(missing_evidence) + len(ownership_review_evidence)
    unknown_excluded = sum("unknown_fields_excluded_by_policy" in unit.reasons for unit in excluded_evidence)
    counts = DenominatorEvidence(
        denominator=denominator,
        eligible=denominator,
        owned=len(owned_evidence),
        missing=len(missing_evidence),
        ownership_review=len(ownership_review_evidence),
        needs_review=len(review_evidence),
        excluded=len(excluded_evidence),
        unknown_excluded=unknown_excluded,
        denominator_complete=not review_evidence and not any(bucket.needs_review for bucket in buckets.values()) and unknown_excluded == 0,
        ownership_complete=not ownership_review_evidence and not collection.unresolved_provider_ids,
    )
    return ScopePreview(
        catalog_snapshot_id=snapshot.snapshot_id,
        collection_snapshot_id=collection.snapshot_id,
        scope=scope,
        owned=owned_evidence,
        missing=missing_evidence,
        ownership_review=ownership_review_evidence,
        needs_review=review_evidence,
        excluded=excluded_evidence,
        denominator_evidence=counts,
    )
