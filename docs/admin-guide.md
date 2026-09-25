# catalog-api administration

This guide documents the administrative API and command-line tools owned by `catalog-api`.
The operator UI is owned by
[`operations-console`](https://github.com/groovemap-music/operations-console), while container
execution and production topology are owned by
[`deployment`](https://github.com/groovemap-music/deployment).

```mermaid
flowchart LR
    Operator[Operator] --> Console[operations-console]
    Console --> AdminAPI[catalog-api admin endpoints]
    AdminAPI --> PostgreSQL[(PostgreSQL)]
    AdminAPI --> RabbitMQ[RabbitMQ management API]
    AdminAPI -->|retained trigger wire| Discogs[discogs-ingestion]
    Discogs --> DiscogsResults[Mounted Discogs results]
    MusicBrainz[musicbrainz-ingestion] --> MusicBrainzResults[Mounted MusicBrainz results]
    DiscogsResults --> Analysis[Extraction-analysis routes]
    MusicBrainzResults --> Analysis
    Analysis --> AdminAPI
```

## Bootstrap an administrator

The packaged `admin-setup` command creates, lists, or updates administrators in PostgreSQL.
It never accepts a password as a command-line argument. Interactive use prompts without echo;
automation must supply `ADMIN_PASSWORD` or `ADMIN_PASSWORD_FILE`.

```bash
admin-setup --email admin@example.com
admin-setup --list
```

Passwords must contain at least eight characters. Running the create command for an existing
email updates its password. The exact container invocation belongs in the deployment runbook.

## Authentication and audit

All administrative endpoints except login require an administrator bearer token.

| Method | Path | Responsibility |
| --- | --- | --- |
| `POST` | `/api/admin/auth/login` | Create an administrator session |
| `POST` | `/api/admin/auth/logout` | Revoke the current session |
| `GET` | `/api/admin/audit-log` | Read the paginated 90-day audit window |
| `GET` | `/api/admin/users/stats` | Aggregate account statistics |
| `GET` | `/api/admin/users/sync-activity` | Recent user synchronization activity |
| `GET` | `/api/admin/storage` | Catalog storage summaries |

Authentication failures do not log email addresses or secrets. Administrative mutations append
an audit entry with the actor, action, target, and non-secret details.

## Extraction control and analysis

`POST /api/admin/extractions/trigger` records a pending run, calls the configured
`EXTRACTOR_HOST` trigger, and tracks progress through its health contract. That retained wire
targets [`discogs-ingestion`](https://github.com/groovemap-music/discogs-ingestion). The
independent [`musicbrainz-ingestion`](https://github.com/groovemap-music/musicbrainz-ingestion)
producer has no Catalog API trigger or scheduling dependency. Each producer owns its extraction
implementation and release schedule; Catalog API owns only the authenticated Discogs trigger,
progress record, local result analysis, and failure translation exposed to clients.

| Method | Path | Responsibility |
| --- | --- | --- |
| `POST` | `/api/admin/extractions/trigger` | Request a forced ingestion run |
| `GET` | `/api/admin/extractions` | List tracked runs |
| `GET` | `/api/admin/extractions/{extraction_id}` | Read one tracked run |
| `GET` | `/api/admin/extraction-analysis/versions` | List locally visible result versions |
| `GET` | `/api/admin/extraction-analysis/{version}/summary` | Summarize validation results |
| `GET` | `/api/admin/extraction-analysis/{version}/violations` | Page through violations |
| `GET` | `/api/admin/extraction-analysis/{version}/violations/{record_id}` | Read one violation record |
| `GET` | `/api/admin/extraction-analysis/{version}/skipped` | Page through skipped records |
| `GET` | `/api/admin/extraction-analysis/{version}/parsing-errors` | Page through parsing errors |
| `GET` | `/api/admin/extraction-analysis/{version}/compare/{other_version}` | Compare two result versions |
| `POST` | `/api/admin/extraction-analysis/{version}/prompt-context` | Build bounded rule context |
| `POST` | `/api/admin/extraction-analysis/{version}/generate-ai-prompt` | Generate a bounded prompt from selected rules |

Mounted extraction result paths are a deployment choice. The API bounds file reads, validates
version and record identifiers, and caches expensive local scans for five minutes. Prompt
requests use `PromptContextRequest`: `rules` contains between 1 and 20 `{rule, entity_type}`
selections, and the API returns bounded, truncated sample context. The route identifiers consumed
by operations-console are pinned in
[`api/contracts/operations-console/v1/routes.json`](../api/contracts/operations-console/v1/routes.json).

## Media mapping coverage

| Method | Path | Responsibility |
| --- | --- | --- |
| `GET` | `/api/admin/media/unmapped` | Report bounded unmapped-media coverage for one provider |

The route accepts `provider=discogs|musicbrainz` and `limit=1..100` and returns the current
`UnmappedMediaResponse`: tagged-release count, releases with unmapped names, a bounded rate, and
the top unmapped format or description names. It reads provider-owned loaded data; it does not
alter either producer's taxonomy or source records.

## Native identity projection

| Method | Path | Responsibility |
| --- | --- | --- |
| `POST` | `/api/admin/identity/project` | Trigger a `gm_id` projection run as a tracked background task |
| `POST` | `/api/admin/identity/reattach` | Census (default) or, with `?apply=true`, re-attach and merge load-order-split catalog items |

Returns `202` with a job id immediately; the projection itself pages the currently-valid
Discogs aliases in `provider_aliases` and sets the additive `gm_id` property on the matching
Neo4j nodes, per catalog kind. See
[Projecting `gm_id` onto Neo4j Nodes](../api/README.md#projecting-gm_id-onto-neo4j-nodes) in
the main README for the job's scope, the equivalent `catalog-identity-projection` CLI, and
[Native identity and first-party activity](identity-and-activity.md) for the identity model
this projects, per
[ADR 0009](https://github.com/groovemap-music/design/blob/main/docs/adr/0009-native-identity-and-provider-aliases.md).

The re-attachment route is a dry run unless `apply=true`: it returns `202` with a job id and
logs the census. The census reports, per kind, what an applying run would merge, including the
artifact and owned-copy rows that `will_move`.

An applying run also merges each split item into its Discogs item, per ADR 0009's native-id
merge. The split item is kept as a supersession. Its artifacts and owned copies move to the
survivor, and each move is ledgered in `catalog_item_moves`. Items with dependents are no
longer skipped: the first run after the dependents guard's removal merges every item earlier
runs skipped. Compare its `merged_with_dependents` count against their
`guard_reasons.dependents`.

The run writes one `identity.reattach.apply` audit entry, with the job id as its row id. Every
supersession the run opens names that row as its `decision_ref`. The entry holds per-kind,
per-table counts only, with no user or row ids. A run that fails is recorded under the same id
as `identity.reattach.failed`. Run the projection above afterwards. See
[Re-attaching Load-Order-Split Catalog Items](../api/README.md#re-attaching-load-order-split-catalog-items)
for the rule, the merge, the guards, and the `catalog-identity-reattach` CLI, per
[ADR 0014 section 8](https://github.com/groovemap-music/design/blob/main/docs/adr/0014-cross-catalog-edition-candidates.md)
and its 2026-09-25 amendment.

## Dead-letter queue purge

`POST /api/admin/dlq/purge/{queue}` purges one queue only when its name is generated by the
versioned catalog-event contract. A purge is permanent and creates an audit entry. The API owns
the allowlist and RabbitMQ management call; processing behavior belongs to the consumers:

- [`discogs-graph-enricher`](https://github.com/groovemap-music/discogs-graph-enricher)
- [`discogs-sql-loader`](https://github.com/groovemap-music/discogs-sql-loader)
- [`musicbrainz-graph-enricher`](https://github.com/groovemap-music/musicbrainz-graph-enricher)
- [`musicbrainz-sql-loader`](https://github.com/groovemap-music/musicbrainz-sql-loader)

Operators should diagnose and correct the consumer failure before purging. Replay and queue
recovery procedures belong to the relevant consumer and deployment runbooks.

## Metrics history

The catalog API samples request latency, queue depth, and configured health endpoints, then stores
history in PostgreSQL. Collection failures return an empty sample rather than terminating the API.

| Method | Path | Responsibility |
| --- | --- | --- |
| `GET` | `/api/admin/queues/history` | Queue depth and rate history |
| `GET` | `/api/admin/health/history` | Health and response-time history |

Both history routes accept `range`; supported values are `1h`, `6h`, `24h`, `7d`, `30d`, `90d`,
and `365d`.
`METRICS_COLLECTION_INTERVAL` defaults to 300 seconds and `METRICS_RETENTION_DAYS` defaults to
366 days. The presentation of these records belongs to `operations-console`.

The metrics and audit table definitions are owned by
[`database-schema`](https://github.com/groovemap-music/database-schema). Apply schema changes
through that repository before deploying catalog-api code that depends on them.
