"""Catalog re-attachment of load-order-split items — ADR 0014 section 8.

`musicbrainz-sql-loader` attaches a MusicBrainz release, release group, artist, or label to
its Discogs counterpart's native id only when the Discogs alias already resolves. When the
MusicBrainz row loads first it mints its own native id through its `musicbrainz` alias; the
Discogs row later mints a second item, and a reload cannot heal the split because
`common.identity.attach_aliases` never overwrites. The item is linked by the provider's own
assertion (`discogs_*_id` on the MusicBrainz row) yet lives under two native ids, and any
barcode or catalogue-number alias the MusicBrainz side attached first wins that identifier
against the Discogs item.

ADR 0014 assigns the repair to a job in this module, beside the `gm_id` projection ADR 0009
assigns catalog-api. It has two halves:

- :func:`run_census` is read-only. It runs in a `READ ONLY` transaction, so the server
  rejects any write it might ever issue, and it counts per kind what a re-attachment would
  touch.
- :func:`run_reattachment` (the write half) builds on it.

**Population.** A MusicBrainz row whose Discogs id resolves (a currently valid `discogs`
alias, native id *D*) and which is attached to some other native id *X*:

- *split*: its own `musicbrainz` alias resolves to *X* ≠ *D* — ADR 0014's population;
- *stale*: its `musicbrainz` alias already resolves to *D* but its `gm_item_id` cache still
  names *X*. This is the residue of the one race this job cannot lock out (see "Concurrency"
  below), so a re-run heals it rather than leaving it invisible.

A Discogs id that resolves to nothing is neither: the row is merely *unresolved*, counted and
left to the Discogs loader.

**Guards.** An item is skipped, reported, and never modified when its split native id:

- has a dependent in `artifacts`, `owned_copies`, `observations`, `user_collections`, or
  `user_wantlists` (ADR 0014 section 3's guard; it waits for the native-id merge decision);
- holds a current alias that is not this row's to move — a `discogs` alias, another
  MusicBrainz row's alias, or any namespace outside the identifier set below. That is a real
  item of its own, not a load-order orphan (for instance a MusicBrainz row whose Discogs link
  was edited), and moving its aliases would split it instead;
- holds a current alias whose `source` is not `catalog`. Re-inserting it as `catalog` would
  rewrite a person's or a heuristic's assertion as the provider's.

The census also counts the barcode and catalogue-number aliases a candidate's split native
id holds, and how many of those values the Discogs release's own record also carries — the
identifiers the MusicBrainz side won against the Discogs item.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from dataclasses import dataclass
from typing import Any, Final

import structlog
from common.config import get_secret, parse_postgres_host_port


logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class SplitKind:
    """Where one catalog kind's MusicBrainz rows live and how they alias."""

    table: str
    discogs_column: str
    entity_kind: str


# Fixed and closed: every table, column, and entity kind interpolated into the SQL below is one
# of these literals, chosen by this module, never by caller input. Column names are those of
# database-schema's `musicbrainz.*` tables; a release group aliases as `master`, the kind both
# loaders mint it under.
KINDS: Final[dict[str, SplitKind]] = {
    "release": SplitKind("releases", "discogs_release_id", "release"),
    "release_group": SplitKind("release_groups", "discogs_master_id", "master"),
    "artist": SplitKind("artists", "discogs_artist_id", "artist"),
    "label": SplitKind("labels", "discogs_label_id", "label"),
}

DEPENDENT_TABLES: Final[tuple[str, ...]] = ("artifacts", "owned_copies", "observations", "user_collections", "user_wantlists")

# The identifier namespaces (ADR 0011) an item's own record attaches beside its catalog alias.
# These, and the row's own `musicbrainz` alias, are the only aliases the job moves.
IDENTIFIER_PROVIDERS: Final[tuple[str, ...]] = ("barcode", "catalog_number", "isrc", "matrix")

GUARD_REASONS: Final[tuple[str, ...]] = ("dependents", "shared_native_id", "non_catalog_alias")

