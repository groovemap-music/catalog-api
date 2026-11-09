#!/usr/bin/env python3
"""Compare run-out interpretation to exact signature lookup on a fixture manifest."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from api.runout import SignatureRule
from api.runout.evaluation import evaluate_manifest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SYNTHETIC_MANIFEST = REPOSITORY_ROOT / "tests" / "fixtures" / "runout" / "synthetic-manifest.json"
SYNTHETIC_SIGNATURES = REPOSITORY_ROOT / "tests" / "fixtures" / "runout" / "synthetic-signatures.json"


def _read_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=SYNTHETIC_MANIFEST, help="JSON evaluation manifest")
    parser.add_argument("--signatures", type=Path, default=SYNTHETIC_SIGNATURES, help="JSON signature dictionary")
    args = parser.parse_args()

    manifest = _read_json(args.manifest)
    signature_document = _read_json(args.signatures)
    if not isinstance(manifest, dict):
        parser.error("manifest must be a JSON object")
    entries = signature_document.get("entries") if isinstance(signature_document, dict) else None
    if not isinstance(entries, list):
        parser.error("signature document must contain an 'entries' list")

    rules: list[SignatureRule] = []
    for offset, entry in enumerate(entries):
        if not isinstance(entry, dict) or any(not isinstance(entry.get(key), str) for key in ("signature", "role", "candidate", "source_ref")):
            parser.error(f"signature entries[{offset}] needs string signature, role, candidate, and source_ref fields")
        rules.append(SignatureRule(entry["signature"], entry["role"], entry["candidate"], entry["source_ref"]))

    report = evaluate_manifest(manifest, rules)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
