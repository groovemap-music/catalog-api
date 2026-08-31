# catalog-api logging

This guide covers the logging boundary implemented by `catalog-api`. Organization-wide
observability, log collection, retention, and alerting belong to the
[`deployment` repository](https://github.com/groovemap-music/deployment).

```mermaid
flowchart LR
    Request[Request or background task] --> Logger[catalog-api logger]
    Logger --> Stdout[stdout and stderr]
    Logger --> File[/logs/api.log]
    Stdout --> Runtime[container runtime]
    File --> Runtime
```

## Configuration

`LOG_LEVEL` controls both application logging and Uvicorn's configured level. It defaults to
`INFO` and is normalized to lowercase for Uvicorn.

```bash
LOG_LEVEL=DEBUG uv run catalog-api
LOG_LEVEL=INFO uv run catalog-api
LOG_LEVEL=WARNING uv run catalog-api
```

The application initializes the shared runtime with `setup_logging("api",
log_file=Path("/logs/api.log"))`. Repository code should obtain module loggers through the
existing logging conventions and must never call `logging.basicConfig()` at import time.

## Event conventions

Log messages use the catalog API's emoji vocabulary so lifecycle, success, failure, and data
events remain visually distinct. Use one emoji followed by one space, then a stable event phrase.
See the [emoji guide](emoji-guide.md) for the canonical mapping.

```python
logger.info("🚀 Starting catalog-api")
logger.info("✅ Catalog synchronization complete")
logger.warning("⚠️ Discogs request will be retried")
logger.error("❌ Catalog synchronization failed")
```

Prefer structured fields over interpolating identifiers into the event name. Never log passwords,
OAuth tokens, JWTs, reset links, API keys, connection strings, secret-file contents, or raw
authorization headers. User email addresses are also excluded from authentication success and
failure events.

## Query diagnostics

The query helpers emit diagnostic details at `DEBUG`. Enable that level only for a bounded
troubleshooting window because query and request volume can be high. Keep parameters redacted and
return to `INFO` or `WARNING` afterward.

```bash
LOG_LEVEL=DEBUG
```

## Runtime inspection

Container names and aggregation commands are deployment concerns. From a deployment checkout,
use the container-runtime command documented there to inspect the `catalog-api` service. For a
standalone local process, inspect stdout/stderr and `/logs/api.log` if that path is mounted.

If changing `LOG_LEVEL` appears ineffective:

1. Verify the variable reaches the `catalog-api` process.
1. Restart the process after changing its environment.
1. Check for a narrower logger-level override in the code under test.
1. Confirm the deployment log collector is not filtering the requested level.

Operational monitoring and incident-response procedures are maintained with the deployment that
actually owns those integrations.
