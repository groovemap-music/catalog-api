# Catalog API contracts

See the repository [documentation index](../../docs/README.md) for producer and consumer
ownership.

`catalog-api` owns the HTTP and OpenAPI contracts in this directory. The internal
Insights surface is versioned separately because `analytics-engine` is an independent
release unit. Generate its pinned consumer constants with:

```bash
uv run python api/contracts/generate.py
```

Breaking path or envelope changes require a new contract version and a coordinated
rollout. Authentication material is deliberately absent: the contract states shape,
not deployment secrets.
# Consumer contracts

Producer-owned, versioned compatibility surfaces for independently deployed GrooveMap consumers:

- `graph-explorer/v1/routes.json` records the Catalog API methods and paths used by the public graph application.
- `mcp-server/v1/routes.json` records the API surface exposed to MCP clients.
- `operations-console/v1/routes.json` records the privileged console surface.
- `internal-insights/v1/openapi.yaml` records the analytics engine surface.

Consumers promote an immutable copy plus source commit and digest. Changes remain compatible within a contract version; breaking changes require a new version.
