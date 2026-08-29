set shell := ["bash", "-euo", "pipefail", "-c"]

default:
    @just --list

setup:
    uv sync --dev --frozen

source-check:
    uvx --from ruff==0.16.4 ruff format --check .
    uvx --from ruff==0.16.4 ruff check .
    python scripts/check-contracts.py
    gitleaks git --redact --no-banner
    gitleaks dir . --redact --no-banner

check: source-check typecheck test build install-check license-check bump-preview

format:
    uv run ruff format .
    uv run ruff check --fix .

typecheck:
    uv run mypy

test:
    uv run pytest --cov=api --cov-report=term-missing --cov-report=xml

build:
    uv build --out-dir dist --clear

install-check: build
    bash scripts/install-check.sh

license-check: build
    uv run python scripts/check-license.py
    uv run pip-licenses --format=json | uv run python scripts/check_dependency_licenses.py

audit:
    uv run pip-audit

prepare-private-wheels:
    bash scripts/prepare-runtime-wheel.sh

image: build prepare-private-wheels
    bash scripts/build-image.sh
    docker run --rm --entrypoint /app/.venv/bin/python catalog-api:local -c 'import api.api; import api.config'
    test "$(docker run --rm --entrypoint /usr/bin/id catalog-api:local -u):$(docker run --rm --entrypoint /usr/bin/id catalog-api:local -g)" = "1000:1000"

bump-preview:
    uv run cz bump --dry-run --changelog --yes --check-consistency

# Update local version metadata and changelog only; do not commit, tag, push, or publish.
bump:
    uv run cz bump --version-files-only --changelog --yes --check-consistency
    uv lock

performance-image: prepare-private-wheels
    bash scripts/build-image.sh performance/Dockerfile catalog-api-performance:local

release-dry-run: check
    bash scripts/release-dry-run.sh
