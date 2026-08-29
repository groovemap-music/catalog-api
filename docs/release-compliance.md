# Release compliance

The repository has one wheel and two OCI images: `catalog-api` and
`catalog-api-performance`. All release work is local or a dry run until an operator separately
approves a version tag and publication.

```mermaid
flowchart TD
    Change[Pull request, main push, schedule, or Dependabot] --> CI[Required shared CI]
    CI --> Tests[Tests and coverage]
    CI --> Policy[Audit, licenses, and secret scans]
    CI --> Artifacts[Wheel and install smoke test]
    CI --> Images[Both local images]
    Tag[Separately approved version tag] --> Release[Shared release workflow]
    Release --> Evidence[Checksums, notices, SBOM, provenance]
    Release --> Primary[catalog-api image]
    Release --> Performance[catalog-api-performance image]
```

## Local gates

- `just check` runs formatting, linting, type checks, the complete test suite, secret scans,
  wheel construction, installed-wheel smoke tests, dependency-license policy, and version checks.
- `just audit` checks the locked environment for known vulnerabilities.
- `just coverage` produces `coverage.xml`, which CI always retains and uploads to Codecov.
- `just image` and `just performance-image` build the repository-named local images from a clean
  commit and inject the exact source revision.
- `just release-dry-run` creates the wheel, source archive, checksums, third-party notice, SBOM,
  and provenance locally. It does not commit, tag, push, publish, or create a release.

The thin workflow callers pin `groovemap-music/automation` by a reviewed forty-character commit.
They also pin the private Python-library checkout. Pull requests from Dependabot use the ordinary
`pull_request` event and the same `required` job, commands, private-library gate, and result job as
every other pull request.

## Publication boundary

The tag workflow is dormant until a separately approved `v*` tag exists. Repository visibility,
tag creation, GHCR publication, and release creation are not performed by local validation.
