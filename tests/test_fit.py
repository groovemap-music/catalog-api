"""CrateFit v0 scoring — every component, on hand-built collections.

No database, no driver, no pool: :mod:`api.fit` takes the folded collection and the
candidate context as plain dictionaries, so every case here is stated as data. The
collections are built through :func:`api.queries.fit_queries.fold_collection` rather than
by hand so the two halves cannot drift — a test that asserts against an invented shape
would keep passing after the fold changed.

What each test defends is that the *evidence* is true of the collection, not only that
the number moved: the decomposition is the product, and a component whose score a
collector cannot check against their own shelves is not an explanation.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from api.fit import (
    FIT_CONSTANTS,
    FIT_VERSION,
    _render_entry,
    compute_fit,
    identity_confidence,
    score_affinity,
    score_bridge,
    score_depth,
    score_novelty,
    score_redundancy,
)
from api.queries.fit_queries import empty_collection, fold_collection


def _row(
    release_id: str,
    title: str,
    *,
    artists: list[str] | None = None,
    labels: list[str] | None = None,
    genres: list[str] | None = None,
    styles: list[str] | None = None,
    master: str | None = None,
) -> dict[str, Any]:
    """One held release, shaped the way the collection Cypher returns it."""
    return {
        "release_id": release_id,
        "title": title,
        "artist_ids": artists or [],
        "label_ids": labels or [],
        "genres": genres or [],
        "styles": styles or [],
        "master_ids": [master] if master else [],
    }


def _context(
    release_id: str = "555",
    title: str = "Blue Train",
    *,
    artists: list[tuple[str, str]] | None = None,
    labels: list[tuple[str, str]] | None = None,
    genres: list[str] | None = None,
    styles: list[str] | None = None,
    media_families: list[str] | None = None,
    master_id: str | None = "m-blue-train",
    siblings: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """One candidate's context, shaped the way `get_release_context` returns it."""
    return {
        "id": release_id,
        "title": title,
        "year": 1957,
        "artists": [{"id": identifier, "name": name} for identifier, name in artists or [("a-coltrane", "John Coltrane")]],
        "labels": [{"id": identifier, "name": name} for identifier, name in labels or [("l-blue-note", "Blue Note")]],
        "genres": genres if genres is not None else ["Jazz"],
        "styles": styles if styles is not None else ["Hard Bop"],
        "media_families": media_families if media_families is not None else ["grooved"],
        "master_id": master_id,
        "master_title": "Blue Train",
        "siblings": siblings or [],
    }


def _jazz_collection() -> dict[str, Any]:
    """Three Blue Note hard bop records and nothing else."""
    return fold_collection(
        [
            _row("1", "Moanin'", artists=["a-blakey"], labels=["l-blue-note"], genres=["Jazz"], styles=["Hard Bop"]),
            _row("2", "Somethin' Else", artists=["a-adderley"], labels=["l-blue-note"], genres=["Jazz"], styles=["Hard Bop"]),
            _row("3", "Go", artists=["a-gordon"], labels=["l-blue-note"], genres=["Jazz"], styles=["Hard Bop"]),
        ]
    )


# ──────────────────────────────────────────────────────────────────────────────
# affinity
# ──────────────────────────────────────────────────────────────────────────────


def test_affinity_of_an_empty_collection_is_zero() -> None:
    """Nothing held, nothing shared — and no evidence invented to fill the gap."""
    component = score_affinity(empty_collection(), _context())

    assert component["score"] == 0.0
    assert component["evidence"] == []


def test_affinity_weights_each_dimension_by_the_shared_fraction() -> None:
    """The label and genre match; the artist and style do not, and the weights say so."""
    component = score_affinity(_jazz_collection(), _context(styles=["Modal"]))

    weights = FIT_CONSTANTS["affinity_weights"]
    expected = (weights["label"] + weights["genre"]) / sum(weights.values())
    assert component["score"] == round(expected, 4)


def test_affinity_evidence_names_the_label_and_counts_the_holding() -> None:
    """ "shares label X with N releases you hold" — a fact a collector can check."""
    component = score_affinity(_jazz_collection(), _context())

    assert "shares label Blue Note with 3 releases you hold" in component["evidence"]


def test_affinity_evidence_is_loudest_first() -> None:
    """Three held on the label outrank one held by the artist, deterministically."""
    collection = fold_collection(
        [
            _row("1", "Moanin'", artists=["a-blakey"], labels=["l-blue-note"], genres=["Jazz"]),
            _row("2", "Somethin' Else", artists=["a-adderley"], labels=["l-blue-note"], genres=["Jazz"]),
            _row("3", "Blue Train", artists=["a-coltrane"], labels=["l-blue-note"], genres=["Jazz"]),
        ]
    )

    component = score_affinity(collection, _context(release_id="999"))

    assert component["evidence"][:2] == [
        "shares genre Jazz with 3 releases you hold",
        "shares label Blue Note with 3 releases you hold",
    ]
    assert component["evidence"][2] == "shares artist John Coltrane with 1 release you hold"


