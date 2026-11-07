"""Verify promoted contracts, generated binding, and immutable dependency pins."""

import json
import tomllib
from hashlib import sha256
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PERSISTENCE_PRODUCER_COMMIT = "7937373f328e21efd745fe51357600d564a2ae75"
MUSICBRAINZ_PROJECTIONS_PRODUCER_COMMIT = "27937811f8fe8cf8aca72064557957e69f9e5dae"


def digest(path: Path) -> str:
    """Return the hexadecimal SHA-256 digest for a file."""
    return sha256(path.read_bytes()).hexdigest()


persistence_source = json.loads((ROOT / "contracts/persistence/v1/source.json").read_text())
compatibility = json.loads((ROOT / "contracts/persistence/v1/compatibility.json").read_text())
consumer_runtime = json.loads((ROOT / "contracts/persistence/v1/consumer-runtime.json").read_text())
assert consumer_runtime["producer_compatibility_sha256"] == persistence_source["contract_sha256"]
assert consumer_runtime["consumer"] == "catalog-api"
assert consumer_runtime["application_runtime"]["tested_version"] == "0.1.0"
with (ROOT / "pyproject.toml").open("rb") as source:
    pyproject = tomllib.load(source)

# groovemap.catalog-events/v1 is produced independently by two source-owned repositories
# (ADR 0005); catalog-api composes the contract from both promoted producers rather than
# from one combined upstream.
for catalog_producer in ("discogs", "musicbrainz"):
    producer_root = ROOT / "contracts/catalog-events/v1" / catalog_producer
    catalog_source = json.loads((producer_root / "source.json").read_text())
    assert digest(producer_root / "contract.json") == catalog_source["contract_sha256"]
    assert digest(producer_root / "python/catalog_contract.py") == catalog_source["binding_sha256"]

# The MusicBrainz relationship types musicbrainz-graph-enricher projects onto Neo4j, promoted byte-for-byte. The typed
# queries are checked against it by tests/test_musicbrainz_relationship_types.py on every run.
projections_root = ROOT / "contracts/musicbrainz-projections/v1"
projections_source = json.loads((projections_root / "source.json").read_text())
relationship_types = json.loads((projections_root / "relationship-types.json").read_text())
assert digest(projections_root / "relationship-types.json") == projections_source["contract_sha256"]
assert projections_source["producer_commit"] == MUSICBRAINZ_PROJECTIONS_PRODUCER_COMMIT
assert projections_source["producer_repository"] == "https://github.com/groovemap-music/musicbrainz-graph-enricher"
assert relationship_types["contract"] == "groovemap.musicbrainz-graph-enricher.relationship-types"
assert relationship_types["version"] == 1
assert relationship_types["edge_types"] == sorted(set(relationship_types["relationship_types"].values()))

assert digest(ROOT / "contracts/persistence/v1/compatibility.json") == persistence_source["contract_sha256"]
assert persistence_source["producer_commit"] == PERSISTENCE_PRODUCER_COMMIT
assert compatibility["contract"] == "groovemap.persistence"
assert compatibility["version"] == 1
assert compatibility["application_runtime"]["tested_version"] == "0.1.0"
runtime_source = pyproject["tool"]["uv"]["sources"]["groovemap-runtime"]
assert runtime_source["rev"] == consumer_runtime["application_runtime"]["tested_commit"]
agent_tools_source = pyproject["tool"]["uv"]["sources"]["groovemap-agent-tools"]
assert agent_tools_source["rev"] == consumer_runtime["application_runtime"]["tested_commit"]
assert agent_tools_source["subdirectory"] == "agent-tools"

internal_root = ROOT / "api/contracts/internal-insights/v2"
internal_source = json.loads((internal_root / "python/source.json").read_text())
assert digest(internal_root / "openapi.yaml") == internal_source["contract_sha256"]
assert digest(internal_root / "python/catalog_api_contract.py") == internal_source["binding_sha256"]
assert internal_source["contract_version"] == "2.0.0"

internal_root = ROOT / "api/contracts/internal-insights/v1"
internal_source = json.loads((internal_root / "python/source.json").read_text())
assert digest(internal_root / "openapi.yaml") == internal_source["contract_sha256"]
assert digest(internal_root / "python/catalog_api_contract.py") == internal_source["binding_sha256"]
assert internal_source["contract_version"] == "1.10.0"

admin_root = ROOT / "api/contracts/operations-console/v1"
admin_source = json.loads((admin_root / "python/source.json").read_text())
assert digest(admin_root / "routes.json") == admin_source["contract_sha256"]
assert digest(admin_root / "python/catalog_admin_contract.py") == admin_source["binding_sha256"]
assert admin_source["contract_version"] == 1
