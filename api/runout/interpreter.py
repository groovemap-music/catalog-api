"""Deterministic run-out spans and exact, evidence supplied attributions.

This module never guesses plants, engineers, or pressing lineage. Callers provide reviewed
literal signatures together with their role, candidate, and source reference.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum


class SpanKind(StrEnum):
    """Lexical kind of a contiguous source span."""

    LETTERS = "letters"
    NUMBERS = "numbers"
    ALPHANUMERIC = "alphanumeric"
    MARKS = "marks"
    WHITESPACE = "whitespace"


class AttributionStatus(StrEnum):
    """Strength of exact, caller-supplied signature evidence."""

    SUPPORTED = "supported"
    AMBIGUOUS = "ambiguous"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class Span:
    """A half-open character range into the original inscription."""

    text: str
    start: int
    end: int
    kind: SpanKind


@dataclass(frozen=True, slots=True, order=True)
class Candidate:
    """A traceable attribution record supplied by a caller's reviewed dictionary."""

    role: str
    value: str
    source_ref: str


@dataclass(frozen=True, slots=True)
class SignatureRule:
    """A literal inscription signature and its attributed candidate."""

    signature: str
    role: str
    candidate: str
    source_ref: str

    @property
    def attribution(self) -> Candidate:
        return Candidate(self.role, self.candidate, self.source_ref)


@dataclass(frozen=True, slots=True)
class MatchedSpan:
    """An exact signature match inside an inscription, with source offsets."""

    signature: str
    text: str
    start: int
    end: int
    candidates: tuple[Candidate, ...]
    conflicts: bool = False

    @property
    def status(self) -> AttributionStatus:
        identities = {(candidate.role, candidate.value) for candidate in self.candidates}
        return AttributionStatus.AMBIGUOUS if self.conflicts or len(identities) > 1 else AttributionStatus.SUPPORTED


@dataclass(frozen=True, slots=True)
class Interpretation:
    """Full source-preserving parse with exact signature matches."""

    original: str
    signature: str
    spans: tuple[Span, ...]
    matches: tuple[MatchedSpan, ...]
    status: AttributionStatus

    @property
    def accepted(self) -> tuple[Candidate, ...]:
        """Unambiguous candidates only; ambiguous spans are never accepted."""
        return tuple(candidate for match in self.matches if match.status is AttributionStatus.SUPPORTED for candidate in match.candidates)

    @property
    def unknown_spans(self) -> tuple[Span, ...]:
        """Original lexical spans outside every matched signature, including their whitespace."""
        merged: list[tuple[int, int]] = []
        for start, end in sorted((match.start, match.end) for match in self.matches):
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
            else:
                merged.append((start, end))
        unknown: list[Span] = []
        cursor = 0
        for start, end in merged:
            if cursor < start:
                unknown.extend(_offset_spans(self.original, cursor, start))
            cursor = max(cursor, end)
        if cursor < len(self.original):
            unknown.extend(_offset_spans(self.original, cursor, len(self.original)))
        return tuple(unknown)


def signature_for(inscription: str) -> str:
    """Collapse whitespace while preserving inscription case and punctuation.

    Matrix normalization remains case preserving under ADR 0011. No alias normalization,
    case folding, punctuation stripping, or fuzzy matching is performed.
    """
    return re.sub(r"\s+", " ", inscription).strip()


def _char_kind(character: str) -> SpanKind:
    if character.isspace():
        return SpanKind.WHITESPACE
    if character.isalpha():
        return SpanKind.LETTERS
    if character.isdecimal():
        return SpanKind.NUMBERS
    if character.isalnum():
        return SpanKind.ALPHANUMERIC
    return SpanKind.MARKS