def test_affinity_renormalises_over_the_dimensions_the_candidate_has() -> None:
    """A release the graph carries no style for is scored on what it does carry."""
    component = score_affinity(_jazz_collection(), _context(styles=[]))

    weights = FIT_CONSTANTS["affinity_weights"]
    expected = (weights["label"] + weights["genre"]) / (weights["artist"] + weights["label"] + weights["genre"])
    assert component["score"] == round(expected, 4)


def test_affinity_of_a_candidate_with_nothing_in_common_is_zero() -> None:
    """A techno twelve against a hard bop collection shares nothing and says nothing."""
    candidate = _context(artists=[("a-aphex", "Aphex Twin")], labels=[("l-r-and-s", "R&S")], genres=["Electronic"], styles=["Techno"])

    component = score_affinity(_jazz_collection(), candidate)

    assert component["score"] == 0.0
    assert component["evidence"] == []


# ──────────────────────────────────────────────────────────────────────────────
# novelty
# ──────────────────────────────────────────────────────────────────────────────


def test_novelty_of_an_empty_collection_is_total() -> None:
    """Everything is new to a collector who holds nothing."""
    component = score_novelty(empty_collection(), _context())

    assert component["score"] == 1.0


def test_novelty_is_the_unheld_fraction_of_the_pooled_facets() -> None:
    """One artist, one label, one genre, one style: the artist alone is new."""
    component = score_novelty(_jazz_collection(), _context())

    assert component["score"] == 0.25


def test_novelty_evidence_names_what_is_new() -> None:
    """A collector reads which facet is new, not that some count of them is."""
    component = score_novelty(_jazz_collection(), _context())

    assert component["evidence"] == ["John Coltrane is an artist your collection has never held"]


def test_novelty_of_a_candidate_with_no_facets_at_all_is_zero() -> None:
    """A release the graph knows nothing about is not novel; it is unknown."""
    bare = _context(artists=[], labels=[], genres=[], styles=[])
    bare["artists"] = []
    bare["labels"] = []

    assert score_novelty(_jazz_collection(), bare)["score"] == 0.0


def test_novelty_ignores_an_artist_entry_with_no_id() -> None:
    """A graph row that lost its id is dropped rather than counted as a new artist."""
    candidate = _context()
    candidate["artists"] = [{"id": None, "name": "Unknown"}]

    component = score_novelty(_jazz_collection(), candidate)

    assert component["evidence"] == []
    assert component["score"] == 0.0


# ──────────────────────────────────────────────────────────────────────────────
# depth
# ──────────────────────────────────────────────────────────────────────────────


def test_depth_is_the_holding_over_the_saturation_point() -> None:
    """Three of five held on the label is three fifths of the way to "you collect this"."""
    component = score_depth(_jazz_collection(), _context())

    assert component["score"] == round(3 / int(FIT_CONSTANTS["depth_saturation"]), 4)


def test_depth_saturates_rather_than_growing_without_bound() -> None:
    """A collector with eight on a label is not eight fifths deep in it."""
    collection = fold_collection([_row(str(index), f"LP {index}", labels=["l-blue-note"], genres=["Jazz"]) for index in range(8)])

    assert score_depth(collection, _context())["score"] == 1.0


def test_depth_evidence_names_the_thread_and_the_holding() -> None:
    """ "deepens label X (N held)" — the thread the record continues."""
    component = score_depth(_jazz_collection(), _context())

    assert component["evidence"] == ["deepens label Blue Note (3 held)"]


def test_depth_of_an_empty_collection_is_zero() -> None:
    """There is no thread to deepen."""
    assert score_depth(empty_collection(), _context()) == {"score": 0.0, "evidence": [], "evidence_items": [], "evidence_entries": []}


def test_depth_takes_the_maximum_thread_not_the_sum() -> None:
    """Three on the label and one by the artist is three fifths, not four fifths."""
    collection = fold_collection(
        [
            _row("1", "Moanin'", artists=["a-blakey"], labels=["l-blue-note"], genres=["Jazz"]),
            _row("2", "Somethin' Else", artists=["a-adderley"], labels=["l-blue-note"], genres=["Jazz"]),
            _row("3", "Giant Steps", artists=["a-coltrane"], labels=["l-blue-note"], genres=["Jazz"]),
        ]
    )

    component = score_depth(collection, _context(release_id="999"))

    assert component["score"] == round(3 / 5, 4)
    assert component["evidence"] == ["deepens label Blue Note (3 held)", "deepens artist John Coltrane (1 held)"]


# ──────────────────────────────────────────────────────────────────────────────
# bridge (v0 heuristic)
# ──────────────────────────────────────────────────────────────────────────────


def _two_region_collection() -> dict[str, Any]:
    """Jazz and Electronic, sharing no artist and no label with each other."""
    return fold_collection(
        [
            _row("1", "Moanin'", artists=["a-blakey"], labels=["l-blue-note"], genres=["Jazz"], styles=["Hard Bop"]),
            _row("2", "Selected Ambient Works", artists=["a-aphex"], labels=["l-r-and-s"], genres=["Electronic"], styles=["Ambient"]),
        ]
    )


