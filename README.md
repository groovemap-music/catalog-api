# GrooveMap catalog API

Owns GrooveMap authentication, Discogs OAuth and synchronization, catalog search, graph
queries, recommendations, natural-language queries, internal analytics endpoints, and
operator setup CLIs.

## Development

This service consumes `groovemap-runtime` and `groovemap-agent-tools` from the private
`groovemap-music/python-libraries` repository. Local setup requires normal Git credential
helper access to that repository; the lockfile records the reviewed source revision.

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

The current tree is licensed under the [GNU Affero General Public License v3 only](LICENSE).
The AGPL permits commercial use when its terms are followed; commercial use by itself does
not require a separate license. Copyright holders may negotiate optional alternative terms
with parties that do not want to use the software under the AGPL; see
[commercial licensing](COMMERCIAL-LICENSING.md). [NOTICE](NOTICE) records prior-license
history.

External code and documentation contributions are paused until the project adopts a
relicensing-capable contributor license agreement. See [CONTRIBUTING.md](CONTRIBUTING.md).

## Documentation

See the [documentation index](docs/README.md) for configuration, administration,
performance, examples, and retained design records.
