"""Tests for pure run-out lexical spans and evidence supplied signature matching."""

from api.runout import AttributionStatus, SignatureRule, SpanKind, interpret, signature_for


def _rule(signature: str, role: str, candidate: str, source_ref: str = "synthetic://test") -> SignatureRule:
    return SignatureRule(signature, role, candidate, source_ref)


def test_interpret_preserves_original_and_exact_source_spans() -> None:
    inscription = "  A1 12345-1A / SYNTHETIC  "
    result = interpret(inscription, ())

    assert result.original == inscription
    assert "".join(span.text for span in result.spans) == inscription
    assert all(inscription[span.start : span.end] == span.text for span in result.spans)
    assert result.spans[0].kind is SpanKind.WHITESPACE
    assert any(span.text == "A1" and span.kind is SpanKind.ALPHANUMERIC for span in result.spans)
    assert any(span.text == "/" and span.kind is SpanKind.MARKS for span in result.spans)
    assert result.status is AttributionStatus.UNKNOWN
    assert "".join(span.text for span in result.unknown_spans) == inscription


def test_signature_collapses_whitespace_but_preserves_case_and_marks() -> None:
    assert signature_for("  AB-1   X/Y\t ") == "AB-1 X/Y"
    assert signature_for("ab-1 X/Y") != signature_for("AB-1 X/Y")
    assert signature_for("AB 1") != signature_for("AB-1")


def test_matches_exact_phrase_with_flexible_whitespace_and_original_offsets() -> None:
    inscription = "prefix PLANT\t X / suffix"
    result = interpret(inscription, (_rule("PLANT X", "pressing_plant", "fixture:plant-x"),))

    assert result.status is AttributionStatus.SUPPORTED
    assert len(result.matches) == 1
    match = result.matches[0]
    assert match.text == "PLANT\t X"
    assert inscription[match.start : match.end] == match.text
    assert match.start == inscription.index("PLANT")
    assert "".join(span.text for span in result.unknown_spans) == "prefix  / suffix"
    assert all(inscription[span.start : span.end] == span.text for span in result.unknown_spans)


def test_matching_requires_token_boundaries_and_preserves_case() -> None:
    rules = (_rule("AB", "pressing_plant", "fixture:ab"),)

    embedded = interpret("ZABZ", rules)
    different_case = interpret("ab", rules)
    exact_token = interpret("X AB-1", rules)

    assert embedded.status is AttributionStatus.UNKNOWN
    assert different_case.status is AttributionStatus.UNKNOWN
    assert exact_token.status is AttributionStatus.SUPPORTED


def test_same_candidate_from_multiple_sources_is_corroboration() -> None:
    result = interpret(
        "PLANT X",
        (
            _rule("PLANT X", "pressing_plant", "fixture:plant-x", "synthetic://source-a"),
            _rule("PLANT X", "pressing_plant", "fixture:plant-x", "synthetic://source-b"),
        ),
    )

    assert result.status is AttributionStatus.SUPPORTED
    assert len(result.matches[0].candidates) == 2
    assert {candidate.source_ref for candidate in result.accepted} == {"synthetic://source-a", "synthetic://source-b"}


def test_conflicting_same_signature_and_overlapping_signatures_are_ambiguous() -> None:
    same_signature = interpret(
        "PLANT X",
        (
            _rule("PLANT X", "pressing_plant", "fixture:plant-x"),
            _rule("PLANT X", "pressing_plant", "fixture:plant-y"),
        ),
    )
    overlapping = interpret(
        "PLANT X",
        (
            _rule("PLANT X", "pressing_plant", "fixture:plant-x"),
            _rule("X", "pressing_plant", "fixture:plant-y"),
        ),
    )

    assert same_signature.status is AttributionStatus.AMBIGUOUS
    assert same_signature.matches[0].status is AttributionStatus.AMBIGUOUS
    assert overlapping.status is AttributionStatus.AMBIGUOUS
    assert all(match.status is AttributionStatus.AMBIGUOUS for match in overlapping.matches)
    assert overlapping.accepted == ()