def test_bridge_scores_a_candidate_that_touches_two_separate_regions() -> None:
    """Two regions the collector's own records never connect: the cap."""
    candidate = _context(genres=["Jazz", "Electronic"], styles=[])

    component = score_bridge(_two_region_collection(), candidate)

    assert component["score"] == FIT_CONSTANTS["bridge_score_cap"]


def test_bridge_evidence_names_the_regions_and_labels_itself_v0() -> None:
    """The heuristic says what it joined and that it is a heuristic."""
    component = score_bridge(_two_region_collection(), _context(genres=["Jazz", "Electronic"], styles=[]))

    assert component["evidence"][0] == "bridges Electronic and Jazz, which share no artist or label in your collection"
    assert "v0 genre heuristic" in component["evidence"][1]


def test_bridge_reaches_a_region_through_a_style() -> None:
    """A record with no Electronic genre still touches it by sharing an Ambient style."""
    candidate = _context(genres=["Jazz"], styles=["Ambient"])

    assert score_bridge(_two_region_collection(), candidate)["score"] == FIT_CONSTANTS["bridge_score_cap"]


def test_bridge_of_one_region_is_zero() -> None:
    """Touching one corner of a collection connects nothing."""
    component = score_bridge(_two_region_collection(), _context(genres=["Jazz"], styles=[]))

    assert component["score"] == 0.0
    assert component["evidence"] == []


def test_bridge_ignores_regions_the_collection_already_connects() -> None:
    """Two genres joined by a shared label are one region, not two."""
    collection = fold_collection(
        [
            _row("1", "Moanin'", artists=["a-blakey"], labels=["l-shared"], genres=["Jazz"]),
            _row("2", "Bitches Brew", artists=["a-davis"], labels=["l-shared"], genres=["Fusion"]),
        ]
    )

    assert score_bridge(collection, _context(genres=["Jazz", "Fusion"], styles=[]))["score"] == 0.0


def test_bridge_of_an_empty_collection_is_zero() -> None:
    """No regions, nothing to bridge."""
    assert score_bridge(empty_collection(), _context())["score"] == 0.0


def test_bridge_caps_at_one_across_three_regions() -> None:
    """Three separate regions is still the cap; the number is a hint, not a count."""
    collection = fold_collection(
        [
            _row("1", "A", artists=["a-1"], labels=["l-1"], genres=["Jazz"]),
            _row("2", "B", artists=["a-2"], labels=["l-2"], genres=["Electronic"]),
            _row("3", "C", artists=["a-3"], labels=["l-3"], genres=["Folk"]),
        ]
    )

    assert score_bridge(collection, _context(genres=["Jazz", "Electronic", "Folk"], styles=[]))["score"] == 1.0


# ──────────────────────────────────────────────────────────────────────────────
# redundancy
# ──────────────────────────────────────────────────────────────────────────────


def test_redundancy_of_an_exact_duplicate_is_total() -> None:
    """The collector is holding a record they already own."""
    component = score_redundancy(_jazz_collection(), _context(release_id="1"))

    assert component["score"] == FIT_CONSTANTS["redundancy_held_release"]
    assert component["evidence"] == ["you already hold this exact release"]


def test_redundancy_of_the_same_master_on_the_same_family_is_total() -> None:
    """The same record in the same form, reached through the master hop."""
    candidate = _context(release_id="999", siblings=[{"id": "1", "title": "Moanin'", "year": 1958, "media_families": ["grooved"]}])

    component = score_redundancy(_jazz_collection(), candidate)

    assert component["score"] == FIT_CONSTANTS["redundancy_same_master_same_family"]
    assert component["evidence"] == ["you hold Moanin', the same record on grooved"]


def test_redundancy_of_the_same_master_on_another_family_is_partial() -> None:
    """The same record as a different object, which many collectors deliberately want."""
    candidate = _context(release_id="999", siblings=[{"id": "1", "title": "Moanin'", "year": 1990, "media_families": ["digital"]}])

    component = score_redundancy(_jazz_collection(), candidate)

    assert component["score"] == FIT_CONSTANTS["redundancy_same_master_other_family"]
    assert component["evidence"] == ["you hold Moanin', the same record on digital"]


def test_redundancy_names_another_format_when_the_sibling_has_no_media() -> None:
    """A sibling the graph never gave a medium is still a held version worth naming."""
    candidate = _context(release_id="999", siblings=[{"id": "1", "title": "Moanin'", "year": 1990, "media_families": []}])

    assert score_redundancy(_jazz_collection(), candidate)["evidence"] == ["you hold Moanin', the same record on another format"]


