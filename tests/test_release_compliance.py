"""Release-readiness contracts that must stay local and publication-safe."""

import re
from pathlib import Path

import yaml

from api.api import STARTUP_BANNER
from api.config import DEFAULT_DISCOGS_USER_AGENT, ApiConfig


ROOT = Path(__file__).parent.parent
AUTOMATION_REVISION = "833cb464507678c38ab78bd4718ce697399463e9"
PYTHON_LIBRARIES_REVISION = "455523ec388fdb9862d7aca65d9434aa7073dcb5"


def _workflow(name: str) -> dict[str, object]:
    document = yaml.safe_load((ROOT / ".github" / "workflows" / name).read_text())
    assert isinstance(document, dict)
    return document


def _dependabot() -> dict[str, object]:
    document = yaml.safe_load((ROOT / ".github" / "dependabot.yml").read_text())
    assert isinstance(document, dict)
    return document


def _assert_full_automation_pin(reference: object, workflow: str) -> None:
    expected = f"groovemap-music/automation/.github/workflows/{workflow}@{AUTOMATION_REVISION}"
    assert reference == expected
    assert re.fullmatch(r"[0-9a-f]{40}", AUTOMATION_REVISION)


def _indexed_document_paths() -> tuple[Path, ...]:
    docs_index = ROOT / "docs" / "README.md"
    targets: list[Path] = []
    for target in re.findall(r"\[[^]]+\]\(([^)]+)\)", docs_index.read_text()):
        if "://" in target:
            continue
        path = (docs_index.parent / target.split("#", maxsplit=1)[0]).resolve()
        assert path.exists(), f"indexed documentation target does not exist: {target}"
        assert path.is_file(), f"indexed documentation target is not a file: {target}"
        assert path.read_text().strip(), f"indexed documentation target is empty: {target}"
        targets.append(path)

    api_readme = ROOT / "api" / "README.md"
    assert api_readme.exists()
    return tuple(dict.fromkeys((*targets, api_readme)))


def test_ci_uses_one_immutable_graph_for_every_trigger() -> None:
    workflow = _workflow("ci.yml")
    # PyYAML implements YAML 1.1 and parses the plain key ``on`` as ``True``.
    triggers = workflow.get("on", workflow.get(True))
    assert isinstance(triggers, dict)
    assert set(triggers) == {"pull_request", "push", "schedule", "workflow_dispatch"}

    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    assert set(jobs) == {"required"}
    required = jobs["required"]
    assert isinstance(required, dict)
    _assert_full_automation_pin(required["uses"], "reusable-ci.yml")
    assert "if" not in required

    inputs = required["with"]
    assert isinstance(inputs, dict)
    assert inputs["image-command"] == "just image && just performance-image"
    assert inputs["check-command"] == "just ci-check"
    assert inputs["coverage-command"] == "just coverage"
    assert inputs["audit-command"] == "just audit"
    assert inputs["license-command"] == "just license-check"
    assert inputs["secret-scan-command"] == "just security"

    secrets = required["secrets"]
    assert isinstance(secrets, dict)
    assert secrets["CODECOV_TOKEN"] == "${{ secrets.CODECOV_TOKEN }}"

    source = (ROOT / ".github" / "workflows" / "ci.yml").read_text().lower()
    assert "dependabot" not in source
    assert "github.actor" not in source
    for marker in (
        "requires-private-library",
        "private-library-client-id",
        "private-library-revision",
        "private_library_private_key",
        "groovemap_ci_app_client_id",
        "groovemap_ci_app_private_key",
    ):
        assert marker not in source


def test_release_callers_pin_distinct_artifacts_and_repository_named_images() -> None:
    workflow = _workflow("release.yml")
    assert workflow["permissions"] == {
        "contents": "read",
        "packages": "write",
        "id-token": "write",
        "attestations": "write",
    }
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    assert set(jobs) == {"release", "release-performance"}

    expected_artifact_identities = {
        "release": "catalog-api-v0.1.0-primary",
        "release-performance": "catalog-api-v0.1.0-performance",
    }
    artifact_identities: set[str] = set()
    for name, artifact_variant, image_variant in (
        ("release", "primary", None),
        ("release-performance", "performance", "performance"),
    ):
        job = jobs[name]
        assert isinstance(job, dict)
        _assert_full_automation_pin(job["uses"], "reusable-release.yml")
        inputs = job["with"]
        assert isinstance(inputs, dict)
        assert inputs["repository-name"] == "catalog-api"
        assert inputs["artifact-variant"] == artifact_variant
        artifact_identity = f"{inputs['repository-name']}-v0.1.0-{inputs['artifact-variant']}"
        assert artifact_identity == expected_artifact_identities[name]
        artifact_identities.add(artifact_identity)
        assert inputs["release-command"] == "just release-dry-run"
        assert inputs["publish-image"] is True
        if image_variant is None:
            assert "image-variant" not in inputs
        else:
            assert inputs["image-variant"] == image_variant
            assert inputs["dockerfile"] == "performance/Dockerfile"

    assert artifact_identities == set(expected_artifact_identities.values())

    source = (ROOT / ".github" / "workflows" / "release.yml").read_text().lower()
    for marker in (
        "requires-private-library",
        "private-library-client-id",
        "private-library-revision",
        "private_library_private_key",
        "groovemap_ci_app_client_id",
        "groovemap_ci_app_private_key",
    ):
        assert marker not in source

    pyproject = (ROOT / "pyproject.toml").read_text()
    assert "https://github.com/groovemap-music/python-libraries.git" in pyproject
    assert PYTHON_LIBRARIES_REVISION in pyproject


