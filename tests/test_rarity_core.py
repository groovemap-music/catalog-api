"""Tests for the media-neutral rarity core and the per-family extension seam (ADR 0007)."""

import json
from importlib import import_module
from importlib.resources import files
from typing import ClassVar

import pytest

from api.rarity import (
    CORE_SIGNAL_WEIGHTS,
    DEFAULT_MEDIUM_RARITY,
    FAMILY_DEFAULT_MEDIUM_RARITY,
    GROOVED_FAMILIES,
    MEDIUM_RARITY_SCORES,
    ReleaseContext,
    ReleaseMedia,
    compose,
    compute_medium_rarity_score,
    effective_weights,
    medium_rarity_score,
    module_weights,
    modules_for,
    registry,
    resolve_media,
    score_release,
)
from api.rarity.families.grooved import PRESSING_FACT, GroovedSignals
from api.rarity.families.registry import FamilySignals, register


def _taxonomy() -> dict:
    document = (files("common.media_taxonomy") / "media-taxonomy.json").read_text(encoding="utf-8")
    return json.loads(document)


_CORE_ONLY = {
    "label_catalog": 50.0,
    "medium_rarity": 60.0,
    "temporal_scarcity": 70.0,
    "graph_isolation": 80.0,
    "collection_prevalence": 90.0,
}


def _context(families: tuple[str, ...], pressing_count: int = 1) -> ReleaseContext:
    return ReleaseContext(
        release_id="1",
        media=ReleaseMedia(families=families),
        facts={PRESSING_FACT: {"pressing_count": pressing_count}},
    )


# ── The signal that motivated the split ─────────────────────────────


class TestLoneCdVersusLoneLp:
    """The bug ADR 0007 names: pressing scarcity was applied to every medium.

    A CD has no pressings of a master in the sense the signal means, so a lone CD used to be
    handed exactly the same 100.0 the rarest lone LP got.
    """

    def test_lp_receives_the_pressing_signal(self) -> None:
        scored = score_release(_context(("vinyl",)), _CORE_ONLY)
        assert scored.family_signals == {"grooved": {"pressing_scarcity": 100.0}}
        assert scored.signals["pressing_scarcity"] == 100.0

    def test_cd_receives_no_pressing_signal_at_all(self) -> None:
        scored = score_release(_context(("optical",)), _CORE_ONLY)
        assert scored.family_signals == {}
        assert "pressing_scarcity" not in scored.signals

    def test_the_two_no_longer_score_the_same(self) -> None:
        lp = score_release(_context(("vinyl",)), _CORE_ONLY)
        cd = score_release(_context(("optical",)), _CORE_ONLY)
        assert lp.score != cd.score
        # Both still land on the 0-100 scale, which is what renormalisation buys.
        assert 0.0 <= cd.score <= 100.0
        assert 0.0 <= lp.score <= 100.0

    def test_the_cd_score_is_the_core_signals_renormalised(self) -> None:
        cd = score_release(_context(("optical",)), _CORE_ONLY)
        expected = sum(CORE_SIGNAL_WEIGHTS[name] * score for name, score in _CORE_ONLY.items()) / sum(CORE_SIGNAL_WEIGHTS.values())
        assert cd.score == pytest.approx(expected)

    def test_shellac_and_grooved_other_are_grooved_too(self) -> None:
        for family in ("shellac", "grooved_other"):
            assert score_release(_context((family,)), _CORE_ONLY).family_signals
        assert set(GROOVED_FAMILIES) == {"grooved_other", "shellac", "vinyl"}

    @pytest.mark.parametrize("family", ["tape", "optical", "digital", "video", "other"])
    def test_no_non_grooved_family_receives_a_pressing_signal(self, family: str) -> None:
        assert score_release(_context((family,)), _CORE_ONLY).family_signals == {}

    def test_a_release_on_both_cd_and_vinyl_still_counts_as_grooved(self) -> None:
        """Applicability is any-match: a CD+LP set has pressings to count."""
        scored = score_release(_context(("optical", "vinyl")), _CORE_ONLY)
        assert scored.family_signals == {"grooved": {"pressing_scarcity": 100.0}}


# ── Weight renormalisation ──────────────────────────────────────────


