"""Measure the ``admin_identity_ledger_*`` routes' reads against a seeded synthetic catalog.

``scripts/ledger_scale/README.md`` describes the whole run. This script is its measuring half: given a
PostgreSQL that ``scripts/ledger_scale/seed.sql`` filled, it

- prints the seeded catalog's loj9 figures (``--counts``), so a run states what it measured;
- calls each route's query function, ``get_ledger_works``, ``get_ledger_density`` and
  ``get_ledger_work``, exactly as the admin router does, for every case the acceptance names: works
  by default, by each event filter, and in a one-month window; density for each kind; and the
  work with the most editions;
- runs ``EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)`` on every statement a case's call executes, in the
  same ``REPEATABLE READ, READ ONLY`` transaction shape, after one warm-up call;
- times ``--runs`` further calls and reports p50 and p95 (nearest rank) in milliseconds.

With ``--precomputed`` it first builds the precomputed ledger the fix proposes: four materialized views,
their unique indexes, and ``identity_ledger_refresh()``. They are composed from the route module's
own SQL, so they hold exactly what the routes compute today. It times their build and one
concurrent refresh, then measures the same cases with the works list and density statements read
from the views. That is the prototype the database-schema and catalog-api follow-ups implement.

Aggregates go to stdout as a Markdown table; the full plans go to ``--out`` as JSON, which is
scratch output and never committed. Without ``--precomputed`` nothing here writes to the database.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any

from common import AsyncPostgreSQLPool

from api.identity_ledger import EVENT_TYPES, KINDS
from api.queries import identity_ledger_queries
from api.queries.identity_ledger_queries import get_ledger_density, get_ledger_work, get_ledger_works


# The window the acceptance asks for: the second cycle's month (seed.sql's t4 to t6).
WINDOW = (datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 11, 1, tzinfo=UTC))

# The work whose editions are most numerous: Discogs masters by derived releases, resolved to the
# item that holds the master's current alias.
_LARGEST_WORK = """
SELECT alias.native_id
FROM (SELECT master_id FROM graph.derived_from GROUP BY master_id ORDER BY count(*) DESC, master_id LIMIT 1) AS largest
JOIN provider_aliases AS alias
  ON alias.provider = 'discogs' AND alias.entity_kind = 'master' AND alias.external_id = largest.master_id AND alias.valid_to IS NULL
