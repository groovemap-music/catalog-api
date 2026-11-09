"""Hand-counted tests for the transient artist/label scope preview core."""

from dataclasses import replace

import pytest

from api.pursuit_scope import (
    AuthorizedCollection,
    CatalogSnapshot,
    CoReleasePolicy,
    DenominatorEvidence,
    IdentityState,
    PursuitScope,
    ReleaseFact,
    ScopePreview,
    TargetKind,
    UnitKind,
    UnknownPolicy,
    preview_scope,
)


def _release(
    release_id: str,
    *,
    artists: tuple[str, ...] | None = ("artist-a",),
    labels: tuple[str, ...] | None = ("label-a",),
    media: tuple[str, ...] | None = ("vinyl",),
    year: int | None = 2000,
    countries: tuple[str, ...] | None = ("US",),
    release_identity: IdentityState = IdentityState.CONFIRMED,
    work_id: str | None = None,
    work_identity: IdentityState = IdentityState.UNKNOWN,
    edition_id: str | None = None,
    source_provider: str = "discogs",
) -> ReleaseFact:
    if edition_id is None and release_identity is IdentityState.CONFIRMED:
        edition_id = f"native:{release_id}"
    return ReleaseFact(release_id, edition_id, artists, labels, media, year, countries, release_identity, work_id, work_identity, source_provider)


def _scope(**changes: object) -> PursuitScope:
    return replace(PursuitScope(TargetKind.ARTIST, "artist-a", co_release_policy=CoReleasePolicy.INCLUDE), **changes)


def _collection(*release_ids: str, mappings: dict[str, tuple[str, ...]] | None = None) -> AuthorizedCollection:
    native_map = mappings or {rid: (f"native:{rid}",) for rid in release_ids}
    return AuthorizedCollection.from_folded(
        "collection-r1", {"release_ids": list(release_ids)}, source_provider="discogs", provider_to_edition_candidates=native_map
    )


def _preview(
    releases: tuple[ReleaseFact, ...],
    scope: PursuitScope | None = None,
    collection: AuthorizedCollection | None = None,
    *,
    ancestors: dict[str, tuple[str, ...] | None] | None = None,
):
    snapshot = CatalogSnapshot("catalog-r1", releases, ancestors or {})
    return preview_scope(snapshot, scope or _scope(), collection or _collection())


def test_artist_scope_deduplicates_owned_copies_and_exposes_checked_keys() -> None:
    result = _preview(
        (
            _release("owned"),
            _release("missing"),
            _release("outside", artists=("artist-b",)),
        ),
        collection=_collection("owned", "owned"),
    )

    assert result.catalog_snapshot_id == "catalog-r1"
    assert result.collection_snapshot_id == "collection-r1"
    assert result.scope_key == result.scope.scope_key
    assert [unit.unit_id for unit in result.owned] == ["native:owned"]
    assert [unit.unit_id for unit in result.missing] == ["native:missing"]
    assert [unit.unit_key for unit in result.missing] == [("edition", "native:missing")]
    assert [unit.unit_id for unit in result.excluded] == ["native:outside"]
    assert result.denominator_evidence.denominator == 2
    assert result.denominator_evidence.eligible == 2
    assert result.denominator_evidence.denominator_complete


def test_snapshot_duplicate_rows_are_collapsed_but_conflicting_rows_fail_closed() -> None:
    release = _release("same")
    result = _preview((release, release))

    assert len(result.missing) == 1
    assert result.denominator_evidence.denominator == 1
    with pytest.raises(ValueError, match="conflicting rows"):
        CatalogSnapshot("bad", (release, replace(release, year=2001)))


def test_label_scope_can_include_known_sublabels_and_reports_unknown_ancestry() -> None:
    scope = _scope(target_kind=TargetKind.LABEL, target_id="parent", include_sublabels=True)
    result = _preview(
        (
            _release("child", artists=("other",), labels=("child-label",)),
            _release("unknown-child", artists=("other",), labels=("unindexed-label",)),
            _release("root", artists=("other",), labels=("root-label",)),
        ),
        scope,
        ancestors={"child-label": ("parent",), "root-label": ()},
    )

    assert [unit.unit_id for unit in result.missing] == ["native:child"]
    assert [unit.unit_id for unit in result.needs_review] == ["native:unknown-child"]
    assert [unit.unit_id for unit in result.excluded] == ["native:root"]
    assert "unknown_sublabel_ancestry" in result.needs_review[0].reasons
    assert result.denominator_evidence.denominator_complete is False