def test_redundancy_falls_back_to_the_same_artist_and_title() -> None:
    """No master links the two, so the graph's own titles are what is left."""
    collection = fold_collection([_row("1", "Blue Train", artists=["a-coltrane"], labels=["l-blue-note"], genres=["Jazz"])])
    candidate = _context(release_id="999", master_id=None, siblings=[])

    component = score_redundancy(collection, candidate)

    assert component["score"] == FIT_CONSTANTS["redundancy_same_artist_and_title"]
    assert component["evidence"] == ["you hold Blue Train by John Coltrane, which no master links to this pressing"]


def test_redundancy_matches_a_held_title_case_insensitively() -> None:
    """Discogs titles are typed by contributors; case is not a distinction."""
    collection = fold_collection([_row("1", "BLUE TRAIN", artists=["a-coltrane"])])

    assert score_redundancy(collection, _context(release_id="999", master_id=None))["score"] == FIT_CONSTANTS["redundancy_same_artist_and_title"]


def test_redundancy_of_an_unheld_record_is_zero() -> None:
    """Nothing held, nothing duplicated."""
    assert score_redundancy(empty_collection(), _context()) == {"score": 0.0, "evidence": [], "evidence_items": [], "evidence_entries": []}


def test_redundancy_ignores_a_sibling_the_collector_does_not_hold() -> None:
    """The master's other pressings are only redundancy when they are on the shelf."""
    candidate = _context(release_id="999", siblings=[{"id": "unheld", "title": "Moanin'", "media_families": ["grooved"]}])

    assert score_redundancy(_jazz_collection(), candidate)["score"] == 0.0


def test_redundancy_of_a_candidate_with_no_title_is_zero() -> None:
    """The title fallback needs a title; without one there is nothing left to compare."""
    candidate = _context(release_id="999", title="", master_id=None)

    assert score_redundancy(_jazz_collection(), candidate)["score"] == 0.0


def test_redundancy_picks_the_lowest_sibling_id_deterministically() -> None:
    """Two held pressings of one master cite the same one on every request."""
    candidate = _context(
        release_id="999",
        siblings=[
            {"id": "2", "title": "Somethin' Else", "media_families": ["grooved"]},
            {"id": "1", "title": "Moanin'", "media_families": ["grooved"]},
        ],
    )

    assert score_redundancy(_jazz_collection(), candidate)["evidence"] == ["you hold Moanin', the same record on grooved"]


def test_redundancy_names_a_sibling_by_id_when_it_has_no_title() -> None:
    """An untitled row still identifies the held version."""
    candidate = _context(release_id="999", siblings=[{"id": "1", "title": None, "media_families": ["grooved"]}])

    assert score_redundancy(_jazz_collection(), candidate)["evidence"] == ["you hold 1, the same record on grooved"]


# ──────────────────────────────────────────────────────────────────────────────
# confidence and the combined fit
# ──────────────────────────────────────────────────────────────────────────────


def test_confidence_is_exact_when_the_release_resolves() -> None:
    """A Discogs release id that resolved is the record in the collector's hand."""
    assert identity_confidence(_context()) == "exact"


def test_confidence_is_master_when_only_the_master_resolved() -> None:
    """The same record, but not this pressing — what barcode lookup will report."""
    assert identity_confidence({"id": None, "master_id": "m-blue-train"}) == "master"


def test_fit_combines_the_components_and_reports_the_version() -> None:
    """fit = affinity + novelty + bridge + depth - redundancy, clipped to [0, 1]."""
    profile = compute_fit(_jazz_collection(), _context())

    components = profile["components"]
    raw = (
        components["affinity"]["score"]
        + components["novelty"]["score"]
        + components["bridge"]["score"]
        + components["depth"]["score"]
        - components["redundancy"]["score"]
    )
    assert profile["fit"] == round(min(1.0, max(0.0, raw)), 4)
    assert profile["fit_version"] == FIT_VERSION == "cratefit_v0"
    assert profile["confidence"] == "exact"
    assert set(components) == {"affinity", "novelty", "bridge", "depth", "redundancy"}


def test_fit_clips_a_duplicate_to_zero_rather_than_going_negative() -> None:
    """A record the collector already owns cannot fit less than not at all."""
    collection = fold_collection([_row("1", "Untitled")])
    bare = _context(release_id="1", title="Untitled", artists=[], labels=[], genres=[], styles=[])
    bare["artists"] = []
    bare["labels"] = []

    profile = compute_fit(collection, bare)

    assert profile["components"]["redundancy"]["score"] == 1.0
    assert profile["fit"] == 0.0


def test_fit_subtracts_redundancy_from_the_other_four() -> None:
    """Owning the record is what pulls a well-matched candidate's fit back down."""
    held = compute_fit(_jazz_collection(), _context(release_id="1"))
    unheld = compute_fit(_jazz_collection(), _context(release_id="999"))

    assert held["components"]["redundancy"]["score"] == 1.0
    assert unheld["components"]["redundancy"]["score"] == 0.0
    assert held["fit"] < unheld["fit"]


def test_fit_of_an_empty_collection_is_all_novelty() -> None:
    """A collector who holds nothing is told so: everything is new, nothing is deep."""
    profile = compute_fit(empty_collection(), _context())

    assert profile["components"]["novelty"]["score"] == 1.0
    assert profile["components"]["affinity"]["score"] == 0.0
    assert profile["components"]["depth"]["score"] == 0.0
    assert profile["fit"] == 1.0


