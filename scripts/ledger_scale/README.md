# Identity Ledger scale measurement

A deterministic synthetic catalog at design spike `gm-design-loj9`'s replayed scale, and a
harness that times the `admin_identity_ledger_*` routes' reads against it (bead
`gm-catalog-api-278z`). GrooveMap has never been deployed, so no real catalog exists to measure.
This is the stand-in until one does.

| File | What it is |
| --- | --- |
| `compose.yaml` | A throwaway PostgreSQL 18, compose project `gm-ledger-scale`, on one named volume |
| `seed.sql` | The generator: `psql -v scale=<s> -f seed.sql` fills the ledger's tables at `s` × loj9 |
| `../ledger_scale.py` | The harness: loj9's figures for the seeded catalog, then `EXPLAIN (ANALYZE, BUFFERS)` and repeated timings for every route case |

## Run it

The disk needed grows linearly: about 0.95 GB at `scale=0.05`, 4.7 GB at 0.25, and 9.3 GB at
0.5. Full scale needs about 18.5 GB, plus temporary sort space while the largest indexes build.
Check `df -h /` first. Under Colima the volume lives in the VM's sparse disk image, which does
not shrink on its own when the volume is removed (see teardown below).

```console
$ docker compose -f scripts/ledger_scale/compose.yaml up -d
$ PORT=$(docker compose -f scripts/ledger_scale/compose.yaml port postgres 5432 | cut -d: -f2)
$ uv run python - <<EOF
import asyncio
from common import AsyncPostgreSQLPool
from groovemap_schema.postgres import create_postgres_schema
async def main():
    pool = AsyncPostgreSQLPool(connection_params={"host": "127.0.0.1", "port": $PORT, "dbname": "groovemap",
                                                  "user": "groovemap", "password": "ledger-scale"})
    await pool.initialize()
    assert await create_postgres_schema(pool) == 0
    await pool.close()
asyncio.run(main())
EOF
$ docker compose -f scripts/ledger_scale/compose.yaml exec -T postgres \
    psql -X -q -U groovemap -d groovemap -v scale=0.5 < scripts/ledger_scale/seed.sql
$ uv run python scripts/ledger_scale.py --port "$PORT" --counts --runs 5 --out /tmp/ledger-scale.json
```

Add `--precomputed` to build the prototype precomputed ledger first: four materialized views
and `identity_ledger_refresh()`, composed from the route module's own SQL. The harness times
their build and one concurrent refresh, then measures the same cases reading from them. This is
the fix `gm-database-schema-k8e9` and `gm-catalog-api-6bxr` implement. `api/README.md`
("Latency at catalog scale") has the results.

The schema is `groovemap_schema.postgres.create_postgres_schema` from the database-schema
revision `pyproject.toml` pins, which is the same DDL the integration tests apply. The harness
prints a Markdown table of aggregates. `--out` keeps every full plan, as scratch output that is
never committed.

Teardown removes the container, the network, and the volume. Under Colima, also return the
freed blocks to the host:

```console
$ docker compose -f scripts/ledger_scale/compose.yaml down --volumes
$ colima ssh -- sudo fstrim -v /var/lib/docker
```

## The model

`seed.sql` replays loj9's two cycles (spike section 2), one month apart:

| Instant | Step | What it writes |
| --- | --- | --- |
| 2026-09-01 02:00 | Discogs load, month 1 | Every record mints an item under its `discogs` alias |
| 2026-09-02 02:00 | MusicBrainz load, month 1 | A row whose Discogs id resolves attaches to that item (linked at ingest). Any other row mints its own item |
| 2026-10-01 02:00 | Discogs load, month 2 | New records mint |
| 2026-10-02 02:00 | MusicBrainz load, month 2 | New rows attach or mint. Rows whose link was added or changed write a `groovemap_load` link event |
| 2026-10-02 20:00 | Re-attachment run R2 | Each split row's alias closes on its own item and reopens on the Discogs item at one instant, and one supersession opens |

R1 writes nothing, so it has no rows. A load commits 1,000 rows per transaction, one second
apart, in kind order: artist, label, master, then release. Every count below is multiplied by
`scale`.