def _lexical_spans(inscription: str) -> tuple[Span, ...]:
    """Partition the original inscription into typed spans without discarding characters."""
    spans: list[Span] = []
    start = 0
    while start < len(inscription):
        initial = _char_kind(inscription[start])
        end = start + 1
        while end < len(inscription):
            kind = _char_kind(inscription[end])
            alphanumeric_run = initial in (SpanKind.LETTERS, SpanKind.NUMBERS, SpanKind.ALPHANUMERIC) and kind in (
                SpanKind.LETTERS,
                SpanKind.NUMBERS,
                SpanKind.ALPHANUMERIC,
            )
            if kind != initial and not alphanumeric_run:
                break
            end += 1
        text = inscription[start:end]
        if initial in (SpanKind.LETTERS, SpanKind.NUMBERS, SpanKind.ALPHANUMERIC):
            has_letters = any(character.isalpha() for character in text)
            has_numbers = any(character.isdecimal() for character in text)
            kind = SpanKind.ALPHANUMERIC if has_letters and has_numbers else initial
        else:
            kind = initial
        spans.append(Span(text, start, end, kind))
        start = end
    return tuple(spans)


def _offset_spans(inscription: str, start: int, end: int) -> list[Span]:
    """Lex a source slice while retaining its absolute offsets."""
    return [Span(span.text, start + span.start, start + span.end, span.kind) for span in _lexical_spans(inscription[start:end])]


def _matches(inscription: str, rules: Sequence[SignatureRule]) -> tuple[MatchedSpan, ...]:
    grouped: dict[tuple[str, int, int], set[Candidate]] = defaultdict(set)
    for rule in rules:
        signature = signature_for(rule.signature)
        if not signature:
            continue
        # Whitespace in signatures matches one or more source whitespace characters; every
        # other character is literal. Case and punctuation remain exact.
        pattern = re.compile(r"\s+".join(re.escape(part) for part in signature.split(" ")))
        for match in pattern.finditer(inscription):
            left = match.start() == 0 or not inscription[match.start() - 1].isalnum()
            right = match.end() == len(inscription) or not inscription[match.end()].isalnum()
            if left and right:
                grouped[(signature, match.start(), match.end())].add(rule.attribution)
    rows = [
        MatchedSpan(signature, inscription[start:end], start, end, tuple(sorted(candidates)))
        for (signature, start, end), candidates in sorted(grouped.items(), key=lambda item: (item[0][1], item[0][2], item[0][0]))
    ]
    conflicted: set[int] = set()
    for first_index, first in enumerate(rows):
        first_candidates: dict[str, set[str]] = defaultdict(set)
        for candidate in first.candidates:
            first_candidates[candidate.role].add(candidate.value)
        for second_index in range(first_index + 1, len(rows)):
            second = rows[second_index]
            if first.start >= second.end or second.start >= first.end:
                continue
            second_candidates: dict[str, set[str]] = defaultdict(set)
            for candidate in second.candidates:
                second_candidates[candidate.role].add(candidate.value)
            if any(first_candidates[role] != second_candidates[role] for role in first_candidates.keys() & second_candidates.keys()):
                conflicted.update((first_index, second_index))
    return tuple(
        MatchedSpan(match.signature, match.text, match.start, match.end, match.candidates, index in conflicted) for index, match in enumerate(rows)
    )


def interpret(inscription: str, rules: Sequence[SignatureRule]) -> Interpretation:
    """Match caller supplied signatures anywhere in the inscription, preserving source spans.

    Matches are literal and case sensitive with only flexible whitespace. A token embedded in a
    larger alphanumeric token is not matched. Multiple distinct candidate records for the same
    signature and location remain ambiguous.
    """
    signature = signature_for(inscription)
    matches = _matches(inscription, rules)
    if not matches:
        status = AttributionStatus.UNKNOWN
    elif any(match.status is AttributionStatus.AMBIGUOUS for match in matches):
        status = AttributionStatus.AMBIGUOUS
    else:
        status = AttributionStatus.SUPPORTED
    return Interpretation(inscription, signature, _lexical_spans(inscription), matches, status)


def exact_signature_candidates(inscription: str, rules: Sequence[SignatureRule]) -> tuple[Candidate, ...]:
    """Return candidates only when a rule signature equals the whole normalized inscription."""
    signature = signature_for(inscription)
    return tuple(sorted({rule.attribution for rule in rules if signature_for(rule.signature) == signature}))
