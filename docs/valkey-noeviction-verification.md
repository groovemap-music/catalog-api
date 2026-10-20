# Valkey write rejection, verified against real servers

`api/valkey_errors.py` turns a write that Valkey refuses into a 503. The unit suites
(`tests/test_valkey_write_rejection_503.py` and `tests/test_password_change_valkey_oom.py`) run
the routes against `tests/noeviction_valkey.py`, a fakeredis proxy that raises the out-of-memory
error. `just integration-valkey` checks that fake against a **real** full Valkey and a **real**
PostgreSQL (gm-catalog-api-u5i2).

This is local verification only. The recipe starts throwaway containers on loopback, removes
them afterwards, and deploys and pushes nothing.

## Running it

```bash
just integration-valkey                 # all scenarios
just integration-valkey -k Password     # extra pytest arguments pass through
```

- **Opt-in by design.** The tests carry the `integration` and `valkey_noeviction` markers.
  `just test` and `just check` deselect `integration`. A bare `pytest` also skips the module
  unless `CATALOG_VALKEY_INTEGRATION=1` is set, which only the recipe does. So the default
  checks still connect to nothing.
- **Docker is required, and its absence is a clean skip.** With no `docker` CLI, or with a
  daemon that does not answer, the recipe exits 0 and the tests report `SKIPPED`. Nothing fails.
- **Containers are throwaway.** Each container is named `gm-catalog-api-valkey-it-<random hex>`
  and labelled `gm-catalog-api-valkey-it`. It publishes `127.0.0.1::<port>`, a random loopback
  port. The session fixture's finalizer always runs `docker rm --force --volumes`, then asserts
  that its containers are gone. After pytest, the wrapper `scripts/test-integration-valkey.sh`
  fails the run if `docker ps --all` still lists any labelled container.
- **Image overrides.** `VALKEY_INTEGRATION_IMAGE` and `POSTGRES_INTEGRATION_IMAGE` replace the
  pinned defaults.

## What runs where

| Piece | Value |
|---|---|
| Valkey image | `valkey/valkey:9.1.2-alpine@sha256:48332870af354a799964c0012ae1194a0bf2bf894eb508f945810596dc2d8d11`, the exact pin in `groovemap-music/deployment` `docker-compose.yml` |
| Valkey flags | `--maxmemory 4194304 --maxmemory-policy noeviction --save "" --appendonly no`. Production uses the same policy with `512mb` |
| PostgreSQL image | `postgres:18-alpine@sha256:d3e1620b530c944afa6e887d22eb899824da68e19c52024bf98f5220c88a65b2`, the database-schema required tier and `just test-integration` pin |
| Schema | `groovemap_schema.postgres.create_postgres_schema`, the producer DDL pinned in `pyproject.toml` |
| Valkey client | `api.api._create_valkey`, the production builder: `instrument_valkey(valkey.asyncio.from_url(..., decode_responses=True))` |
| App | The real FastAPI app over `httpx.ASGITransport`, with routers wired to the real pool and client |

**How Valkey is filled.** A separate connection writes `filler:*` keys. Value sizes step down
from 32 KiB to 1 byte, and filling stops when even a 1-byte `SET` is refused. That first
rejection comes from the server: Valkey reached its limit because it was filled. The limit is
not configured into a rejection.

One real behaviour the fake does not need: when Valkey rejects a command, it frees that
command's query buffer. A server just past the limit can therefore dip back under it and accept
a small write. To keep "full" stable, the filler then pins `maxmemory` 256 KiB below the
observed `used_memory`, still under `noeviction`. `drain` restores the limit and deletes only
the fillers, so reset tokens and quota counters survive a retry.

**Mid-request fills.** Some scenarios need Valkey to fill up between two writes of one request,
for example "SET accepted, INCR rejected". For these, `FillAfterWrites` lets N writes through
the real client and then runs the filler. The rejection that follows is still the server's.

## Evidence — 2026-10-19

| Item | Observed |
|---|---|
| Valkey | `Valkey server v=9.1.2 sha=00000000:1 malloc=jemalloc-5.3.0 bits=64`, digest `valkey/valkey@sha256:48332870af354a799964c0012ae1194a0bf2bf894eb508f945810596dc2d8d11` |
| PostgreSQL | `postgres (PostgreSQL) 18.6`, digest `postgres@sha256:d3e1620b530c944afa6e887d22eb899824da68e19c52024bf98f5220c88a65b2`. The suite was also re-run with `POSTGRES_INTEGRATION_IMAGE` set to the deployment compose pin `postgres:18-alpine@sha256:77f585114c32fbca283dc835b0596f4e52b51b4c6662d7810b2f4084f60a1873`, also 18.6: 21/21 pass |
| valkey-py | 6.1.1 |
| Docker engine | 29.5.2 (local, darwin) |
| Result | **21 passed**. `docker ps -a --filter label=gm-catalog-api-valkey-it` was empty afterwards |