_IDENTIFIER_LIST: Final = ", ".join(f"'{provider}'" for provider in IDENTIFIER_PROVIDERS)

# Every MusicBrainz row carrying a Discogs id, beside the native ids its two aliases resolve
# to. Both joins are single probes of the partial unique index.
_LINKED: Final = """
SELECT mb.mbid                 AS mbid_key,
       mb.mbid::text           AS mbid,
       mb.{column}::text       AS discogs_id,
       mb.gm_item_id           AS gm_item_id,
       mb_alias.native_id      AS musicbrainz_native_id,
       discogs_alias.native_id AS discogs_native_id
FROM musicbrainz.{table} AS mb
LEFT JOIN provider_aliases AS discogs_alias
       ON discogs_alias.provider = 'discogs'
      AND discogs_alias.entity_kind = '{entity_kind}'
      AND discogs_alias.external_id = mb.{column}::text
      AND discogs_alias.valid_to IS NULL
LEFT JOIN provider_aliases AS mb_alias
       ON mb_alias.provider = 'musicbrainz'
      AND mb_alias.entity_kind = '{entity_kind}'
      AND mb_alias.external_id = mb.mbid::text
      AND mb_alias.valid_to IS NULL
WHERE mb.{column} IS NOT NULL
"""

_CANDIDATE: Final = """(
    linked.discogs_native_id IS NOT NULL
    AND linked.musicbrainz_native_id IS NOT NULL
    AND (linked.musicbrainz_native_id <> linked.discogs_native_id
         OR (linked.gm_item_id IS NOT NULL AND linked.gm_item_id <> linked.discogs_native_id))
)"""

_SPLIT_ID: Final = "CASE WHEN linked.musicbrainz_native_id <> linked.discogs_native_id THEN linked.musicbrainz_native_id ELSE linked.gm_item_id END"

# The guard flags for one split native id, shared by the census (correlated to a candidate
# row) and the per-item transaction (bound to parameters), so the two cannot disagree about
# what is guarded. An observation always hangs off an artifact or an owned copy, so it is
# reached through both.
_GUARDS: Final = f"""
EXISTS (SELECT 1 FROM artifacts AS dep WHERE dep.item_id = {{split_id}}) AS artifacts,
EXISTS (SELECT 1 FROM owned_copies AS dep WHERE dep.item_id = {{split_id}}) AS owned_copies,
(EXISTS (SELECT 1 FROM observations AS dep JOIN artifacts AS subject ON subject.id = dep.artifact_id
         WHERE subject.item_id = {{split_id}})
 OR EXISTS (SELECT 1 FROM observations AS dep JOIN owned_copies AS subject ON subject.id = dep.owned_copy_id
            WHERE subject.item_id = {{split_id}})) AS observations,
EXISTS (SELECT 1 FROM user_collections AS dep WHERE dep.gm_item_id = {{split_id}}) AS user_collections,
EXISTS (SELECT 1 FROM user_wantlists AS dep WHERE dep.gm_item_id = {{split_id}}) AS user_wantlists,
EXISTS (SELECT 1 FROM provider_aliases AS held
        WHERE held.native_id = {{split_id}} AND held.valid_to IS NULL
          AND NOT (held.provider IN ({_IDENTIFIER_LIST})
                   OR (held.provider = 'musicbrainz' AND held.entity_kind = '{{entity_kind}}' AND held.external_id = {{mbid}})))
    AS shared_native_id,
EXISTS (SELECT 1 FROM provider_aliases AS held
        WHERE held.native_id = {{split_id}} AND held.valid_to IS NULL AND held.source <> 'catalog') AS non_catalog_alias
"""  # noqa: S608 — composed of module constants only

_DEPENDENT_FLAGS: Final = " OR ".join(f"flags.{table}" for table in DEPENDENT_TABLES)

