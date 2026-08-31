"""Reject forbidden dependency licenses and verify reciprocal-license notices."""

import json
import re
import sys
import tomllib
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
FIRST_PARTY_PACKAGE = "groovemap-catalog-api"
LICENSE_TOKEN = re.compile(
    r"(?<![A-Z])(?P<family>AGPL|GPL|LGPL|MPL)"
    r"(?:[- ]?V?\d+(?:\.\d+)?)?(?:-(?:ONLY|OR-LATER)|\+)?(?![A-Z])",
    re.IGNORECASE,
)


def canonical_name(name: str) -> str:
    """Return a normalized Python distribution name."""
    return re.sub(r"[-_.]+", "-", name).lower()


def license_families(license_text: str) -> set[str]:
    """Identify license families in SPDX expressions and common metadata labels."""
    families = {match.group("family").upper() for match in LICENSE_TOKEN.finditer(license_text)}
    normalized = " ".join(license_text.upper().split())
    if "GNU AFFERO GENERAL PUBLIC LICENSE" in normalized:
        families.add("AGPL")
    if "GNU LESSER GENERAL PUBLIC LICENSE" in normalized:
        families.add("LGPL")
    elif "GNU GENERAL PUBLIC LICENSE" in normalized:
        families.add("GPL")
    if "MOZILLA PUBLIC LICENSE" in normalized:
        families.add("MPL")
    return families


def validate_inventory(
    packages: list[dict[str, Any]],
    locked_versions: dict[str, str],
    notices: str,
) -> None:
    """Validate forbidden families and reciprocal-license notice coverage."""
    forbidden: list[str] = []
    missing_notices: list[str] = []
    unlocked: list[str] = []

    for package in packages:
        name = canonical_name(str(package["Name"]))
        version = str(package["Version"])
        license_text = str(package["License"])
        if name == FIRST_PARTY_PACKAGE:
            continue

        families = license_families(license_text)
        if families & {"GPL", "AGPL"}:
            forbidden.append(f"{name} {version} ({license_text})")

        if families & {"LGPL", "MPL"}:
            locked_version = locked_versions.get(name)
            if locked_version != version:
                unlocked.append(f"{name}: installed {version}, locked {locked_version or 'missing'}")
                continue
            if f"`{name}` {locked_version}" not in notices:
                missing_notices.append(f"{name} {locked_version} ({license_text})")

    errors: list[str] = []
    if forbidden:
        errors.append("forbidden GPL/AGPL dependencies: " + "; ".join(sorted(forbidden)))
    if unlocked:
        errors.append("reciprocal-license inventory differs from uv.lock: " + "; ".join(sorted(unlocked)))
    if missing_notices:
        errors.append("reciprocal-license dependencies missing from THIRD_PARTY_NOTICES.md: " + "; ".join(sorted(missing_notices)))
    if errors:
        raise ValueError("\n".join(errors))


def main() -> None:
    """Read the pip-licenses JSON inventory from stdin and validate repository policy."""
    packages = json.load(sys.stdin)
    if not isinstance(packages, list):
        raise TypeError("pip-licenses inventory must be a JSON list")
    with (ROOT / "uv.lock").open("rb") as source:
        locked_versions = {canonical_name(package["name"]): package["version"] for package in tomllib.load(source)["package"]}
    notices = (ROOT / "THIRD_PARTY_NOTICES.md").read_text()
    validate_inventory(packages, locked_versions, notices)


if __name__ == "__main__":
    main()