| # | Scenario (test) | Result |
|---|---|---|
| 1–5 | `SET`, `SETEX`, `SET NX EX`, `INCR`, `DECR` on a full server raise `valkey.exceptions.OutOfMemoryError`. The fake raises the identical class, `args == ("command not allowed when used memory > 'maxmemory'.",)` and `str()`. The class is in `VALKEY_UNAVAILABLE_ERRORS`, and `valkey_write_guard` maps it to 503 (`test_real_rejection_is_classified_and_identical_to_the_fake[*]`) | pass |
| 6 | `GET`, `GETDEL`, `DEL`, `EXPIRE NX` and `SCAN` still work while full, and none of them is in the fake's `WRITE_COMMANDS`. The next `SET` is still rejected | pass |
| 7 | Real `WRONGTYPE` is a plain `ResponseError`, which the mapping does not classify, so it stays a 500. A real refused connection is `ConnectionError`, and a real `READONLY` from a replica is `ReadOnlyError`; both are classified | pass |
| 8 | Reset-confirm with the marker rejected returns 503 "nothing was changed" with `Retry-After: 30`. In **PostgreSQL** the hash, `password_changed_at` (NULL) and the live app-token count (1) are all unchanged: the transaction really rolled back. The reset token is still in Valkey, no marker was written, and an old session stays valid. After a drain, the **same token** resets the password: hash changed, app token revoked, token consumed, old session revoked | pass |
| 9 | Change-password: same rollback in PostgreSQL. The retry succeeds after a drain | pass |
| 10 | Pre-commit marker accepted, then Valkey fills and the post-commit re-stamp is rejected: the response is 200, the change is committed, the first marker still revokes old sessions, and a warning is logged | pass |
| 11 | Reset-request while full returns the unchanged 200. No `reset:*` key is stored, and an error-level `password_reset_request_failed` alert is logged with `stage=store_reset_token, error_type=OutOfMemoryError` | pass |
| 12 | Login 2FA challenge returns 503 and stores no `2fa_challenge:*` key. After a drain, the challenge is issued | pass |
| 13 | Logout returns 503 "still active", stores no `revoked:jti:*` key, and the token still authenticates. After a drain, logout works and the token is refused | pass |
| 14 | Admin logout returns 503, stores no revocation, and writes no `admin.logout` row to the real `admin_audit_log` | pass |
| 15 | Discogs OAuth state returns 503 with no authorize URL and no state key. Discogs itself is stubbed, and the encrypted credentials come from the real `app_config` table | pass |
| 16 | Snapshot save while full returns 503 and the quota counter stays exactly `3`, with no snapshot key. After a drain, a save succeeds and the counter reads `4` | pass |
| 17 | Snapshot `SET` accepted, then the `INCR` is rejected: the snapshot is removed with `DEL` while Valkey is full, and the counter stays `2` | pass |
| 18 | Ten rejected saves reserve nothing. After a drain the full quota of 5 is usable, the counter has a TTL, and the sixth save gets the quota error | pass |
| 19 | A user already at the cap gets `SnapshotQuotaExceededError`, not a 503, even while Valkey is full | pass |
| 20 | The sync lock `SET NX` is rejected: 503, no task started, no `sync_history` row in PostgreSQL, no lock key | pass |
| 21 | Lock accepted, then the cooldown `SETEX` is rejected: 202 "started", one `sync_history` row, the lock is released by `DEL` while Valkey is full, no cooldown key, and a warning is logged | pass |

**Fake vs real.** No mismatch was found in exception class, args or message, or in which
commands are rejected. `tests/noeviction_valkey.py` and `api/valkey_errors.py` needed no fix.
The fake models "full" as a binary flag. A real server near the limit can briefly accept a small
write after rejecting a large one, as described above. No route depends on that, so the fake
stays as it is.

Two mutation checks confirmed the suite is not vacuous:

- Suppressing the marker rejection inside the password transaction, so the change commits
  anyway, fails scenarios 8 and 9.
- Changing the fake's message to `"OOM command not allowed…"` fails scenarios 1–5.