class TestRenormalisation:
    def test_core_weights_alone_do_not_sum_to_one(self) -> None:
        assert sum(CORE_SIGNAL_WEIGHTS.values()) == pytest.approx(0.75)

    def test_effective_weights_sum_to_one_without_a_family(self) -> None:
        applied = effective_weights(_CORE_ONLY.keys(), CORE_SIGNAL_WEIGHTS)
        assert sum(applied.values()) == pytest.approx(1.0)
        assert set(applied) == set(_CORE_ONLY)

    def test_effective_weights_sum_to_one_with_a_family(self) -> None:
        scored = score_release(_context(("vinyl",)), _CORE_ONLY)
        assert sum(scored.weights.values()) == pytest.approx(1.0)
        assert scored.weights["pressing_scarcity"] == pytest.approx(0.25)

    def test_grooved_weights_are_the_historical_ones(self) -> None:
        """A grooved release scores under exactly the pre-split weights."""
        scored = score_release(_context(("vinyl",)), _CORE_ONLY)
        assert scored.weights == pytest.approx(
            {
                "pressing_scarcity": 0.25,
                "label_catalog": 0.10,
                "medium_rarity": 0.10,
                "temporal_scarcity": 0.20,
                "graph_isolation": 0.15,
                "collection_prevalence": 0.20,
            }
        )

    def test_an_unweighted_signal_is_dropped_from_the_composite(self) -> None:
        """format_rarity is reported but deprecated, so it must not move the score."""
        with_deprecated = dict(_CORE_ONLY, format_rarity=100.0)
        assert compose(with_deprecated, CORE_SIGNAL_WEIGHTS) == compose(_CORE_ONLY, CORE_SIGNAL_WEIGHTS)

    def test_no_weighted_signal_scores_zero(self) -> None:
        assert compose({"format_rarity": 100.0}, CORE_SIGNAL_WEIGHTS) == 0.0
        assert effective_weights(["format_rarity"], CORE_SIGNAL_WEIGHTS) == {}

    def test_a_single_signal_carries_the_whole_score(self) -> None:
        assert compose({"medium_rarity": 42.0}, CORE_SIGNAL_WEIGHTS) == pytest.approx(42.0)

    def test_every_signal_at_100_scores_100(self) -> None:
        """Renormalisation is what keeps the tier thresholds meaningful across media."""
        maxed = dict.fromkeys(_CORE_ONLY, 100.0)
        assert compose(maxed, CORE_SIGNAL_WEIGHTS) == pytest.approx(100.0)
        assert score_release(_context(("optical",)), maxed).tier == "ultra-rare"
        assert score_release(_context(("vinyl",)), maxed).tier == "ultra-rare"


class TestOrderIndependence:
    def test_shuffled_signals_give_a_bit_identical_score(self) -> None:
        forward = compose(_CORE_ONLY, CORE_SIGNAL_WEIGHTS)
        reverse = compose(dict(reversed(list(_CORE_ONLY.items()))), CORE_SIGNAL_WEIGHTS)
        assert forward == reverse

    def test_shuffled_weights_give_a_bit_identical_score(self) -> None:
        reversed_weights = dict(reversed(list(CORE_SIGNAL_WEIGHTS.items())))
        assert compose(_CORE_ONLY, CORE_SIGNAL_WEIGHTS) == compose(_CORE_ONLY, reversed_weights)

    def test_family_order_does_not_change_the_score(self) -> None:
        forward = score_release(_context(("optical", "vinyl")), _CORE_ONLY)
        reverse = score_release(_context(("vinyl", "optical")), _CORE_ONLY)
        assert forward.score == reverse.score
        assert forward.weights == reverse.weights

    def test_medium_order_does_not_change_medium_rarity(self) -> None:
        forward = resolve_media(mediums=[{"id": "optical_cd", "family": "optical"}, {"id": "vinyl_12", "family": "vinyl"}])
        reverse = resolve_media(mediums=[{"id": "vinyl_12", "family": "vinyl"}, {"id": "optical_cd", "family": "optical"}])
        assert forward == reverse
        assert compute_medium_rarity_score(forward) == compute_medium_rarity_score(reverse)


# ── Medium rarity ───────────────────────────────────────────────────


