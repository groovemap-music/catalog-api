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
