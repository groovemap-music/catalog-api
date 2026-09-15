"""PostgreSQL reads for the additive blocks ``catalog-api`` serves on one release.

Two reads, both keyed by ``releases.data_id`` — the same id as the Neo4j ``Release.id``
node property, which is what lets the release-detail response fill graph fields from the
relational store without a second identity hop:

* the ADR 0007 canonical ``media`` block, in its own ``media`` column, computed by the
  Discogs SQL loader at load time;
* the ADR 0011 ``identifiers`` and ``companies`` blocks and ``country``, which ride inside
  the existing ``data`` JSONB rather than in columns of their own — ADR 0011 adds no
  column, so these are read out of the document.

They stay two statements rather than one because they answer to different callers and
different fallbacks: a missing media block is derived from the deprecated format names,
while a missing identifiers block is simply a release whose catalogue markings nobody
published. The release-detail path runs both concurrently.
"""

from typing import Any

from common.query_debug import execute_sql
from psycopg.rows import dict_row


__all__ = ["get_release_catalog_blocks", "get_release_media"]


async def get_release_media(pg_pool: Any, release_id: str) -> dict[str, Any] | None:
    """Return the canonical ``media`` block stored for one release.

    Args:
        pg_pool: The async PostgreSQL pool.
        release_id: The Discogs release id (``releases.data_id`` / Neo4j ``Release.id``).

    Returns:
        The JSON-ready media block, or ``None`` when the release row does not
        exist in PostgreSQL, or exists but its ``media`` column is NULL.
        Callers derive a best-effort fallback for either case (see
        ``common.media.legacy_format_names_to_media``).
    """
    async with pg_pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await execute_sql(cur, "SELECT media FROM releases WHERE data_id = %s", (release_id,))
        row: dict[str, Any] | None = await cur.fetchone()
    if row is None:
        return None
    media: dict[str, Any] | None = row["media"]
    return media


def _items(block: Any) -> list[dict[str, Any]]:
    """Return the ``items`` list of a canonical block, or ``[]`` when there is none.

    Defensive about the block's shape rather than trusting it: the column is JSONB written
    by a loader pinned to its own contract revision, and a release loaded before ADR 0011
    (or by a producer mid-rollout) can carry no block, a null one, or one without items.
    Every one of those is the same answer to the caller — this release has no markings we
    know of — and none of them is a reason to fail an otherwise complete detail response.
    """
    if not isinstance(block, dict):
        return []
    items = block.get("items")
    return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


async def get_release_catalog_blocks(pg_pool: Any, release_id: str) -> dict[str, Any]:
    """Return the ADR 0011 identifier, company, and country facts for one release.

    Args:
        pg_pool: The async PostgreSQL pool.
        release_id: The Discogs release id (``releases.data_id`` / Neo4j ``Release.id``).

    Returns:
        ``identifiers`` and ``companies`` as the blocks' item lists — the catalogue
        markings and the manufacturing and rights credits, in the source order the
        producer published them — and ``country`` as the string the catalog stored, or
        ``None``. A release with no row, or a row with no blocks, yields two empty lists
        and a null country, so the three keys are never absent from a release response.
    """
    async with pg_pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await execute_sql(
            cur,
            "SELECT data->'identifiers' AS identifiers, data->'companies' AS companies, data->>'country' AS country FROM releases WHERE data_id = %s",
            (release_id,),
        )
        row: dict[str, Any] | None = await cur.fetchone()
    if row is None:
        return {"identifiers": [], "companies": [], "country": None}
    return {
        "identifiers": _items(row["identifiers"]),
        "companies": _items(row["companies"]),
        "country": row["country"],
    }