def test_no_reduced_dependency_or_legacy_automation_path_exists() -> None:
    paths = [path.relative_to(ROOT).as_posix() for path in ROOT.rglob("*") if path.is_file()]
    lowered = "\n".join(paths).lower()
    assert "renovate" not in lowered
    assert ".github/workflows/claude" not in lowered


def test_dependabot_leaves_managed_python_image_upgrades_atomic() -> None:
    updates = _dependabot()["updates"]
    assert isinstance(updates, list)
    docker = next(update for update in updates if update["package-ecosystem"] == "docker")

    assert docker["directories"] == ["/", "/performance"]
    assert docker["ignore"] == [{"dependency-name": "python"}]
    assert docker["open-pull-requests-limit"] > 0


def test_runtime_identity_is_repository_specific() -> None:
    assert "GrooveMap catalog-api" in STARTUP_BANNER
    assert "analytics-engine" not in STARTUP_BANNER
    assert DEFAULT_DISCOGS_USER_AGENT == ("GrooveMap-catalog-api/1.0 +https://github.com/groovemap-music/catalog-api")

    # The environment override remains supported while the default is a single shared identity.
    config_field = ApiConfig.__dataclass_fields__["discogs_user_agent"]
    assert config_field.default == DEFAULT_DISCOGS_USER_AGENT
    assert "DEFAULT_DISCOGS_USER_AGENT" in (ROOT / "api" / "routers" / "insights_compute.py").read_text()


def test_public_documentation_uses_repo_identity_and_mermaid() -> None:
    readme = (ROOT / "README.md").read_text()
    docs_index = (ROOT / "docs" / "README.md").read_text()
    assert "GrooveMap" in readme
    assert "catalog-api" in readme
    assert "```mermaid" in readme
    assert "```mermaid" in docs_index
    assert not any(path.is_file() for path in (ROOT / "docs" / "superpowers").rglob("*"))
    assert not (ROOT / "docs" / "extraction.md").exists()

    configuration = (ROOT / "docs" / "configuration.md").read_text()
    logging = (ROOT / "docs" / "logging-guide.md").read_text()
    assert "only configuration read by the `catalog-api` repository" in configuration
    assert "logging boundary implemented by `catalog-api`" in logging
    for monolith_heading in (
        "### Schema-Init",
        "### Extractor",
        "### Graphinator",
        "### Tableinator",
        "### Explore",
        "### Dashboard",
        "### Insights",
        "### Brainzgraphinator",
        "### Brainztableinator",
        "### MCP Server",
    ):
        assert monolith_heading not in configuration
    assert "all GrooveMap services" not in logging
    assert "brainzgraphinator" not in logging
    assert "Rust extractor" not in logging

    expected_user_agent = 'DISCOGS_USER_AGENT="GrooveMap-catalog-api/1.0 +https://github.com/groovemap-music/catalog-api"'
    user_agent_examples = [
        line.strip()
        for path in (ROOT / "README.md", ROOT / "api" / "README.md", *sorted((ROOT / "docs").glob("*.md")))
        for line in path.read_text().splitlines()
        if "DISCOGS_USER_AGENT=" in line
    ]
    assert user_agent_examples
    assert all(line == expected_user_agent for line in user_agent_examples)
    public_documentation = "\n".join(
        path.read_text() for path in (ROOT / "README.md", ROOT / "api" / "README.md", *sorted((ROOT / "docs").glob("*.md")))
    )
    assert "GrooveMap/1.0 " not in public_documentation
    assert "GrooveMap/1.0-dev" not in public_documentation