"""

# loj9's figures, by the ledger's own definitions (api/queries/identity_ledger_queries.py).
_COUNTS = """
WITH edition AS (
    SELECT item.id
    FROM catalog_items AS item
    WHERE item.kind = 'release'
      AND NOT EXISTS (SELECT 1 FROM catalog_item_supersessions AS s WHERE s.superseded_id = item.id AND s.valid_to IS NULL)
),
member AS (
    SELECT id AS edition_id, id AS item_id FROM edition
    UNION ALL
    SELECT survivor_id, superseded_id FROM catalog_item_supersessions WHERE valid_to IS NULL
),
parent AS (
    SELECT member.edition_id, COALESCE(work_open.survivor_id, master.native_id) AS work_id
    FROM member
    JOIN provider_aliases AS release ON release.native_id = member.item_id AND release.provider = 'discogs'
     AND release.entity_kind = 'release' AND release.valid_to IS NULL
    JOIN graph.derived_from AS derived ON derived.release_id = release.external_id
    JOIN provider_aliases AS master ON master.provider = 'discogs' AND master.entity_kind = 'master'
     AND master.external_id = derived.master_id AND master.valid_to IS NULL
    LEFT JOIN catalog_item_supersessions AS work_open ON work_open.superseded_id = master.native_id AND work_open.valid_to IS NULL
    UNION
    SELECT member.edition_id, COALESCE(work_open.survivor_id, master.native_id)
    FROM member
    JOIN provider_aliases AS release ON release.native_id = member.item_id AND release.provider = 'musicbrainz'
     AND release.entity_kind = 'release' AND release.valid_to IS NULL
    JOIN musicbrainz.releases AS mb_release ON mb_release.mbid = release.external_id::uuid
    JOIN provider_aliases AS master ON master.provider = 'musicbrainz' AND master.entity_kind = 'master'
     AND master.external_id = mb_release.release_group_mbid::text AND master.valid_to IS NULL
    LEFT JOIN catalog_item_supersessions AS work_open ON work_open.superseded_id = master.native_id AND work_open.valid_to IS NULL
),
parents AS (SELECT edition_id, count(*) AS works FROM parent GROUP BY edition_id)
SELECT
    (SELECT count(*) FROM provider_aliases) AS provider_aliases,
    (SELECT count(*) FROM edition) AS editions,
    (SELECT count(*) FROM catalog_items AS item WHERE item.kind = 'master'
       AND NOT EXISTS (SELECT 1 FROM catalog_item_supersessions AS s WHERE s.superseded_id = item.id AND s.valid_to IS NULL)) AS works,
    (SELECT count(*) FROM provider_aliases AS alias JOIN catalog_items AS item ON item.id = alias.native_id
       WHERE alias.provider = 'musicbrainz' AND alias.entity_kind = 'release' AND item.kind = 'release'
         AND alias.valid_from > item.created_at
         AND NOT EXISTS (SELECT 1 FROM catalog_item_supersessions AS s WHERE s.survivor_id = item.id AND s.valid_from = alias.valid_from)
    ) AS release_linked_at_ingest,
    (SELECT count(*) FROM catalog_item_supersessions) AS supersessions,
    (SELECT count(*) FROM edition) - (SELECT count(*) FROM parents) AS editions_no_parent,
    (SELECT count(*) FROM parents WHERE works = 1) AS editions_one_parent,
    (SELECT count(*) FROM parents WHERE works > 1) AS editions_two_parents,
    (SELECT count(*) FROM identity_link_events WHERE dated_from = 'musicbrainz_fullexport') AS link_events_backfill,
    (SELECT count(*) FROM identity_link_events WHERE dated_from = 'groovemap_load') AS link_events_load
"""

_SIZE = "SELECT pg_size_pretty(pg_database_size(current_database())), pg_database_size(current_database())"

# ---------------------------------------------------------------------------------------------
# The precomputed ledger (--precomputed). Each view is the route module's statement with the
# filters lifted out, so a refresh computes what a request computes today, once.
# ---------------------------------------------------------------------------------------------

REPAIRS = "('reattached', 'promoted', 'reverted')"


def _for_kind(sql: str, kind: str) -> str:
    return sql.replace("%(kind)s", f"'{kind}'")


def _side_events() -> str:
    """`_WORKS`'s `side_event` body: every non-mint event of a release item, supersessions per side."""
    body = identity_ledger_queries._WORKS.split("side_event AS (", 1)[1].split("),\nedition_event AS (", 1)[0]
    assert "supersession_of_kind" in body, "the works statement changed shape; update the prototype"
    return body


