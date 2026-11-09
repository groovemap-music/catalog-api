#!/usr/bin/env python3
"""Run a deliberately synthetic pressing-candidate comparison example."""

from __future__ import annotations

import json
from dataclasses import asdict

from api.pressing_comparison import CatalogField, CopyObservation, KnownReleaseCandidate, ProviderReference, compare_known_candidates
from api.runout import SignatureRule


def main() -> None:
    source = "synthetic:catalog-fixture"
    candidates = (
        KnownReleaseCandidate(
            "fixture:native-release-a",
            (ProviderReference("fixture-discogs", "release", "101"),),
            {
                "country": CatalogField(("US",), (source,)),
                "year": CatalogField((1978,), (source,)),
                "pressing_plant": CatalogField(("fixture-plant-a",), (source,)),
                "barcode": CatalogField(("000000000001",), (source,)),
            },
        ),
        KnownReleaseCandidate(
            "fixture:native-release-b",
            (ProviderReference("fixture-musicbrainz", "release", "202"),),
            {
                "country": CatalogField(("US",), (source,)),
                "year": CatalogField((1979,), (source,)),
                "pressing_plant": CatalogField(("fixture-plant-b",), (source,)),
                "barcode": CatalogField(("000000000001",), (source,)),
            },
        ),
    )
    observations = (
        CopyObservation(
            owned_copy_id="fixture:owned-copy-a",
            kind="matrix",
            value="SIDE A / PLANT A",
            source="user",
            observed_at="2026-01-01T12:00:00Z",
            created_at="2026-01-01T12:01:00Z",
            observation_id="fixture:observation-a",
        ),
    )
    result = compare_known_candidates(
        "fixture:owned-copy-a",
        candidates,
        observations,
        runout_rules=(SignatureRule("PLANT A", "pressing_plant", "fixture-plant-a", "synthetic:signature-fixture"),),
    )
    report = asdict(result)
    report["ranked_observations"] = [
        {**asdict(rank), "distinguishable_pair_fraction": rank.distinguishable_pair_fraction} for rank in result.ranked_observations
    ]
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
