"""The golden set is deterministic, format-balanced, and split-ready."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
from common.media import families_of

from api.evaluation.fixtures import GOLDEN_DIR, Release, load_golden_set, parse_golden_set
from scripts import generate_golden_set


BALANCE_FLOOR = 0.30
BALANCE_CEILING = 0.36


@pytest.fixture(scope="module")
def golden():
    return load_golden_set()


def test_regeneration_is_byte_identical(tmp_path: Path) -> None:
    """A regenerated set must match the committed bytes, or the fixture is not a fixture."""
    generate_golden_set.write(tmp_path)
    for name in ("catalog.json", "collectors.json"):
        assert (tmp_path / name).read_bytes() == (GOLDEN_DIR / name).read_bytes(), (
            f"{name} drifted from the committed golden set; regenerate it with `uv run python scripts/generate_golden_set.py` and commit the result"
        )


def test_generation_is_reproducible_across_calls() -> None:
    assert generate_golden_set.generate() == generate_golden_set.generate()


def test_a_different_seed_produces_a_different_set() -> None:
    assert generate_golden_set.generate(seed=1) != generate_golden_set.generate()


def test_release_and_collector_counts_match_the_design(golden) -> None:
    assert len(golden.releases) == generate_golden_set.RELEASE_COUNT
    assert len(golden.collectors) == generate_golden_set.COLLECTOR_COUNT
    assert golden.generator_version == generate_golden_set.GENERATOR_VERSION


def test_every_media_family_is_within_the_balance_band(golden) -> None:
    counts = golden.family_counts()
    assert set(counts) == set(generate_golden_set.BALANCED_FAMILIES)
    for family, count in counts.items():
        share = count / len(golden.releases)
        assert BALANCE_FLOOR <= share <= BALANCE_CEILING, f"{family} holds {share:.2%} of releases"


def test_families_resolve_through_the_vendored_taxonomy(golden) -> None:
    for release in golden.releases.values():
        assert release.media["items"], f"{release.id} carries no canonical media item"
        assert release.media["unmapped"] == {"formats": [], "descriptions": []}
        assert release.families == tuple(families_of(release.media))
        assert release.media["items"][0]["medium"] == release.medium


def test_release_years_span_the_designed_range(golden) -> None:
    years = [release.year for release in golden.releases.values()]
    assert min(years) >= generate_golden_set.YEAR_FIRST
    assert max(years) <= generate_golden_set.YEAR_LAST
    assert max(years) - min(years) > 40


def test_masters_group_several_releases(golden) -> None:
    per_master: dict[str, int] = {}
    for release in golden.releases.values():
        if release.master_id is not None:
            per_master[release.master_id] = per_master.get(release.master_id, 0) + 1
    assert set(per_master) == set(golden.masters)
    assert max(per_master.values()) > 1, "no master has sibling pressings to count"
    assert any(count == 1 for count in per_master.values()), "no master has a unique pressing"
    assert any(release.master_id is None for release in golden.releases.values()), "no standalone release"


def test_every_collector_straddles_the_split_cut(golden) -> None:
    for collector in golden.collectors.values():
        size = len(collector.collected)
        assert generate_golden_set.MIN_COLLECTION <= size <= generate_golden_set.MAX_COLLECTION
        before = [item for item in collector.collected if item.date_added < golden.split_cut]
        after = [item for item in collector.collected if item.date_added >= golden.split_cut]
        assert len(before) >= generate_golden_set.MIN_OBSERVED
        assert len(after) >= generate_golden_set.MIN_HELD_OUT
        assert len(before) + len(after) == size


def test_acquisition_dates_stay_inside_the_designed_window(golden) -> None:
    for collector in golden.collectors.values():
        for item in collector.collected:
            assert generate_golden_set.EARLIEST_ADDED <= item.date_added <= generate_golden_set.LATEST_ADDED


def test_collections_mix_formats(golden) -> None:
    for collector in golden.collectors.values():
        families = {family for release_id in collector.release_ids for family in golden.releases[release_id].families}
        assert len(families) >= 2, f"{collector.id} holds only {families}"


def test_collections_and_wantlists_reference_real_releases(golden) -> None:
    for collector in golden.collectors.values():
        owned = set(collector.release_ids)
        assert len(owned) == len(collector.collected), f"{collector.id} holds a duplicate"
        assert owned <= set(golden.releases)
        assert collector.wants <= set(golden.releases)
        assert not (owned & collector.wants), f"{collector.id} wants something it owns"


def test_releases_reference_real_artists_and_labels(golden) -> None:
    for release in golden.releases.values():
        assert release.label_id in golden.labels
        assert set(release.artist_ids) <= set(golden.artists)
        assert release.artist_ids


def test_split_cut_matches_the_generator(golden) -> None:
    assert golden.split_cut == generate_golden_set.SPLIT_CUT
    assert golden.split_cut == date(2023, 1, 1)


def test_loader_is_cached(golden) -> None:
    assert load_golden_set() is golden


def test_parse_golden_set_round_trips_the_committed_documents(golden) -> None:
    catalog = json.loads((GOLDEN_DIR / "catalog.json").read_text(encoding="utf-8"))
    collectors = json.loads((GOLDEN_DIR / "collectors.json").read_text(encoding="utf-8"))
    parsed = parse_golden_set(catalog, collectors)
    assert parsed.release_ids == golden.release_ids
    assert parsed.collector_ids == golden.collector_ids
    assert parsed.seed == generate_golden_set.SEED


def test_release_families_are_derived_not_stored() -> None:
    release = Release(
        id="r0",
        title="T",
        artist_ids=("a1",),
        label_id="l1",
        master_id=None,
        year=1999,
        genres=("Rock",),
        styles=(),
        medium="vinyl_12",
        media=generate_golden_set.map_discogs_formats([generate_golden_set.MEDIUM_FORMATS["vinyl_12"]]),
        have_count=1,
        want_count=0,
    )
    assert release.families == ("vinyl",)