class TestMediumRarity:
    def test_judgments_migrated_from_the_format_table(self) -> None:
        assert medium_rarity_score("grooved_lathe_cut") == 98.0
        assert medium_rarity_score("grooved_flexi_disc") == 95.0
        assert medium_rarity_score("shellac_10") == 90.0
        assert medium_rarity_score("vinyl_10") == 65.0
        assert medium_rarity_score("tape_8_track") == 60.0
        assert medium_rarity_score("optical_cdr") == 50.0
        assert medium_rarity_score("vinyl_7") == 45.0
        assert medium_rarity_score("vinyl_12") == 40.0
        assert medium_rarity_score("tape_cassette") == 35.0
        assert medium_rarity_score("optical_cd") == 10.0
        assert medium_rarity_score("digital_file") == 5.0

    def test_unknown_medium_falls_back_to_its_family_default(self) -> None:
        assert medium_rarity_score("vinyl_from_a_later_taxonomy", family="vinyl") == 40.0
        assert medium_rarity_score("optical_from_a_later_taxonomy", family="optical") == 20.0
        assert medium_rarity_score("shellac_from_a_later_taxonomy", family="shellac") == 90.0

    def test_unknown_medium_and_unknown_family_falls_back_to_neutral(self) -> None:
        assert medium_rarity_score("who_knows") == DEFAULT_MEDIUM_RARITY
        assert medium_rarity_score("who_knows", family="not_a_family") == DEFAULT_MEDIUM_RARITY

    def test_the_table_covers_every_medium_in_the_pinned_vocabulary(self) -> None:
        vocabulary = {medium["id"] for medium in _taxonomy()["media"]}
        assert vocabulary - set(MEDIUM_RARITY_SCORES) == set()

    def test_the_table_invents_no_medium_the_vocabulary_lacks(self) -> None:
        vocabulary = {medium["id"] for medium in _taxonomy()["media"]}
        assert set(MEDIUM_RARITY_SCORES) - vocabulary == set()

    def test_every_family_has_a_default(self) -> None:
        vocabulary = {family["id"] for family in _taxonomy()["families"]}
        assert set(FAMILY_DEFAULT_MEDIUM_RARITY) == vocabulary

    def test_every_score_is_on_the_0_100_scale(self) -> None:
        assert all(0.0 <= score <= 100.0 for score in MEDIUM_RARITY_SCORES.values())
        assert all(0.0 <= score <= 100.0 for score in FAMILY_DEFAULT_MEDIUM_RARITY.values())

    def test_takes_the_rarest_medium_of_a_multi_medium_release(self) -> None:
        media = resolve_media(mediums=[{"id": "optical_cd", "family": "optical"}, {"id": "grooved_lathe_cut", "family": "grooved_other"}])
        assert compute_medium_rarity_score(media) == 98.0

    def test_families_only_release_uses_the_family_default(self) -> None:
        assert compute_medium_rarity_score(ReleaseMedia(families=("shellac",))) == 90.0
        assert compute_medium_rarity_score(ReleaseMedia(families=("optical", "vinyl"))) == 40.0

    def test_a_release_with_no_media_evidence_scores_neutral(self) -> None:
        assert compute_medium_rarity_score(ReleaseMedia()) == DEFAULT_MEDIUM_RARITY
        assert compute_medium_rarity_score([]) == DEFAULT_MEDIUM_RARITY

    def test_accepts_bare_medium_ids(self) -> None:
        assert compute_medium_rarity_score(["optical_cd", "vinyl_12"]) == 40.0


# ── Media resolution ────────────────────────────────────────────────


class TestResolveMedia:
    def test_issued_on_edges_win(self) -> None:
        media = resolve_media(
            mediums=[{"id": "vinyl_12", "family": "vinyl"}],
            media_families=["optical"],
            formats=["CD"],
        )
        assert media.mediums == (("vinyl_12", "vinyl"),)
        assert media.families == ("vinyl",)

    def test_media_families_property_is_the_second_source(self) -> None:
        media = resolve_media(mediums=[], media_families=["tape"], formats=["CD"])
        assert media.mediums == ()
        assert media.families == ("tape",)

    def test_legacy_formats_are_the_last_resort(self) -> None:
        media = resolve_media(formats=["Vinyl", "LP", "Album"])
        assert media.families == ("vinyl",)
        assert media.medium_ids == ("vinyl_12",)

    def test_no_evidence_resolves_to_nothing(self) -> None:
        assert resolve_media() == ReleaseMedia()
        assert resolve_media(mediums=[], media_families=[], formats=[]) == ReleaseMedia()

    def test_unusable_format_names_resolve_to_nothing(self) -> None:
        """A descriptor with no format name behind it carries no medium."""
        assert resolve_media(formats=["Album", "Reissue"]) == ReleaseMedia()

    def test_medium_rows_are_sorted_and_deduplicated(self) -> None:
        media = resolve_media(
            mediums=[
                {"id": "vinyl_12", "family": "vinyl"},
                {"id": "optical_cd", "family": "optical"},
                {"id": "vinyl_12", "family": "vinyl"},
            ]
        )
        assert media.mediums == (("optical_cd", "optical"), ("vinyl_12", "vinyl"))
        assert media.families == ("optical", "vinyl")

    def test_a_bare_id_does_not_erase_a_known_family(self) -> None:
        media = resolve_media(mediums=[{"id": "vinyl_12", "family": "vinyl"}, "vinyl_12"])
        assert media.mediums == (("vinyl_12", "vinyl"),)

    def test_pairs_and_bare_strings_are_accepted(self) -> None:
        assert resolve_media(mediums=[("vinyl_12", "vinyl")]).families == ("vinyl",)
        assert resolve_media(mediums=["vinyl_12"]).mediums == (("vinyl_12", None),)

    def test_junk_rows_are_skipped_rather_than_raising(self) -> None:
        media = resolve_media(mediums=[None, 7, {}, {"id": ""}, {"id": "vinyl_12", "family": "vinyl"}])
        assert media.mediums == (("vinyl_12", "vinyl"),)

    def test_junk_family_names_are_skipped(self) -> None:
        assert resolve_media(media_families=[None, 7, "", "tape"]).families == ("tape",)


