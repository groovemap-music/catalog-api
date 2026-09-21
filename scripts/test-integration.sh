#!/usr/bin/env bash
set -euo pipefail

suffix="$$"
postgres_container="${POSTGRES_INTEGRATION_CONTAINER:-groovemap-catalog-api-postgres-${suffix}}"
neo4j_container="${NEO4J_INTEGRATION_CONTAINER:-groovemap-catalog-api-neo4j-${suffix}}"
postgres_image="${POSTGRES_INTEGRATION_IMAGE:-postgres:18-alpine@sha256:d3e1620b530c944afa6e887d22eb899824da68e19c52024bf98f5220c88a65b2}"
neo4j_image="${NEO4J_INTEGRATION_IMAGE:-neo4j:2026-community@sha256:dbc377fb9cd8fe8dabc19d3041b197d5ca0ef8bae514cea175b8df265e5b7a76}"
password="${CATALOG_INTEGRATION_PASSWORD:-integration-test-password}"
# Which integration suites to run — a space-separated list — and whether the schema
# initializer should declare the `graph.catalog` property graph on top of the graph views.
# Both default to the required tier: the standard suite on PostgreSQL 18 with no property
# graph, where the parity harness's property-graph families skip themselves. `just
# test-integration-pg19` overrides all three of image, targets, and switch together,
# because the SQL/PGQ suites need every one of them.
read -r -a integration_targets <<< "${INTEGRATION_TEST_TARGET:-tests/test_real_databases.py}"
property_graph="${SCHEMA_PROPERTY_GRAPH:-}"

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

POSTGRES_HOST="127.0.0.1:${postgres_port}" \
POSTGRES_DATABASE=groovemap \
POSTGRES_USERNAME=groovemap \
POSTGRES_PASSWORD="${password}" \
NEO4J_HOST="bolt://127.0.0.1:${neo4j_port}" \
NEO4J_USERNAME=neo4j \
NEO4J_PASSWORD="${password}" \
SCHEMA_PROPERTY_GRAPH="${property_graph}" \
    uv run pytest -m integration "${integration_targets[@]}"
