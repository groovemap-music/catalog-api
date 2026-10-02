set shell := ["bash", "-euo", "pipefail", "-c"]

default:
    @just --list

setup:
    uv sync --dev --frozen

source-check: format-check lint contract-check

format-check:
    uv run ruff format --check .

lint:
    uv run ruff check .

contract-check:
    uv run python scripts/check-contracts.py

secret-scan:
    gitleaks git --redact --no-banner
    gitleaks dir . --redact --no-banner

ci-check: source-check typecheck coverage bump-preview

check: ci-check secret-scan build install-check license-check

format:
    uv run ruff format .
    uv run ruff check --fix .

typecheck:
    uv run mypy

test:
    uv run pytest -m "not integration" --cov=api --cov-report=term-missing --cov-report=xml

test-integration:
    bash scripts/test-integration.sh

coverage: test

# Run the offline evaluation harness against the committed golden set.
evaluate:
    uv run python -m api.evaluation.report

# Regenerate the committed synthetic golden set (deterministic; commit the result).
generate-golden-set:
    uv run python scripts/generate_golden_set.py

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
    uv run python scripts/check_bump_preview.py

# Update local version metadata and changelog only; do not commit, tag, push, or publish.
bump:
    uv run cz bump --version-files-only --changelog --yes --check-consistency
    uv lock

performance-image: prepare-private-wheels
    bash scripts/build-image.sh performance/Dockerfile catalog-api-performance:local

release-dry-run: check prepare-private-wheels
    bash scripts/release-dry-run.sh
