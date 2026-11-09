# Catalog API contracts

See the repository [documentation index](../../docs/README.md) for producer and consumer
ownership.

`catalog-api` owns the HTTP and OpenAPI contracts in this directory. The internal
Insights surface is versioned separately because `analytics-engine` is an independent
release unit. Generate its pinned consumer constants, together with the
`operations-console` route constants, with:

```bash
uv run python api/contracts/generate.py
```

`--check` verifies the committed bindings instead of writing them. After regenerating,
update the matching `python/source.json` digests; `just contract-check` verifies both
the contract and its binding against that record.

Breaking path or envelope changes require a new contract version and a coordinated
rollout. Authentication material is deliberately absent: the contract states shape,
not deployment secrets.

## Graph Explorer v1 promotion

The producer registry is `graph-explorer/v1/routes.json`. The reviewed source revision for
this change is `fab7e993384f032a0ea78817cbdcba798a2e97f8`, and its contract SHA-256 is
`3726ff54d5d76ee3c0bc91d1b0c40314e9fa63def7c1ce7017c06f53569c69d7`. This source revision
is immutable and will be an ancestor of `main` after the bead is merged. A consumer must
verify that ancestry and digest before promoting; source integration does not imply deployment.
Run these commands from the consumer checkout, with `CATALOG_API_REPO` set to a local checkout
of the producer repository:

```bash
CATALOG_API_REPO=/path/to/groovemap-music/catalog-api
git -C "$CATALOG_API_REPO" merge-base --is-ancestor fab7e993384f032a0ea78817cbdcba798a2e97f8 main
git -C "$CATALOG_API_REPO" show fab7e993384f032a0ea78817cbdcba798a2e97f8:api/contracts/graph-explorer/v1/routes.json > /tmp/graph-explorer-routes-v1.json
printf '%s  %s\n' '3726ff54d5d76ee3c0bc91d1b0c40314e9fa63def7c1ce7017c06f53569c69d7' /tmp/graph-explorer-routes-v1.json | shasum -a 256 -c -
```

Promote those exact bytes with the source repository, revision, and verified digest in
the consumer's provenance record. The existing observation GET/POST operations remain in
the registry with unchanged semantics; the new copy-list route only enumerates the current
session owner's copies and does not change observation timeline ordering or writes.
# Consumer contracts

Producer-owned, versioned compatibility surfaces for independently deployed GrooveMap consumers:

- `graph-explorer/v1/routes.json` records the Catalog API methods and paths used by the public graph application.
- `mcp-server/v1/routes.json` records the API surface exposed to MCP clients.
- `operations-console/v1/routes.json` records the privileged console surface.
- `internal-insights/v2/openapi.yaml` records the analytics engine surface. v2 removes the whole-catalog
  `rarity-scores` route; `internal-insights/v1` stays frozen as the copy analytics-engine pins.

Consumers promote an immutable copy plus source commit and digest. Changes remain compatible within a contract version; breaking changes require a new version.
