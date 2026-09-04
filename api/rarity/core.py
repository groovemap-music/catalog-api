"""Media-neutral rarity core.

ADR 0007 ("Canonical media taxonomy and media-neutral product core") splits rarity scoring
into a core that reasons about **every** medium the same way and per-family extension modules
that contribute the signals only their family can justify. This module is the core. It holds:

* the core signal weights (:data:`CORE_SIGNAL_WEIGHTS`),
* the pure per-signal scoring functions,
* the ``medium_rarity`` table keyed by canonical medium id, with a documented default per
  family (:data:`MEDIUM_RARITY_SCORES`, :data:`FAMILY_DEFAULT_MEDIUM_RARITY`),
* media resolution off a release's graph facts (:func:`resolve_media`),
* the weight renormalisation that keeps every composite score on one 0-100 scale
  (:func:`effective_weights`, :func:`compose`).

Nothing here may reason about a specific family. Pressing scarcity, sibling counting, matrix
and runout, plant and stamper lineage, and every other property of a *physical pressing*
rather than a *release* belong in :mod:`api.rarity.families`. That boundary is the seam a
future vinyl-specific service would own, so keeping it clean is the point of this module.

The deprecated descriptor-keyed ``format_rarity`` lookup is retained here, unscored, for one
minor version so existing consumers keep seeing the key they read today.
"""

from __future__ import annotations

import math
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from common.media import families_of, legacy_format_names_to_media


# ── Core signal weights ─────────────────────────────────────────────
#
# These are the signals every medium has. They sum to 0.75, NOT to 1.0: the remaining 0.25 is
# what a family extension contributes when it applies (today, the grooved module's
# pressing_scarcity). `compose` renormalises over whichever signals are actually present, so a
# release with no applicable family module still scores on a full 0-100 scale — its core
# signals simply carry proportionally more of the score.
#
# Historically these were six weights summing to 1.0 with pressing_scarcity at 0.25 and a
# descriptor-keyed format_rarity at 0.10. medium_rarity inherits format_rarity's 0.10 and every
# other core weight is unchanged, so a grooved release scores exactly as it did before.

CORE_SIGNAL_WEIGHTS: Final[dict[str, float]] = {
    "label_catalog": 0.10,
    "medium_rarity": 0.10,
    "temporal_scarcity": 0.20,
    "graph_isolation": 0.15,
    "collection_prevalence": 0.20,
}


# ── Rarity tiers ────────────────────────────────────────────────────
#
# Unchanged by the core/extension split: renormalisation is exactly what keeps these
# thresholds meaningful across media.

RARITY_TIERS: Final[list[tuple[float, str]]] = [
    (80.0, "ultra-rare"),
    (60.0, "rare"),
    (40.0, "scarce"),
    (20.0, "uncommon"),
    (0.0, "common"),
]


# ── Medium rarity ───────────────────────────────────────────────────
#
# Keyed by canonical medium id from the vendored taxonomy (common.media_taxonomy). This
# replaces the old FORMAT_RARITY_SCORES table, which keyed on raw Discogs format *names* and
# so mixed media ("Vinyl", "CD") with descriptors ("LP", "Box Set", "Test Pressing") on one
# scale. Descriptors now live in the canonical block's own fields (`container`, `variants`,
# `edition`) and are no longer scored as if they were media.
#
# The judgments migrated verbatim from the old table: lathe cut 98, flexi-disc 95, shellac 90,
# 10" 65, 8-track 60, CD-R 50, vinyl 40, cassette 35, CD 10, digital file 5. The rest are
# scored on the same scale, relative to how hard the medium is to find in trade today.
#
# COMPLETENESS: this table covers every medium id in the pinned vocabulary, and
# tests/test_rarity_core.py asserts that. A medium id it does NOT cover (a vocabulary that
# adds one after this pin) falls back to its family's default rather than to a hardcoded
# midpoint, so a taxonomy bump degrades sensibly instead of flattening new media to 50.

