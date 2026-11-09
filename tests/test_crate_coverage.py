"""Hand-counted complementarity, source identity, and conservative evidence tests."""

from dataclasses import replace

import pytest

from api.crate_coverage import (
    MAX_CANDIDATES,
    MAX_OBJECTIVE_UNITS,
    MAX_SCOPE_ROWS,
    CoverageObjective,
    CrateCandidate,
    compare_crates,
)
from api.evaluation.crate_coverage import main, synthetic_replay
from api.fit import compute_fit
from api.pursuit_scope import (
    AuthorizedCollection,
    CatalogSnapshot,
    IdentityState,
    PursuitScope,
    ReleaseFact,
    TargetKind,
    UnitKind,
    UnknownPolicy,
)
from api.queries.fit_queries import empty_collection, fold_collection


def fact(identifier="a", **changes):
    return replace(ReleaseFact(identifier, f"native-{identifier}", ("artist",), ("label",), ("vinyl",), 2000, ("US",)), **changes)


def candidate(identifier="a", **changes):
    return replace(CrateCandidate(identifier, {"id": identifier}), **changes)


def compare(releases=None, candidates=None, *, collection=None, folded=None, scope=None, **kwargs):
    return compare_crates(
        CatalogSnapshot("catalog", tuple(releases if releases is not None else (fact(),))),
        scope or PursuitScope(TargetKind.ARTIST, "artist"),
        collection or AuthorizedCollection("collection", "discogs", (), {}),
        folded or empty_collection(),
        tuple(candidates if candidates is not None else (candidate(),)),
        **kwargs,
    )


def test_joint_selection_covers_complementary_units_and_real_fit_baseline():
    report = synthetic_replay()
    policies = report["policies"]
    greedy = policies["greedy_coverage"]
    fit = policies["individual_fit"]
    popularity = policies["popularity"]
    assert [row["source_key"] for row in greedy["additions"]] == ["discogs:a", "discogs:c"]
    assert [row["marginal_gain"] for row in greedy["additions"]] == [3, 2]
    assert [row["source_key"] for row in fit["additions"]] == ["discogs:a", "discogs:b"]
    assert [row["marginal_gain"] for row in fit["additions"]] == [3, 0]
    assert fit["additions"][1]["overlap"] == 3
    assert len(fit["additions"][1]["overlap_units"]) == 3
    assert fit["additions"][1]["reason"] == "no_guaranteed_marginal_gain"
    assert fit["objective_after"]["covered"] == popularity["objective_after"]["covered"] == 3
    assert greedy["objective_after"] == {"denominator": 5, "covered": 5, "missing": 0, "ownership_review": 0}
    folded = fold_collection(
        [
            {
                "release_id": "held",
                "title": "Synthetic held record",
                "artist_ids": ["familiar"],
                "label_ids": ["scope"],
                "genres": ["Jazz"],
                "styles": [],
            }
        ]
    )
    context = {
        "id": "a",
        "title": "Synthetic candidate a",
        "artists": [{"id": "artist-one", "name": "Synthetic artist-one"}],
        "labels": [{"id": "scope", "name": "Synthetic scope"}, {"id": "label-one", "name": "Synthetic label-one"}],
        "genres": ["Jazz"],
    }
    assert fit["additions"][0]["fit"] == compute_fit(folded, context)
    assert report["synthetic"] and report["real_checked_feedback_labels"] == 0
    assert "PENDING" in report["real_quality_status"]
    for policy in policies.values():
        before, after = policy["scope_before"], policy["scope_after"]
        assert before["denominator"] == after["denominator"] == 4
        assert (before["owned"], before["missing"], after["owned"], after["missing"]) == (1, 3, 3, 1)
        assert sum(row["marginal_gain"] for row in policy["additions"]) == len(policy["new_units"])
        assert policy["objective_after"]["covered"] + policy["objective_after"]["missing"] + policy["objective_after"]["ownership_review"] == 5
        assert policy["additions"][-1]["covered_after"] == policy["objective_after"]["covered"]


