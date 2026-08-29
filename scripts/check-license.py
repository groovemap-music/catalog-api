"""Validate the current first-party license and synchronized package version."""

import hashlib
import re
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
with (ROOT / "pyproject.toml").open("rb") as source:
    project = tomllib.load(source)["project"]

version_match = re.search(r'^__version__ = "([^"]+)"$', (ROOT / "api/__init__.py").read_text(), re.MULTILINE)
assert version_match is not None
assert project["license"] == "AGPL-3.0-only"
assert "License :: OSI Approved :: GNU Affero General Public License v3" in project["classifiers"]
assert not any(classifier.startswith("License :: Other/Proprietary") for classifier in project["classifiers"])
assert project["version"] == version_match.group(1)
license_bytes = (ROOT / "LICENSE").read_bytes()
assert hashlib.sha256(license_bytes).hexdigest() == "0d96a4ff68ad6d4b6f1f30f713b18d5184912ba8dd389f86aa7710db079abcb0"
notice_text = " ".join((ROOT / "NOTICE").read_text().split())
assert "Versions released on or before 2026-05-13 were made available under the MIT License." in notice_text