MEDIUM_RARITY_SCORES: Final[dict[str, float]] = {
    # vinyl — the mainstream collector medium; size drives scarcity.
    "vinyl_7": 45.0,
    "vinyl_10": 65.0,
    "vinyl_12": 40.0,
    "vinyl_unspecified": 40.0,
    # shellac — pre-vinyl, uniformly scarce and fragile regardless of size.
    "shellac_7": 90.0,
    "shellac_10": 90.0,
    "shellac_12": 90.0,
    "shellac_unspecified": 90.0,
    # grooved_other — short-run, one-off, and obsolete grooved carriers.
    "grooved_acetate": 96.0,
    "grooved_lathe_cut": 98.0,
    "grooved_flexi_disc": 95.0,
    "grooved_cylinder": 97.0,
    "grooved_edison_disc": 96.0,
    "grooved_pathe_disc": 96.0,
    "grooved_piano_roll": 92.0,
    "grooved_other_unspecified": 90.0,
    # tape — cassette stayed mass-market; the cartridge and pro formats did not.
    "tape_cassette": 35.0,
    "tape_microcassette": 75.0,
    "tape_reel_to_reel": 70.0,
    "tape_8_track": 60.0,
    "tape_4_track": 80.0,
    "tape_playtape": 85.0,
    "tape_dat": 70.0,
    "tape_dcc": 80.0,
    "tape_elcaset": 90.0,
    "tape_unspecified": 50.0,
    # optical — the CD is the most abundant medium in the catalog.
    "optical_cd": 10.0,
    "optical_cdr": 50.0,
    "optical_sacd": 45.0,
    "optical_dvd_audio": 50.0,
    "optical_minidisc": 65.0,
    "optical_dualdisc": 60.0,
    "optical_umd": 75.0,
    "optical_unspecified": 20.0,
    # digital — a file is infinitely reproducible; its physical carriers are not.
    "digital_file": 5.0,
    "digital_usb": 45.0,
    "digital_memory_card": 55.0,
    "digital_download_card": 20.0,
    "digital_floppy_disk": 75.0,
    "digital_unspecified": 5.0,
    # video — the dead consumer formats are the scarce ones.
    "video_dvd": 20.0,
    "video_dvdr": 45.0,
    "video_blu_ray": 25.0,
    "video_hd_dvd": 70.0,
    "video_laserdisc": 55.0,
    "video_cdv": 70.0,
    "video_vcd": 55.0,
    "video_svcd": 65.0,
    "video_vhs": 40.0,
    "video_betamax": 75.0,
    "video_vhd": 85.0,
    "video_ced": 80.0,
    "video_film_reel": 85.0,
    "video_unspecified": 45.0,
    # other — the vocabulary's explicit "we could not classify this" medium.
    "other_unspecified": 50.0,
}

# Fallback for a medium id the table does not cover, chosen per family. Each is the score of
# that family's "unspecified" medium, so an unrecognised vinyl medium scores like plain vinyl
# rather than like a flexi-disc.
FAMILY_DEFAULT_MEDIUM_RARITY: Final[dict[str, float]] = {
    "vinyl": 40.0,
    "shellac": 90.0,
    "grooved_other": 90.0,
    "tape": 50.0,
    "optical": 20.0,
    "digital": 5.0,
    "video": 45.0,
    "other": 50.0,
}

# Last resort: neither the medium nor its family is known. Deliberately the same neutral
# midpoint the old _DEFAULT_FORMAT_SCORE used, so a release with no usable media evidence
# scores exactly as it did before this split.
DEFAULT_MEDIUM_RARITY: Final[float] = 50.0


# ── Deprecated: descriptor-keyed format rarity ──────────────────────
#
# Retained for one minor version so `format_rarity` keeps appearing in the precomputed row and
# the breakdown response. It is COMPUTED but NOT SCORED — it carries no weight in
# CORE_SIGNAL_WEIGHTS, having been replaced by medium_rarity. Remove with the rest of the
# `formats` deprecation window.

FORMAT_RARITY_SCORES: Final[dict[str, float]] = {
    "Test Pressing": 100.0,
    "Lathe Cut": 98.0,
    "Flexi-disc": 95.0,
    "Shellac": 90.0,
    "Blu-spec CD": 80.0,
    "Box Set": 70.0,
    '10"': 65.0,
    "8-Track Cartridge": 60.0,
    "CDr": 50.0,
    "Vinyl": 40.0,
    "Cassette": 35.0,
    "LP": 30.0,
    "CD": 10.0,
    "File": 5.0,
}

_DEFAULT_FORMAT_SCORE: Final[float] = 50.0


# ── Release media ───────────────────────────────────────────────────


@dataclass(frozen=True)
class ReleaseMedia:
    """The media a release was issued on, as the core understands them.

    Attributes:
        mediums: ``(medium id, family id or None)`` pairs, sorted and de-duplicated. Empty when
            the release has no medium-level evidence, only families.
        families: The sorted, unique taxonomy family ids the release covers.
    """

    mediums: tuple[tuple[str, str | None], ...] = ()
    families: tuple[str, ...] = ()

    @property
    def medium_ids(self) -> tuple[str, ...]:
        """Just the medium ids, in the same order as :attr:`mediums`."""
        return tuple(medium for medium, _family in self.mediums)


@dataclass(frozen=True)
class ReleaseContext:
    """Everything a signal module may read about one release.

    Attributes:
        release_id: The release's graph id.
        media: The resolved media, from :func:`resolve_media`.
        year: The release year, when known.
        facts: Rows returned by family-owned graph queries, keyed by the fact name the owning
            module declared in its ``queries`` mapping. A module reads only its own facts; a
            fact whose query returned nothing for this release is an empty mapping.
    """

    release_id: str
    media: ReleaseMedia = field(default_factory=ReleaseMedia)
    year: int | None = None
    facts: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)

    @property
    def families(self) -> tuple[str, ...]:
        """The taxonomy family ids this release covers."""
        return self.media.families


