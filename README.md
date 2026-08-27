# GrooveMap catalog API

Owns GrooveMap authentication, Discogs OAuth and synchronization, catalog search, graph
queries, recommendations, natural-language queries, internal analytics endpoints, and
operator setup CLIs.

## Development

This service consumes `groovemap-runtime` and `groovemap-agent-tools` from the private
`groovemap-music/python-libraries` repository at immutable commit
`28fa329702bc76896cc54ab8d05ec5b1bd3d929e`. Local setup requires normal Git credential
helper access to that repository.

```bash
mise install
just setup
just check
just image
```

`just check` is the authoritative, credential-free pre-merge gate. It uses fakes and mocks
for external systems. Live integration, load, and deployment checks are deliberately
separate. The performance runner is owned here, while deployment owns its environment and
orchestration.

The source-only GitHub workflow requires no cross-repository credentials. Full CI remains
operator-local until a narrowly installed GitHub App can mint short-lived read access to
the private Python libraries repository; a cross-repository PAT is not accepted.

## Contracts

- Catalog events are promoted from `catalog-ingestion` and verified by digest.
- Persistence compatibility is promoted from `database-schema` and pins the tested runtime.
- The internal Analytics OpenAPI document and generated consumer binding are owned in
  `api/contracts/internal-insights/v1/`; downstream consumers promote the artifact rather
  than this repository writing across a boundary.

## Release and license

This repository versions one service wheel and container image. Commitizen reads the PEP
621 version and uses annotated `v$version` tags. `just release-dry-run` creates local
checksums, an SBOM, notices, and provenance without tagging, pushing, publishing, or
releasing.

The current tree is licensed under PolyForm Noncommercial 1.0.0. Historical revisions
retain their then-applicable license.
