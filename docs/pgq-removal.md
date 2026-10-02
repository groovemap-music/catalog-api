# SQL/PGQ backend removal and recovery

PostgreSQL removed SQL/PGQ from the PostgreSQL 19 release line, so catalog-api keeps
Neo4j as the only graph query engine. The optional `GRAPH_BACKEND=postgres` path,
`GRAPH_TABLE` statements, backend selectors, parity harness, and PostgreSQL-default
cutover documentation were removed in October 2026.

Git history is the recovery record. No archive branch was created or pushed.

## Recovery points

- `c5de6e5b16defe7c0e296cefdaad2f8116721efd` is the last catalog-api `main`
  revision containing the complete optional PostgreSQL graph backend.
- `origin/backup/gm-catalog-api-wpku.6` records the held default-flip work at
  `61124ce65b69c38b6170b0793b160a173b5a23d8`.
- The migration entered through molecules `7f16d63` (`gm-catalog-api-0da`),
  `0194786` (`gm-catalog-api-91a`), `15f70c7` (`gm-catalog-api-dl8`), and
  `9722032` (`gm-catalog-api-wpku`).

Inspect the old implementation without changing the worktree:

```console
git show c5de6e5:api/graph_backend.py
git ls-tree -r --name-only c5de6e5 api/queries tests docs | grep -E 'pg_queries|graph-table|graph_parity'
git show 61124ce
```

Restore an individual historical file for investigation with:

```console
git show c5de6e5:path/to/file > /tmp/historical-file
```

## Removed and retained boundaries

Removed paths include `api/graph_backend.py`, the graph-backend `*_pg_queries.py`
modules, `docs/graph-table-migration-template.md`, and their selector, parity, and
PostgreSQL graph integration tests.

Neo4j query families remain under `api/queries/*_queries.py` and are wired directly by
the routers. Ordinary PostgreSQL persistence remains supported. In particular, the
Explore router still uses `api/queries/release_media_queries.py` for release media and
catalog-block reads, insights keeps its precomputed relational reads, and the shared
PostgreSQL pool, persistence contracts, sync, identity, audit, and reattachment code are
unchanged. Those consumers follow the schema package pinned by `pyproject.toml` and can
be rolled back by reverting that dependency pin together with its generated persistence
contract update.
