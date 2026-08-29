"""Static regression tests for the repository-owned runtime image."""

import re
import shlex
from pathlib import Path


ROOT = Path(__file__).parent.parent
DOCKERFILE = (ROOT / "Dockerfile").read_text()
PERFORMANCE_DOCKERFILE = (ROOT / "performance" / "Dockerfile").read_text()
BUILD_SCRIPT = (ROOT / "scripts" / "build-image.sh").read_text()
JUSTFILE = (ROOT / "Justfile").read_text()
SENSITIVE_ENV = re.compile(r"(?:PASSWORD|USERNAME|SECRET|TOKEN|CREDENTIAL|PRIVATE_KEY)(?:$|_)")


def _instructions() -> list[str]:
    instructions: list[str] = []
    parts: list[str] = []
    for raw_line in DOCKERFILE.splitlines():
        line = raw_line.strip()
        if not parts and (not line or line.startswith("#")):
            continue
        continued = line.endswith("\\")
        parts.append(line.removesuffix("\\").rstrip())
        if not continued:
            instructions.append(" ".join(parts))
            parts.clear()
    return instructions


def test_image_metadata_uses_repository_name() -> None:
    for dockerfile, title in (
        (DOCKERFILE, "catalog-api"),
        (PERFORMANCE_DOCKERFILE, "catalog-api-performance"),
    ):
        assert f'org.opencontainers.image.title="{title}"' in dockerfile
        assert "github.com/groovemap-music/catalog-api" in dockerfile


def test_image_metadata_identifies_license_and_exact_source_revision() -> None:
    for dockerfile in (DOCKERFILE, PERFORMANCE_DOCKERFILE):
        assert 'org.opencontainers.image.licenses="AGPL-3.0-only"' in dockerfile
        assert 'org.opencontainers.image.revision="${VCS_REF}"' in dockerfile
        assert '[ "${#VCS_REF}" -eq 40 ]' in dockerfile
    assert "rev-parse --verify 'HEAD^{commit}'" in BUILD_SCRIPT
    assert '--build-arg "VCS_REF=${vcs_ref}"' in BUILD_SCRIPT
    assert "bash scripts/build-image.sh performance/Dockerfile catalog-api-performance:local" in JUSTFILE


def test_runtime_image_provides_revision_to_api_source_metadata() -> None:
    assert 'GROOVEMAP_SOURCE_REVISION="${VCS_REF}"' in DOCKERFILE


def test_performance_image_carries_legal_files() -> None:
    for filename in ("COMMERCIAL-LICENSING.md", "CONTRIBUTING.md", "LICENSE", "NOTICE", "THIRD_PARTY_NOTICES.md"):
        assert filename in PERFORMANCE_DOCKERFILE


def test_runtime_user_is_numeric_and_non_root() -> None:
    users = [line.removeprefix("USER ") for line in _instructions() if line.startswith("USER ")]
    assert users
    assert users[-1] in {"1000:1000", "${UID}:${GID}"}


def test_healthcheck_uses_exec_form() -> None:
    healthchecks = [line for line in _instructions() if line.startswith("HEALTHCHECK ")]
    assert healthchecks
    assert 'CMD ["' in healthchecks[0]


def test_image_does_not_persist_credential_placeholders() -> None:
    for instruction in _instructions():
        if instruction.startswith("ENV "):
            keys = (assignment.split("=", 1)[0] for assignment in shlex.split(instruction.removeprefix("ENV ")))
            assert not [key for key in keys if SENSITIVE_ENV.search(key)]
