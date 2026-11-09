"""Synthetic cases for bounded comparison of already-known release candidates."""

from dataclasses import replace

import pytest

from api.pressing_comparison import (
    MAX_CANDIDATES,
    MAX_OBSERVATIONS,
    CatalogField,
    CopyObservation,
    KnownReleaseCandidate,
    ProviderReference,
    compare_known_candidates,
)
from api.runout import AttributionStatus, SignatureRule


COPY_A = "copy-a"


def _field(*values: str | int, ref: str = "catalog:fixture") -> CatalogField:
    return CatalogField(tuple(values), (ref,) if values else ())


def _candidate(native_id: str, **fields: CatalogField) -> KnownReleaseCandidate:
    return KnownReleaseCandidate(
        native_id,
        (ProviderReference("synthetic-provider", "release", f"source-{native_id}"),),
        fields,
    )


def _observation(kind: str, value: str, *, copy_id: str = COPY_A, observation_id: str = "obs-1") -> CopyObservation:
    return CopyObservation(copy_id, kind, value, "user", "2026-01-01T00:00:00Z", "2026-01-01T00:00:01Z", observation_id)


def test_identical_candidates_stay_indistinguishable_even_with_shared_barcode() -> None:
    candidates = (
        _candidate("native-a", barcode=_field("123", ref="discogs:barcode"), country=_field("US")),
        _candidate("native-b", barcode=_field("123", ref="mb:barcode"), country=_field("US")),
    )

    result = compare_known_candidates(COPY_A, candidates, ())

    assert result.unresolved_candidates == ("native-a", "native-b")
    assert result.identity_promoted is False
    assert result.ranked_observations == ()
    assert [candidate.native_release_id for candidate in result.candidates] == ["native-a", "native-b"]


def test_discriminating_catalog_fields_rank_by_pair_fraction_and_unknowns_are_neutral() -> None:
    candidates = (
        _candidate("native-a", year=_field(1978), country=_field("US"), matrix=_field("A1")),
        _candidate("native-b", year=_field(1979), country=_field("US"), matrix=CatalogField((), ())),
        _candidate("native-c", year=_field(1980), country=_field("JP"), matrix=_field("C1")),
    )

    result = compare_known_candidates(COPY_A, candidates, ())

    assert result.ranked_observations[0].field == "year"
    year = result.ranked_observations[0]
    assert year.known_pair_count == 3
    assert year.unknown_pair_count == 0
    assert year.distinguishable_pairs == (("native-a", "native-b"), ("native-a", "native-c"), ("native-b", "native-c"))
    matrix = next(item for item in result.ranked_observations if item.field == "matrix")
    assert matrix.known_pair_count == 1
    assert matrix.unknown_pair_count == 2
    assert matrix.distinguishable_pair_fraction == 1 / 3
    assert matrix.known_pair_fraction == 1 / 3


def test_observed_contradictions_remain_inspectable_and_never_prune_candidates() -> None:
    candidates = (
        _candidate("native-a", country=_field("US")),
        _candidate("native-b", country=_field("JP")),
    )

    result = compare_known_candidates(COPY_A, candidates, (_observation("country", "CA"),))

    assessment = result.observations[0]
    assert assessment.status == "contradictory"
    assert {check.native_release_id for check in assessment.candidate_checks if check.status == "contradicts"} == {"native-a", "native-b"}
    assert result.unresolved_candidates == ("native-a", "native-b")


def test_rank_uses_all_candidate_pairs_so_sparse_unknown_fields_do_not_win() -> None:
    candidates = (
        _candidate("a", sparse=_field("one"), broad=_field("group-a")),
        _candidate("b", sparse=_field("two"), broad=_field("group-a")),
        _candidate("c", broad=_field("group-b")),
        _candidate("d", broad=_field("group-b")),
    )

    result = compare_known_candidates(COPY_A, candidates, ())

    assert [rank.field for rank in result.ranked_observations] == ["broad", "sparse"]
    assert result.ranked_observations[0].distinguishable_pair_fraction == 4 / 6
    assert result.ranked_observations[1].distinguishable_pair_fraction == 1 / 6
    assert result.ranked_observations[1].known_pair_count == 1
    assert result.ranked_observations[1].unknown_pair_count == 5


