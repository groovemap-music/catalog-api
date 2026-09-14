"""The `gm_id` graph projection job — ADR 0009 "Graph projection".

PostgreSQL's `provider_aliases` table is the authority for native identity; Neo4j only
holds a projection of it. This job reads the currently-valid Discogs aliases for each
catalog kind (artist, label, master, release) and sets the additive `gm_id` property on
the matching node. It is the *only* thing it does: it creates no node, writes no
relationship, and touches no other property, so a failed or lagging run leaves a stale
property rather than a mutated graph.

The Neo4j label for each kind is read from a fixed mapping below, never from request
input, so the Cypher's node label is always one of the four literals this module
declares — nothing user- or database-supplied ever reaches the label position.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from typing import Any, Final

import structlog
from common.config import get_secret, parse_postgres_host_port
from psycopg.rows import dict_row

from api.queries.helpers import run_query


logger = structlog.get_logger(__name__)

# entity_kind (provider_aliases) -> Neo4j node label. Fixed and closed: the Cypher label
# is always one of these four literals, chosen by this module, never by caller input.
_LABEL_BY_KIND: Final[dict[str, str]] = {
    "artist": "Artist",
    "label": "Label",
    "master": "Master",
    "release": "Release",
}

DEFAULT_BATCH_SIZE: Final[int] = 1000

_SELECT_PAGE: Final = """
    SELECT external_id, native_id
    FROM provider_aliases
    WHERE provider = 'discogs'
      AND entity_kind = %s
      AND valid_to IS NULL
      AND external_id > %s
    ORDER BY external_id
    LIMIT %s
