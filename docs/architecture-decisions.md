# Catalog API architecture decisions

This record preserves the reusable conclusions from implementation planning without carrying
task transcripts, internal execution prompts, or private planning paths into the public
repository surface.

```mermaid
flowchart LR
    DI[discogs-ingestion] -->|Discogs v1 events| EventContracts[Promoted catalog-event contracts]
    MI[musicbrainz-ingestion] -->|MusicBrainz v1 events| EventContracts
    EventContracts --> Loaders[Source-matching graph and SQL loaders]
    Loaders --> Stores[(PostgreSQL and Neo4j)]
    Clients[GrooveMap consumers] --> API[catalog-api]
    API --> Stores
    API --> Cache[(Redis)]
    API --> HTTPContracts[Versioned HTTP contracts]
    HTTPContracts --> Clients
```

## Accepted decisions

- Keep authentication, catalog search, graph exploration, recommendation, NLQ, analytics
  computation, and operator endpoints together because they share one identity and persistence
  boundary. Consumers use versioned HTTP contracts rather than source imports.
- Follow [ADR 0005](https://github.com/groovemap-music/design/blob/main/docs/adr/0005-source-owned-catalog-ingestion.md):
  `discogs-ingestion` and `musicbrainz-ingestion` independently own their source acquisition,
  event contracts, images, releases, and schedules. Catalog API promotes both v1 contracts and
  does not coordinate the producers. Its retained `EXTRACTOR_HOST` administrative trigger is the
  Discogs compatibility endpoint, not a combined ingestion service.
- Keep application construction side-effect free. FastAPI lifespan owns configuration and
  adapter creation, router wiring, background tasks, and reverse-order shutdown; routers depend
  on the configured capabilities while public module and route entry points stay stable.
- Keep database retry, TLS, and query instrumentation in `groovemap-runtime`; this repository
  pins the tested runtime revision and owns query behavior, bounds, and API error mapping.
- Bound expensive graph and enrichment work at the API edge. Pagination, maximum path depth,
  batch size, and timeout classification are contract behavior and have regression coverage.
- Keep NLQ data access behind typed tools. Streaming and non-streaming responses expose the same
  result keys, and interrupted streams cancel their engine work.
- Keep release rarity and community enrichment as internal authenticated API operations. The
  analytics scheduler calls the contract; it does not import catalog implementation modules.
- Keep password reset and optional TOTP inside the catalog identity boundary. Transactional mail
  uses the notification-channel interface and sends through Resend over HTTP without a vendor SDK.
- Build the performance runner as the repository-named `catalog-api-performance` image. Runtime
  deployment and environment orchestration remain outside this repository.
- Follow [ADR 0009](https://github.com/groovemap-music/design/blob/main/docs/adr/0009-native-identity-and-provider-aliases.md):
  key every catalog entity by a native id that `provider_aliases` maps a provider's id onto;
  mint only on the write side (the Discogs sync), never from a read path. See
  [Native identity and first-party activity](identity-and-activity.md).
- Follow [ADR 0010](https://github.com/groovemap-music/design/blob/main/docs/adr/0010-first-party-activity-events.md):
  record first-party behavioural events and recommendation impressions in process, pseudonymised
  by subject, against the closed published vocabulary; consent, erasure, and export are this
  repository's endpoints over that same record. See
  [Native identity and first-party activity](identity-and-activity.md).

Historical references to the combined `catalog-ingestion` repository describe the pre-split
lineage retained by ADR 0005. They are migration records, not the name of a current producer or
an active ownership boundary.

## Superseded planning material

Detailed planning transcripts under `docs/superpowers/` are not part of the intended public
documentation contract. Their useful conclusions are represented here and in the focused guides
linked from the documentation index. Removing those paths from every historical object requires
the separately approved procedure in [the history rewrite gate](history-rewrite-gate.md).