def test_supported_runout_is_applied_only_with_caller_supplied_signature_evidence() -> None:
    candidates = (
        _candidate("native-a", matrix=_field("NOT THE OBSERVED INSCRIPTION"), pressing_plant=_field("plant-a", ref="catalog:plant")),
        _candidate("native-b", pressing_plant=_field("plant-b", ref="catalog:plant")),
    )
    rules = (SignatureRule("PLANT A", "pressing_plant", "plant-a", "review:fixture"),)
    observation = _observation("matrix", "SIDE A / PLANT A", observation_id="matrix-1")

    result = compare_known_candidates(COPY_A, candidates, (observation,), runout_rules=rules)

    assessment = result.observations[0]
    assert assessment.runout_status == AttributionStatus.SUPPORTED.value
    assert assessment.status == "contradictory"
    assert assessment.candidate_checks[0].status == "contradicts"
    assert assessment.candidate_checks[0].source_refs == ("catalog:fixture", "catalog:plant")
    assert assessment.candidate_checks[0].field_checks == (
        ("matrix", "SIDE A / PLANT A", "contradicts", ("catalog:fixture",)),
        ("pressing_plant", "plant-a", "compatible", ("catalog:plant",)),
    )
    assert assessment.candidate_checks[1].status == "contradicts"
    assert all(item.field != "pressing_plant" for item in result.ranked_observations)


def test_ambiguous_and_unknown_runout_text_remains_visible_and_is_not_used_as_evidence() -> None:
    candidates = (
        _candidate("native-a", pressing_plant=_field("plant-a")),
        _candidate("native-b", pressing_plant=_field("plant-b")),
    )
    rules = (
        SignatureRule("PX", "pressing_plant", "plant-a", "review:one"),
        SignatureRule("PX", "pressing_plant", "plant-b", "review:two"),
    )
    ambiguous = _observation("matrix", "ETCH PX!", observation_id="one")
    unknown = _observation("matrix", "unmapped ✧ text", observation_id="two")

    result = compare_known_candidates(COPY_A, candidates, (ambiguous, unknown), runout_rules=rules)

    assert result.observations[0].status == "ambiguous_runout"
    assert result.observations[0].runout_status == AttributionStatus.AMBIGUOUS.value
    assert result.observations[0].runout_unknown_spans
    assert result.observations[1].status == "unknown_runout"
    assert any(item.field == "pressing_plant" for item in result.ranked_observations)
    assert result.unresolved_candidates == ("native-a", "native-b")


def test_supported_runout_span_is_used_when_another_span_is_ambiguous() -> None:
    candidates = (
        _candidate("native-a", pressing_plant=_field("plant-a")),
        _candidate("native-b", pressing_plant=_field("plant-b")),
    )
    rules = (
        SignatureRule("PLANT A", "pressing_plant", "plant-a", "review:supported"),
        SignatureRule("PX", "pressing_plant", "plant-b", "review:ambiguous-b"),
        SignatureRule("PX", "pressing_plant", "plant-c", "review:ambiguous-c"),
    )
    result = compare_known_candidates(COPY_A, candidates, (_observation("matrix", "PLANT A / PX"),), runout_rules=rules)

    assessment = result.observations[0]
    assert assessment.runout_status == AttributionStatus.AMBIGUOUS.value
    assert {match["status"] for match in assessment.runout_matches} == {"supported", "ambiguous"}
    assert assessment.candidate_checks[0].field_checks[1] == ("pressing_plant", "plant-a", "compatible", ("catalog:fixture",))
    assert assessment.candidate_checks[1].field_checks[1] == ("pressing_plant", "plant-a", "contradicts", ("catalog:fixture",))


