"""Regression tests for the repository's supported Python runtime contract."""

import re
import tomllib
from datetime import datetime
from pathlib import Path
from typing import get_type_hints
from uuid import UUID

from api.models import UserResponse


ROOT = Path(__file__).parent.parent
PYTHON_VERSION = "3.14.5"
PYTHON_MINOR = "3.14"
PYTHON_IMAGE = f"python:{PYTHON_VERSION}-slim"
PYTHON_BASE_NAME = f"docker.io/library/{PYTHON_IMAGE}"


def _toml(path: str) -> dict[str, object]:
    with (ROOT / path).open("rb") as stream:
        return tomllib.load(stream)


def _assert_pinned_python_image(dockerfile: Path) -> None:
    contents = dockerfile.read_text()
    assert re.search(rf"(?:ARG PYTHON_IMAGE=|FROM ){re.escape(PYTHON_IMAGE)}@sha256:[0-9a-f]{{64}}", contents)
    assert f'org.opencontainers.image.base.name="{PYTHON_BASE_NAME}"' in contents


def test_python_project_and_tooling_share_one_minor_line() -> None:
    project = _toml("pyproject.toml")
    project_metadata = project["project"]
    tools = project["tool"]

    assert isinstance(project_metadata, dict)
    assert isinstance(tools, dict)
    assert project_metadata["requires-python"] == f">={PYTHON_MINOR},<3.15"
    assert "Programming Language :: Python :: 3.14" in project_metadata["classifiers"]
    assert not any("Python :: 3.13" in classifier for classifier in project_metadata["classifiers"])
    assert tools["ruff"]["target-version"] == "py314"
    assert tools["mypy"]["python_version"] == PYTHON_MINOR
    assert "docs/superpowers" in tools["ruff"]["extend-exclude"]


def test_managed_runtime_uses_the_approved_patch() -> None:
    mise = _toml(".mise.toml")
    lock = _toml("uv.lock")
    assert mise["tools"]["python"] == PYTHON_VERSION
    assert lock["requires-python"] == "==3.14.*"


def test_runtime_images_and_annotations_match_the_managed_runtime() -> None:
    _assert_pinned_python_image(ROOT / "Dockerfile")
    _assert_pinned_python_image(ROOT / "performance/Dockerfile")


def test_runtime_annotations_remain_resolvable() -> None:
    hints = get_type_hints(UserResponse)
    assert hints["id"] is UUID
    assert hints["created_at"] is datetime


def test_active_documentation_does_not_advertise_python_313() -> None:
    active_documents = [ROOT / "README.md", ROOT / "api/README.md"]
    active_documents.extend(path for path in (ROOT / "docs").glob("*.md") if path.is_file())

    stale_claims = [str(path.relative_to(ROOT)) for path in active_documents if re.search(r"(?:Python )?3\.13\+?", path.read_text())]
    assert stale_claims == []
