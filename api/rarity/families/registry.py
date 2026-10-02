"""The family-extension seam: protocol and registry.

ADR 0007 makes rarity a media-neutral core plus extension modules keyed by family. A module
owns the signals that only make sense for its media, the weights those signals carry, and the
graph questions they need answered. Registering it is the whole integration: the orchestrator
discovers its queries, runs them alongside the core's, and folds its signals into the
composite with the core's weights renormalised around them.

This is the boundary a future vinyl-specific service would own. Everything a module declares
here is the API that service would be handed.

## Adding a family module

1. Write ``api/rarity/families/<family>.py`` with a class satisfying :class:`FamilySignals`.
2. Declare its ``weights``. They are absolute, on the same scale as
   :data:`api.rarity.core.CORE_SIGNAL_WEIGHTS`, and are renormalised at compose time — so
   there is no total to keep balanced, only a relative importance to choose.
3. Declare its ``queries`` if it needs graph facts the core does not fetch. Each Cypher takes
   an ``$ids`` page and must return a ``release_id`` column; the fact name keys the row into
   :attr:`api.rarity.core.ReleaseContext.facts`. Fact names are unique across all modules.
4. Register it in ``api/rarity/families/__init__.py`` against the taxonomy family ids it
   serves.

Nothing in the core changes.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from typing import Protocol, runtime_checkable

from api.rarity.core import ReleaseContext


@runtime_checkable
class FamilySignals(Protocol):
    """One family extension module.

    Attributes:
        module_id: The module's own id, used to key its contribution in the ``family_signals``
            output and to look its weights back up from a stored row. It names the module, not
            a taxonomy family: ``grooved`` serves ``vinyl``, ``shellac``, and ``grooved_other``.
        weights: Absolute weight per signal this module contributes, on the same scale as the
            core weights. Applied only when the module applies.
        queries: Fact name to Cypher, for graph facts the core does not fetch. Each query takes
            an ``$ids`` page and returns a ``release_id`` column. May be empty.
    """

    module_id: str
    weights: Mapping[str, float]
    queries: Mapping[str, str]

    def applies_to(self, families: Collection[str]) -> bool:
        """Return whether this module contributes to a release covering ``families``."""
        ...

    def signals(self, release_ctx: ReleaseContext) -> dict[str, float]:
        """Return this module's 0-100 signal scores for one release.

        Called only when :meth:`applies_to` accepted the release's families. Returning an empty
        mapping withdraws the module for this release, and its weights are then not applied.
        """
        ...


# Taxonomy family id to the module serving it. One module may be registered under several
# family ids; iteration de-duplicates by identity.
_REGISTRY: dict[str, FamilySignals] = {}


def register(families: Iterable[str], module: FamilySignals) -> None:
    """Register ``module`` as the extension serving each of ``families``.

    Args:
        families: The taxonomy family ids this module serves.
        module: The module instance.

    Raises:
        ValueError: If a family already has a different module, or if a fact name the module
            declares is already claimed by another module.
    """
    claimed = {fact: owner for owner in distinct_modules() for fact in owner.queries}
    for fact in module.queries:
        owner = claimed.get(fact)
        if owner is not None and owner is not module:
            raise ValueError(f"fact name {fact!r} is already declared by module {owner.module_id!r}")
    for family in families:
        existing = _REGISTRY.get(family)
        if existing is not None and existing is not module:
            raise ValueError(f"family {family!r} is already served by module {existing.module_id!r}")
        _REGISTRY[family] = module


def registry() -> Mapping[str, FamilySignals]:
    """Return the family id to module mapping, as a read-only view."""
    return dict(_REGISTRY)


def distinct_modules() -> list[FamilySignals]:
    """Return each registered module once, in registration order."""
    seen: list[FamilySignals] = []
    for module in _REGISTRY.values():
        if not any(module is known for known in seen):
            seen.append(module)
    return seen


def modules_for(families: Collection[str]) -> list[FamilySignals]:
    """Return the modules that contribute to a release covering ``families``.

    A module is a candidate when it is registered against one of the release's families, and
    contributes when its own :meth:`FamilySignals.applies_to` also accepts. The two agree for
    every module today; the predicate exists so a module can decline on evidence beyond the
    family id without having to be unregistered.
    """
    candidates: list[FamilySignals] = []
    for family in families:
        module = _REGISTRY.get(family)
        if module is not None and not any(module is known for known in candidates):
            candidates.append(module)
    return [module for module in candidates if module.applies_to(families)]


def module_weights() -> dict[str, dict[str, float]]:
    """Return every registered module's weights, keyed by module id.

    Lets a consumer holding only a stored ``family_signals`` value rebuild the weights that
    were applied, without re-running the score.
    """
    return {module.module_id: dict(module.weights) for module in distinct_modules()}


def family_queries() -> dict[str, str]:
    """Return every registered module's graph queries, keyed by fact name."""
    return {fact: cypher for module in distinct_modules() for fact, cypher in module.queries.items()}