def test_label_exact_scope_does_not_include_children() -> None:
    scope = _scope(target_kind=TargetKind.LABEL, target_id="parent")
    result = _preview(
        (_release("child", artists=("other",), labels=("child-label",)),),
        scope,
        ancestors={"child-label": ("parent",)},
    )
    assert result.missing == ()
    assert result.denominator_evidence.excluded == 1


def test_media_year_and_country_filters_intersect_and_report_unknown_fields() -> None:
    scope = _scope(media_any=("vinyl", "cd"), year_from=1990, year_to=2010, countries_any=("US", "CA"))
    releases = (
        _release("eligible", media=("cd",), year=2000, countries=("CA",)),
        _release("wrong-media", media=("tape",)),
        _release("wrong-year", media=None, year=1980),
        _release("wrong-country", countries=("UK",)),
        _release("unknown-media", media=None),
        _release("unknown-year", year=None),
        _release("unknown-country", countries=None),
    )
    result = _preview(releases, scope)

    assert [unit.unit_id for unit in result.missing] == ["native:eligible"]
    assert {unit.unit_id for unit in result.excluded} == {"native:wrong-media", "native:wrong-year", "native:wrong-country"}
    assert {unit.unit_id for unit in result.needs_review} == {"native:unknown-media", "native:unknown-year", "native:unknown-country"}
    assert result.denominator_evidence.denominator == 1
    assert result.denominator_evidence.denominator_complete is False


def test_known_and_filters_exclude_even_if_another_filter_is_unknown() -> None:
    scope = _scope(media_any=("vinyl",), year_from=1990, unknown_policy=UnknownPolicy.INCLUDE)
    result = _preview((_release("known-year-false", media=None, year=1980),), scope)

    assert result.needs_review == ()
    assert result.missing == ()
    assert result.excluded[0].reasons == ("year_filter_mismatch",)


def test_unknown_policy_include_and_exclude_are_explicit_in_evidence() -> None:
    release = _release("unknown-media", media=None)
    scope = _scope(media_any=("vinyl",), unknown_policy=UnknownPolicy.INCLUDE)
    included = _preview((release,), scope)
    excluded = _preview((release,), replace(scope, unknown_policy=UnknownPolicy.EXCLUDE))

    assert [unit.unit_id for unit in included.missing] == ["native:unknown-media"]
    assert "unknown_fields_included_by_policy" in included.missing[0].reasons
    assert excluded.missing == ()
    assert excluded.denominator_evidence.denominator == 0
    assert excluded.denominator_evidence.unknown_excluded == 1
    assert excluded.denominator_evidence.denominator_complete is False


def test_country_filter_is_exact_and_known_empty_differs_from_unknown() -> None:
    scope = _scope(countries_any=("US",))
    result = _preview(
        (
            _release("wrong-case", countries=("us",)),
            _release("known-none", countries=()),
            _release("not-known", countries=None),
        ),
        scope,
    )

    assert {unit.unit_id for unit in result.excluded} == {"native:wrong-case", "native:known-none"}
    assert [unit.unit_id for unit in result.needs_review] == ["native:not-known"]


def test_unknown_target_credits_follow_explicit_policy_without_inventing_a_match() -> None:
    release = _release("unknown-credits", artists=None)
    review = _preview((release,))
    included = _preview((release,), _scope(unknown_policy=UnknownPolicy.INCLUDE))

    assert [unit.unit_id for unit in review.needs_review] == ["native:unknown-credits"]
    assert [unit.unit_id for unit in included.missing] == ["native:unknown-credits"]
    assert "unknown_artist_credits" in included.missing[0].reasons


def test_co_release_policies_hold_or_exclude_independent_of_unknown_include() -> None:
    release = _release("shared-credit", artists=("artist-a", "artist-b"), media=None)
    scope = _scope(media_any=("vinyl",), unknown_policy=UnknownPolicy.INCLUDE)
    review = _preview((release,), replace(scope, co_release_policy=CoReleasePolicy.NEEDS_REVIEW))
    included = _preview((release,), replace(scope, co_release_policy=CoReleasePolicy.INCLUDE))
    excluded = _preview((release,), replace(scope, co_release_policy=CoReleasePolicy.EXCLUDE))

    assert [unit.unit_id for unit in review.needs_review] == ["native:shared-credit"]
    assert "co_release_requires_review" in review.needs_review[0].reasons
    assert [unit.unit_id for unit in included.missing] == ["native:shared-credit"]
    assert [unit.unit_id for unit in excluded.excluded] == ["native:shared-credit"]