# ── The extension seam itself ───────────────────────────────────────


class TestFamilyRegistry:
    def test_the_grooved_module_is_registered_against_its_three_families(self) -> None:
        installed = registry()
        assert set(GROOVED_FAMILIES) <= set(installed)
        assert {installed[family].module_id for family in GROOVED_FAMILIES} == {"grooved"}

    def test_no_non_grooved_family_is_registered(self) -> None:
        assert set(registry()) == set(GROOVED_FAMILIES)

    def test_modules_for_returns_nothing_for_a_non_grooved_release(self) -> None:
        assert modules_for(("optical",)) == []
        assert modules_for(()) == []

    def test_modules_for_returns_each_module_once(self) -> None:
        found = modules_for(("vinyl", "shellac", "grooved_other"))
        assert len(found) == 1
        assert found[0].module_id == "grooved"

    def test_module_weights_are_exposed_for_rebuilding_a_stored_breakdown(self) -> None:
        assert module_weights() == {"grooved": {"pressing_scarcity": 0.25}}

    def test_the_grooved_module_satisfies_the_protocol(self) -> None:
        assert isinstance(GroovedSignals(), FamilySignals)

    def test_the_grooved_module_owns_its_own_graph_query(self) -> None:
        """ADR 0007: sibling counting is grooved-specific and moves with the module."""
        cypher = GroovedSignals.queries[PRESSING_FACT]
        assert "DERIVED_FROM" in cypher
        assert "UNWIND $ids AS rid" in cypher
        assert "RETURN r.id AS release_id, pressing_count" in cypher

    def test_a_missing_pressing_row_scores_as_a_standalone_release(self) -> None:
        bare = ReleaseContext(release_id="1", media=ReleaseMedia(families=("vinyl",)))
        assert GroovedSignals().signals(bare) == {"pressing_scarcity": 90.0}

    def test_registering_a_second_module_for_a_claimed_family_is_refused(self) -> None:
        class Rival:
            module_id = "rival"
            weights: ClassVar[dict[str, float]] = {}
            queries: ClassVar[dict[str, str]] = {}

            def applies_to(self, families):  # noqa: ARG002
                return True

            def signals(self, release_ctx):  # noqa: ARG002
                return {}

        with pytest.raises(ValueError, match="already served by module 'grooved'"):
            register(["vinyl"], Rival())

    def test_registering_a_duplicate_fact_name_is_refused(self) -> None:
        class Copycat:
            module_id = "copycat"
            weights: ClassVar[dict[str, float]] = {}
            queries: ClassVar[dict[str, str]] = {PRESSING_FACT: "RETURN 1"}

            def applies_to(self, families):  # noqa: ARG002
                return True

            def signals(self, release_ctx):  # noqa: ARG002
                return {}

        with pytest.raises(ValueError, match="already declared by module 'grooved'"):
            register(["tape"], Copycat())

    def test_registering_the_same_module_again_is_a_no_op(self) -> None:
        register(GROOVED_FAMILIES, registry()["vinyl"])
        assert set(registry()) == set(GROOVED_FAMILIES)


class TestComposition:
    def test_a_module_that_withdraws_contributes_no_weight(self) -> None:
        """An empty signals() mapping must not leave its weight in the denominator."""

        class Silent:
            module_id = "silent"
            weights: ClassVar[dict[str, float]] = {"never": 1.0}
            queries: ClassVar[dict[str, str]] = {}

            def applies_to(self, families):  # noqa: ARG002
                return True

            def signals(self, release_ctx):  # noqa: ARG002
                return {}

        register(["tape"], Silent())
        try:
            scored = score_release(_context(("tape",)), _CORE_ONLY)
            assert scored.family_signals == {}
            assert sum(scored.weights.values()) == pytest.approx(1.0)
            assert scored.score == pytest.approx(compose(_CORE_ONLY, CORE_SIGNAL_WEIGHTS))
        finally:
            # `api.rarity.families.registry` the function shadows the submodule on the package.
            import_module("api.rarity.families.registry")._REGISTRY.pop("tape", None)

    def test_the_tier_thresholds_are_unchanged_by_the_split(self) -> None:
        for score, tier in ((100.0, "ultra-rare"), (80.0, "ultra-rare"), (60.0, "rare"), (40.0, "scarce"), (20.0, "uncommon"), (0.0, "common")):
            flat = dict.fromkeys(_CORE_ONLY, score)
            assert score_release(_context(("optical",)), flat).tier == tier