# One pass over the linked rows per kind. The LATERAL subquery yields a row only for a
# candidate, so the guard EXISTS probes run for candidates alone.
_CENSUS: Final = f"""
SELECT count(*) AS linked,
       count(*) FILTER (WHERE linked.discogs_native_id IS NULL) AS unresolved,
       count(*) FILTER (WHERE flags.split) AS split,
       count(*) FILTER (WHERE NOT flags.split) AS stale_gm_item_id,
       {", ".join(f"count(*) FILTER (WHERE flags.{table}) AS {table}" for table in DEPENDENT_TABLES)},
       count(*) FILTER (WHERE {_DEPENDENT_FLAGS}) AS dependents,
       count(*) FILTER (WHERE flags.shared_native_id) AS shared_native_id,
       count(*) FILTER (WHERE flags.non_catalog_alias) AS non_catalog_alias,
       count(*) FILTER (WHERE {_DEPENDENT_FLAGS} OR flags.shared_native_id OR flags.non_catalog_alias) AS guarded
FROM ({{linked}}) AS linked
LEFT JOIN LATERAL (
    SELECT linked.musicbrainz_native_id <> linked.discogs_native_id AS split,
           {_GUARDS.format(split_id=_SPLIT_ID, mbid="linked.mbid", entity_kind="{entity_kind}")}
    WHERE {_CANDIDATE}
) AS flags ON TRUE
"""  # noqa: S608 — composed of module constants only

_CENSUS_COLUMNS: Final[tuple[str, ...]] = (
    "linked",
    "unresolved",
    "split",
    "stale_gm_item_id",
    *DEPENDENT_TABLES,
    "dependents",
    "shared_native_id",
    "non_catalog_alias",
    "guarded",
)

# The Discogs release document carries its identifier aliases already normalized (ADR 0011's
# `identifiers.aliases`), so "the Discogs item also carries this value" is one containment
# test against the row its primary key finds. Only releases carry identifiers.
_CONTESTED_RELEASE: Final = """EXISTS (
    SELECT 1 FROM public.releases AS discogs
    WHERE discogs.data_id = linked.discogs_id
      AND discogs.data -> 'identifiers' -> 'aliases'
          @> jsonb_build_array(jsonb_build_object('provider', held.provider, 'external_id', held.external_id))
)"""

_IDENTIFIERS: Final = f"""
SELECT held.provider,
       count(*) AS held,
       count(*) FILTER (WHERE {{contested}}) AS contested
FROM ({{linked}}) AS linked
JOIN provider_aliases AS held
  ON held.native_id = {_SPLIT_ID}
 AND held.valid_to IS NULL
 AND held.provider IN ({_IDENTIFIER_LIST})
WHERE {_CANDIDATE}
GROUP BY held.provider
ORDER BY held.provider
"""  # noqa: S608 — composed of module constants only


def _linked_sql(kind: SplitKind) -> str:
    return _LINKED.format(table=kind.table, column=kind.discogs_column, entity_kind=kind.entity_kind)


def census_sql(kind: SplitKind) -> str:
    """Return the per-kind census statement (counts, dependents, guards)."""
    return _CENSUS.format(linked=_linked_sql(kind), entity_kind=kind.entity_kind)


def identifier_sql(kind: SplitKind) -> str:
    """Return the per-kind identifier-alias census statement."""
    contested = _CONTESTED_RELEASE if kind.entity_kind == "release" else "FALSE"
    return _IDENTIFIERS.format(linked=_linked_sql(kind), contested=contested)