def test_known_filter_mismatch_excludes_before_co_release_review() -> None:
    scope = _scope(co_release_policy=CoReleasePolicy.NEEDS_REVIEW, year_from=2000)
    release = _release("old-co-release", artists=("artist-a", "artist-b"), year=1990)
    result = _preview((release,), scope)

    assert result.needs_review == ()
    assert result.excluded[0].reasons == ("year_filter_mismatch",)


def test_ambiguous_target_identity_cannot_be_included_by_unknown_policy() -> None:
    scope = _scope(target_identity=IdentityState.AMBIGUOUS, unknown_policy=UnknownPolicy.INCLUDE)
    result = _preview((_release("candidate"),), scope)
    filtered_out = _preview((_release("wrong-year", year=1900),), replace(scope, year_from=2000))

    assert [unit.unit_id for unit in result.needs_review] == ["native:candidate"]
    assert result.missing == ()
    assert filtered_out.needs_review == ()
    assert filtered_out.excluded[0].reasons == ("year_filter_mismatch",)


def test_work_unit_closes_only_through_an_owned_qualifying_edition() -> None:
    scope = _scope(unit_kind=UnitKind.WORK, media_any=("vinyl",))
    releases = (
        _release("held-cd", media=("cd",), work_id="work-1", work_identity=IdentityState.CONFIRMED),
        _release("vinyl-one", media=("vinyl",), work_id="work-1", work_identity=IdentityState.CONFIRMED),
        _release("vinyl-two", media=("vinyl",), work_id="work-1", work_identity=IdentityState.CONFIRMED),
        _release("other-work", media=("vinyl",), work_id="work-2", work_identity=IdentityState.CONFIRMED),
    )
    still_missing = _preview(releases, scope, _collection("held-cd"))
    now_owned = _preview(releases, scope, _collection("held-cd", "vinyl-one", "vinyl-one"))

    assert [unit.unit_id for unit in still_missing.missing] == ["work-1", "work-2"]
    assert still_missing.denominator_evidence.denominator == 2
    assert [unit.unit_id for unit in now_owned.owned] == ["work-1"]
    assert [unit.unit_id for unit in now_owned.missing] == ["work-2"]
    assert now_owned.owned[0].release_ids == ("discogs:vinyl-one", "discogs:vinyl-two")


def test_known_eligible_work_gap_survives_unknown_sibling_eligibility() -> None:
    scope = _scope(unit_kind=UnitKind.WORK, year_from=1990)
    result = _preview(
        (
            _release("known-edition", work_id="work-1", work_identity=IdentityState.CONFIRMED),
            _release("unknown-year-edition", year=None, work_id="work-1", work_identity=IdentityState.CONFIRMED),
        ),
        scope,
    )

    assert [unit.unit_id for unit in result.missing] == ["work-1"]
    assert result.denominator_evidence.denominator == 1
    assert not result.denominator_evidence.denominator_complete
    assert [unit.unit_id for unit in result.missing] == ["work-1"]


def test_unknown_eligibility_on_held_work_edition_requires_ownership_review() -> None:
    scope = _scope(unit_kind=UnitKind.WORK, year_from=1990)
    releases = (
        _release("held-unknown", year=None, work_id="work-1", work_identity=IdentityState.CONFIRMED),
        _release("known-unheld", work_id="work-1", work_identity=IdentityState.CONFIRMED),
    )
    result = _preview(releases, scope, _collection("held-unknown"))

    assert result.denominator_evidence.denominator == 1
    assert result.owned == ()
    assert result.missing == ()
    assert [unit.unit_id for unit in result.ownership_review] == ["work-1"]


