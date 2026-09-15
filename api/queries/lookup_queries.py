"""PostgreSQL reads behind ``GET /api/lookup/{provider}/{value}``.

Two statements, both index-answered. The first resolves a normalized identifier through
``provider_aliases`` — the table ADR 0009 built and ADR 0011 finally mints release rows
into — using the partial unique index on ``(provider, entity_kind, external_id) WHERE
valid_to IS NULL``, so a barcode resolves to at most one native id. The second walks that
native id back out to the release rows that point at it, through the additive
``gm_item_id`` column both the Discogs ``releases`` table and ``musicbrainz.releases``
carry.

The second statement is a union across both catalogs on purpose: one barcode is one
pressing, and both catalogs describe it. A caller holding the record wants every row that
names it, labelled with which catalog said so, rather than whichever one happens to be
loaded.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from common.query_debug import execute_sql
from psycopg.rows import dict_row


__all__ = ["releases_for_native_id", "resolve_alias_native_id"]

# ADR 0009 keys a release alias under this entity kind; the lookup surface addresses
# releases only, so it is a constant rather than a parameter.
_RELEASE_KIND = "release"

_SELECT_ALIAS_NATIVE_ID = """
SELECT native_id
FROM provider_aliases
WHERE provider = %s
  AND entity_kind = %s
  AND external_id = %s
  AND valid_to IS NULL
"""

# `data->'artists'->0->>'name'` is the Discogs primary credit: the normalizer collapses the
# XML container into a list of objects, and the first entry is the billed artist. MusicBrainz
# releases carry no artist credit through the ingestion whitelist, so that column is NULL
# there rather than guessed at, and the year comes from the first release event's date.
_SELECT_RELEASES_BY_NATIVE_ID = """
SELECT * FROM (
    SELECT data_id::text AS id,
           'discogs'::text AS source,
           data->>'title' AS title,
           data->'artists'->0->>'name' AS artist,
           data->>'year' AS year,
           media->'families' AS media_families
    FROM releases
    WHERE gm_item_id = %s
    UNION ALL
    SELECT mbid::text AS id,
           'musicbrainz'::text AS source,
           name AS title,
           NULL::text AS artist,
           data->'release_events'->0->>'date' AS year,
           media->'families' AS media_families
    FROM musicbrainz.releases
    WHERE gm_item_id = %s
) AS matched
ORDER BY source, id
"""


def _year(raw: Any) -> int | None:
    """Return the four-digit year a release row carries, or ``None`` when it has none.

    Both catalogs store the year as text and neither guarantees its shape: Discogs carries
    ``"1969"`` or ``"0"``, and a MusicBrainz release event carries a full or partial ISO
    date. Taking the leading four digits covers both and rejects everything else, so a
    malformed value costs the field rather than the response.
    """
    if raw is None:
        return None
    head = str(raw).strip()[:4]
    if not head.isdigit():
        return None
    year = int(head)
    return year if year > 0 else None


async def resolve_alias_native_id(pool: Any, provider: str, external_id: str) -> UUID | None:
    """Return the native id one normalized identifier resolves to, or ``None``.

    Reads only the currently valid alias row. A provider identifier that was reassigned
    upstream has its old row closed with ``valid_to``, so a lookup answers with the release
    the identifier names now and never with the one it used to name.

    Args:
        pool: The async PostgreSQL pool.
        provider: An ADR 0009 alias namespace — ``barcode``, ``catalog_number``, or ``matrix``.
        external_id: The value under that namespace's normalization.

    Returns:
        The native catalog item id, or ``None`` when no valid alias carries the value.
    """
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await execute_sql(cur, _SELECT_ALIAS_NATIVE_ID, (provider, _RELEASE_KIND, external_id))
        row: dict[str, Any] | None = await cur.fetchone()
    if row is None:
        return None
    native_id: UUID | None = row["native_id"]
    return native_id


async def releases_for_native_id(pool: Any, native_id: UUID) -> list[dict[str, Any]]:
    """Return every release row that points at one native id, from both catalogs.

    Args:
        pool: The async PostgreSQL pool.
        native_id: The native catalog item id an alias resolved to.

    Returns:
        One dict per row — ``id``, ``source``, ``title``, ``artist``, ``year``, and
        ``media_families`` — ordered by catalog then id so two identical requests answer
        identically. A release with no canonical media block carries an empty family list.
    """
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await execute_sql(cur, _SELECT_RELEASES_BY_NATIVE_ID, (native_id, native_id))
        rows: list[dict[str, Any]] = await cur.fetchall()

    return [
        {
            "id": row["id"],
            "source": row["source"],
            "title": row["title"],
            "artist": row["artist"],
            "year": _year(row["year"]),
            "media_families": list(row["media_families"] or []),
        }
        for row in rows
    ]