def test_fit_of_a_candidate_with_nothing_in_common_is_novelty_alone() -> None:
    """Sharing nothing is a high novelty and a zero everywhere else."""
    candidate = _context(artists=[("a-aphex", "Aphex Twin")], labels=[("l-r-and-s", "R&S")], genres=["Electronic"], styles=["Techno"])

    profile = compute_fit(_jazz_collection(), candidate)

    assert profile["components"]["novelty"]["score"] == 1.0
    assert profile["components"]["affinity"]["score"] == 0.0
    assert profile["components"]["depth"]["score"] == 0.0
    assert profile["components"]["bridge"]["score"] == 0.0


def test_fit_is_deterministic() -> None:
    """Same collection, same candidate, same profile — byte for byte, every time."""
    collection = _two_region_collection()
    candidate = _context(genres=["Jazz", "Electronic"], styles=["Hard Bop"])

    assert compute_fit(collection, candidate) == compute_fit(collection, candidate)


def test_every_component_score_is_within_the_unit_interval() -> None:
    """The contract the endpoint and any later evaluation both rely on."""
    for collection in (empty_collection(), _jazz_collection(), _two_region_collection()):
        for candidate in (_context(), _context(release_id="1"), _context(genres=["Jazz", "Electronic"])):
            profile = compute_fit(collection, candidate)
            assert 0.0 <= profile["fit"] <= 1.0
            for component in profile["components"].values():
                assert 0.0 <= component["score"] <= 1.0


def test_evidence_never_exceeds_the_stated_limit() -> None:
    """A person holding a record reads a few facts, not all of them."""
    collection = fold_collection(
        [_row(str(index), f"LP {index}", artists=[f"a-{index}"], labels=["l-blue-note"], genres=["Jazz"], styles=["Hard Bop"]) for index in range(6)]
    )
    candidate = _context(
        artists=[("a-0", "One"), ("a-1", "Two"), ("a-2", "Three"), ("a-3", "Four")],
        labels=[("l-blue-note", "Blue Note")],
        genres=["Jazz"],
        styles=["Hard Bop"],
    )

    component = score_affinity(collection, candidate)

    assert len(component["evidence"]) == int(FIT_CONSTANTS["evidence_limit"])


# ──────────────────────────────────────────────────────────────────────────────
# evidence_items — the structured claim beside each sentence
# ──────────────────────────────────────────────────────────────────────────────


def test_evidence_items_never_exceeds_the_stated_limit_either() -> None:
    """The cap a collector's sentence list respects is not looser for the structured one."""
    collection = fold_collection(
        [_row(str(index), f"LP {index}", artists=[f"a-{index}"], labels=["l-blue-note"], genres=["Jazz"], styles=["Hard Bop"]) for index in range(6)]
    )
    candidate = _context(
        artists=[("a-0", "One"), ("a-1", "Two"), ("a-2", "Three"), ("a-3", "Four")],
        labels=[("l-blue-note", "Blue Note")],
        genres=["Jazz"],
        styles=["Hard Bop"],
    )

    component = score_affinity(collection, candidate)

    assert len(component["evidence_items"]) == int(FIT_CONSTANTS["evidence_limit"])
    assert len(component["evidence_items"]) == len(component["evidence"])


def test_affinity_evidence_items_name_the_dimension_entity_and_count() -> None:
    """A consumer that wants the claim rather than the sentence reads the same fact."""
    component = score_affinity(_jazz_collection(), _context())

    assert {"dimension": "genre", "entity": "Jazz", "kind": "shared", "count": 3} in component["evidence_items"]
    assert {"dimension": "label", "entity": "Blue Note", "kind": "shared", "count": 3} in component["evidence_items"]


def test_bridge_evidence_items_name_the_regions_and_flag_the_heuristic() -> None:
    """The bridged regions and the caveat that they are a v0 heuristic, both structured."""
    component = score_bridge(_two_region_collection(), _context(genres=["Jazz", "Electronic"], styles=[]))

    assert component["evidence_items"][0] == {"dimension": "genre", "entity": "Electronic and Jazz", "kind": "bridge", "count": 2}
    assert component["evidence_items"][1] == {"dimension": "bridge", "entity": "v0 heuristic", "kind": "heuristic"}


def test_redundancy_exact_duplicate_evidence_item_carries_the_matched_release_id() -> None:
    """The held version a low fit is scored on is nameable, not just readable in prose."""
    component = score_redundancy(_jazz_collection(), _context(release_id="1"))

    assert component["evidence_items"] == [{"dimension": "release", "entity": "1", "kind": "duplicate", "release_id": "1"}]