def test_aliases_copies_and_equivalent_editions_share_one_strategy_neutral_pool():
    releases = (fact("b", edition_id="canonical"), fact("a", edition_id="canonical"), fact("c", edition_id="other"))
    candidates = (candidate("c"), candidate("b", popularity=100), candidate("a"), candidate("a"))
    report = compare(releases, candidates)
    reversed_report = compare(tuple(reversed(releases)), tuple(reversed(candidates)))
    assert report == reversed_report
    assert report["input_rows"] == 4 and report["canonical_candidates"] == 2
    assert report["pool"] == [
        {"scope_unit": ["edition", "canonical"], "source_key": "discogs:a"},
        {"scope_unit": ["edition", "other"], "source_key": "discogs:c"},
    ]
    assert all(len(policy["additions"]) == 2 for policy in report["policies"].values())
    work_releases = tuple(replace(row, work_id="one-work", work_identity=IdentityState.CONFIRMED) for row in releases)
    work = compare(work_releases, candidates, scope=PursuitScope(TargetKind.ARTIST, "artist", unit_kind=UnitKind.WORK))
    assert work["canonical_candidates"] == 1
    assert work["policies"]["greedy_coverage"]["new_units"] == [["work", "one-work"]]


def test_native_source_namespaces_do_not_confuse_ownership_or_fit_ids():
    collection = AuthorizedCollection("collection", "discogs", ("native-a",), {"native-a": ("held-edition",)})
    report = compare(collection=collection, folded={"release_ids": ["native-a"]})
    assert report["policies"]["greedy_coverage"]["additions"][0]["source_key"] == "discogs:a"
    assert report["policies"]["individual_fit"]["additions"][0]["fit"]["components"]["redundancy"]["score"] == 0


def test_owned_unknown_identity_excluded_and_ownership_review_are_withheld():
    releases = (
        fact("owned"),
        fact("review"),
        fact("unknown", edition_id=None, release_identity=IdentityState.UNKNOWN),
        fact("excluded", artist_ids=("other",)),
        fact("a"),
    )
    collection = AuthorizedCollection(
        "collection", "discogs", ("owned", "ambiguous"), {"owned": ("native-owned",), "ambiguous": ("native-review", "other")}
    )
    report = compare(releases, [candidate(row.release_id) for row in releases], collection=collection, folded={"release_ids": ["owned", "ambiguous"]})
    assert report["pool"] == []  # incomplete ownership withholds all guaranteed gains
    assert len(report["withheld"]) == 5
    assert [len(report["scope_evidence"][status]) for status in ("owned", "missing", "ownership_review", "needs_review", "excluded")] == [
        1,
        1,
        1,
        1,
        1,
    ]
    for policy in report["policies"].values():
        assert policy["new_units"] == []
        assert policy["coverage_fraction_after"] is None
        assert policy["objective_before"] == {"denominator": 3, "covered": 1, "missing": 0, "ownership_review": 2}


def test_unmapped_owned_row_and_policy_included_unknown_fields_do_not_gain():
    collection = AuthorizedCollection("collection", "discogs", ("unmapped",), {})
    report = compare(collection=collection, folded={"release_ids": ["unmapped"]})
    assert report["pool"] == []
    included = compare([fact(year=None)], scope=PursuitScope(TargetKind.ARTIST, "artist", year_from=1990, unknown_policy=UnknownPolicy.INCLUDE))
    assert len(included["scope_evidence"]["missing"]) == 1
    assert included["pool"] == []


def test_empty_sparse_context_ties_caps_and_zero_gain_stopping():
    empty = compare([], [])
    assert empty["policies"]["greedy_coverage"]["coverage_fraction_after"] is None
    assert empty["policies"]["greedy_coverage"]["objective_after"]["denominator"] == 0
    report = compare([fact("b"), fact("a"), fact("c")], [candidate("c"), candidate("b"), candidate("a")], candidate_limit=2, selection_limit=1)
    assert report["capped_out"] == 1
    for policy in report["policies"].values():
        assert [row["source_key"] for row in policy["additions"]] == ["discogs:a"]
        assert policy["additions"][0]["fit"] == compute_fit(empty_collection(), {"id": "a"})
    assert all(not policy["additions"] for policy in compare(selection_limit=0)["policies"].values())
    objective = CoverageObjective("catalog", "no-known-coverage", (("artist", "unreachable"),), {})
    zero = compare(objective=objective)
    assert not zero["policies"]["greedy_coverage"]["additions"]
    assert zero["policies"]["individual_fit"]["additions"][0]["marginal_gain"] == 0


