# Test assertion audit

The repository-local audit at `scripts/audit_test_assertions.py` inventories pytest-style
test functions that either contain no recognized outcome assertion or rely exclusively on
mock call/await assertions. It recognizes Python `assert`, unittest-style `assert*`
methods, `pytest.raises`/`warns`/`deprecated_call`, and the standard `Mock`/`AsyncMock`
call assertion methods.

Run the audit from the repository root:

```console
uv run python scripts/audit_test_assertions.py --json
```

## Fixture-hardening audit

| Revision | Test functions | Assertion-free | Call-only |
| --- | ---: | ---: | ---: |
| Before (`3151db4`) | 2,177 | 20 | 24 |
| After (`gm-catalog-api-e2w.1`) | 2,180 | 20 | 24 |

The three added fixture-contract tests account for the test-function increase. Tightening
the shared and NLQ database mocks exposed no runtime-interface failures, so there are no
latent product failures to file. The 44 pre-existing weak-assertion findings remain visible
in the JSON audit output and are owned by follow-up bead `gm-catalog-api-87h`.

## Finding triage — 2026-10-02

The exact fixture-hardening revision `2199555de775b9faa6c140da107dd1de9f725f08`
contains the original 44 findings. At the start of this follow-up the current tree
had 2,901 test functions and 56 findings: the same 44 plus 12 later findings.
[The triage inventory](test-assertion-triage.json) records every test's class-qualified
identity, original category, origin, disposition, and individual rationale.

Fifteen tests now assert outcomes rather than only successful execution or calls:

- Extraction tracking checks persisted failure status, error, record counts, and
  extraction identity; unreachable/non-success health responses also check the
  bounded five-request failure path.
- Authentication tests validate a signed token using the configured secret and
  return no optional user when authentication is disabled. Crypto tests round-trip
  plaintext with the derived Fernet key and strictly decode the SHA-1 signature.
- Autocomplete tests check the escaped database query and requested limit. An
  input star is a literal escaped character followed by one prefix wildcard.
- NLQ tests check the returned tool result and delivered private SSE summary;
  endpoint tests check the successful response while retaining privacy and
  requested-release assertions.
- Media-profile and offloaded violation tests check their returned data as well
  as the fallback/thread boundary.

Nine intentional failure-tolerance/no-op tests now also verify that the failing
operation was attempted, or that no unnecessary database connection was acquired.
Disabled NLQ shutdown explicitly forbids constructing an Anthropic client.

The remaining 41 findings are intentional no-raise boundaries, precise interaction
contracts, or delegated assertions. Their individual explanations remain visible
rather than removing them from the raw counts. Class-qualified identities avoid
collisions between tests named `test_passes_database`; line numbers are diagnostic
only. Each exclusion matches one test and its current assertion category. A new
finding, a changed category, or an obsolete exclusion requires another review.

```console
uv run python scripts/audit_test_assertions.py --json --check
```

The final raw inventory has 11 assertion-free and 30 call-only tests, all explained,
and zero stale exclusions. The ordinary test suite checks the repository inventory
alongside synthetic regressions for new findings, class-name collisions, line moves,
and stale exclusions. This is test hardening; application behavior is unchanged.