def _view_statements() -> list[tuple[str, str]]:
    resolved = identity_ledger_queries._RESOLVED.format(alias="edition_open", item="side_event.item_id")
    density = [
        "("
        + _for_kind(identity_ledger_queries._DENSITY, kind)
        .replace("SELECT to_char(", f"SELECT '{kind}'::text AS kind, to_char(", 1)
        .replace("GROUP BY 1, 2, 3", "GROUP BY 1, 2, 3, 4")
        .replace("ORDER BY 1, 2, 3", "ORDER BY 2, 3, 4")
        .strip()
        + ")"
        for kind in KINDS
    ]
    return [
        (
            "identity_ledger_edition_events",
            f"""CREATE MATERIALIZED VIEW identity_ledger_edition_events AS
WITH {_for_kind(identity_ledger_queries._ITEM_EVENTS, "release")},
side_event AS ({_side_events()})
SELECT COALESCE(edition_open.survivor_id, side_event.item_id) AS edition_id, side_event.at, side_event.type, count(*) AS events
FROM side_event
{resolved}
GROUP BY 1, 2, 3""",  # noqa: S608 — composed of module constants only
        ),
        ("identity_ledger_edition_events key", "CREATE UNIQUE INDEX ON identity_ledger_edition_events (edition_id, at, type)"),
        ("identity_ledger_edition_events at", "CREATE INDEX ON identity_ledger_edition_events (at)"),
        ("identity_ledger_edition_events type", "CREATE INDEX ON identity_ledger_edition_events (type, at)"),
        (
            "identity_ledger_edition_parents",
            f"""CREATE MATERIALIZED VIEW identity_ledger_edition_parents AS
WITH member AS MATERIALIZED (
    SELECT edition_id, edition_id AS item_id FROM (SELECT DISTINCT edition_id FROM identity_ledger_edition_events) AS evented
    UNION
    SELECT supersession.survivor_id, supersession.superseded_id
    FROM catalog_item_supersessions AS supersession
    WHERE supersession.valid_to IS NULL
      AND supersession.survivor_id IN (SELECT edition_id FROM identity_ledger_edition_events)
),
{identity_ledger_queries._PARENTS_OF_MEMBER}
SELECT DISTINCT edition_id, work_id FROM edition_parent""",  # noqa: S608 — composed of module constants only
        ),
        ("identity_ledger_edition_parents key", "CREATE UNIQUE INDEX ON identity_ledger_edition_parents (edition_id, work_id)"),
        (
            "identity_ledger_work_summary",
            f"""CREATE MATERIALIZED VIEW identity_ledger_work_summary AS
WITH by_type AS (
    SELECT parent.work_id, event.edition_id, event.type, min(event.at) AS first_at, max(event.at) AS last_at
    FROM identity_ledger_edition_events AS event
    JOIN identity_ledger_edition_parents AS parent ON parent.edition_id = event.edition_id
    GROUP BY parent.work_id, event.edition_id, event.type
),
by_edition AS (
    SELECT work_id, edition_id, min(first_at) AS first_at, max(last_at) AS last_at, bool_or(type IN {REPAIRS}) AS repaired
    FROM by_type
    GROUP BY work_id, edition_id
)
SELECT work_id, 'any'::text AS event, count(*) AS evented_editions, count(*) FILTER (WHERE repaired) AS repaired_editions,
       min(first_at) AS first_event, max(last_at) AS last_event
FROM by_edition
GROUP BY work_id
UNION ALL
SELECT work_id, type, count(*), count(*) FILTER (WHERE type IN {REPAIRS}), min(first_at), max(last_at)
FROM by_type
GROUP BY work_id, type""",  # noqa: S608 — composed of module constants only
        ),
        ("identity_ledger_work_summary key", "CREATE UNIQUE INDEX ON identity_ledger_work_summary (event, work_id)"),
        ("identity_ledger_work_summary page", "CREATE INDEX ON identity_ledger_work_summary (event, last_event DESC, work_id)"),
        ("identity_ledger_density", "CREATE MATERIALIZED VIEW identity_ledger_density AS\n" + "\nUNION ALL\n".join(density)),
        ("identity_ledger_density key", "CREATE UNIQUE INDEX ON identity_ledger_density (kind, month, type, dated_from)"),
        (
            "identity_ledger_refresh_state",
            """CREATE TABLE identity_ledger_refresh_state (
    singleton    boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    refreshed_at timestamptz NOT NULL,
    duration     interval NOT NULL
)""",
        ),
        (
            "identity_ledger_refresh()",
            """CREATE FUNCTION identity_ledger_refresh() RETURNS timestamptz LANGUAGE plpgsql AS $$
DECLARE
    started timestamptz := clock_timestamp();
BEGIN
    REFRESH MATERIALIZED VIEW CONCURRENTLY identity_ledger_edition_events;
    REFRESH MATERIALIZED VIEW CONCURRENTLY identity_ledger_edition_parents;
    REFRESH MATERIALIZED VIEW CONCURRENTLY identity_ledger_work_summary;
    REFRESH MATERIALIZED VIEW CONCURRENTLY identity_ledger_density;
    INSERT INTO identity_ledger_refresh_state (refreshed_at, duration) VALUES (started, clock_timestamp() - started)
    ON CONFLICT (singleton) DO UPDATE SET refreshed_at = EXCLUDED.refreshed_at, duration = EXCLUDED.duration;
    RETURN started;
END
$$""",
        ),
    ]


