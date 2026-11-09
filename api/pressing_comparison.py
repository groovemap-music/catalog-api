"""Pure comparison of a bounded, caller-selected set of known release editions.

No candidate discovery, identity resolution, persistence, or identity promotion occurs here.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import combinations
from typing import Any

from api.runout import AttributionStatus, SignatureRule, interpret


MAX_CANDIDATES = 12
MAX_OBSERVATIONS = 100
MAX_FIELDS = 32


@dataclass(frozen=True, slots=True, order=True)
class ProviderReference:
    """A source-scoped identifier; values from different providers never share a namespace."""

    provider: str
    entity_kind: str
    source_id: str

    def __post_init__(self) -> None:
        if not all((self.provider, self.entity_kind, self.source_id)):
            raise ValueError("provider references require provider, entity_kind, and source_id")


@dataclass(frozen=True, slots=True)
class CatalogField:
    """Known values for one field plus traceable source references; empty values mean unknown."""

    values: tuple[str | int, ...]
    source_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        if any(not isinstance(value, (str, int)) or isinstance(value, bool) for value in self.values):
            raise ValueError("catalog field values must be strings or integers")
        if self.values and not self.source_refs:
            raise ValueError("known catalog field values require source references")
        if any(not ref for ref in self.source_refs):
            raise ValueError("catalog field source references must be non-empty")


@dataclass(frozen=True, slots=True)
class KnownReleaseCandidate:
    """A release the caller has already selected, identified only by its native catalog ID."""

    native_release_id: str
    provider_refs: tuple[ProviderReference, ...]
    fields: Mapping[str, CatalogField]

    def __post_init__(self) -> None:
        if not self.native_release_id:
            raise ValueError("native_release_id must be non-empty")
        if not all(name for name in self.fields):
            raise ValueError("catalog field names must be non-empty")
        if len(self.fields) > MAX_FIELDS:
            raise ValueError(f"candidate fields exceed the {MAX_FIELDS}-field bound")


@dataclass(frozen=True, slots=True)
class CopyObservation:
    """An already-authorized observation tied to one physical owned-copy UUID."""

    owned_copy_id: str
    kind: str
    value: str
    source: str
    observed_at: str | None
    created_at: str | None
    observation_id: str
    confidence: float | None = None

    def __post_init__(self) -> None:
        if not all((self.owned_copy_id, self.kind, self.source, self.observation_id)):
            raise ValueError("observation requires copy, kind, source, and observation IDs")
        if self.confidence is not None and not 0.0 <= self.confidence <= 1.0:
            raise ValueError("observation confidence must be between 0 and 1")


@dataclass(frozen=True, slots=True)
class CandidateCheck:
    native_release_id: str
    status: str
    reasons: tuple[str, ...]
    source_refs: tuple[str, ...]
    field_checks: tuple[tuple[str, str, str, tuple[str, ...]], ...] = ()


@dataclass(frozen=True, slots=True)
class ObservationAssessment:
    observation_id: str
    owned_copy_id: str
    kind: str
    value: str
    source: str
    observed_at: str | None
    created_at: str | None
    confidence: float | None
    status: str
    candidate_checks: tuple[CandidateCheck, ...]
    runout_status: str | None = None
    runout_matches: tuple[Mapping[str, Any], ...] = ()
    runout_unknown_spans: tuple[Mapping[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class RankedObservation:
    field: str
    distinguishable_pairs: tuple[tuple[str, str], ...]
    known_pair_count: int
    unknown_pair_count: int
    total_pair_count: int
    fraction_numerator: int
    fraction_denominator: int

    @property
    def distinguishable_pair_fraction(self) -> float:
        return self.fraction_numerator / self.fraction_denominator

    @property
    def known_pair_fraction(self) -> float:
        return self.known_pair_count / self.total_pair_count


@dataclass(frozen=True, slots=True)
class PressingComparison:
    selected_copy_id: str
    candidates: tuple[KnownReleaseCandidate, ...]
    observations: tuple[ObservationAssessment, ...]
    ranked_observations: tuple[RankedObservation, ...]
    unresolved_candidates: tuple[str, ...]
    identity_promoted: bool = False


def _field_check(candidate: KnownReleaseCandidate, field: str, value: str) -> CandidateCheck:
    fact = candidate.fields.get(field)
    if fact is None or not fact.values:
        return CandidateCheck(candidate.native_release_id, "unknown", ("catalog_value_unknown",), () if fact is None else fact.source_refs)

    def matches(expected: str | int) -> bool:
        if isinstance(expected, int):
            try:
                parsed = int(value)
            except ValueError:
                return False
            return str(parsed) == value and parsed == expected
        return value == expected

    if any(matches(expected) for expected in fact.values):
        return CandidateCheck(candidate.native_release_id, "compatible", (), fact.source_refs)
    return CandidateCheck(candidate.native_release_id, "contradicts", ("observed_value_not_in_catalog_values",), fact.source_refs)


def _known_role_matches(parsed: Any) -> tuple[tuple[str, str], ...]:
    return tuple(sorted({(candidate.role, candidate.value) for candidate in parsed.accepted}))


def _assess_observation(
    observation: CopyObservation,
    candidates: Sequence[KnownReleaseCandidate],
    rules: Sequence[SignatureRule],
) -> ObservationAssessment:
    parsed = interpret(observation.value, rules) if observation.kind in {"matrix", "runout"} else None
    roles = _known_role_matches(parsed) if parsed is not None else ()
    checks: list[CandidateCheck] = []
    for candidate in candidates:
        checks_for_candidate = [_field_check(candidate, observation.kind, observation.value)]
        checks_for_candidate.extend(_field_check(candidate, role, value) for role, value in roles)
        if any(check.status == "contradicts" for check in checks_for_candidate):
            status = "contradicts"
        elif any(check.status == "compatible" for check in checks_for_candidate):
            status = "compatible"
        else:
            status = "unknown"
        reasons = tuple(sorted({reason for check in checks_for_candidate for reason in check.reasons}))
        refs = tuple(sorted({ref for check in checks_for_candidate for ref in check.source_refs}))
        details = [(observation.kind, observation.value, checks_for_candidate[0].status, checks_for_candidate[0].source_refs)]
        details.extend((role, value, check.status, check.source_refs) for (role, value), check in zip(roles, checks_for_candidate[1:], strict=True))
        checks.append(CandidateCheck(candidate.native_release_id, status, reasons, refs, tuple(details)))
    assessment_status = "inspectable"
    if any(check.status == "contradicts" for check in checks):
        assessment_status = "contradictory"
    elif parsed is not None and parsed.status is AttributionStatus.AMBIGUOUS:
        assessment_status = "ambiguous_runout"
    elif parsed is not None and parsed.status is AttributionStatus.UNKNOWN:
        assessment_status = "unknown_runout"
    return ObservationAssessment(
        observation_id=observation.observation_id,
        owned_copy_id=observation.owned_copy_id,
        kind=observation.kind,
        value=observation.value,
        source=observation.source,
        observed_at=observation.observed_at,
        created_at=observation.created_at,
        confidence=observation.confidence,
        status=assessment_status,
        candidate_checks=tuple(checks),
        runout_status=None if parsed is None else parsed.status.value,
        runout_matches=()
        if parsed is None
        else tuple(
            {
                "text": match.text,
                "start": match.start,
                "end": match.end,
                "status": match.status.value,
                "candidates": tuple(
                    {"role": candidate.role, "value": candidate.value, "source_ref": candidate.source_ref} for candidate in match.candidates
                ),
            }
            for match in parsed.matches
        ),
        runout_unknown_spans=()
        if parsed is None
        else tuple({"text": span.text, "start": span.start, "end": span.end} for span in parsed.unknown_spans),
    )


def _rank_fields(candidates: Sequence[KnownReleaseCandidate], observed_fields: set[str]) -> tuple[RankedObservation, ...]:
    fields = sorted({field for candidate in candidates for field in candidate.fields} - observed_fields)
    ranked: list[RankedObservation] = []
    total_pairs = len(candidates) * (len(candidates) - 1) // 2
    for field in fields:
        known_pairs = 0
        unknown_pairs = 0
        distinguishable: list[tuple[str, str]] = []
        for first, second in combinations(candidates, 2):
            first_fact = first.fields.get(field)
            second_fact = second.fields.get(field)
            if first_fact is None or second_fact is None or not first_fact.values or not second_fact.values:
                unknown_pairs += 1
                continue
            known_pairs += 1
            if set(first_fact.values).isdisjoint(second_fact.values):
                distinguishable.append((first.native_release_id, second.native_release_id))
        if distinguishable:
            ranked.append(
                RankedObservation(field, tuple(distinguishable), known_pairs, unknown_pairs, total_pairs, len(distinguishable), total_pairs)
            )
    return tuple(sorted(ranked, key=lambda item: (-item.distinguishable_pair_fraction, -item.known_pair_count, item.field)))


def compare_known_candidates(
    selected_copy_id: str,
    candidates: Sequence[KnownReleaseCandidate],
    observations: Sequence[CopyObservation],
    *,
    runout_rules: Sequence[SignatureRule] = (),
) -> PressingComparison:
    """Compare explicit candidate releases and observations for one selected copy.

    Equal barcodes, cover art, or group identifiers are ordinary field values and never imply
    identity. Contradictory evidence is visible but does not prune the candidate set; ties remain.
    """
    if not selected_copy_id:
        raise ValueError("selected_copy_id must be non-empty")
    if not 2 <= len(candidates) <= MAX_CANDIDATES:
        raise ValueError(f"candidate count must be between 2 and {MAX_CANDIDATES}")
    if len(observations) > MAX_OBSERVATIONS:
        raise ValueError(f"observation count exceeds the {MAX_OBSERVATIONS}-item bound")
    ids = [candidate.native_release_id for candidate in candidates]
    if len(set(ids)) != len(ids):
        raise ValueError("candidate native release IDs must be unique; group repeated provider aliases under one candidate")
    if any(observation.owned_copy_id != selected_copy_id for observation in observations):
        raise ValueError("observations must belong to the explicitly selected physical copy")

    ordered_candidates = tuple(sorted(candidates, key=lambda item: item.native_release_id))
    ordered_observations = tuple(sorted(observations, key=lambda item: (item.observed_at or "", item.observation_id)))
    assessments = tuple(_assess_observation(observation, ordered_candidates, runout_rules) for observation in ordered_observations)
    observed_fields = {observation.kind for observation in ordered_observations}
    # Only unambiguous runout roles count as already observed fields; ambiguous spans remain inspectable.
    for assessment in assessments:
        for match in assessment.runout_matches:
            if match["status"] == AttributionStatus.SUPPORTED.value:
                observed_fields.update(candidate["role"] for candidate in match["candidates"])
    ranked = _rank_fields(ordered_candidates, observed_fields)
    unresolved = tuple(candidate.native_release_id for candidate in ordered_candidates)
    return PressingComparison(selected_copy_id, ordered_candidates, assessments, ranked, unresolved, identity_promoted=False)
