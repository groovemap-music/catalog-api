#!/usr/bin/env bash
set -euo pipefail

suffix="$(uuidgen | tr -d '-' | cut -c1-12)"
container="gm-catalog-api-rarity-sentinel-it-${suffix}"
password="rarity-sentinel-${suffix}"
image="${NEO4J_INTEGRATION_IMAGE:-neo4j:2026-community@sha256:dbc377fb9cd8fe8dabc19d3041b197d5ca0ef8bae514cea175b8df265e5b7a76}"

cleanup() {
    docker rm --force --volumes "${container}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker run --detach --rm \
    --name "${container}" \
    --label "com.groovemap.test=rarity-sentinel-control" \
    --publish 127.0.0.1::7687 \
    --env "NEO4J_AUTH=neo4j/${password}" \
    --env NEO4J_server_memory_heap_initial__size=256m \
    --env NEO4J_server_memory_heap_max__size=256m \
    "${image}" >/dev/null

ready=false
for _attempt in $(seq 1 60); do
    if docker exec "${container}" cypher-shell --username neo4j --password "${password}" "RETURN 1" >/dev/null 2>&1; then
        ready=true
        break
    fi
    sleep 2
done

if [[ "${ready}" != true ]]; then
    docker logs "${container}" >&2
    echo "Owned disposable Neo4j did not become ready within 120 seconds" >&2
    exit 1
fi

published="$(docker port "${container}" 7687/tcp)"
port="${published##*:}"
NEO4J_HOST="bolt://127.0.0.1:${port}" \
NEO4J_USERNAME=neo4j \
NEO4J_PASSWORD="${password}" \
    uv run python -m tests.rarity_sentinel_neo4j_control