def test_uncertain_work_or_release_identity_is_review_evidence_not_a_unit_or_owned() -> None:
    releases = (
        _release("ambiguous-edition", release_identity=IdentityState.AMBIGUOUS),
        _release("unknown-work", work_id=None),
        _release("ambiguous-work", work_id="work-x", work_identity=IdentityState.AMBIGUOUS),
    )
    edition = _preview(releases[:1], _scope(), _collection("ambiguous-edition"))
    work = _preview(releases[1:], _scope(unit_kind=UnitKind.WORK), _collection("unknown-work", "ambiguous-work"))

    assert edition.owned == ()
    assert edition.denominator_evidence.denominator == 0
    assert edition.needs_review[0].unit_id is None
    assert edition.needs_review[0].unit_key == ("needs_review_edition", "discogs:ambiguous-edition")
    assert len(work.needs_review) == 2
    assert len({unit.unit_key for unit in work.needs_review}) == 2
    assert work.denominator_evidence.denominator_complete is False


def test_ambiguous_collection_memberships_never_close_candidate_units() -> None:
    releases = (_release("candidate-a"), _release("candidate-b"))
    collection = _collection("collection-row", mappings={"collection-row": ("native:candidate-a", "native:candidate-b")})
    releases = (_release("candidate-a"), _release("candidate-b"))
    result = _preview(releases, collection=collection)

    assert result.owned == ()
    assert result.missing == ()
    assert {unit.unit_id for unit in result.ownership_review} == {"native:candidate-a", "native:candidate-b"}
    assert all("uncertain_collection_identity" in unit.reasons for unit in result.ownership_review)
    assert result.denominator_evidence.denominator == 2
    assert result.denominator_evidence.ownership_review == 2
    assert not result.denominator_evidence.ownership_complete
    assert result.denominator_evidence.completion_fraction is None


def test_provider_aliases_deduplicate_to_one_native_edition() -> None:
    releases = (
        _release("discogs-one", edition_id="native:shared"),
        _release("discogs-two", edition_id="native:shared", source_provider="other-catalog"),
    )
    collection = AuthorizedCollection.from_folded(
        "collection-r1",
        {"release_ids": ["discogs-one"]},
        source_provider="discogs",
        provider_to_edition_candidates={"discogs-one": ["native:shared"]},
    )
    result = _preview(releases, collection=collection)

    assert [unit.unit_id for unit in result.owned] == ["native:shared"]
    assert result.owned[0].release_ids == ("discogs:discogs-one", "other-catalog:discogs-two")
    assert result.denominator_evidence.denominator == 1


def test_unresolved_mapping_does_not_assert_a_unit_or_close_a_gap() -> None:
    collection = AuthorizedCollection.from_folded(
        "collection-r1",
        {"release_ids": ["not-in-snapshot"]},
        source_provider="discogs",
        provider_to_edition_candidates={},
    )
    result = _preview((_release("eligible"),), collection=collection)

    assert [unit.unit_id for unit in result.missing] == ["native:eligible"]
    assert result.denominator_evidence.denominator == 1
    assert result.denominator_evidence.denominator_complete is False
    assert result.denominator_evidence.ownership_complete is False
    assert any("unmapped_collection_identity" in unit.reasons for unit in result.needs_review)


def test_unresolved_same_provider_id_does_not_mark_other_provider_edition_uncertain() -> None:
    collection = AuthorizedCollection.from_folded(
        "collection-r1",
        {"release_ids": ["same-id"]},
        source_provider="discogs",
        provider_to_edition_candidates={"same-id": ("native:missing", "native:also-missing")},
    )
    result = _preview((_release("same-id", edition_id="native:other", source_provider="different"),), collection=collection)

    assert [unit.unit_id for unit in result.missing] == ["native:other"]
    assert result.ownership_review == ()
    assert result.denominator_evidence.ownership_complete is False


def test_blank_native_and_work_ids_are_rejected() -> None:
    with pytest.raises(ValueError, match="edition_id"):
        _release("blank-edition", edition_id="")
    with pytest.raises(ValueError, match="work_id"):
        _release("blank-work", work_id="")
    with pytest.raises(ValueError, match="non-empty IDs"):
        AuthorizedCollection.from_folded(
            "c", {"release_ids": ["source"]}, source_provider="discogs", provider_to_edition_candidates={"source": ("",)}
        )