def test_redundancy_same_family_evidence_item_carries_the_shared_family_as_detail() -> None:
    """The same-master, same-family case names the sibling and the family it shares."""
    candidate = _context(release_id="999", siblings=[{"id": "1", "title": "Moanin'", "media_families": ["grooved"]}])

    component = score_redundancy(_jazz_collection(), candidate)

    assert component["evidence_items"] == [{"dimension": "release", "entity": "Moanin'", "kind": "duplicate", "release_id": "1", "detail": "grooved"}]


def test_redundancy_same_title_evidence_item_uses_its_own_kind() -> None:
    """The weakest redundancy case — no master link — is its own claim kind, not `duplicate`."""
    collection = fold_collection([_row("1", "Blue Train", artists=["a-coltrane"], labels=["l-blue-note"], genres=["Jazz"])])
    candidate = _context(release_id="999", master_id=None, siblings=[])

    component = score_redundancy(collection, candidate)

    assert component["evidence_items"] == [{"dimension": "release", "entity": "Blue Train", "kind": "duplicate_title", "detail": "John Coltrane"}]


def test_every_evidence_sentence_is_rendered_from_its_own_entry() -> None:
    """The sentence and the structured claim cannot disagree because one produces the other.

    Exercised across every component and every branch this bead touches: a shared facet,
    an unheld one, a deepened thread, a bridge between two regions plus its heuristic
    caveat, and each of redundancy's four cases — so a hand-typed sentence that drifted
    from its entry would fail here rather than surviving because no test built the
    scenario that exercises it.
    """
    profiles = [
        compute_fit(_jazz_collection(), _context(release_id="1")),
        compute_fit(_jazz_collection(), _context(release_id="999", siblings=[{"id": "1", "title": "Moanin'", "media_families": ["grooved"]}])),
        compute_fit(_jazz_collection(), _context(release_id="999", siblings=[{"id": "1", "title": "Moanin'", "media_families": ["digital"]}])),
        compute_fit(
            fold_collection([_row("1", "Blue Train", artists=["a-coltrane"], labels=["l-blue-note"], genres=["Jazz"])]),
            _context(release_id="999", master_id=None, siblings=[]),
        ),
        compute_fit(_two_region_collection(), _context(genres=["Jazz", "Electronic"], styles=[])),
        compute_fit(_jazz_collection(), _context()),
        compute_fit(empty_collection(), _context()),
    ]

    for profile in profiles:
        for component in profile["components"].values():
            assert len(component["evidence_items"]) == len(component["evidence"])
            for sentence, item in zip(component["evidence"], component["evidence_items"], strict=True):
                assert _render_entry(item) == sentence


# ──────────────────────────────────────────────────────────────────────────────
# evidence_entries — uncapped, with ids, for the Strata atlas
# ──────────────────────────────────────────────────────────────────────────────

_LEGACY_EVIDENCE = Path(__file__).parent / "fixtures" / "fit_legacy_evidence.json"


def _legacy_scenarios() -> dict[str, tuple[dict[str, Any], dict[str, Any]]]:
    """The scenarios `fixtures/fit_legacy_evidence.json` was recorded from, before ids existed.

    One per evidence kind and redundancy case, plus the two caps: affinity's and novelty's.
    """
    sibling = {"id": "1", "title": "Moanin'"}
    return {
        "exact_duplicate": (_jazz_collection(), _context(release_id="1")),
        "same_master_same_family": (_jazz_collection(), _context(release_id="999", siblings=[{**sibling, "media_families": ["grooved"]}])),
        "same_master_other_family": (_jazz_collection(), _context(release_id="999", siblings=[{**sibling, "media_families": ["digital"]}])),
        "same_artist_and_title": (
            fold_collection([_row("1", "Blue Train", artists=["a-coltrane"], labels=["l-blue-note"], genres=["Jazz"])]),
            _context(release_id="999", master_id=None),
        ),
        "bridge_two_regions": (_two_region_collection(), _context(genres=["Jazz", "Electronic"], styles=[])),
        "bridge_comma_genre": (_comma_genre_collection(), _context(genres=["Folk, World, & Country", "Jazz", "Rock"], styles=[])),
        "jazz_candidate": (_jazz_collection(), _context()),
        "empty_collection": (empty_collection(), _context()),
        "capped_affinity": (
            fold_collection(
                [
                    _row(str(index), f"LP {index}", artists=[f"a-{index}"], labels=["l-blue-note"], genres=["Jazz"], styles=["Hard Bop"])
                    for index in range(6)
                ]
            ),
            _context(artists=[("a-0", "One"), ("a-1", "Two"), ("a-2", "Three"), ("a-3", "Four")], genres=["Jazz"], styles=["Hard Bop"]),
        ),
        "novelty_cap_hides_style": (_jazz_collection(), _modal_candidate()),
    }


def _comma_genre_collection() -> dict[str, Any]:
    """Three separate regions, one of them a Discogs genre whose name contains commas."""
    return fold_collection(
        [
            _row("1", "A", artists=["a-1"], labels=["l-1"], genres=["Folk, World, & Country"]),
            _row("2", "B", artists=["a-2"], labels=["l-2"], genres=["Jazz"]),
            _row("3", "C", artists=["a-3"], labels=["l-3"], genres=["Rock"]),
        ]
    )