def test_every_indexed_document_respects_repository_ownership() -> None:
    documents = {path.relative_to(ROOT).as_posix(): path.read_text() for path in _indexed_document_paths()}
    assert "api/README.md" in documents

    banned_phrases = (
        "all groovemap services",
        "across all services",
        "all services now",
        "groovemap platform",
        "platform-wide",
        "dashboard service",
        "extractor service",
        "insights service",
        "former curator",
        "discogsography",
        "monorepo-specific",
    )
    for relative_path, content in documents.items():
        lowered = content.casefold()
        for phrase in banned_phrases:
            assert phrase not in lowered, f"{relative_path} contains retired platform wording: {phrase}"

    legacy_identity = re.compile(
        r"\b(?:brainzgraphinator|brainztableinator|graphinator|tableinator|schema-init|curator|extractor|insights)\b",
        re.IGNORECASE,
    )
    wire_identity_allowlist = {
        "api/README.md": (
            "/api/insights/",
            "/api/internal/insights/",
            "`/insights/`",
        ),
        "docs/configuration.md": ("`extractor-discogs`",),
        "docs/database-resilience.md": ("`/api/insights/*`",),
        "docs/usage-examples.md": ("/api/insights/",),
    }
    for relative_path, content in documents.items():
        allowed_markers = wire_identity_allowlist.get(relative_path, ())
        for line_number, line in enumerate(content.splitlines(), start=1):
            line_without_wire_ids = line
            for marker in allowed_markers:
                line_without_wire_ids = line_without_wire_ids.replace(marker, "")
            assert not legacy_identity.search(line_without_wire_ids), f"{relative_path}:{line_number} contains an active retired identity: {line}"

    required_owner_links = {
        "api/README.md": (
            "groovemap-music/graph-explorer",
            "groovemap-music/analytics-engine",
            "groovemap-music/discogs-graph-enricher",
            "groovemap-music/musicbrainz-graph-enricher",
            "groovemap-music/musicbrainz-sql-loader",
            "groovemap-music/database-schema",
            "groovemap-music/deployment",
        ),
        "docs/admin-guide.md": (
            "groovemap-music/operations-console",
            "groovemap-music/deployment",
            "groovemap-music/catalog-ingestion",
            "groovemap-music/discogs-graph-enricher",
            "groovemap-music/discogs-sql-loader",
            "groovemap-music/musicbrainz-graph-enricher",
            "groovemap-music/musicbrainz-sql-loader",
            "groovemap-music/database-schema",
        ),
        "docs/database-resilience.md": (
            "groovemap-music/deployment",
            "groovemap-music/operations-console",
            "groovemap-music/database-schema",
            "groovemap-music/analytics-engine",
            "groovemap-music/catalog-ingestion",
            "groovemap-music/discogs-graph-enricher",
            "groovemap-music/discogs-sql-loader",
            "groovemap-music/musicbrainz-graph-enricher",
            "groovemap-music/musicbrainz-sql-loader",
        ),
        "docs/performance-guide.md": (
            "groovemap-music/deployment",
            "groovemap-music/database-schema",
            "groovemap-music/analytics-engine",
            "groovemap-music/discogs-graph-enricher",
            "groovemap-music/discogs-sql-loader",
            "groovemap-music/musicbrainz-graph-enricher",
            "groovemap-music/musicbrainz-sql-loader",
        ),
        "docs/query-performance-optimizations.md": (
            "groovemap-music/database-schema",
            "groovemap-music/analytics-engine",
            "groovemap-music/discogs-graph-enricher",
            "groovemap-music/discogs-sql-loader",
            "groovemap-music/musicbrainz-graph-enricher",
            "groovemap-music/musicbrainz-sql-loader",
        ),
    }
    for relative_path, owner_links in required_owner_links.items():
        assert relative_path in documents
        for owner_link in owner_links:
            assert owner_link in documents[relative_path], f"{relative_path} must link the current owner: {owner_link}"


def test_history_rewrite_gate_preserves_external_archive_contract() -> None:
    gate = (ROOT / "docs" / "history-rewrite-gate.md").read_text()
    normalized_gate = " ".join(gate.split())
    assert "/Users/" not in gate
    assert ': "${EVIDENCE_ROOT:?Set EVIDENCE_ROOT to a new absolute private evidence directory}"' in gate
    assert "readonly EVIDENCE_ROOT" in gate
    assert "must not be recorded in this public repository" in normalized_gate
    assert "mode `0700`" in gate
    assert "mode `0600`" in gate
    assert "90 days after cutover" in gate
    assert "30 days after the repository becomes public" in normalized_gate
    assert "Deletion requires separate operator approval" in gate
    assert "rewrite.git/filter-repo/commit-map" in gate
    assert "rewrite.git/filter-repo/ref-map" in gate
    assert "copy `.git/filter-repo/commit-map`" not in gate

    verification = gate.split("## Verification before cutover", maxsplit=1)[1].split("## Separate cutover approval", maxsplit=1)[0]
    assert 'readonly REWRITE_MIRROR="${EVIDENCE_ROOT}/rewrite.git"' in verification
    assert 'readonly GITLEAKS_CONFIG="${EVIDENCE_ROOT}/rewritten-gitleaks.toml"' in verification
    assert 'git -C "${REWRITE_MIRROR}" show HEAD:.gitleaks.toml > "${GITLEAKS_CONFIG}"' in verification
    assert 'chmod 600 "${GITLEAKS_CONFIG}"' in verification
    assert 'git -C "${REWRITE_MIRROR}" fsck --full --strict' in verification
    assert 'git -C "${REWRITE_MIRROR}" rev-list --objects --all' in verification
    assert 'gitleaks git --redact --no-banner --config "${GITLEAKS_CONFIG}" "${REWRITE_MIRROR}"' in verification
    assert 'trufflehog git "file://${REWRITE_MIRROR}" --bare --fail --only-verified' in verification
    assert "file://${PWD}" not in verification
