# Catalog API coverage policy

The catalog API owns its coverage floor. It is based on this repository's measured Python
source and tests, not on a percentage inherited from the former monorepo.

## Recorded baseline

The baseline was measured from commit `2199555` after the database fixtures became faithful to
the Neo4j and PostgreSQL driver interfaces:

```text
Python 3.14.5
coverage.py 7.16.0
2,248 tests passed
7,403 statements; 7,289 covered; 114 missed
total line coverage: 98.46%
```

The measurement command is `just coverage`. Coverage collects every test under `tests/` and
measures the `api` package, omitting only test files and package `__init__.py` files. In
particular, the existing `tests/test_admin_setup.py` suite remains included and
`api/admin_setup.py` contributed 54 covered statements out of 54 to this baseline.

## Enforcement

`pyproject.toml` sets `fail_under = 98.46` with two-decimal precision. Both `just coverage` and
the coverage step inside `just ci-check` fail when total coverage falls below that value. The
required GitHub Actions job runs those recipes, and `codecov.yml` uses the same fixed project
target with no regression threshold.

Codecov's patch target remains a complementary signal. It does not replace the repository-wide
floor, and an upload or display rounding difference cannot make a failing local coverage run
pass.

## Ratchet

1. Do not lower the floor to make a change pass. Add tests or remove genuinely unreachable code.
2. When a merged change produces a stable higher result from `just coverage`, raise
   `fail_under` and the Codecov project target together to that measured two-decimal value.
3. Keep the source and test scope unchanged when comparing measurements. Any intentional scope
   change must record a fresh statement count and explain why the new scope is more faithful.
4. Keep operator entry points such as `api/admin_setup.py` in scope; do not omit them to protect
   the percentage.
5. Treat the floor as repository history. Do not substitute a fleet-wide or former-monorepo
   target for a measurement from this repository.