| Figure (scale 1) | Count | Source |
| --- | ---: | --- |
| `provider_aliases` | 48,588,202 | Section 3: 48,416,897 in month 1 and 171,305 in month 2 |
| Editions (release items, less superseded) | 23,480,146 | Section 3, the release ledger |
| Works (master items, less superseded) | 6,842,218 | Section 3 gives 6,839,569. This count is every Discogs master and own release group, less the 337 re-attached |
| Releases linked at ingest | 1,881,444 + 1,315 | Section 2 |
| Supersessions, all `catalog_reattachment` | 2,206 | Section 2: release 1,204, release group 337, artist 621, label 44 |
| Editions with two work parents | 564,667 | Section 3. The section 5 cases split the rest, and 7,453,079 editions have no parent |
| Backfill `link_added` events (`musicbrainz_fullexport`) | 1,888,733 | One per MusicBrainz release naming a Discogs release at the first load, dated mid-2004 to 2026-09, denser towards the present |
| Load link events (`groovemap_load`) | 1,213 | Section 2's "link added" and "link changed" re-attachments |
| `musicbrainz.releases` | 5,868,091 | Each names its release group, and its Discogs release when linked |
| `graph.derived_from` | about 11.4M | Release to master, giving the editions' Discogs work parents |
| `public.releases` | 19,496,018 | Key-only rows: the ledger probes them by `data_id` |

Any MusicBrainz row a later step changes sits at the front of its month-1 batch: arrived,
link added, link changed, then unresolved. 17,103 Discogs releases are named by a second
MusicBrainz release, as in section 5. Masters and release groups get their releases from a
power-law draw, so a few works have hundreds of editions and most have one.

**Determinism.** Every row is a pure function of its position. Ids are UUIDv7-shaped. Each
encodes its mint instant in the time bits, so they sort by mint time as `uuidv7()` ids do, and
it carries a kind tag and counter in the rest. MBIDs are md5 values. Draws use PostgreSQL's
stable `hashint8extended`. There is no `random()` and no clock, so two runs at one scale write
identical tables.

**Generated in the server.** Each table is a `ledger_scale.gen_*` view over `generate_series`.
A psql inside the container streams it into `COPY ... FROM PROGRAM ... WITH (FREEZE)`, in the
same transaction as a `TRUNCATE`. Nothing crosses the client connection. With
`wal_level=minimal` (compose only) the load writes no WAL, and the rows land frozen and
all-visible, so no vacuum or hint-bit writes disturb the first measurements.

**Indexes.** The seeded tables' constraints and indexes come off before the load. Each one is
rebuilt from its own catalog definition after its table fills, except three groups no ledger
statement can read, which would only cost disk: `provider_aliases_pkey`, `public.releases`'
document indexes, and `musicbrainz.releases`' `gm_item_id` and `media` indexes.
`ledger_scale.deferred` lists every one with `rebuilt`.

**What is not modelled.** Identifier aliases (loj9 did not replay them), `catalog_item_moves`
(0 by construction), `admin_audit_log` rows (a `decision_ref` is not a foreign key, and the
ledger never joins it), and the provider tables for artists, labels, and release groups (no
ledger statement reads them).

## The cases

`scripts/ledger_scale.py` calls `get_ledger_works`, `get_ledger_density` and `get_ledger_work`,
the functions the admin router calls, for these cases:

- the works list by default;
- the works list for each event filter (`EVENT_TYPES`);
- the works list for one month, 2026-10;
- density for each kind;
- one work: the Discogs master with the most derived releases.

One warm-up call records the statements a case executes. Each runs again under
`EXPLAIN (ANALYZE, BUFFERS)` in the same read-only transaction shape, and then `--runs` timed
calls follow. p50 and p95 are nearest-rank over those calls, so with fewer than 20 runs, p95 is
the slowest run. The server has production's memory, I/O, and parallelism settings
(deployment `docker-compose.prod.yml`), but not its host: a Colima VM with 6 vCPUs and 16 GiB,
shared with other containers.