def resolve_media(
    mediums: Iterable[Any] | None = None,
    media_families: Iterable[Any] | None = None,
    formats: Iterable[Any] | None = None,
) -> ReleaseMedia:
    """Resolve a release's media from the best evidence the graph carries.

    Sources are tried in descending order of fidelity, and the first that yields anything wins:

    1. ``mediums`` — the ``(:Release)-[:ISSUED_ON]->(:Medium {id, family})`` edges ADR 0007
       adds. Authoritative: canonical ids, with the family on the node.
    2. ``media_families`` — the ``Release.media_families`` list property. Families only, no
       medium-level detail, but enough to pick family modules and a family default.
    3. ``formats`` — the deprecated raw Discogs name list, recovered through
       :func:`common.media.legacy_format_names_to_media`. Best effort: a flattened list has
       lost which description belonged to which medium.

    Args:
        mediums: Medium rows — mappings with ``id`` and ``family``, ``(id, family)`` pairs, or
            bare medium id strings. Anything else in the iterable is skipped.
        media_families: Family ids from the release node's list property.
        formats: Raw Discogs format names.

    Returns:
        The resolved media. Empty when no source yields anything.
    """
    pairs: dict[str, str | None] = {}
    for entry in mediums or ():
        medium, family = _medium_entry(entry)
        if medium is None:
            continue
        # First non-None family for a medium id wins; a later bare id must not erase it.
        if pairs.get(medium) is None:
            pairs[medium] = family
    if pairs:
        families = {family for family in pairs.values() if family}
        return ReleaseMedia(
            mediums=tuple(sorted((medium, pairs[medium]) for medium in pairs)),
            families=tuple(sorted(families)),
        )

    declared = sorted({family for family in media_families or () if isinstance(family, str) and family})
    if declared:
        return ReleaseMedia(families=tuple(declared))

    names = [name for name in formats or () if name is not None]
    if names:
        block = legacy_format_names_to_media(names)
        items = block.get("items") or []
        recovered = {item["medium"]: item.get("family") for item in items if isinstance(item, Mapping) and isinstance(item.get("medium"), str)}
        if recovered:
            return ReleaseMedia(
                mediums=tuple(sorted((medium, recovered[medium]) for medium in recovered)),
                families=tuple(families_of(block)),
            )

    return ReleaseMedia()


def _medium_entry(entry: Any) -> tuple[str | None, str | None]:
    """Normalise one medium row into a ``(medium id, family id)`` pair."""
    if isinstance(entry, str):
        return (entry or None, None)
    if isinstance(entry, Mapping):
        medium = entry.get("id")
        family = entry.get("family")
    elif isinstance(entry, tuple | list) and len(entry) == 2:
        medium, family = entry
    else:
        return (None, None)
    medium = medium if isinstance(medium, str) and medium else None
    family = family if isinstance(family, str) and family else None
    return (medium, family)


# ── Pure scoring functions ──────────────────────────────────────────


def medium_rarity_score(medium_id: str, family: str | None = None) -> float:
    """Score one canonical medium, falling back through its family.

    Lookup order: the explicit :data:`MEDIUM_RARITY_SCORES` entry, then
    :data:`FAMILY_DEFAULT_MEDIUM_RARITY` for ``family``, then
    :data:`DEFAULT_MEDIUM_RARITY`.

    Args:
        medium_id: A canonical medium id.
        family: The medium's family id, when known. Supplying it is what lets a medium the
            table does not cover still score like its family rather than like nothing.

    Returns:
        A 0-100 rarity score for the medium.
    """
    score = MEDIUM_RARITY_SCORES.get(medium_id)
    if score is not None:
        return score
    if family is not None:
        return FAMILY_DEFAULT_MEDIUM_RARITY.get(family, DEFAULT_MEDIUM_RARITY)
    return DEFAULT_MEDIUM_RARITY


def compute_medium_rarity_score(media: ReleaseMedia | Iterable[Any]) -> float:
    """Score a release's media, taking the rarest medium it was issued on.

    Max rather than mean: a release pressed on both CD and lathe-cut vinyl is as hard to
    complete as its rarest carrier. This mirrors what the deprecated format signal did.

    Args:
        media: A :class:`ReleaseMedia`, or an iterable of medium rows in any shape
            :func:`resolve_media` accepts.

    Returns:
        A 0-100 score. Falls back to the family default when only families are known, and to
        :data:`DEFAULT_MEDIUM_RARITY` when the release carries no media evidence at all.
    """
    resolved = media if isinstance(media, ReleaseMedia) else resolve_media(mediums=media)
    if resolved.mediums:
        return max(medium_rarity_score(medium, family) for medium, family in resolved.mediums)
    if resolved.families:
        return max(FAMILY_DEFAULT_MEDIUM_RARITY.get(family, DEFAULT_MEDIUM_RARITY) for family in resolved.families)
    return DEFAULT_MEDIUM_RARITY


