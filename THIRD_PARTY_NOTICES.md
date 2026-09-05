# Third-party dependency notices

GrooveMap Catalog API depends on third-party software. Upstream license terms control;
this file records the distribution boundary and the obligations that must accompany
releases. `uv.lock` is the authoritative version lock, and
`scripts/release-dry-run.sh` writes a complete machine-readable
`THIRD_PARTY_NOTICES.json` from an isolated installation of the runtime wheel.

The catalog-api wheel declares dependencies but does not bundle their code. The OCI
images install locked runtime dependencies and retain the license files supplied in each
package's `.dist-info` directory. The optional alternative licensing described in
`COMMERCIAL-LICENSING.md` applies only to first-party rights held by the participating
copyright holders; it does not change any third-party license.

## Reciprocal-license runtime dependencies

- `certifi` 2026.7.22 is licensed under `MPL-2.0` and is included through the HTTP
  clients. Source: <https://github.com/certifi/python-certifi>.
- `orjson` 3.12.0 declares `MPL-2.0 AND (Apache-2.0 OR MIT)` and is a direct runtime
  dependency. Source: <https://github.com/ijl/orjson>.
- `psycopg` 3.3.5 and `psycopg-binary` 3.3.5 are licensed under `LGPL-3.0-only` and
  provide PostgreSQL access. Source: <https://github.com/psycopg/psycopg>.

For MPL-covered files, preserve license and copyright notices and make the source form of
any distributed modifications to those files available under MPL-2.0. For the
LGPL-covered Psycopg libraries, preserve their notices and license texts, provide the
applicable covered source, and do not prevent replacement or reverse engineering for
debugging modifications. Reassess these obligations before modifying, statically
combining, vendoring, or changing how any covered dependency is distributed.

## Build and test dependencies

The locked development environment also includes `chardet` 5.2.0 under
`LGPL-2.1-or-later`, plus `fqdn` 1.5.1, `hypothesis` 6.167.1, and `pathspec` 1.1.1
under `MPL-2.0`. They are tools or transitive development dependencies and are not
installed in the runtime images. Their distribution obligations must be reassessed if
that boundary changes.

## Other licenses

The remaining locked dependencies primarily use permissive `MIT`, BSD, ISC, Python, or
`Apache-2.0` terms. Preserve their copyright, attribution, license, and upstream NOTICE
material as required. The release-generated JSON inventory records the exact package
versions, declared licenses, upstream URLs, and available license texts for review
before distribution.