_DROP_VIEWS = """
DROP FUNCTION IF EXISTS identity_ledger_refresh();
DROP TABLE IF EXISTS identity_ledger_refresh_state;
DROP MATERIALIZED VIEW IF EXISTS identity_ledger_work_summary, identity_ledger_edition_parents, identity_ledger_edition_events,
    identity_ledger_density
"""

_VIEW_SIZES = """
SELECT relname, reltuples::bigint, pg_size_pretty(pg_total_relation_size(oid))
FROM pg_class
WHERE relkind = 'm' AND relname LIKE 'identity_ledger_%'
ORDER BY relname
"""

# The works list read from the views. Without a window it pages the summary; a window reads the
# event rows in its range. per_work is NOT MATERIALIZED so the count and the page each plan alone.
_WORKS_PRECOMPUTED = f"""
WITH summary AS (
    SELECT work_id, evented_editions, repaired_editions, first_event, last_event
    FROM identity_ledger_work_summary
    WHERE %(since)s::timestamptz IS NULL AND %(until)s::timestamptz IS NULL
      AND event = COALESCE(%(event)s::text, 'any')
),
edition_event AS (
    SELECT edition_id, at, type
    FROM identity_ledger_edition_events
    WHERE NOT (%(since)s::timestamptz IS NULL AND %(until)s::timestamptz IS NULL)
      AND (%(event)s::text IS NULL OR type = %(event)s::text)
      AND (%(since)s::timestamptz IS NULL OR at >= %(since)s::timestamptz)
      AND (%(until)s::timestamptz IS NULL OR at < %(until)s::timestamptz)
),
evented AS (
    SELECT edition_id, min(at) AS first_at, max(at) AS last_at, bool_or(type IN {REPAIRS}) AS repaired
    FROM edition_event
    GROUP BY edition_id
),
windowed AS (
    SELECT parent.work_id, count(*) AS evented_editions, count(*) FILTER (WHERE evented.repaired) AS repaired_editions,
           min(evented.first_at) AS first_event, max(evented.last_at) AS last_event
    FROM identity_ledger_edition_parents AS parent
    JOIN evented ON evented.edition_id = parent.edition_id
    GROUP BY parent.work_id
),
per_work AS NOT MATERIALIZED (
    SELECT * FROM summary
    UNION ALL
    SELECT * FROM windowed
)
SELECT total.works, page.work_id, page.evented_editions, page.repaired_editions, page.first_event, page.last_event
FROM (SELECT count(*) AS works FROM per_work) AS total
LEFT JOIN LATERAL (
    SELECT * FROM per_work ORDER BY last_event DESC, work_id LIMIT %(limit)s OFFSET %(offset)s
) AS page ON TRUE
ORDER BY page.last_event DESC, page.work_id
"""  # noqa: S608 — composed of module constants only

_DENSITY_PRECOMPUTED = """
SELECT month, type, dated_from, events
FROM identity_ledger_density
WHERE kind = %(kind)s
ORDER BY 1, 2, 3
"""


