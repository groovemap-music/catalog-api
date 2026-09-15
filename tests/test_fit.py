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

from typing import Any

from api.fit import (
    FIT_CONSTANTS,
    FIT_VERSION,
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
    assert score_depth(empty_collection(), _context()) == {"score": 0.0, "evidence": []}


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
    assert score_redundancy(empty_collection(), _context()) == {"score": 0.0, "evidence": []}


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
