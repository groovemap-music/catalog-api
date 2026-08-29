"""Behavior tests for third-party dependency license policy."""

from typing import Any

import pytest

from scripts.check_dependency_licenses import license_families, validate_inventory


@pytest.mark.parametrize(
    "license_text",
    [
        "GPL-2.0-only",
        "GPL-3.0-or-later",
        "AGPL-3.0-only",
        "GPLv2+",
        "GNU General Public License v3",
        "GNU Affero General Public License v3 or later",
        "MIT OR GPL-3.0-only",
    ],
)
def test_gpl_and_agpl_spellings_are_rejected(license_text: str) -> None:
    packages = [{"Name": "third-party", "Version": "1.0", "License": license_text}]

    with pytest.raises(ValueError, match="forbidden GPL/AGPL dependencies"):
        validate_inventory(packages, {"third-party": "1.0"}, "")


@pytest.mark.parametrize(
    ("license_text", "expected"),
    [
        ("LGPL-3.0-only", {"LGPL"}),
        ("GNU Lesser General Public License v2 or later (LGPLv2+)", {"LGPL"}),
        ("Mozilla Public License 2.0 (MPL 2.0)", {"MPL"}),
        ("MPL-2.0 AND (Apache-2.0 OR MIT)", {"MPL"}),
        ("MIT", set()),
    ],
)
def test_reciprocal_and_permissive_families_are_not_misclassified_as_gpl(
    license_text: str,
    expected: set[str],
) -> None:
    assert license_families(license_text) == expected


def test_only_the_exact_first_party_distribution_is_exempt() -> None:
    packages = [
        {"Name": "groovemap-catalog-api", "Version": "0.1.0", "License": "AGPL-3.0-only"},
        {"Name": "groovemap-catalog-api-plugin", "Version": "1.0", "License": "AGPL-3.0-only"},
    ]

    with pytest.raises(ValueError, match="groovemap-catalog-api-plugin"):
        validate_inventory(packages, {}, "")


def test_every_reciprocal_dependency_requires_an_exact_locked_notice() -> None:
    packages: list[dict[str, Any]] = [
        {"Name": "hypothesis", "Version": "6.165.10", "License": "MPL-2.0"},
        {"Name": "psycopg", "Version": "3.3.4", "License": "LGPL-3.0-only"},
    ]
    locked = {"hypothesis": "6.165.10", "psycopg": "3.3.4"}

    with pytest.raises(ValueError, match=r"hypothesis 6\.165\.10"):
        validate_inventory(packages, locked, "`psycopg` 3.3.4")

    validate_inventory(packages, locked, "`hypothesis` 6.165.10\n`psycopg` 3.3.4")


def test_reciprocal_inventory_must_match_the_lock() -> None:
    packages = [{"Name": "hypothesis", "Version": "6.165.11", "License": "MPL-2.0"}]

    with pytest.raises(ValueError, match=r"installed 6\.165\.11, locked 6\.165\.10"):
        validate_inventory(packages, {"hypothesis": "6.165.10"}, "`hypothesis` 6.165.10")