async def _build_views(pool: AsyncPostgreSQLPool) -> dict[str, Any]:
    """Build the views, then time one concurrent refresh of all four; report their sizes."""
    timings: dict[str, float] = {}
    async with pool.connection() as conn:
        await conn.set_autocommit(True)
        async with conn.cursor() as cur:
            await cur.execute(_DROP_VIEWS)
            for name, statement in _view_statements():
                started = time.perf_counter()
                await cur.execute(statement)
                timings[name] = round(time.perf_counter() - started, 1)
                print(f"built {name} in {timings[name]} s", flush=True)
            await cur.execute(
                "VACUUM ANALYZE identity_ledger_edition_events, identity_ledger_edition_parents, identity_ledger_work_summary, identity_ledger_density"
            )
            started = time.perf_counter()
            await cur.execute("SELECT identity_ledger_refresh()")
            refresh = round(time.perf_counter() - started, 1)
            print(f"identity_ledger_refresh() in {refresh} s", flush=True)
            await cur.execute(
                "VACUUM ANALYZE identity_ledger_edition_events, identity_ledger_edition_parents, identity_ledger_work_summary, identity_ledger_density"
            )
            await cur.execute(_VIEW_SIZES)
            sizes = {name: {"rows": rows, "size": size} for name, rows, size in await cur.fetchall()}
    for name, size in sizes.items():
        print(f"{name}: {size['rows']:,} rows, {size['size']}", flush=True)
    return {"build_s": timings, "build_total_s": round(sum(timings.values()), 1), "refresh_s": refresh, "sizes": sizes}


def _percentile(samples: list[float], q: float) -> float:
    """Nearest-rank percentile: the smallest sample with at least q of the samples at or below it."""
    ordered = sorted(samples)
    return ordered[max(0, math.ceil(q * len(ordered)) - 1)]


def _totals(plan: dict[str, Any]) -> dict[str, Any]:
    """One statement's plan reduced to the figures the bead records."""
    root = plan["Plan"]
    slowest: tuple[float, str] = (0.0, "")

    def walk(node: dict[str, Any]) -> None:
        nonlocal slowest
        own = node.get("Actual Total Time", 0.0) * node.get("Actual Loops", 1) - sum(
            child.get("Actual Total Time", 0.0) * child.get("Actual Loops", 1) for child in node.get("Plans", ())
        )
        if own > slowest[0]:
            label = node["Node Type"] + (f" on {node['Relation Name']}" if "Relation Name" in node else "")
            slowest = (own, label)
        for child in node.get("Plans", ()):
            walk(child)

    walk(root)
    return {
        "planning_ms": round(plan.get("Planning Time", 0.0), 1),
        "execution_ms": round(plan.get("Execution Time", 0.0), 1),
        "shared_hit": root.get("Shared Hit Blocks", 0),
        "shared_read": root.get("Shared Read Blocks", 0),
        "temp_written": root.get("Temp Written Blocks", 0),
        "rows": root.get("Actual Rows", 0),
        "slowest_node": slowest[1],
        "slowest_node_ms": round(slowest[0], 1),
    }


async def _explain(pool: AsyncPostgreSQLPool, statements: list[tuple[str, Any]]) -> list[dict[str, Any]]:
    plans = []
    async with pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
        await cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        for sql, params in statements:
            await cur.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql, params)
            row = await cur.fetchone()
            assert row is not None
            plans.append(row[0][0])
    return plans


async def _case(pool: AsyncPostgreSQLPool, name: str, call: Callable[[], Awaitable[Any]], runs: int) -> dict[str, Any]:
    captured: list[tuple[str, Any]] = []
    original = identity_ledger_queries.execute_sql

    async def capture(cursor: Any, query: Any, params: Any = None) -> None:
        captured.append((query, params))
        await original(cursor, query, params)

    # The warm-up call records the statements the case executes.
    identity_ledger_queries.execute_sql = capture
    try:
        await call()
    finally:
        identity_ledger_queries.execute_sql = original
    statements = [(sql, params) for sql, params in captured if not sql.lstrip().upper().startswith("SET TRANSACTION")]
    plans = await _explain(pool, statements)

    timings = []
    for _ in range(runs):
        started = time.perf_counter()
        await call()
        timings.append((time.perf_counter() - started) * 1000)
    result = {
        "case": name,
        "runs": runs,
        "p50_ms": round(_percentile(timings, 0.5), 1),
        "p95_ms": round(_percentile(timings, 0.95), 1),
        "timings_ms": [round(t, 1) for t in timings],
        "statements": [_totals(plan) for plan in plans],
        "plans": plans,
    }
    print(f"{name}: p50 {result['p50_ms']} ms, p95 {result['p95_ms']} ms", flush=True)
    return result