def test_empty_providers_targets_and_malformed_mapping_evidence_fail_closed() -> None:
    with pytest.raises(ValueError, match="source_provider"):
        _release("release", source_provider="")
    with pytest.raises(ValueError, match="collection source_provider"):
        AuthorizedCollection.from_folded("c", {"release_ids": []}, source_provider="", provider_to_edition_candidates={})
    with pytest.raises(ValueError, match="source_provider"):
        AuthorizedCollection("c", "", (), {})
    with pytest.raises(ValueError, match="native IDs"):
        AuthorizedCollection.from_folded("c", {"release_ids": ["source"]}, source_provider="discogs", provider_to_edition_candidates={1: ["native"]})  # type: ignore[dict-item]
    with pytest.raises(ValueError, match="contain strings"):
        AuthorizedCollection.from_folded("c", {"release_ids": ["source"]}, source_provider="discogs", provider_to_edition_candidates={"source": [1]})  # type: ignore[dict-item]
    with pytest.raises(ValueError, match="target_id"):
        PursuitScope(TargetKind.ARTIST, "")
    assert _collection("x", mappings={"x": ("native:x", "native:y")}).ambiguous_provider_candidates == {"x": ("native:x", "native:y")}


def test_negative_counts_and_unresolved_identity_states_are_explicit() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        DenominatorEvidence(-1, -1, 0, 0, 0, 0, 0, 0, False, False)
    complete = DenominatorEvidence(2, 2, 1, 1, 0, 0, 0, 0, True, True)
    assert complete.completion_fraction == 0.5
    assert DenominatorEvidence(0, 0, 0, 0, 0, 0, 0, 0, True, True).completion_fraction is None
    ambiguous_edition = _preview((_release("ambiguous-edition", release_identity=IdentityState.AMBIGUOUS, edition_id="native:ambiguous"),))
    assert "ambiguous_release_identity" in ambiguous_edition.needs_review[0].reasons
    ambiguous_work = _preview(
        (
            _release(
                "ambiguous-release",
                release_identity=IdentityState.AMBIGUOUS,
                edition_id="native:ambiguous",
                work_id="work",
                work_identity=IdentityState.CONFIRMED,
            ),
        ),
        _scope(unit_kind=UnitKind.WORK),
    )
    assert "ambiguous_release_identity" in ambiguous_work.needs_review[0].reasons


def test_sparse_and_empty_snapshots_keep_denominator_explicit() -> None:
    empty = _preview(())
    sparse = _preview((_release("sparse", media=None, year=None, countries=None),), _scope(media_any=("vinyl",)))

    assert empty.denominator_evidence.denominator == 0
    assert empty.denominator_evidence.denominator_complete
    assert len(sparse.needs_review) == 1
    assert sparse.denominator_evidence.denominator == 0
    assert not sparse.denominator_evidence.denominator_complete


def test_collection_adapter_and_snapshot_scope_inputs_validate_required_evidence() -> None:
    with pytest.raises(ValueError, match="release_ids"):
        AuthorizedCollection.from_folded("c", {"release_ids": None}, source_provider="discogs", provider_to_edition_candidates={})
    with pytest.raises(ValueError, match="strings"):
        AuthorizedCollection.from_folded("c", {"release_ids": [1]}, source_provider="discogs", provider_to_edition_candidates={})
    with pytest.raises(ValueError, match="non-empty"):
        CatalogSnapshot("", ())
    with pytest.raises(ValueError, match="year_from"):
        _scope(year_from=2001, year_to=2000)
    with pytest.raises(ValueError, match="include_sublabels"):
        _scope(target_kind=TargetKind.ARTIST, include_sublabels=True)
    with pytest.raises(ValueError, match="release_id"):
        _release("")


def test_denominator_and_preview_evidence_invariants_are_enforced() -> None:
    with pytest.raises(ValueError, match=r"owned \+ missing"):
        DenominatorEvidence(
            denominator=2,
            eligible=2,
            owned=1,
            missing=0,
            ownership_review=0,
            needs_review=0,
            excluded=0,
            unknown_excluded=0,
            denominator_complete=True,
            ownership_complete=True,
        )
    with pytest.raises(ValueError, match="complete"):
        DenominatorEvidence(
            denominator=1,
            eligible=1,
            owned=0,
            missing=1,
            ownership_review=0,
            needs_review=1,
            excluded=0,
            unknown_excluded=0,
            denominator_complete=True,
            ownership_complete=True,
        )
    good = DenominatorEvidence(
        denominator=1,
        eligible=1,
        owned=0,
        missing=1,
        ownership_review=0,
        needs_review=0,
        excluded=0,
        unknown_excluded=0,
        denominator_complete=True,
        ownership_complete=True,
    )
    with pytest.raises(ValueError, match="evidence lists"):
        ScopePreview("catalog", "collection", _scope(), (), (), (), (), (), good)
