"""Installed rarity family extensions.

This module is the single list of which extension serves which taxonomy family. See
:mod:`api.rarity.families.registry` for the protocol and for how to add one.
"""

from __future__ import annotations

from api.rarity.families.grooved import GROOVED_FAMILIES, GroovedSignals
from api.rarity.families.registry import (
    FamilySignals,
    distinct_modules,
    family_queries,
    module_weights,
    modules_for,
    register,
    registry,
)


__all__ = [
    "FamilySignals",
    "distinct_modules",
    "family_queries",
    "module_weights",
    "modules_for",
    "register",
    "registry",
]

# ── Installed modules ───────────────────────────────────────────────

register(GROOVED_FAMILIES, GroovedSignals())