def _cases(work: str) -> list[tuple[str, Callable[[AsyncPostgreSQLPool], Awaitable[Any]]]]:
    cases: list[tuple[str, Callable[[AsyncPostgreSQLPool], Awaitable[Any]]]] = [("works default", get_ledger_works)]
    for event in EVENT_TYPES:
        cases.append((f"works event={event}", partial(get_ledger_works, event=event)))
    since, until = WINDOW
    cases.append((f"works {since:%Y-%m}", partial(get_ledger_works, since=since, until=until)))
    for kind in KINDS:
        cases.append((f"density {kind}", partial(get_ledger_density, kind=kind)))
    cases.append(("work largest", partial(get_ledger_work, work_id=work)))
    return cases


def _markdown(results: list[dict[str, Any]]) -> str:
    lines = [
        "| Case | p50 ms | p95 ms | Statements: execution ms | Slowest node | Temp blocks written |",
        "| --- | ---: | ---: | --- | --- | ---: |",
    ]
    for result in results:
        statements = result["statements"]
        slowest = max(statements, key=lambda s: s["slowest_node_ms"])
        lines.append(
            f"| {result['case']} | {result['p50_ms']:,.0f} | {result['p95_ms']:,.0f} | "
            + " + ".join(f"{s['execution_ms']:,.0f}" for s in statements)
            + f" | {slowest['slowest_node']} ({slowest['slowest_node_ms']:,.0f} ms) | {sum(s['temp_written'] for s in statements):,} |"
        )
    return "\n".join(lines)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--user", default="groovemap")
    parser.add_argument("--password", default="ledger-scale")
    parser.add_argument("--database", default="groovemap")
    parser.add_argument("--runs", type=int, default=5, help="timed calls per case, after one warm-up")
    parser.add_argument("--only", action="append", default=[], help="run only cases whose name starts with this (repeatable)")
    parser.add_argument("--counts", action="store_true", help="print the seeded catalog's loj9 figures first")
    parser.add_argument("--out", type=Path, help="write every result, with full plans, to this JSON file")
    parser.add_argument("--precomputed", action="store_true", help="build the precomputed ledger views and measure reads from them")
    args = parser.parse_args()

    pool = AsyncPostgreSQLPool(
        connection_params={"host": args.host, "port": args.port, "dbname": args.database, "user": args.user, "password": args.password},
        max_connections=2,
        min_connections=1,
    )
    await pool.initialize()
    try:
        async with pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_SIZE)
            size = await cur.fetchone()
            print(f"database size: {size[0] if size else '?'}", flush=True)
            if args.counts:
                await cur.execute(_COUNTS)
                row = await cur.fetchone()
                assert row is not None and cur.description is not None
                for column, value in zip(cur.description, row, strict=True):
                    print(f"{column.name}: {value:,}", flush=True)
            await cur.execute(_LARGEST_WORK)
            largest = await cur.fetchone()
            assert largest is not None, "no Discogs master holds a derived release"
        work = str(largest[0])
        print(f"largest work: {work}", flush=True)

        views: dict[str, Any] | None = None
        if args.precomputed:
            views = await _build_views(pool)
            identity_ledger_queries._WORKS = _WORKS_PRECOMPUTED
            identity_ledger_queries._DENSITY = _DENSITY_PRECOMPUTED

        results = []
        for name, call in _cases(work):
            if args.only and not any(name.startswith(prefix) for prefix in args.only):
                continue
            results.append(await _case(pool, name, partial(call, pool), args.runs))
    finally:
        await pool.close()

    print(_markdown(results))
    if args.out:
        args.out.write_text(json.dumps({"views": views, "cases": results}, indent=1, default=str))


if __name__ == "__main__":
    asyncio.run(main())