def test_declared_owned_units_are_deduplicated_and_overlap_explained():
    unit = ("artist", "shared")
    other = ("credit", "new")
    objective = CoverageObjective("catalog", "declared", (unit, other), {("edition", "native-held"): (unit,), ("edition", "native-a"): (unit, other)})
    collection = AuthorizedCollection("collection", "discogs", ("held",), {"held": ("native-held",)})
    report = compare([fact("held"), fact()], objective=objective, collection=collection, folded={"release_ids": ["held"]})
    row = report["policies"]["greedy_coverage"]["additions"][0]
    assert row["new_units"] == [["credit", "new"]]
    assert row["overlap_units"] == [["artist", "shared"]]
    assert (row["covered_before"], row["covered_after"]) == (1, 2)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"candidate_limit": 0},
        {"candidate_limit": 201},
        {"candidate_limit": True},
        {"selection_limit": -1},
        {"selection_limit": 21},
        {"selection_limit": False},
    ],
)
def test_invalid_controls_fail_before_selection(kwargs):
    with pytest.raises(ValueError, match="limit"):
        compare(**kwargs)


def test_hard_input_bounds_reject_instead_of_silently_ignoring_inputs():
    with pytest.raises(ValueError, match="input candidates"):
        compare(candidates=[candidate()] * (MAX_CANDIDATES + 1))
    with pytest.raises(ValueError, match="catalog snapshot"):
        compare(releases=[fact()] * (MAX_SCOPE_ROWS + 1))
    with pytest.raises(ValueError, match="hard unit bound"):
        CoverageObjective("catalog", "big", tuple(("artist", str(index)) for index in range(MAX_OBJECTIVE_UNITS + 1)), {})


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"release_id": ""}, "source identity"),
        ({"source_provider": ""}, "source identity"),
        ({"fit_context": {"id": "wrong"}}, "context id"),
        ({"popularity": -1}, "popularity"),
        ({"popularity": True}, "popularity"),
    ],
)
def test_invalid_candidate_identity_and_popularity(kwargs, message):
    with pytest.raises(ValueError, match=message):
        candidate(**kwargs)


def test_snapshot_and_conflicting_evidence_fail_closed():
    with pytest.raises(ValueError, match="Fit fold"):
        compare(folded={"release_ids": ["wrong"]})
    with pytest.raises(ValueError, match="provider"):
        compare(candidates=[candidate(source_provider="musicbrainz")])
    with pytest.raises(ValueError, match="conflicting candidate"):
        compare(candidates=[candidate(), candidate(popularity=1)])
    objective = CoverageObjective("wrong", "declared", (), {})
    with pytest.raises(ValueError, match="snapshot"):
        compare(objective=objective)
    with pytest.raises(ValueError, match="known eligible native"):
        compare(objective=replace(objective, snapshot_id="catalog", by_scope_unit={("edition", "unknown"): ()}))


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"snapshot_id": ""}, "IDs"),
        ({"objective_id": ""}, "IDs"),
        ({"units": (("genre", "jazz"),)}, "named"),
        ({"units": (("artist", ""),)}, "named"),
        ({"units": (("artist", "a"), ("artist", "a"))}, "unique"),
        ({"by_scope_unit": {("edition", "a"): (("artist", "missing"),)}}, "undeclared"),
    ],
)
def test_declared_objective_rejects_ambiguous_or_undeclared_units(changes, message):
    with pytest.raises(ValueError, match=message):
        replace(CoverageObjective("catalog", "declared", (), {}), **changes)


def test_replay_cli_is_reproducible_and_invalid_controls_are_errors(capsys):
    assert main([]) == 0
    first = capsys.readouterr().out
    assert main([]) == 0
    assert first == capsys.readouterr().out
    assert '"synthetic": true' in first
    assert main(["--candidate-limit", "0"]) == 2
    assert "crate coverage replay input error" in capsys.readouterr().err


def test_fit_and_popularity_use_their_actual_order_on_the_same_pool():
    rich = {"id": "b", "artists": [{"id": "new-artist", "name": "New Artist"}]}
    report = compare(
        [fact("a"), fact("b")],
        [candidate("a", popularity=10), candidate("b", fit_context=rich, popularity=1)],
        selection_limit=1,
    )
    fit = report["policies"]["individual_fit"]["additions"][0]
    popularity = report["policies"]["popularity"]["additions"][0]
    assert fit["source_key"] == "discogs:b"
    assert fit["fit"] == compute_fit(empty_collection(), rich)
    assert fit["fit"]["fit"] > popularity["fit"]["fit"]
    assert popularity["source_key"] == "discogs:a"
    assert report["policies"]["greedy_coverage"]["additions"][0]["source_key"] == "discogs:a"