def _modal_candidate() -> dict[str, Any]:
    """Three unheld artists and an unheld label sort ahead of the one unheld style."""
    return _context(
        artists=[("a-x", "Xavier"), ("a-y", "Yusef"), ("a-z", "Zoot")],
        labels=[("l-impulse", "Impulse!")],
        genres=["Jazz"],
        styles=["Modal"],
    )


def test_evidence_and_evidence_items_are_byte_identical_to_before_entries_existed() -> None:
    """The capped lists existing consumers read did not change by a byte.

    The fixture was recorded from the scoring as it stood before `evidence_entries` and
    its ids were added, so a new key leaking into `evidence_items`, a reordered entry, or a
    reworded sentence fails here.
    """
    actual = {
        name: {
            component_name: {"evidence": component["evidence"], "evidence_items": component["evidence_items"]}
            for component_name, component in compute_fit(collection, candidate)["components"].items()
        }
        for name, (collection, candidate) in _legacy_scenarios().items()
    }

    assert json.dumps(actual, indent=2, ensure_ascii=False) + "\n" == _LEGACY_EVIDENCE.read_text(encoding="utf-8")


def test_evidence_items_are_the_leading_entries_without_the_new_keys() -> None:
    """Below the cap, entry `i` is item `i` plus ids, so the two never disagree on a fact."""
    for collection, candidate in _legacy_scenarios().values():
        for component in compute_fit(collection, candidate)["components"].values():
            entries = component["evidence_entries"]
            assert len(entries) >= len(component["evidence_items"])
            for item, entry in zip(component["evidence_items"], entries, strict=False):
                assert {key: value for key, value in entry.items() if key in item} == item
                assert set(entry) - set(item) <= {"entity_type", "entity_id", "genres", "master_id", "drawable"}


def test_every_entry_naming_one_entity_carries_its_type_and_id() -> None:
    """Artists and labels by Discogs id, genres and styles by name, releases by id.

    The type is stated on the entry, never left to be inferred from the shape of the id:
    a Discogs id and a genre name are both strings. The bridge names several genres (its
    `genres`) and the caveat names none, so neither carries a type or an id.
    """
    seen: set[tuple[str, str]] = set()
    for collection, candidate in _legacy_scenarios().values():
        for component in compute_fit(collection, candidate)["components"].values():
            for entry in component["evidence_entries"]:
                if entry["kind"] in {"bridge", "heuristic"}:
                    assert "entity_type" not in entry
                    assert "entity_id" not in entry
                    continue
                assert entry["entity_id"], entry
                seen.add((entry["kind"], entry["entity_type"]))

    assert seen == {
        ("shared", "artist"),
        ("shared", "label"),
        ("shared", "genre"),
        ("shared", "style"),
        ("unheld", "artist"),
        ("unheld", "label"),
        ("unheld", "genre"),
        ("unheld", "style"),
        ("thread", "artist"),
        ("thread", "label"),
        ("duplicate", "release"),
        ("duplicate_title", "release"),
    }


def test_artist_and_label_entries_carry_their_discogs_ids_not_their_names() -> None:
    """The display name stays in `entity`; the id a consumer links on is beside it."""
    profile = compute_fit(_jazz_collection(), _context())

    affinity = profile["components"]["affinity"]["evidence_entries"]
    novelty = profile["components"]["novelty"]["evidence_entries"]
    depth = profile["components"]["depth"]["evidence_entries"]
    assert {
        "dimension": "label",
        "entity": "Blue Note",
        "kind": "shared",
        "count": 3,
        "entity_type": "label",
        "entity_id": "l-blue-note",
        "drawable": False,
    } in affinity
    assert {
        "dimension": "genre",
        "entity": "Jazz",
        "kind": "shared",
        "count": 3,
        "entity_type": "genre",
        "entity_id": "Jazz",
        "drawable": True,
    } in affinity
    assert novelty == [
        {"dimension": "artist", "entity": "John Coltrane", "kind": "unheld", "entity_type": "artist", "entity_id": "a-coltrane", "drawable": False}
    ]
    assert depth == [
        {
            "dimension": "label",
            "entity": "Blue Note",
            "kind": "thread",
            "count": 3,
            "entity_type": "label",
            "entity_id": "l-blue-note",
            "drawable": False,
        }
    ]


def test_two_artists_sharing_a_name_keep_their_own_ids() -> None:
    """The case ids exist for: two distinct artists a name alone would merge."""
    candidate = _context(artists=[("a-1", "Sun Ra"), ("a-2", "Sun Ra")])

    novelty = score_novelty(empty_collection(), candidate)["evidence_entries"]

    assert [entry["entity_id"] for entry in novelty if entry["dimension"] == "artist"] == ["a-1", "a-2"]