def test_numeric_catalog_values_match_string_observations_without_normalizing_identifiers() -> None:
    candidates = (_candidate("native-a", year=_field(1978), barcode=_field("01978")), _candidate("native-b", year=_field(1979)))

    year_result = compare_known_candidates(COPY_A, candidates, (_observation("year", "1978"),))
    barcode_result = compare_known_candidates(COPY_A, candidates, (_observation("barcode", "1978"),))
    malformed_numeric = compare_known_candidates(COPY_A, candidates, (_observation("year", "nineteen"),))

    assert {check.native_release_id: check.status for check in year_result.observations[0].candidate_checks} == {
        "native-a": "compatible",
        "native-b": "contradicts",
    }
    assert barcode_result.observations[0].candidate_checks[0].status == "contradicts"
    assert malformed_numeric.observations[0].candidate_checks[0].status == "contradicts"


def test_duplicate_provider_aliases_stay_under_one_native_candidate_and_copies_do_not_mix() -> None:
    candidate = KnownReleaseCandidate(
        "native-a",
        (
            ProviderReference("discogs", "release", "100"),
            ProviderReference("musicbrainz", "release", "200"),
        ),
        {"year": _field(1980)},
    )
    candidate_b = _candidate("native-b", year=_field(1981))
    first = _observation("matrix", "A1", copy_id="copy-a", observation_id="one")
    first = replace(first, confidence=0.73)
    second = _observation("matrix", "B2", copy_id="copy-b", observation_id="two")

    result = compare_known_candidates("copy-a", (candidate, candidate_b), (first,))
    second_result = compare_known_candidates("copy-b", (candidate, candidate_b), (second,))
    assert len(result.candidates) == 2
    assert len(result.observations) == 1
    assert result.observations[0].observation_id == "one"
    assert result.observations[0].confidence == 0.73
    assert second_result.observations[0].observation_id == "two"
    assert second_result.observations[0].owned_copy_id == "copy-b"
    with pytest.raises(ValueError, match="explicitly selected physical copy"):
        compare_known_candidates("copy-a", (candidate, candidate_b), (first, second))


def test_repeated_native_candidate_ids_are_rejected_instead_of_counting_aliases_as_options() -> None:
    candidate = _candidate("native-a", year=_field(1980))
    with pytest.raises(ValueError, match="unique"):
        compare_known_candidates(COPY_A, (candidate, replace(candidate)), ())


def test_candidate_and_observation_bounds_and_source_references_are_enforced() -> None:
    candidates = tuple(_candidate(f"native-{index}", year=_field(index)) for index in range(MAX_CANDIDATES + 1))
    with pytest.raises(ValueError, match="candidate count"):
        compare_known_candidates(COPY_A, candidates, ())
    two = (_candidate("native-a", year=_field(1)), _candidate("native-b", year=_field(2)))
    observations = tuple(_observation("note", "x", observation_id=str(index)) for index in range(MAX_OBSERVATIONS + 1))
    with pytest.raises(ValueError, match="observation count"):
        compare_known_candidates(COPY_A, two, observations)
    with pytest.raises(ValueError, match="source references"):
        CatalogField(("US",), ())
    with pytest.raises(ValueError, match="provider references"):
        ProviderReference("", "release", "1")


def test_malformed_candidate_observation_and_field_inputs_are_rejected() -> None:
    with pytest.raises(ValueError, match="strings or integers"):
        CatalogField((True,), ("source",))
    with pytest.raises(ValueError, match="source references must be non-empty"):
        CatalogField(("US",), ("",))
    with pytest.raises(ValueError, match="native_release_id"):
        KnownReleaseCandidate("", (), {})
    with pytest.raises(ValueError, match="field names"):
        KnownReleaseCandidate("native", (), {"": CatalogField((), ())})
    with pytest.raises(ValueError, match="field bound"):
        KnownReleaseCandidate("native", (), {f"f{index}": CatalogField((), ()) for index in range(33)})
    with pytest.raises(ValueError, match="observation requires"):
        CopyObservation("", "matrix", "x", "user", None, None, "obs")
    with pytest.raises(ValueError, match="confidence"):
        replace(_observation("matrix", "x"), confidence=1.1)
    with pytest.raises(ValueError, match="selected_copy_id"):
        compare_known_candidates("", (_candidate("a"), _candidate("b")), ())
