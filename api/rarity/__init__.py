"""Rarity scoring: a media-neutral core plus per-family extension modules.

ADR 0007 ("Canonical media taxonomy and media-neutral product core") splits this package in
two:

* :mod:`api.rarity.core` — signals every medium has (label catalog size, temporal scarcity,
  graph isolation, collection prevalence, medium rarity), the medium-rarity table keyed by
  canonical medium id, and the weight renormalisation that keeps every score on one 0-100
  scale.
* :mod:`api.rarity.families` — extension modules keyed by taxonomy family. Today that is the
  grooved module, which contributes pressing scarcity for ``vinyl``, ``shellac``, and
  ``grooved_other`` only.

:mod:`api.rarity.composite` joins them. The graph and PostgreSQL access that feeds the core
lives in :mod:`api.queries.rarity_queries`, which remains the entry point for the precomputed
rarity pass.

The family registry is the seam a future vinyl-specific service would own; see
:mod:`api.rarity.families.registry` for how to add a module.
"""

from __future__ import annotations

from api.rarity.composite import RarityScore, score_release
from api.rarity.core import (
    CORE_SIGNAL_WEIGHTS,
    DEFAULT_MEDIUM_RARITY,
    FAMILY_DEFAULT_MEDIUM_RARITY,
    FORMAT_RARITY_SCORES,
    MEDIUM_RARITY_SCORES,
    RARITY_TIERS,
    ReleaseContext,
    ReleaseMedia,
    compose,
    compute_collection_prevalence_score,
    compute_format_rarity_score,
    compute_graph_isolation_score,
    compute_label_catalog_score,
    compute_medium_rarity_score,
    compute_rarity_tier,
    compute_temporal_scarcity_score,
    effective_weights,
    medium_rarity_score,
    resolve_media,
)
from api.rarity.families import FamilySignals, family_queries, module_weights, modules_for, registry
from api.rarity.families.grooved import GROOVED_FAMILIES, compute_pressing_scarcity_score


__all__ = [
    "CORE_SIGNAL_WEIGHTS",
    "DEFAULT_MEDIUM_RARITY",
    "FAMILY_DEFAULT_MEDIUM_RARITY",
    "FORMAT_RARITY_SCORES",
    "GROOVED_FAMILIES",
    "MEDIUM_RARITY_SCORES",
    "RARITY_TIERS",
    "FamilySignals",
    "RarityScore",
    "ReleaseContext",
    "ReleaseMedia",
    "compose",
    "compute_collection_prevalence_score",
    "compute_format_rarity_score",
    "compute_graph_isolation_score",
    "compute_label_catalog_score",
    "compute_medium_rarity_score",
    "compute_pressing_scarcity_score",
    "compute_rarity_tier",
    "compute_temporal_scarcity_score",
    "effective_weights",
    "family_queries",
    "medium_rarity_score",
    "module_weights",
    "modules_for",
    "registry",
    "resolve_media",
    "score_release",
]
