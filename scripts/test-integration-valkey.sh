#!/usr/bin/env bash
# Opt-in verification of the Valkey write-rejection paths against REAL servers
# (gm-catalog-api-u5i2). Local throwaway containers only: nothing is deployed
# or pushed. The pytest fixtures start and always remove the containers; this
# wrapper pre-pulls the pinned images (so the per-test timeout never covers a
# pull) and fails if any labelled container survives the run.
set -euo pipefail

label="gm-catalog-api-valkey-it"
valkey_image="${VALKEY_INTEGRATION_IMAGE:-valkey/valkey:9.1.2-alpine@sha256:48332870af354a799964c0012ae1194a0bf2bf894eb508f945810596dc2d8d11}"
postgres_image="${POSTGRES_INTEGRATION_IMAGE:-postgres:18-alpine@sha256:d3e1620b530c944afa6e887d22eb899824da68e19c52024bf98f5220c88a65b2}"

if ! docker info >/dev/null 2>&1; then
    echo "docker is unavailable; skipping the opt-in Valkey integration tests" >&2
    exit 0
fi

docker pull --quiet "${valkey_image}" >/dev/null
docker pull --quiet "${postgres_image}" >/dev/null

status=0
CATALOG_VALKEY_INTEGRATION=1 \
VALKEY_INTEGRATION_IMAGE="${valkey_image}" \
POSTGRES_INTEGRATION_IMAGE="${postgres_image}" \
    uv run pytest -m valkey_noeviction -v -p no:cacheprovider tests/test_valkey_noeviction_real.py "$@" || status=$?

leftover="$(docker ps --all --quiet --filter "label=${label}")"
if [[ -n "${leftover}" ]]; then
    echo "containers labelled ${label} were left behind:" >&2
    docker ps --all --filter "label=${label}" >&2
    docker rm --force --volumes ${leftover} >/dev/null
    exit 1
fi
echo "no ${label} containers left (docker ps -a)"
exit "${status}"
