"""Verify promoted contracts, generated binding, and immutable dependency pins."""

import json
import tomllib
from hashlib import sha256
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def digest(path: Path) -> str:
    """Return the hexadecimal SHA-256 digest for a file."""
    return sha256(path.read_bytes()).hexdigest()


catalog_source = json.loads((ROOT / "contracts/catalog-events/v1/source.json").read_text())
persistence_source = json.loads((ROOT / "contracts/persistence/v1/source.json").read_text())
compatibility = json.loads((ROOT / "contracts/persistence/v1/compatibility.json").read_text())
with (ROOT / "pyproject.toml").open("rb") as source:
    pyproject = tomllib.load(source)

assert digest(ROOT / "contracts/catalog-events/v1/contract.json") == catalog_source["contract_sha256"]
assert digest(ROOT / "api/catalog_contract.py") == catalog_source["binding_sha256"]
assert digest(ROOT / "contracts/persistence/v1/compatibility.json") == persistence_source["contract_sha256"]
assert compatibility["contract"] == "groovemap.persistence"
assert compatibility["version"] == 1
assert compatibility["application_runtime"]["tested_version"] == "0.1.0"
runtime_source = pyproject["tool"]["uv"]["sources"]["groovemap-runtime"]
assert runtime_source["rev"] == compatibility["application_runtime"]["tested_commit"]
agent_tools_source = pyproject["tool"]["uv"]["sources"]["groovemap-agent-tools"]
assert agent_tools_source["rev"] == compatibility["application_runtime"]["tested_commit"]
assert agent_tools_source["subdirectory"] == "agent-tools"

internal_root = ROOT / "api/contracts/internal-insights/v1"
internal_source = json.loads((internal_root / "python/source.json").read_text())
assert digest(internal_root / "openapi.yaml") == internal_source["contract_sha256"]
assert digest(internal_root / "python/catalog_api_contract.py") == internal_source["binding_sha256"]
assert internal_source["contract_version"] == "1.0.0"

admin_root = ROOT / "api/contracts/operations-console/v1"
admin_source = json.loads((admin_root / "python/source.json").read_text())
assert digest(admin_root / "routes.json") == admin_source["contract_sha256"]
assert digest(admin_root / "python/catalog_admin_contract.py") == admin_source["binding_sha256"]
assert admin_source["contract_version"] == 1
