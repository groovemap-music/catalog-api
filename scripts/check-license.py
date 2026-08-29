"""Validate the current first-party license and synchronized package version."""

import hashlib
import re
import tomllib
import zipfile
from email.parser import Parser
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

legal_files = {
    "COMMERCIAL-LICENSING.md",
    "CONTRIBUTING.md",
    "LICENSE",
    "NOTICE",
    "THIRD_PARTY_NOTICES.md",
}
assert set(project["license-files"]) == legal_files
wheels = list((ROOT / "dist").glob("groovemap_catalog_api-*.whl"))
assert len(wheels) == 1
with zipfile.ZipFile(wheels[0]) as wheel:
    metadata_path = next(path for path in wheel.namelist() if path.endswith(".dist-info/METADATA"))
    metadata = Parser().parsestr(wheel.read(metadata_path).decode())
    assert metadata["License-Expression"] == "AGPL-3.0-only"
    assert set(metadata.get_all("License-File", [])) == legal_files
    for filename in legal_files:
        packaged_path = next(path for path in wheel.namelist() if path.endswith(f".dist-info/licenses/{filename}"))
        assert wheel.read(packaged_path) == (ROOT / filename).read_bytes()

dependency_notices = (ROOT / "THIRD_PARTY_NOTICES.md").read_text()
with (ROOT / "uv.lock").open("rb") as source:
    locked_packages = {package["name"]: package["version"] for package in tomllib.load(source)["package"]}
for dependency in ("certifi", "orjson", "psycopg", "psycopg-binary"):
    assert f"`{dependency}` {locked_packages[dependency]}" in dependency_notices
for obligation in ("LGPL-3.0-only", "LGPL-2.1-or-later", "MPL-2.0", "Apache-2.0", "MIT"):
    assert obligation in dependency_notices
assert "applies only to first-party rights" in dependency_notices
assert "it does not change any third-party license" in dependency_notices

commercial_terms = " ".join((ROOT / "COMMERCIAL-LICENSING.md").read_text().split())
assert "only rights held by the participating copyright holders" in commercial_terms
assert "Third-party components remain governed by their own license terms." in commercial_terms

justfile = (ROOT / "Justfile").read_text()
assert "pip-licenses --format=json | uv run python scripts/check_dependency_licenses.py" in justfile