async def run_census(pool: Any) -> dict[str, dict[str, Any]]:
    """Count, per kind, what a re-attachment would touch. Issues only SELECTs.

    Runs in one `REPEATABLE READ, READ ONLY` transaction, so every count describes the same
    snapshot and the server refuses any write. Two set-based statements per kind; no per-row
    round trips.

    Returns:
        ``{kind: {...}}`` for ``release``, ``release_group``, ``artist``, and ``label``, each
        with ``linked`` (rows carrying a Discogs id), ``unresolved`` (Discogs id with no
        current alias), ``split``, ``stale_gm_item_id``, ``eligible``, ``guarded``,
        ``guard_reasons`` (per reason; an item may carry several), ``dependents`` (items with
        a dependent, per table), and ``identifier_aliases`` (per identifier provider: ``held``
        by candidate split items, and ``contested`` — held for a value the Discogs item's own
        record also carries).
    """
    census: dict[str, dict[str, Any]] = {}
    async with pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
        await cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        for name, kind in KINDS.items():
            await cur.execute(census_sql(kind))
            counts = dict(zip(_CENSUS_COLUMNS, await cur.fetchone() or (0,) * len(_CENSUS_COLUMNS), strict=True))
            await cur.execute(identifier_sql(kind))
            identifiers = {provider: {"held": held, "contested": contested} for provider, held, contested in await cur.fetchall()}
            candidates = counts["split"] + counts["stale_gm_item_id"]
            census[name] = {
                "linked": counts["linked"],
                "unresolved": counts["unresolved"],
                "split": counts["split"],
                "stale_gm_item_id": counts["stale_gm_item_id"],
                "eligible": candidates - counts["guarded"],
                "guarded": counts["guarded"],
                "guard_reasons": {reason: counts[reason] for reason in GUARD_REASONS},
                "dependents": {table: counts[table] for table in DEPENDENT_TABLES},
                "identifier_aliases": {provider: identifiers.get(provider, {"held": 0, "contested": 0}) for provider in IDENTIFIER_PROVIDERS},
            }
            logger.info("📊 Re-attachment census", kind=name, **{key: value for key, value in census[name].items() if isinstance(value, int)})
    return census


def _connection_params() -> dict[str, Any]:
    """Read connection parameters from the environment, exiting on anything missing.

    Same variables and messaging as `api/projection.py`'s CLI.
    """
    address = os.environ.get("POSTGRES_HOST", "")
    params = {
        "dbname": os.environ.get("POSTGRES_DATABASE", ""),
        "user": get_secret("POSTGRES_USERNAME") or "",
        "password": get_secret("POSTGRES_PASSWORD") or "",
    }
    missing = [
        name
        for name, value in (
            ("POSTGRES_HOST", address),
            ("POSTGRES_USERNAME", params["user"]),
            ("POSTGRES_PASSWORD", params["password"]),
            ("POSTGRES_DATABASE", params["dbname"]),
        )
        if not value
    ]
    if missing:
        print(f"❌ Missing required environment variables: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)
    host, port = parse_postgres_host_port(address, int(os.getenv("POSTGRES_PORT", "5432") or "5432"))
    return {"host": host, "port": port, **params}


async def _run_once() -> dict[str, dict[str, Any]]:
    """Build a pool from the environment, run the census once, and close it."""
    from common import AsyncPostgreSQLPool  # noqa: PLC0415

    pool = AsyncPostgreSQLPool(connection_params=_connection_params(), max_connections=2, min_connections=1)
    await pool.initialize()
    try:
        return await run_census(pool)
    finally:
        await pool.close()


def _print_census(census: dict[str, dict[str, Any]]) -> None:
    for name, counts in census.items():
        print(
            f"  {name}: {counts['split']} split, {counts['stale_gm_item_id']} stale, {counts['eligible']} eligible, "
            f"{counts['guarded']} guarded {counts['guard_reasons']}, dependents {counts['dependents']}, "
            f"{counts['unresolved']} unresolved, identifiers {counts['identifier_aliases']}"
        )


def main() -> None:
    """Entry point for the catalog-identity-reattach CLI tool."""
    parser = argparse.ArgumentParser(
        prog="catalog-identity-reattach",
        description=(
            "Catalog re-attachment of load-order-split items (ADR 0014 section 8). Prints the "
            "read-only census of MusicBrainz releases, release groups, artists, and labels split "
            "from their Discogs native id by load order."
        ),
        epilog="Reads DB connection from environment variables: POSTGRES_HOST, POSTGRES_USERNAME, POSTGRES_PASSWORD, POSTGRES_DATABASE",
    )
    parser.parse_args()

    _connection_params()

    print("📋 Running catalog re-attachment census (read-only)…")
    _print_census(asyncio.run(_run_once()))
    print("✅ Census only; nothing was written.")


if __name__ == "__main__":
    main()
