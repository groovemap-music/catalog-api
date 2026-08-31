"""Static regression tests for the repository-owned runtime image."""

import re
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).parent.parent
DOCKERFILE = (ROOT / "Dockerfile").read_text()
PERFORMANCE_DOCKERFILE = (ROOT / "performance" / "Dockerfile").read_text()
BUILD_SCRIPT = (ROOT / "scripts" / "build-image.sh").read_text()
JUSTFILE = (ROOT / "Justfile").read_text()
SENSITIVE_ENV = re.compile(r"(?:PASSWORD|USERNAME|SECRET|TOKEN|CREDENTIAL|PRIVATE_KEY)(?:$|_)")


def _required_executable(name: str) -> str:
    executable = shutil.which(name)
    if executable is None:
        raise RuntimeError(f"required test executable is unavailable: {name}")
    return executable


GIT = _required_executable("git")
BASH = _required_executable("bash")


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
    assert 'bash "${repo_root}/scripts/check-image-source.sh"' in BUILD_SCRIPT
    assert '--build-arg "VCS_REF=${vcs_ref}"' in BUILD_SCRIPT
    assert "bash scripts/build-image.sh performance/Dockerfile catalog-api-performance:local" in JUSTFILE


def _image_source_repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repository"
    scripts = repository / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy(ROOT / "scripts" / "check-image-source.sh", scripts)
    (repository / "tracked.txt").write_text("committed\n")
    subprocess.run([GIT, "init", "--quiet"], cwd=repository, check=True)  # noqa: S603
    subprocess.run([GIT, "add", "."], cwd=repository, check=True)  # noqa: S603
    subprocess.run(  # noqa: S603
        [
            GIT,
            "-c",
            "user.name=GrooveMap Test",
            "-c",
            "user.email=test@groovemap.music",
            "commit",
            "--quiet",
            "-m",
            "test: establish image source boundary",
        ],
        cwd=repository,
        check=True,
    )
    return repository


def test_image_source_allows_ci_dependency_checkout_and_build_artifacts(tmp_path: Path) -> None:
    repository = _image_source_repository(tmp_path)
    (repository / ".gitignore").write_text("dist/\n.build/\n")
    subprocess.run([GIT, "add", ".gitignore"], cwd=repository, check=True)  # noqa: S603
    subprocess.run(  # noqa: S603
        [
            GIT,
            "-c",
            "user.name=GrooveMap Test",
            "-c",
            "user.email=test@groovemap.music",
            "commit",
            "--quiet",
            "-m",
            "test: ignore generated artifacts",
        ],
        cwd=repository,
        check=True,
    )
    dependency = repository / "python-libraries"
    dependency.mkdir()
    (dependency / "README.md").write_text("workflow dependency\n")
    (repository / "dist").mkdir()
    (repository / "dist" / "package.whl").write_text("generated\n")
    (repository / ".build").mkdir()
    (repository / ".build" / "runtime.whl").write_text("generated\n")

    subprocess.run([BASH, "scripts/check-image-source.sh"], cwd=repository, check=True)  # noqa: S603


@pytest.mark.parametrize("staged", [False, True], ids=["unstaged", "staged"])
def test_image_source_rejects_modified_tracked_source(tmp_path: Path, staged: bool) -> None:
    repository = _image_source_repository(tmp_path)
    (repository / "tracked.txt").write_text("modified\n")
    if staged:
        subprocess.run([GIT, "add", "tracked.txt"], cwd=repository, check=True)  # noqa: S603

    result = subprocess.run(  # noqa: S603
        [BASH, "scripts/check-image-source.sh"],
        cwd=repository,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "modified tracked source" in result.stderr


def test_image_source_rejects_repository_without_commit(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    scripts = repository / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy(ROOT / "scripts" / "check-image-source.sh", scripts)
    subprocess.run([GIT, "init", "--quiet"], cwd=repository, check=True)  # noqa: S603

    result = subprocess.run(  # noqa: S603
        [BASH, "scripts/check-image-source.sh"],
        cwd=repository,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "without a verifiable source revision" in result.stderr


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
