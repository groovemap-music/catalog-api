"""Composition of the media-neutral core with the applicable family extensions.

This is the only place the core and the family registry meet. :mod:`api.rarity.core` never
imports a family module, and a family module never imports another.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from api.rarity.core import (
    CORE_SIGNAL_WEIGHTS,
    ReleaseContext,
    compose,
    compute_rarity_tier,
    effective_weights,
)
from api.rarity.families import modules_for


@dataclass(frozen=True)
class RarityScore:
    """One release's composite rarity score and the evidence behind it.

    Attributes:
        score: The composite 0-100 score, unrounded.
        tier: The tier label for ``score``.
        signals: Every scored signal, core and family, flattened by signal name.
        family_signals: Each contributing module's signals, keyed by module id. Empty when no
            family extension applied — which is the normal case for non-grooved media.
        weights: The renormalised weights actually applied, summing to 1.0.
    """

    score: float
    tier: str
    signals: dict[str, float]
    family_signals: dict[str, dict[str, float]]
    weights: dict[str, float]


def score_release(release_ctx: ReleaseContext, core_signals: Mapping[str, float]) -> RarityScore:
    """Score one release from its core signals plus whatever family modules apply.

    A module that does not apply contributes neither signals nor weights, so the core weights
    renormalise to fill the scale. That is the whole mechanism behind "a lone CD and a lone LP
    no longer receive the same pressing signal": the CD has no pressing signal at all, rather
    than a fabricated one.

    Args:
        release_ctx: The release's media, year, and family-owned graph facts.
        core_signals: The media-neutral signal scores, keyed by the names in
            :data:`api.rarity.core.CORE_SIGNAL_WEIGHTS`. A name absent from the weights (the
            deprecated ``format_rarity``) is carried through unscored.

    Returns:
        The composite score and its breakdown.
    """
    signals = dict(core_signals)
    weights = dict(CORE_SIGNAL_WEIGHTS)
    family_signals: dict[str, dict[str, float]] = {}

    for module in modules_for(release_ctx.families):
        contributed = dict(module.signals(release_ctx))
        if not contributed:
            continue
        family_signals[module.module_id] = contributed
        signals.update(contributed)
        weights.update(module.weights)

    score = compose(signals, weights)
    return RarityScore(
        score=score,
        tier=compute_rarity_tier(score),
        signals=signals,
        family_signals=family_signals,
        weights=effective_weights(signals.keys(), weights),
    )