"""


def _set_gm_id_cypher(label: str) -> str:
    """Return the fixed-scope Cypher for one page of one label.

    `label` always comes from :data:`_LABEL_BY_KIND` — never from a row or a caller — so
    this is safe to interpolate directly into the query text; Neo4j has no parameter
    syntax for a node label. The statement sets `gm_id` and nothing else: no node is
    created (`MATCH`, not `MERGE`), no relationship is written, and no other property is
    touched.
    """
    return f"UNWIND $rows AS row MATCH (n:{label} {{id: row.id}}) SET n.gm_id = row.gm_id"


async def run_gm_id_projection(pool: Any, driver: Any, *, batch_size: int = DEFAULT_BATCH_SIZE) -> dict[str, int]:
    """Project native ids from `provider_aliases` onto Neo4j nodes' `gm_id` property.

    For each catalog kind (artist, label, master, release), pages the currently-valid
    Discogs aliases with a keyset cursor on `external_id` (batches of `batch_size`,
    ordered) and, per page, runs one `UNWIND` Cypher statement that sets `gm_id` on the
    matching nodes. PostgreSQL remains the source of truth throughout: the graph is only
    ever written to, never read, by this job.

    Args:
        pool: An `AsyncPostgreSQLPool`-shaped pool exposing `.connection()`.
        driver: An `AsyncResilientNeo4jDriver`-shaped driver exposing `.session()`.
        batch_size: Rows read (and written) per page, per kind.

    Returns:
        A dict from Neo4j label (`"Artist"`, `"Label"`, `"Master"`, `"Release"`) to the
        number of nodes whose `gm_id` this run set.
    """
    counts: dict[str, int] = {}

    for kind, label in _LABEL_BY_KIND.items():
        cypher = _set_gm_id_cypher(label)
        total = 0
        cursor_external_id = ""

        while True:
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(_SELECT_PAGE, (kind, cursor_external_id, batch_size))
                rows = await cur.fetchall()

            if not rows:
                break

            payload = [{"id": row["external_id"], "gm_id": str(row["native_id"])} for row in rows]
            await run_query(driver, cypher, rows=payload)

            total += len(rows)
            cursor_external_id = rows[-1]["external_id"]
            logger.debug("🔗 gm_id projection page written", kind=kind, label=label, page_size=len(rows), total=total)

        counts[label] = total
        logger.info("✅ gm_id projection finished for kind", kind=kind, label=label, count=total)

    return counts


def _build_conninfo() -> str:
    """Build a psycopg conninfo string from environment variables.

    Same contract as `api/media_backfill.py`'s helper of the same name.
    """
    address = os.environ.get("POSTGRES_HOST", "")
    username = get_secret("POSTGRES_USERNAME") or ""
    password = get_secret("POSTGRES_PASSWORD") or ""
    database = os.environ.get("POSTGRES_DATABASE", "")

    missing = []
    if not address:
        missing.append("POSTGRES_HOST")
    if not username:
        missing.append("POSTGRES_USERNAME")
    if not password:
        missing.append("POSTGRES_PASSWORD")
    if not database:
        missing.append("POSTGRES_DATABASE")

    if missing:
        print(f"❌ Missing required environment variables: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)

    default_port = int(os.getenv("POSTGRES_PORT", "5432") or "5432")
    host, port = parse_postgres_host_port(address, default_port)

    return f"host={host} port={port} user={username} password={password} dbname={database}"


def _build_neo4j_kwargs() -> dict[str, Any]:
    """Build Neo4j connection kwargs from environment variables."""
    uri = os.environ.get("NEO4J_HOST", "")
    username = get_secret("NEO4J_USERNAME") or ""
    password = get_secret("NEO4J_PASSWORD") or ""

    missing = []
    if not uri:
        missing.append("NEO4J_HOST")
    if not username:
        missing.append("NEO4J_USERNAME")
    if not password:
        missing.append("NEO4J_PASSWORD")

    if missing:
        print(f"❌ Missing required environment variables: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)

    return {"uri": uri, "auth": (username, password)}


async def _run_once(batch_size: int) -> dict[str, int]:
    """Build a pool and a driver from the environment, run the job once, and close both."""
    from common import AsyncPostgreSQLPool, AsyncResilientNeo4jDriver, neo4j_security_kwargs  # noqa: PLC0415

    host, port = parse_postgres_host_port(os.environ.get("POSTGRES_HOST", ""))
    pool = AsyncPostgreSQLPool(
        connection_params={
            "host": host,
            "port": port,
            "dbname": os.environ.get("POSTGRES_DATABASE", ""),
            "user": get_secret("POSTGRES_USERNAME") or "",
            "password": get_secret("POSTGRES_PASSWORD") or "",
        },
        max_connections=4,
        min_connections=1,
    )
    await pool.initialize()

    neo4j_kwargs = _build_neo4j_kwargs()
    driver = AsyncResilientNeo4jDriver(
        uri=neo4j_kwargs["uri"],
        auth=neo4j_kwargs["auth"],
        max_retries=5,
        **neo4j_security_kwargs(),
    )

    try:
        return await run_gm_id_projection(pool, driver, batch_size=batch_size)
    finally:
        await driver.close()
        await pool.close()


def main() -> None:
    """Entry point for the catalog-identity-projection CLI tool."""
    parser = argparse.ArgumentParser(
        prog="catalog-identity-projection",
        description=(
            "One-shot run of the gm_id graph projection job (ADR 0009). Reads the "
            "currently-valid Discogs aliases from provider_aliases and sets the additive "
            "gm_id property on the matching Neo4j nodes for each catalog kind (artist, "
            "label, master, release). Sets that one property only."
        ),
        epilog=(
            "Reads DB connection from environment variables: POSTGRES_HOST, "
            "POSTGRES_USERNAME, POSTGRES_PASSWORD, POSTGRES_DATABASE, NEO4J_HOST, "
            "NEO4J_USERNAME, NEO4J_PASSWORD"
        ),
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        metavar="N",
        help=f"Rows read and written per page, per kind (default: {DEFAULT_BATCH_SIZE})",
    )
    args = parser.parse_args()

    if args.batch_size < 1:
        parser.error("--batch-size must be at least 1")

    # Validate connection info up front, with the same missing-env-var messaging as the
    # rest of the CLI tools, before the first await.
    _build_conninfo()
    _build_neo4j_kwargs()

    print("📋 Running gm_id projection…")
    counts = asyncio.run(_run_once(args.batch_size))
    for label, count in counts.items():
        print(f"  {label}: {count} node(s) projected")
    print(f"✅ Done. Total nodes projected: {sum(counts.values())}.")


if __name__ == "__main__":
    main()
