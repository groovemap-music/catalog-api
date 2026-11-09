"""Bounded, offline set coverage over one scope and authorized collection snapshot.

No identity resolution, graph-community inference, reads, writes, or purchase behavior lives
here. Caller-declared objective evidence is separate from the unchanged individual Fit score.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any, Final

from api.fit import FIT_VERSION, compute_fit
from api.pursuit_scope import AuthorizedCollection, CatalogSnapshot, PursuitScope, ScopePreview, UnitEvidence, preview_scope


CRATE_COVERAGE_VERSION: Final[str] = "bounded-joint-coverage-v1"
MAX_CANDIDATES: Final[int] = 200
MAX_SELECTIONS: Final[int] = 20
MAX_SCOPE_ROWS: Final[int] = 5000
MAX_OBJECTIVE_UNITS: Final[int] = 2000
UnitKey = tuple[str, str]


@dataclass(frozen=True, slots=True)
class CrateCandidate:
    """A provider release and its existing Fit context, not a native edition ID."""

    release_id: str
    fit_context: Mapping[str, Any]
    popularity: int = 0
    source_provider: str = "discogs"

    def __post_init__(self) -> None:
        if not self.release_id or not self.source_provider:
            raise ValueError("candidate source identity must be non-empty")
        if self.fit_context.get("id") != self.release_id:
            raise ValueError("Fit context id must equal candidate source release_id")
        if type(self.popularity) is not int or self.popularity < 0:
            raise ValueError("popularity must be a non-negative integer")

    @property
    def source_key(self) -> str:
        return f"{self.source_provider}:{self.release_id}"


@dataclass(frozen=True, slots=True)
class CoverageObjective:
    """Explicit, caller-checked artist/label/credit coverage evidence for native units.

    Keys in ``by_scope_unit`` are the preview's native (edition/work, ID) keys, never
    provider release IDs. Empty mappings mean no known coverage, not inferred coverage.
    This evidence must come from the same materialized catalog snapshot.
    """

    snapshot_id: str
    objective_id: str
    units: tuple[UnitKey, ...]
    by_scope_unit: Mapping[UnitKey, tuple[UnitKey, ...]]

    def __post_init__(self) -> None:
        if not self.snapshot_id or not self.objective_id:
            raise ValueError("objective and snapshot IDs must be non-empty")
        if len(self.units) > MAX_OBJECTIVE_UNITS:
            raise ValueError(f"objective exceeds hard unit bound {MAX_OBJECTIVE_UNITS}")
        if any(kind not in {"artist", "label", "credit"} or not identifier for kind, identifier in self.units):
            raise ValueError("declared units must be named artist, label, or credit IDs")
        if len(set(self.units)) != len(self.units):
            raise ValueError("objective units must be unique")
        if any(not set(values) <= set(self.units) for values in self.by_scope_unit.values()):
            raise ValueError("objective mapping contains undeclared units")


def _keys(units: Sequence[UnitEvidence]) -> set[UnitKey]:
    return {unit.unit_key for unit in units}


def _listed(units: set[UnitKey]) -> list[list[str]]:
    return [list(unit) for unit in sorted(units)]


def _objective(
    preview: ScopePreview, objective: CoverageObjective | None
) -> tuple[set[UnitKey], dict[UnitKey, set[UnitKey]], set[UnitKey], set[UnitKey]]:
    eligible = _keys((*preview.owned, *preview.missing, *preview.ownership_review))
    if objective is None:
        universe = eligible
        mapping = {key: {key} for key in eligible}
    else:
        if objective.snapshot_id != preview.catalog_snapshot_id:
            raise ValueError("objective catalog snapshot does not match scope preview")
        if not set(objective.by_scope_unit) <= eligible:
            raise ValueError("objective mapping must reference known eligible native scope units")
        universe = set(objective.units)
        mapping = {key: set(values) for key, values in objective.by_scope_unit.items()}
    owned = set().union(*(mapping.get(key, set()) for key in _keys(preview.owned)))
    review = set().union(*(mapping.get(key, set()) for key in _keys(preview.ownership_review))) - owned
    # An unresolved owned provider row could refer to any remaining objective unit. Do
    # not turn incomplete ownership into a guaranteed incremental coverage claim.
    if not preview.denominator_evidence.ownership_complete:
        review = universe - owned
    return universe, mapping, owned, review


def compare_crates(
    snapshot: CatalogSnapshot,
    scope: PursuitScope,
    collection: AuthorizedCollection,
    folded_collection: Mapping[str, Any],
    candidates: Sequence[CrateCandidate],
    *,
    objective: CoverageObjective | None = None,
    candidate_limit: int = 100,
    selection_limit: int = 5,
) -> dict[str, Any]:
    """Compare greedy marginal coverage, unchanged individual Fit, and popularity.

    Input rows are hard bounded before processing. Canonical aliases/copies/equivalent
    editions collapse by the preview's native unit; the lexically first source row wins
    for every strategy. A stable native-key prefix applies the shared candidate cap.
    Greedy stops at zero marginal gain; reference sorts may expose zero-gain additions.
    All strategies score against the original frozen collection, never a simulated fold.
    """
    if type(candidate_limit) is not int or not 1 <= candidate_limit <= MAX_CANDIDATES:
        raise ValueError(f"candidate_limit must be in [1, {MAX_CANDIDATES}]")
    if type(selection_limit) is not int or not 0 <= selection_limit <= MAX_SELECTIONS:
        raise ValueError(f"selection_limit must be in [0, {MAX_SELECTIONS}]")
    if len(candidates) > MAX_CANDIDATES:
        raise ValueError(f"input candidates exceed hard bound {MAX_CANDIDATES}")
    if set(folded_collection.get("release_ids") or ()) != set(collection.release_ids):
        raise ValueError("Fit fold source IDs must match the authorized collection snapshot")
    if any(candidate.source_provider != collection.source_provider for candidate in candidates):
        raise ValueError("candidate provider must match the existing Fit collection provider")
    if len(snapshot.releases) > MAX_SCOPE_ROWS:
        raise ValueError(f"catalog snapshot exceeds hard row bound {MAX_SCOPE_ROWS}")
    preview = preview_scope(snapshot, scope, collection)
    universe, mapping, initially_covered, review = _objective(preview, objective)
    missing = universe - initially_covered - review
    source_units = {
        source: unit.unit_key
        for unit in preview.missing
        if "unknown_fields_included_by_policy" not in unit.reasons and preview.denominator_evidence.ownership_complete
        for source in unit.release_ids
    }
    withheld = []
    source_rows: dict[str, CrateCandidate] = {}
    for candidate in candidates:
        prior = source_rows.get(candidate.source_key)
        if prior is not None and prior != candidate:
            raise ValueError("conflicting candidate rows for one source release")
        source_rows[candidate.source_key] = candidate
    canonical: dict[UnitKey, CrateCandidate] = {}
    for source_key, candidate in sorted(source_rows.items()):
        unit = source_units.get(source_key)
        if unit is None:
            withheld.append({"source_key": source_key, "reason": "not_confirmed_missing_in_scope"})
        else:
            canonical.setdefault(unit, candidate)
    normalized = sorted(canonical)
    pool = normalized[:candidate_limit]
    fits = {unit: compute_fit(dict(folded_collection), dict(canonical[unit].fit_context)) for unit in pool}
    coverage = {unit: mapping.get(unit, set()) & missing for unit in pool}
    policies: dict[str, Any] = {}
    for strategy in ("greedy_coverage", "individual_fit", "popularity"):
        remaining = set(pool)
        covered = set(initially_covered)
        additions: list[dict[str, Any]] = []
        while remaining and len(additions) < selection_limit:
            if strategy == "greedy_coverage":
                chosen = min(remaining, key=lambda unit: (-len(coverage[unit] - covered), unit))
                if not coverage[chosen] - covered:
                    break
            elif strategy == "individual_fit":
                chosen = min(remaining, key=lambda unit: (-fits[unit]["fit"], unit))
            else:
                chosen = min(remaining, key=lambda unit: (-canonical[unit].popularity, unit))
            declared = mapping.get(chosen, set())
            gain = coverage[chosen] - covered
            overlap = declared & covered
            additions.append(
                {
                    "source_key": canonical[chosen].source_key,
                    "scope_unit": list(chosen),
                    "marginal_gain": len(gain),
                    "new_units": _listed(gain),
                    "overlap": len(overlap),
                    "overlap_units": _listed(overlap),
                    "withheld_units": _listed(declared & review),
                    "fit": fits[chosen],
                    "popularity": canonical[chosen].popularity,
                    "covered_before": len(covered),
                    "covered_after": len(covered | gain),
                    "reason": "new_declared_units" if gain else "no_guaranteed_marginal_gain",
                }
            )
            covered.update(gain)
            remaining.remove(chosen)
        gained = covered - initially_covered
        selected_native = len(additions)
        counts = preview.denominator_evidence
        policies[strategy] = {
            "additions": additions,
            "objective_before": {
                "denominator": len(universe),
                "covered": len(initially_covered),
                "missing": len(missing),
                "ownership_review": len(review),
            },
            "objective_after": {
                "denominator": len(universe),
                "covered": len(covered),
                "missing": len(missing - gained),
                "ownership_review": len(review),
            },
            "scope_before": asdict(counts),
            "scope_after": {**asdict(counts), "owned": counts.owned + selected_native, "missing": counts.missing - selected_native},
            "new_units": _listed(gained),
            "coverage_fraction_after": len(covered) / len(universe) if universe and not review else None,
        }
    return {
        "version": CRATE_COVERAGE_VERSION,
        "fit_version": FIT_VERSION,
        "catalog_snapshot_id": preview.catalog_snapshot_id,
        "collection_snapshot_id": preview.collection_snapshot_id,
        "scope_key": list(preview.scope_key),
        "objective_id": objective.objective_id if objective else f"native-{scope.unit_kind.value}",
        "objective_units": _listed(universe),
        "controls": {
            "candidate_limit": candidate_limit,
            "selection_limit": selection_limit,
            "hard_candidate_bound": MAX_CANDIDATES,
            "hard_selection_bound": MAX_SELECTIONS,
            "hard_scope_row_bound": MAX_SCOPE_ROWS,
            "hard_objective_unit_bound": MAX_OBJECTIVE_UNITS,
        },
        "pool": [{"scope_unit": list(unit), "source_key": canonical[unit].source_key} for unit in pool],
        "input_rows": len(candidates),
        "canonical_candidates": len(normalized),
        "capped_out": len(normalized) - len(pool),
        "withheld": withheld,
        "scope_evidence": {
            status: [asdict(unit) for unit in getattr(preview, status)]
            for status in ("owned", "missing", "ownership_review", "needs_review", "excluded")
        },
        "projection_only": True,
        "policies": policies,
    }