def test_graph_ids_are_stringified_the_way_strata_and_explore_return_them() -> None:
    """An integer node id comes out as its string, so a client can join on the Strata ids."""
    candidate = _context(artists=[(1001, "John Coltrane")], labels=[(2002, "Impulse!")])  # type: ignore[list-item]

    novelty = score_novelty(empty_collection(), candidate)["evidence_entries"]

    assert {(entry["entity_type"], entry["entity_id"]) for entry in novelty} >= {("artist", "1001"), ("label", "2002")}


def test_bridge_entry_carries_its_genres_as_a_list() -> None:
    """A genre name with commas in it survives, which the rendered sentence cannot promise."""
    component = score_bridge(_comma_genre_collection(), _context(genres=["Folk, World, & Country", "Jazz", "Rock"], styles=[]))

    bridge, caveat = component["evidence_entries"]
    assert bridge["genres"] == ["Folk, World, & Country", "Jazz", "Rock"]
    assert bridge["entity"] == "Folk, World, & Country, Jazz and Rock"
    assert bridge["drawable"] is True
    assert "entity_type" not in bridge
    assert "entity_id" not in bridge
    assert caveat == {"dimension": "bridge", "entity": "v0 heuristic", "kind": "heuristic", "drawable": False}


def test_the_novelty_cap_hides_a_drawable_style_that_entries_still_carry() -> None:
    """The 5.9% case: three unheld artists fill the cap and the unheld style is cut."""
    component = score_novelty(_jazz_collection(), _modal_candidate())

    assert [item["dimension"] for item in component["evidence_items"]] == ["artist", "artist", "artist"]
    assert not any(item["dimension"] == "style" for item in component["evidence_items"])
    drawable = [entry for entry in component["evidence_entries"] if entry["drawable"]]
    assert drawable == [{"dimension": "style", "entity": "Modal", "kind": "unheld", "entity_type": "style", "entity_id": "Modal", "drawable": True}]
    assert len(component["evidence_entries"]) == 5


def test_affinity_entries_are_uncapped() -> None:
    """Seven shared facets are all in `evidence_entries`; three are in `evidence_items`."""
    collection, candidate = _legacy_scenarios()["capped_affinity"]

    component = score_affinity(collection, candidate)

    assert len(component["evidence_items"]) == int(FIT_CONSTANTS["evidence_limit"])
    assert len(component["evidence_entries"]) == 7
    assert {entry["entity_id"] for entry in component["evidence_entries"] if entry["drawable"]} == {"Jazz", "Hard Bop"}


def test_redundancy_duplicates_carry_the_release_and_the_master() -> None:
    """The atlas draws a duplicate as the held release under the candidate's master."""
    exact = score_redundancy(_jazz_collection(), _context(release_id="1"))["evidence_entries"]
    sibling = score_redundancy(
        _jazz_collection(), _context(release_id="999", siblings=[{"id": "1", "title": "Moanin'", "media_families": ["digital"]}])
    )["evidence_entries"]

    assert exact == [
        {
            "dimension": "release",
            "entity": "1",
            "kind": "duplicate",
            "release_id": "1",
            "entity_type": "release",
            "entity_id": "1",
            "master_id": "m-blue-train",
            "drawable": True,
        }
    ]
    assert sibling == [
        {
            "dimension": "release",
            "entity": "Moanin'",
            "kind": "duplicate",
            "release_id": "1",
            "detail": "digital",
            "entity_type": "release",
            "entity_id": "1",
            "master_id": "m-blue-train",
            "drawable": True,
        }
    ]


def test_a_held_masterless_release_is_not_drawable() -> None:
    """With no master there is no L2 slot to put the duplicate in."""
    entries = score_redundancy(_jazz_collection(), _context(release_id="1", master_id=None))["evidence_entries"]

    assert entries == [
        {"dimension": "release", "entity": "1", "kind": "duplicate", "release_id": "1", "entity_type": "release", "entity_id": "1", "drawable": False}
    ]


def test_a_title_only_duplicate_names_the_held_release_id_but_is_not_drawable() -> None:
    """No master links the two, so the held record is listed rather than placed."""
    collection = fold_collection(
        [
            _row("7", "Blue Train", artists=["a-coltrane"]),
            _row("4", "BLUE TRAIN", artists=["a-coltrane"]),
        ]
    )

    entries = score_redundancy(collection, _context(release_id="999", master_id=None))["evidence_entries"]

    assert entries[0]["kind"] == "duplicate_title"
    assert entries[0]["entity_type"] == "release"
    assert entries[0]["entity_id"] == "4"
    assert entries[0]["drawable"] is False


def test_a_title_only_duplicate_from_a_collection_cached_before_ids_has_no_id() -> None:
    """A fold cached before `held_title_releases` existed still scores; the id is just absent."""
    collection = fold_collection([_row("1", "Blue Train", artists=["a-coltrane"])])
    del collection["held_title_releases"]

    component = score_redundancy(collection, _context(release_id="999", master_id=None))

    assert component["score"] == FIT_CONSTANTS["redundancy_same_artist_and_title"]
    assert "entity_id" not in component["evidence_entries"][0]
    assert "entity_type" not in component["evidence_entries"][0]
