"""The in-memory adapter answers the exact shapes the DB-bound query functions return.

Every expectation here is pinned to a captured example from the tests that cover the real
queries (``tests/test_recommend_queries.py``, ``tests/test_rarity_queries.py``). A shape that
drifts from those breaks here rather than silently producing a baseline computed on
differently-shaped rows.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from api.evaluation.fixtures import load_golden_set
from api.evaluation.graph import CollectorHoldings, GoldenGraph, _counted
from api.queries.recommend_queries import (
    MIN_ARTIST_RELEASES,
    compute_similar_artists,
    merge_recommendation_candidates,
    score_discoveries,
)
from api.rarity import ReleaseContext, resolve_media, score_release
from api.rarity.core import (
    compute_collection_prevalence_score,
    compute_graph_isolation_score,
    compute_label_catalog_score,
    compute_medium_rarity_score,
    compute_temporal_scarcity_score,
)
from api.rarity.families.grooved import PRESSING_FACT


# ── Captured examples, copied from the tests covering the real queries ──

CAPTURED_PROFILE_ITEM = {"name": "Rock", "count": 10}
CAPTURED_CANDIDATE_ARTIST = {
    "artist_id": "c1",
    "artist_name": "Candidate One",
    "release_count": 50,
    "genres": [{"name": "Rock", "count": 40}],
    "styles": [],
    "labels": [],
    "collaborators": [],
}
CAPTURED_RECOMMENDATION_CANDIDATE = {
    "id": "r1",
    "title": "A",
    "artist": "X",
    "label": "Warp",
    "year": 2000,
    "genres": ["Electronic"],
    "score": 10,
    "source": "label: Warp (top label)",
}
CAPTURED_EXPLORE_ROW = {
    "id": "a2",
    "name": "Related Artist",
    "type": "artist",
    "path_names": ["Start", "Related Artist"],
    "rel_types": ["BY"],
    "dist": 1,
}
CAPTURED_RARITY_ROWS = {
    "release": {"release_id": "1", "title": "R1", "artist_name": "A1", "year": 1970},
    "media": {"release_id": "1", "mediums": [{"id": "optical_cd", "family": "optical"}], "media_families": ["optical"], "formats": ["CD"]},
    "label": {"release_id": "1", "label_catalog_size": 20},
    "temporal": {"release_id": "1", "year": 1970, "latest_sibling_year": None},
    "degree": {"release_id": "1", "degree": 5},
    "artist_degree": {"release_id": "1", "artist_max_degree": 500},
    "label_size": {"release_id": "1", "label_max_catalog": 2000},
    "genre_count": {"release_id": "1", "genre_max_release_count": 50000},
}
CAPTURED_PRESSING_ROW = {"release_id": "1", "pressing_count": 1}


@pytest.fixture(scope="module")
def graph() -> GoldenGraph:
    return GoldenGraph(load_golden_set())


@pytest.fixture(scope="module")
def collector_id(graph: GoldenGraph) -> str:
    return graph.golden.collector_ids[0]


def _assert_shape(row: dict, captured: dict) -> None:
    """Assert ``row`` carries the captured keys, with the captured value types."""
    assert set(row) >= set(captured), f"missing keys: {set(captured) - set(row)}"
    for key, example in captured.items():
        if example is None or row[key] is None:
            continue
        assert isinstance(row[key], type(example)), f"{key}: {type(row[key])} is not {type(example)}"


# ── Artist similarity shapes ────────────────────────────────────────


def test_artist_identity_matches_the_captured_shape(graph: GoldenGraph) -> None:
    identity = graph.artist_identity("a001")
    assert identity is not None
    _assert_shape(identity, {"artist_id": "a1", "artist_name": "Miles Davis", "release_count": 200})
    assert identity["release_count"] > 0


def test_artist_identity_is_none_for_an_unknown_artist(graph: GoldenGraph) -> None:
    assert graph.artist_identity("nope") is None


def test_artist_profile_carries_every_dimension_as_counted_items(graph: GoldenGraph) -> None:
    profile = graph.artist_profile("a001")
    assert set(profile) == {"genres", "styles", "labels", "collaborators"}
    for dimension, items in profile.items():
        for item in items:
            _assert_shape(item, CAPTURED_PROFILE_ITEM), dimension
    assert profile["genres"], "the fixture's first artist has no genres"


def test_profile_items_are_ordered_by_count_descending() -> None:
    counted = _counted(["a", "b", "b", "c", "c", "c"])
    assert counted == [{"name": "c", "count": 3}, {"name": "b", "count": 2}, {"name": "a", "count": 1}]


def test_profile_ties_are_broken_by_name_so_runs_are_reproducible() -> None:
    assert _counted(["z", "a"]) == [{"name": "a", "count": 1}, {"name": "z", "count": 1}]


def test_unknown_artist_profiles_to_empty_dimensions(graph: GoldenGraph) -> None:
    assert graph.artist_profile("nope") == {"genres": [], "styles": [], "labels": [], "collaborators": []}


def test_batch_profiles_answer_for_every_requested_id(graph: GoldenGraph) -> None:
    profiles = graph.batch_artist_profiles(["a001", "a002", "nope"])
    assert set(profiles) == {"a001", "a002", "nope"}
    assert profiles["nope"]["genres"] == []


def test_candidate_artists_match_the_captured_shape(graph: GoldenGraph) -> None:
    candidates = graph.candidate_artists("a001")
    assert candidates
    for candidate in candidates:
        _assert_shape(candidate, CAPTURED_CANDIDATE_ARTIST)
        assert candidate["artist_id"] != "a001"
        assert candidate["release_count"] >= MIN_ARTIST_RELEASES
    scores = [candidate["release_count"] for candidate in candidates]
    assert scores == sorted(scores, reverse=True)


def test_candidate_artists_is_empty_for_an_unknown_artist(graph: GoldenGraph) -> None:
    assert graph.candidate_artists("nope") == []


def test_compute_similar_artists_runs_end_to_end_on_the_fixture(graph: GoldenGraph) -> None:
    target = graph.artist_profile("a001")
    results = compute_similar_artists(target, graph.candidate_artists("a001"), limit=10)
    assert results
    assert set(results[0]) == {"artist_id", "artist_name", "similarity", "breakdown", "release_count", "shared_genres", "shared_labels"}
    assert set(results[0]["breakdown"]) == {"genre", "style", "label", "collaborator"}
    assert all(0.0 < row["similarity"] <= 1.0 for row in results)
    assert [row["similarity"] for row in results] == sorted((row["similarity"] for row in results), reverse=True)


# ── Recommendation candidate shapes ─────────────────────────────────


@pytest.mark.parametrize("method", ["artist_affinity_candidates", "label_affinity_candidates", "blindspot_candidates"])
def test_candidate_rows_match_the_captured_shape(graph: GoldenGraph, collector_id: str, method: str) -> None:
    rows = getattr(graph, method)(collector_id)
    assert rows, f"{method} produced nothing for {collector_id}"
    for row in rows:
        _assert_shape(row, CAPTURED_RECOMMENDATION_CANDIDATE)
    assert [row["score"] for row in rows] == sorted((row["score"] for row in rows), reverse=True)


@pytest.mark.parametrize("method", ["artist_affinity_candidates", "label_affinity_candidates"])
def test_candidates_never_include_what_the_collector_owns_or_wants(graph: GoldenGraph, collector_id: str, method: str) -> None:
    holding = graph.holdings[collector_id]
    returned = {row["id"] for row in getattr(graph, method)(collector_id)}
    assert not returned & set(holding.owned)
    assert not returned & holding.wants


def test_candidate_limits_are_respected(graph: GoldenGraph, collector_id: str) -> None:
    assert len(graph.label_affinity_candidates(collector_id, limit=7)) == 7
    assert len(graph.artist_affinity_candidates(collector_id, limit=3)) == 3


def test_sources_name_the_signal_that_produced_them(graph: GoldenGraph, collector_id: str) -> None:
    assert all(row["source"].startswith("label: ") for row in graph.label_affinity_candidates(collector_id))
    assert all(row["source"].startswith("blind_spot: ") for row in graph.blindspot_candidates(collector_id))
    assert all(row["source"].startswith("artist: ") for row in graph.artist_affinity_candidates(collector_id))


def test_blindspot_genres_are_genres_the_collector_owns_nothing_in(graph: GoldenGraph, collector_id: str) -> None:
    owned_genres = {genre for release_id in graph.holdings[collector_id].owned for genre in graph.golden.releases[release_id].genres}
    for row in graph.blindspot_candidates(collector_id):
        assert row["genres"]
        assert not set(row["genres"]) & owned_genres


def test_an_unknown_collector_has_no_candidates(graph: GoldenGraph) -> None:
    assert graph.label_affinity_candidates("nope") == []
    assert graph.blindspot_candidates("nope") == []
    assert graph.artist_affinity_candidates("nope") == []
    assert graph.taste_genre_vector("nope") == {}
    assert graph.blind_spot_genres("nope") == set()


def test_collector_counts_count_distinct_collectors(graph: GoldenGraph) -> None:
    release_ids = list(graph.golden.release_ids[:10])
    counts = graph.collector_counts(release_ids)
    assert set(counts) == set(release_ids)
    assert all(isinstance(value, int) and value >= 0 for value in counts.values())
    for release_id, count in counts.items():
        expected = sum(1 for holding in graph.holdings.values() if release_id in holding.owned)
        assert count == expected


def test_collector_counts_ignore_unknown_ids_and_empty_input(graph: GoldenGraph) -> None:
    assert graph.collector_counts([]) == {}
    assert graph.collector_counts(["nope"]) == {}


def test_merge_recommendation_candidates_runs_end_to_end_on_the_fixture(graph: GoldenGraph, collector_id: str) -> None:
    artist = graph.artist_affinity_candidates(collector_id)
    label = graph.label_affinity_candidates(collector_id)
    blindspot = graph.blindspot_candidates(collector_id)
    all_ids = sorted({row["id"] for rows in (artist, label, blindspot) for row in rows})
    merged = merge_recommendation_candidates(artist, label, blindspot, graph.collector_counts(all_ids), limit=25)
    assert merged
    assert len(merged) <= 25
    assert set(merged[0]) == {"id", "title", "artist", "label", "year", "genres", "score", "reasons"}
    assert [row["score"] for row in merged] == sorted((row["score"] for row in merged), reverse=True)
    assert {row["id"] for row in merged} <= set(all_ids)


def test_taste_vector_is_a_normalised_genre_share(graph: GoldenGraph, collector_id: str) -> None:
    vector = graph.taste_genre_vector(collector_id)
    assert vector
    assert abs(sum(vector.values()) - 1.0) < 1e-12


def test_blind_spot_genres_exclude_owned_genres(graph: GoldenGraph, collector_id: str) -> None:
    owned_genres = {genre for release_id in graph.holdings[collector_id].owned for genre in graph.golden.releases[release_id].genres}
    assert not graph.blind_spot_genres(collector_id) & owned_genres


# ── Explore traversal ───────────────────────────────────────────────


def test_explore_traversal_matches_the_captured_shape(graph: GoldenGraph) -> None:
    rows = graph.explore_traversal("artist", "a001")
    assert rows
    for row in rows:
        _assert_shape(row, CAPTURED_EXPLORE_ROW)
        assert row["type"] in {"artist", "label", "genre", "style"}
        assert 1 <= row["dist"] <= 2
        assert len(row["path_names"]) == row["dist"] + 1
        assert len(row["rel_types"]) == row["dist"]
    assert [row["dist"] for row in rows] == sorted(row["dist"] for row in rows)


def test_explore_traversal_rejects_an_unknown_entity_type(graph: GoldenGraph) -> None:
    assert graph.explore_traversal("user", "u001") == []


def test_explore_traversal_returns_nothing_for_an_unknown_node(graph: GoldenGraph) -> None:
    assert graph.explore_traversal("artist", "nope") == []
    assert graph.explore_traversal("genre", "Nonexistent Genre") == []


@pytest.mark.parametrize("hops", [0, 4, -1])
def test_out_of_range_hops_fall_back_to_two(graph: GoldenGraph, hops: int) -> None:
    assert graph.explore_traversal("artist", "a001", hops=hops) == graph.explore_traversal("artist", "a001", hops=2)


def test_deeper_traversal_discovers_at_least_as_much(graph: GoldenGraph) -> None:
    near = graph.explore_traversal("artist", "a001", hops=1)
    far = graph.explore_traversal("artist", "a001", hops=3)
    assert {row["id"] for row in near} <= {row["id"] for row in far}
    assert len(far) > len(near)


def test_genre_nodes_are_keyed_by_name_not_id(graph: GoldenGraph) -> None:
    rows = graph.explore_traversal("genre", "Jazz", hops=2)
    assert rows
    for row in rows:
        if row["type"] in {"genre", "style"}:
            assert row["id"] == row["name"]


def test_score_discoveries_runs_end_to_end_on_the_fixture(graph: GoldenGraph, collector_id: str) -> None:
    discoveries = graph.explore_traversal("artist", "a001", hops=2)
    scored = score_discoveries(discoveries, graph.taste_genre_vector(collector_id), graph.blind_spot_genres(collector_id), limit=10)
    assert scored
    assert set(scored[0]) == {"id", "name", "type", "score", "path", "reason"}
    assert all(row["reason"] in {"graph_proximity", "blind_spot_boost"} for row in scored)
    assert [row["score"] for row in scored] == sorted((row["score"] for row in scored), reverse=True)


# ── Rarity signal shapes ────────────────────────────────────────────


def test_core_signal_rows_match_the_captured_shapes(graph: GoldenGraph) -> None:
    release_ids = list(graph.golden.release_ids)
    rows = graph.core_signal_rows(release_ids)
    assert set(rows) == set(CAPTURED_RARITY_ROWS)
    for fact, captured in CAPTURED_RARITY_ROWS.items():
        assert len(rows[fact]) == len(release_ids), f"{fact} did not answer for every release"
        for row in rows[fact]:
            _assert_shape(row, captured)


def test_media_rows_carry_canonical_mediums_families_and_legacy_formats(graph: GoldenGraph) -> None:
    for row in graph.core_signal_rows(graph.golden.release_ids)["media"]:
        assert row["mediums"]
        for medium in row["mediums"]:
            assert set(medium) == {"id", "family"}
            assert medium["family"] in {"vinyl", "optical", "tape"}
        assert row["media_families"] == sorted({medium["family"] for medium in row["mediums"]})
        assert row["formats"]


def test_temporal_rows_report_the_latest_sibling_pressing(graph: GoldenGraph) -> None:
    rows = {row["release_id"]: row for row in graph.core_signal_rows(graph.golden.release_ids)["temporal"]}
    with_siblings = 0
    for release_id, row in rows.items():
        release = graph.golden.releases[release_id]
        siblings = [other for other in graph.golden.releases.values() if other.master_id == release.master_id and other.id != release_id]
        if release.master_id is not None and siblings:
            with_siblings += 1
            assert row["latest_sibling_year"] == max(other.year for other in siblings)
        else:
            assert row["latest_sibling_year"] is None
    assert with_siblings > 0


def test_pressing_rows_match_the_captured_shape(graph: GoldenGraph) -> None:
    rows = graph.grooved_pressing_rows(graph.golden.release_ids)
    assert len(rows) == len(graph.golden.releases)
    for row in rows:
        _assert_shape(row, CAPTURED_PRESSING_ROW)
    counts = {row["pressing_count"] for row in rows}
    assert 0 in counts, "no standalone release, so the no-master branch is untested"
    assert counts - {0}, "no release with a master link"


def test_pressing_count_is_zero_without_a_master_and_counts_this_one(graph: GoldenGraph) -> None:
    for release in graph.golden.releases.values():
        expected = 0 if release.master_id is None else sum(1 for other in graph.golden.releases.values() if other.master_id == release.master_id)
        assert graph.pressing_count(release.id) == expected


def test_signal_rows_skip_unknown_release_ids(graph: GoldenGraph) -> None:
    assert graph.core_signal_rows(["nope"])["release"] == []
    assert graph.grooved_pressing_rows(["nope"]) == []


def test_score_release_runs_end_to_end_on_the_fixture(graph: GoldenGraph) -> None:
    release_ids = list(graph.golden.release_ids)
    rows = graph.core_signal_rows(release_ids)
    indexed = {fact: {row["release_id"]: row for row in fact_rows} for fact, fact_rows in rows.items()}
    pressing = {row["release_id"]: row for row in graph.grooved_pressing_rows(release_ids)}
    community = graph.community_counts(release_ids)

    tiers: set[str] = set()
    grooved_scored = 0
    for release_id in release_ids:
        media_row = indexed["media"][release_id]
        media = resolve_media(mediums=media_row["mediums"], media_families=media_row["media_families"], formats=media_row["formats"])
        have, want = community[release_id]
        scored = score_release(
            ReleaseContext(
                release_id=release_id,
                media=media,
                year=indexed["release"][release_id]["year"],
                facts={PRESSING_FACT: pressing[release_id]},
            ),
            {
                "label_catalog": compute_label_catalog_score(indexed["label"][release_id]["label_catalog_size"]),
                "medium_rarity": compute_medium_rarity_score(media),
                "temporal_scarcity": compute_temporal_scarcity_score(
                    indexed["temporal"][release_id]["year"], indexed["temporal"][release_id]["latest_sibling_year"], 2026
                ),
                "graph_isolation": compute_graph_isolation_score(indexed["degree"][release_id]["degree"]),
                "collection_prevalence": compute_collection_prevalence_score(have, want),
            },
        )
        assert 0.0 <= scored.score <= 100.0
        assert abs(sum(scored.weights.values()) - 1.0) < 1e-12
        tiers.add(scored.tier)
        if media.families == ("vinyl",):
            grooved_scored += 1
            assert "pressing_scarcity" in scored.signals
        else:
            assert "pressing_scarcity" not in scored.signals
    assert grooved_scored == 40, "the vinyl third of the set must carry the grooved signal"
    assert len(tiers) > 1, "every release landed in one tier; the fixture has no rarity spread"


# ── Holdings scoping ────────────────────────────────────────────────


def test_restricting_holdings_hides_collected_edges(graph: GoldenGraph) -> None:
    golden = graph.golden
    collector_id = golden.collector_ids[0]
    full_owned = graph.holdings[collector_id].owned
    trimmed = GoldenGraph(golden, {collector_id: CollectorHoldings(collector_id, full_owned[:2], frozenset())})

    assert trimmed.holdings[collector_id].owned == full_owned[:2]
    assert set(trimmed.collector_counts(golden.release_ids).values()) <= {0, 1}
    assert sum(trimmed.collector_counts(golden.release_ids).values()) == 2
    assert trimmed.release_degree(full_owned[0]) < graph.release_degree(full_owned[0])


def test_the_adapter_is_deterministic(graph: GoldenGraph, collector_id: str) -> None:
    rebuilt = GoldenGraph(graph.golden)
    assert rebuilt.label_affinity_candidates(collector_id) == graph.label_affinity_candidates(collector_id)
    assert rebuilt.candidate_artists("a001") == graph.candidate_artists("a001")
    assert rebuilt.explore_traversal("artist", "a001") == graph.explore_traversal("artist", "a001")
    assert rebuilt.core_signal_rows(graph.golden.release_ids) == graph.core_signal_rows(graph.golden.release_ids)


def test_path_names_follow_the_coalesce_rule_for_every_node_kind(graph: GoldenGraph) -> None:
    """``coalesce(n.name, n.title, n.id)``: name for artists and labels, title for releases and
    masters, and the id itself for the name-keyed Genre and Style nodes.

    A Master never surfaces as a *discovery* -- it is not one of the four discoverable labels
    -- and in this fixture it never appears as an intermediate either, because sibling
    pressings share their artist, label, and genres, so everything reachable through a master
    was already reached at a shorter distance. The rendering rule still has to be right, since
    a graph where reissues carry different credits would route through it.
    """
    golden = graph.golden
    artist = next(iter(golden.artists.values()))
    label = next(iter(golden.labels.values()))
    release = next(iter(golden.releases.values()))
    master = next(iter(golden.masters.values()))

    assert graph._display_name(("artist", artist.id)) == artist.name
    assert graph._display_name(("label", label.id)) == label.name
    assert graph._display_name(("release", release.id)) == release.title
    assert graph._display_name(("master", master.id)) == master.title
    assert graph._display_name(("genre", "Jazz")) == "Jazz"


def test_a_release_with_no_known_artist_reports_a_null_artist_name(graph: GoldenGraph) -> None:
    """``collect(DISTINCT a.name)[0]`` is null when a release has no Artist edge."""
    golden = graph.golden
    orphan = replace(next(iter(golden.releases.values())), id="r9999", artist_ids=())
    patched = GoldenGraph(replace(golden, releases={**golden.releases, orphan.id: orphan}))

    assert patched._artist_name(orphan) is None
    assert patched.core_signal_rows([orphan.id])["release"][0]["artist_name"] is None
    assert patched.core_signal_rows([orphan.id])["artist_degree"][0]["artist_max_degree"] == 0
