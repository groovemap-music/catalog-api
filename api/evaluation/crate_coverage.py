"""Reproducible synthetic complementarity example; not real usefulness evidence."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from api.crate_coverage import CoverageObjective, CrateCandidate, compare_crates
from api.pursuit_scope import AuthorizedCollection, CatalogSnapshot, CoReleasePolicy, PursuitScope, ReleaseFact, TargetKind
from api.queries.fit_queries import fold_collection


def synthetic_replay(*, candidate_limit: int = 100, selection_limit: int = 2) -> dict[str, Any]:
    """Two individually attractive overlapping records versus a complementary third."""
    folded = fold_collection(
        [
            {
                "release_id": "held",
                "title": "Synthetic held record",
                "artist_ids": ["familiar"],
                "label_ids": ["scope"],
                "genres": ["Jazz"],
                "styles": [],
            }
        ]
    )
    collection = AuthorizedCollection.from_folded(
        "synthetic-collection-v1", folded, source_provider="discogs", provider_to_edition_candidates={"held": ("edition-held",)}
    )
    releases = tuple(
        ReleaseFact(
            identifier,
            f"edition-{identifier}",
            ("familiar" if identifier == "held" else "artist-two" if identifier == "c" else "artist-one",),
            ("scope",) if identifier == "held" else ("scope", "label-two" if identifier == "c" else "label-one"),
            ("vinyl",),
            2000,
            ("US",),
        )
        for identifier in ("held", "a", "b", "c")
    )
    snapshot = CatalogSnapshot("synthetic-crate-catalog-v1", releases)
    shared = (("artist", "artist-one"), ("label", "label-one"), ("credit", "credit-one"))
    complement = (("artist", "artist-two"), ("label", "label-two"))
    objective = CoverageObjective(
        snapshot.snapshot_id,
        "synthetic-declared-artist-label-credit-v1",
        (*shared, *complement),
        {("edition", "edition-a"): shared, ("edition", "edition-b"): shared, ("edition", "edition-c"): complement},
    )
    candidates = tuple(
        CrateCandidate(
            identifier,
            {
                "id": identifier,
                "title": f"Synthetic candidate {identifier}",
                "artists": [{"id": value, "name": f"Synthetic {value}"} for value in releases[index + 1].artist_ids or ()],
                "labels": [{"id": value, "name": f"Synthetic {value}"} for value in releases[index + 1].label_ids or ()],
                "genres": ["Jazz"],
            },
            popularity=100 - index,
        )
        for index, identifier in enumerate(("a", "b", "c"))
    )
    result = compare_crates(
        snapshot,
        PursuitScope(TargetKind.LABEL, "scope", co_release_policy=CoReleasePolicy.INCLUDE),
        collection,
        folded,
        candidates,
        objective=objective,
        candidate_limit=candidate_limit,
        selection_limit=selection_limit,
    )
    return {
        "synthetic": True,
        "real_quality_status": "PENDING: no real checked usefulness labels supplied",
        "real_checked_feedback_labels": 0,
        "missing_labels_are_negative": False,
        "note": "Hand-declared synthetic units test complementarity only; no claim of real 15% usefulness gain or graph communities.",
        **result,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-limit", type=int, default=100)
    parser.add_argument("--selection-limit", type=int, default=2)
    args = parser.parse_args(argv)
    try:
        report = synthetic_replay(candidate_limit=args.candidate_limit, selection_limit=args.selection_limit)
    except ValueError as exc:
        sys.stderr.write(f"crate coverage replay input error: {exc}\n")
        return 2
    sys.stdout.write(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
