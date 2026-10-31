#!/usr/bin/env bash
set -euo pipefail

suffix="$$"
postgres_container="${POSTGRES_INTEGRATION_CONTAINER:-groovemap-catalog-api-postgres-${suffix}}"
neo4j_container="${NEO4J_INTEGRATION_CONTAINER:-groovemap-catalog-api-neo4j-${suffix}}"
postgres_image="${POSTGRES_INTEGRATION_IMAGE:-postgres:18-alpine@sha256:d3e1620b530c944afa6e887d22eb899824da68e19c52024bf98f5220c88a65b2}"
neo4j_image="${NEO4J_INTEGRATION_IMAGE:-neo4j:2026-community@sha256:dbc377fb9cd8fe8dabc19d3041b197d5ca0ef8bae514cea175b8df265e5b7a76}"
password="${CATALOG_INTEGRATION_PASSWORD:-integration-test-password}"

cleanup() {
    docker rm --force --volumes "${postgres_container}" "${neo4j_container}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker run --detach --rm \
    --name "${postgres_container}" \
    --publish 127.0.0.1::5432 \
    --env POSTGRES_USER=groovemap \
    --env "POSTGRES_PASSWORD=${password}" \
    --env POSTGRES_DB=groovemap \
    "${postgres_image}" >/dev/null

docker run --detach --rm \
    --name "${neo4j_container}" \
    --publish 127.0.0.1::7687 \
    --env "NEO4J_AUTH=neo4j/${password}" \
    --env NEO4J_server_memory_heap_initial__size=256m \
    --env NEO4J_server_memory_heap_max__size=256m \
    "${neo4j_image}" >/dev/null

postgres_ready=false
neo4j_ready=false
for _attempt in $(seq 1 60); do
    if docker exec "${postgres_container}" pg_isready --username groovemap --dbname groovemap >/dev/null 2>&1; then
        postgres_ready=true
    fi
    if docker exec "${neo4j_container}" cypher-shell --username neo4j --password "${password}" "RETURN 1" >/dev/null 2>&1; then
        neo4j_ready=true
    fi
    if [[ "${postgres_ready}" == true && "${neo4j_ready}" == true ]]; then
        break
    fi
    sleep 2
done

if [[ "${postgres_ready}" != true ]]; then
    docker logs "${postgres_container}" >&2
    echo "PostgreSQL did not become ready within 120 seconds" >&2
    exit 1
fi
if [[ "${neo4j_ready}" != true ]]; then
    docker logs "${neo4j_container}" >&2
    echo "Neo4j did not become ready within 120 seconds" >&2
    exit 1
fi

postgres_published="$(docker port "${postgres_container}" 5432/tcp)"
postgres_port="${postgres_published##*:}"
neo4j_published="$(docker port "${neo4j_container}" 7687/tcp)"
neo4j_port="${neo4j_published##*:}"

# The image's POSTGRES_USER is a superuser. The suite runs as a separate, unprivileged runtime role
# that owns the database (the least-privilege provisioning in database-schema's
# docs/runtime-configuration.md), so a statement that needs superuser fails here instead of in
# production. The superuser only prepares what a plain role cannot: the extension is trusted
# (PostgreSQL 13+) but the role must own the database, and the harness checks it is not a superuser.
runtime_role="${CATALOG_INTEGRATION_RUNTIME_ROLE:-catalog_runtime}"
docker exec "${postgres_container}" psql --username groovemap --dbname postgres --set ON_ERROR_STOP=1 \
    --command "CREATE ROLE ${runtime_role} LOGIN PASSWORD '${password}' NOSUPERUSER NOCREATEDB NOCREATEROLE" \
    --command "ALTER DATABASE groovemap OWNER TO ${runtime_role}" >/dev/null
is_super="$(docker exec "${postgres_container}" psql --username groovemap --dbname groovemap --tuples-only --no-align \
    --command "SELECT rolsuper OR rolcreaterole OR rolcreatedb FROM pg_roles WHERE rolname = '${runtime_role}'")"
if [[ "${is_super}" != "f" ]]; then
    echo "runtime role ${runtime_role} is not an unprivileged role" >&2
    exit 1
fi

POSTGRES_HOST="127.0.0.1:${postgres_port}" \
POSTGRES_DATABASE=groovemap \
POSTGRES_USERNAME="${runtime_role}" \
POSTGRES_PASSWORD="${password}" \
NEO4J_HOST="bolt://127.0.0.1:${neo4j_port}" \
NEO4J_USERNAME=neo4j \
NEO4J_PASSWORD="${password}" \
    uv run pytest -m integration \
        tests/test_real_databases.py \
        tests/test_reattach_integration.py \
        tests/test_reattach_trigger_integration.py \
        tests/test_ledger_backfill_integration.py \
        tests/test_identity_ledger_integration.py \
        tests/test_lookup_integration.py \
        tests/test_activity_dedup_integration.py \
        tests/test_activity_request_keys_integration.py \
        tests/test_similar_precomputed_integration.py \
        tests/test_role_leaderboard_integration.py
