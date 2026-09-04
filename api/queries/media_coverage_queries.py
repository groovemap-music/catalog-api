"""Aggregations over the ``unmapped`` names in the ADR 0007 canonical media block.

Mapping coverage is observable from the stored data alone, without a rules engine.
Whenever the taxonomy does not recognise a provider's format vocabulary, the loader
keeps the raw name in the release's ``media`` block under ``unmapped``, split into
``formats`` (the provider's own format names) and ``descriptions`` (their
qualifiers). Ranking those names across a provider's release table by how many
releases carry them shows exactly which raw vocabulary the taxonomy is still
missing, and how much of the catalogue it costs.

Each provider stores its releases in its own table: Discogs in the default schema's
``releases`` (written by the Discogs SQL loader), MusicBrainz in ``musicbrainz.releases``
(written by ``musicbrainz-sql-loader``). Both carry the same canonical ``media`` column,
so one aggregation serves both.
"""

from __future__ import annotations

from typing import Any, Final

from common.query_debug import execute_sql
from psycopg import sql
from psycopg.rows import dict_row


#: Provider id → the identifier parts of the release table holding its ``media`` column.
_PROVIDER_TABLES: Final[dict[str, tuple[str, ...]]] = {
    "discogs": ("releases",),
    "musicbrainz": ("musicbrainz", "releases"),
}

#: The two lists inside the ``unmapped`` object, as ``(response kind, JSON key)``.
#: Ordered so ``format`` — the coarser, more actionable gap — is reported first
#: whenever two entries tie on release count.
_UNMAPPED_KINDS: Final[tuple[tuple[str, str], ...]] = (
    ("format", "formats"),
    ("description", "descriptions"),
)

#: Default and ceiling for how many top unmapped names one response carries.
DEFAULT_LIMIT: Final = 20
MAX_LIMIT: Final = 200


class UnknownProviderError(ValueError):
    """Raised when a provider has no release table carrying a canonical ``media`` column."""

    def __init__(self, provider: str) -> None:
        self.provider = provider
        super().__init__(f"Unknown provider: {provider!r}. Expected one of: {', '.join(known_providers())}")


def known_providers() -> list[str]:
    """Return the provider ids this aggregation can report on, sorted."""
    return sorted(_PROVIDER_TABLES)


def _release_table(provider: str) -> sql.Identifier:
    """Resolve a provider id to its (optionally schema-qualified) release table.

    Raises:
        UnknownProviderError: If the provider is not one of :func:`known_providers`.
    """
    parts = _PROVIDER_TABLES.get(provider)
    if parts is None:
        raise UnknownProviderError(provider)
    return sql.Identifier(*parts)


#: Alias the release table is given in every query, so each reference stays qualified
#: and can never be shadowed by a column the unnest introduces.
_RELEASE = sql.Identifier("r")


def _unmapped_array(json_key: str) -> sql.Composed:
    """An ``r.media -> 'unmapped' -> <json_key>`` expression guaranteed to be a JSON array.

    ``jsonb_array_elements_text`` and ``jsonb_array_length`` both raise on a non-array
    argument, so a row whose block predates the ``unmapped`` object — or stores something
    unexpected under it — must degrade to an empty array rather than fail the aggregate
    for the whole table.
    """
    path = sql.SQL("{release}.media -> 'unmapped' -> {key}").format(release=_RELEASE, key=sql.Literal(json_key))
    return sql.SQL("CASE WHEN jsonb_typeof({path}) = 'array' THEN {path} ELSE '[]'::jsonb END").format(path=path)


def _coverage_query(table: sql.Identifier) -> sql.Composed:
    """Count media-tagged releases and those carrying at least one unmapped name."""
    any_unmapped = sql.SQL(" OR ").join(
        sql.SQL("jsonb_array_length({array}) > 0").format(array=_unmapped_array(json_key)) for _, json_key in _UNMAPPED_KINDS
    )
    return sql.SQL(
        "SELECT count(*) AS media_tagged_releases,"
        " count(*) FILTER (WHERE {any_unmapped}) AS releases_with_unmapped"
        " FROM {table} AS {release} WHERE {release}.media IS NOT NULL"
    ).format(any_unmapped=any_unmapped, table=table, release=_RELEASE)


def _top_names_query(table: sql.Identifier) -> sql.Composed:
    """Rank unmapped raw names by the number of releases carrying them.

    ``common.media`` sorts and de-duplicates each ``unmapped`` list when it finishes a
    block, so a name appears at most once per release per kind — which makes the row
    count over the unnested elements exactly a release count.
    """
    branches = sql.SQL(" UNION ALL ").join(
        sql.SQL(
            "SELECT {kind} AS kind, unmapped.name AS name"
            " FROM {table} AS {release}, LATERAL jsonb_array_elements_text({array}) AS unmapped(name)"
            " WHERE {release}.media IS NOT NULL"
        ).format(kind=sql.Literal(kind), table=table, release=_RELEASE, array=_unmapped_array(json_key))
        for kind, json_key in _UNMAPPED_KINDS
    )
    return sql.SQL(
        "SELECT kind, name, count(*) AS releases FROM ({branches}) AS entries GROUP BY kind, name ORDER BY releases DESC, kind ASC, name ASC LIMIT %s"
    ).format(branches=branches)


async def get_unmapped_media(pool: Any, provider: str, limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
    """Report one provider's media-mapping coverage and its most common unmapped names.

    Args:
        pool: The async PostgreSQL pool.
        provider: A provider id from :func:`known_providers`.
        limit: How many top unmapped names to return, clamped to ``1..MAX_LIMIT``.

    Returns:
        A JSON-ready dict with the provider, the number of media-tagged releases, how
        many of those carry any unmapped name, that ratio, the applied limit, and the
        ranked ``top_unmapped`` entries (``kind``, ``name``, ``releases``).

    Raises:
        UnknownProviderError: If ``provider`` is not one this aggregation knows.
    """
    table = _release_table(provider)
    limit = min(max(limit, 1), MAX_LIMIT)

    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await execute_sql(cur, _coverage_query(table))
        coverage = await cur.fetchone() or {}

        await execute_sql(cur, _top_names_query(table), (limit,))
        rows = await cur.fetchall()

    tagged = int(coverage.get("media_tagged_releases") or 0)
    with_unmapped = int(coverage.get("releases_with_unmapped") or 0)

    return {
        "provider": provider,
        "media_tagged_releases": tagged,
        "releases_with_unmapped": with_unmapped,
        "unmapped_rate": round(with_unmapped / tagged, 4) if tagged else 0.0,
        "limit": limit,
        "top_unmapped": [{"kind": row["kind"], "name": row["name"], "releases": int(row["releases"])} for row in rows],
    }