def compute_label_catalog_score(catalog_size: int) -> float:
    """Score based on label catalog size (smaller = rarer)."""
    if catalog_size < 10:
        return 100.0
    if catalog_size <= 50:
        return 75.0
    if catalog_size <= 200:
        return 50.0
    if catalog_size <= 1000:
        return 25.0
    return 10.0


def compute_format_rarity_score(formats: list[Any]) -> float:
    """Score based on rarest raw Discogs format name. Takes max across all formats.

    Deprecated: superseded by :func:`compute_medium_rarity_score`, which keys on canonical
    medium ids instead of provider descriptor strings. Retained unscored for one minor version
    so the ``format_rarity`` key survives its deprecation window.
    """
    if not formats:
        return _DEFAULT_FORMAT_SCORE
    scores = [FORMAT_RARITY_SCORES.get(str(f), _DEFAULT_FORMAT_SCORE) for f in formats if f is not None]
    return max(scores) if scores else _DEFAULT_FORMAT_SCORE


def compute_temporal_scarcity_score(
    release_year: int | None,
    latest_sibling_year: int | None,
    current_year: int,
) -> float:
    """Score based on age and reissue status."""
    if release_year is None:
        return 50.0
    age = current_year - release_year
    base = max(0.0, min(100.0, age * 1.5))
    if latest_sibling_year is not None and latest_sibling_year >= current_year - 10:
        base = max(0.0, base - 40.0)
    return base


def compute_graph_isolation_score(degree: int) -> float:
    """Score based on graph node degree (fewer connections = rarer)."""
    if degree <= 2:
        return 90.0
    if degree <= 4:
        return 70.0
    if degree <= 7:
        return 50.0
    if degree <= 12:
        return 30.0
    return 10.0


def compute_collection_prevalence_score(have_count: int, want_count: int) -> float:
    """Score based on community ownership rarity (inverse of prevalence).

    Uses log-scale thresholds since community counts follow power-law distribution.
    Want > have adds a +5 bonus (capped at 100) indicating scarcity pressure.
    """
    if have_count <= 0:
        base = 95.0
    elif have_count <= 10:
        base = 85.0
    elif have_count <= 100:
        base = 70.0
    elif have_count <= 1000:
        base = 50.0
    elif have_count <= 10000:
        base = 25.0
    else:
        base = 10.0

    if want_count > have_count:
        base = min(100.0, base + 5.0)

    return base


def compute_rarity_tier(score: float) -> str:
    """Map composite score to rarity tier label."""
    for threshold, tier in RARITY_TIERS:
        if score >= threshold:
            return tier
    return "common"


# ── Weight renormalisation and composition ──────────────────────────


def effective_weights(signals: Collection[str], weights: Mapping[str, float]) -> dict[str, float]:
    """Renormalise ``weights`` over the signals actually present so they sum to 1.0.

    This is what keeps every release on one 0-100 scale no matter how many family modules
    contributed. A CD scores on the five core signals; an LP scores on those five plus the
    grooved module's pressing scarcity. Without renormalisation the CD's score would be capped
    at the core weights' 0.75 of the scale and every tier threshold would mean something
    different per medium.

    Args:
        signals: The signal names present for this release.
        weights: The declared weight of each signal. Names absent here are dropped, which is
            how the unscored deprecated ``format_rarity`` stays out of the composite.

    Returns:
        The weights to actually apply, summing to 1.0. Empty when no weighted signal is
        present.
    """
    present = sorted(name for name in signals if name in weights)
    total = math.fsum(weights[name] for name in present)
    if total <= 0.0:
        return {}
    return {name: weights[name] / total for name in present}


def compose(signals: Mapping[str, float], weights: Mapping[str, float]) -> float:
    """Combine signals into one 0-100 rarity score under renormalised weights.

    Order-independent by construction: the terms are summed in sorted signal order with
    :func:`math.fsum`, so the same signals in a different dict order give a bit-identical
    score rather than one that differs in the last few bits of the mantissa.

    Args:
        signals: Signal name to 0-100 score.
        weights: Declared weights; see :func:`effective_weights`.

    Returns:
        The composite 0-100 score, or ``0.0`` when no weighted signal is present.
    """
    applied = effective_weights(signals.keys(), weights)
    if not applied:
        return 0.0
    return math.fsum(applied[name] * signals[name] for name in sorted(applied))
