"""Catalog re-attachment of load-order-split items — ADR 0014 section 8.

`musicbrainz-sql-loader` attaches a MusicBrainz release, release group, artist, or label to
its Discogs counterpart's native id only when the Discogs alias already resolves. When the
MusicBrainz row loads first it mints its own native id through its `musicbrainz` alias; the
Discogs row later mints a second item, and a reload cannot heal the split because
`common.identity.attach_aliases` never overwrites. The item is linked by the provider's own
assertion (`discogs_*_id` on the MusicBrainz row) yet lives under two native ids, and any
barcode or catalogue-number alias the MusicBrainz side attached first wins that identifier
against the Discogs item.

ADR 0014 assigns the repair to this job, beside the `gm_id` projection ADR 0009 assigns
catalog-api. It has two halves:

- :func:`run_census` is read-only. It runs in a `READ ONLY` transaction, so the server
  rejects any write it might ever issue, and it counts per kind what an applying run would
  touch. It is also the whole of a dry run.
- :func:`run_reattachment` with ``apply=True`` repairs each item in its own transaction:
  close every currently valid alias on the split native id, re-insert the same aliases
  against the Discogs native id as `source = 'catalog'`, and point that one MusicBrainz
  row's `gm_item_id` at the Discogs native id. The former native id is never deleted
  (ADR 0009 defines no merge).

`source = 'catalog'` is correct because every re-inserted alias is one the provider already
asserted — the value the loader would have written had the Discogs row arrived first. This
module is the only writer of `catalog` outside ingestion, and it writes nothing else.

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

**Identifier-alias rule.** The partial unique index on `(provider, entity_kind, external_id)
WHERE valid_to IS NULL` means a barcode or catalogue number held by the split item cannot at
the same time be held by the Discogs item: the split item won it. Closing the split row and
re-inserting the value against *D* is therefore exactly the attach the Discogs loader lost.
The re-insert is still `ON CONFLICT DO NOTHING`: if, when it runs, the Discogs item already
holds a current alias for the same value, it is not duplicated and the split item's row stays
closed (counted as an ``identifier_collision``). If any *other* item holds it, the item's
transaction is rolled back and counted as failed, because re-attaching would then drop an
alias rather than move it.

**Concurrency.** Each item's transaction locks, in order: the MusicBrainz row (`FOR UPDATE`,
so a loader mid-upsert of the same row finishes first and is re-read), the two alias rows
that define the split (`FOR UPDATE`, then re-verified, so a second job run or an item already
repaired is a no-op), and the split `catalog_items` row (`FOR UPDATE`, which conflicts with
the `FOR KEY SHARE` a foreign-key insert into `artifacts` or `owned_copies` takes, so the
dependents guard cannot be raced by one). The closing `UPDATE` then makes any concurrent
loader `attach_aliases` of the same key wait on this transaction, after which its `ON
CONFLICT DO NOTHING` + re-select converges on *D*. The remaining window is a loader whose
attach resolved *X* before this transaction started and whose row upsert lands after it
commits: that loader writes `gm_item_id = X` back and may attach new identifier aliases to
*X*. The row then falls into the *stale* population, and the next run moves it; the loader's
own next message for that row converges on *D* as well.

After an applying run, trigger the `gm_id` projection (`POST /api/admin/identity/project` or
`catalog-identity-projection`) so the graph follows the alias table.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Final
from uuid import UUID, uuid4

import structlog
from common.config import get_secret, parse_postgres_host_port

from api.audit_log import record_audit_entry


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

OUTCOMES: Final[tuple[str, ...]] = ("reattached", "guarded", "unchanged", "failed")

DEFAULT_BATCH_SIZE: Final[int] = 500

_FIRST_MBID: Final = "00000000-0000-0000-0000-000000000000"

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

# Keyset paging on the MusicBrainz primary key, so an applying run reads each candidate once
# however many pages it spans and never holds a transaction across a page.
_CANDIDATE_PAGE: Final = f"""
SELECT linked.mbid, linked.discogs_id, linked.mbid_key::text
FROM ({{linked}}) AS linked
WHERE {_CANDIDATE}
  AND linked.mbid_key > %s::uuid
ORDER BY linked.mbid_key
LIMIT %s
"""  # noqa: S608 — composed of module constants only

_LOCK_ROW: Final = "SELECT {column}::text, gm_item_id FROM musicbrainz.{table} WHERE mbid = %s::uuid FOR UPDATE"

_LOCK_ALIASES: Final = """
SELECT provider, native_id
FROM provider_aliases
WHERE valid_to IS NULL
  AND entity_kind = %s
  AND ((provider = 'musicbrainz' AND external_id = %s) OR (provider = 'discogs' AND external_id = %s))
FOR UPDATE
"""

_LOCK_ITEM: Final = "SELECT id FROM catalog_items WHERE id = %s FOR UPDATE"

_GUARD_ITEM: Final = "SELECT " + _GUARDS.format(split_id="%(split_id)s", mbid="%(mbid)s", entity_kind="{entity_kind}")

_CLOSE_ALIASES: Final = """
UPDATE provider_aliases
SET valid_to = now()
WHERE native_id = %s AND valid_to IS NULL
RETURNING provider, entity_kind, external_id, confidence
"""

# `now()` is the transaction's start, so each closed row's `valid_to` equals its successor's
# `valid_from` exactly. The ON CONFLICT names the partial unique index by its predicate.
_REINSERT_ALIASES: Final = """
INSERT INTO provider_aliases (provider, entity_kind, external_id, native_id, source, confidence, valid_from, asserted_at)
SELECT moved.provider, moved.entity_kind, moved.external_id, %s, 'catalog', moved.confidence, now(), now()
FROM unnest(%s::text[], %s::text[], %s::text[], %s::real[]) AS moved(provider, entity_kind, external_id, confidence)
ON CONFLICT (provider, entity_kind, external_id) WHERE valid_to IS NULL DO NOTHING
RETURNING provider, entity_kind, external_id
"""

_CURRENT_HOLDERS: Final = """
SELECT alias.provider, alias.entity_kind, alias.external_id, alias.native_id
FROM unnest(%s::text[], %s::text[], %s::text[]) AS k(provider, entity_kind, external_id)
JOIN provider_aliases AS alias
  ON alias.provider = k.provider AND alias.entity_kind = k.entity_kind AND alias.external_id = k.external_id
WHERE alias.valid_to IS NULL
"""

_SET_GM_ITEM_ID: Final = "UPDATE musicbrainz.{table} SET gm_item_id = %s WHERE mbid = %s::uuid"


def _linked_sql(kind: SplitKind) -> str:
    return _LINKED.format(table=kind.table, column=kind.discogs_column, entity_kind=kind.entity_kind)


def census_sql(kind: SplitKind) -> str:
    """Return the per-kind census statement (counts, dependents, guards)."""
    return _CENSUS.format(linked=_linked_sql(kind), entity_kind=kind.entity_kind)


def identifier_sql(kind: SplitKind) -> str:
    """Return the per-kind identifier-alias census statement."""
    contested = _CONTESTED_RELEASE if kind.entity_kind == "release" else "FALSE"
    return _IDENTIFIERS.format(linked=_linked_sql(kind), contested=contested)


def candidate_page_sql(kind: SplitKind) -> str:
    """Return the per-kind keyset page over split and stale candidates."""
    return _CANDIDATE_PAGE.format(linked=_linked_sql(kind))


class ReattachConflictError(RuntimeError):
    """A re-inserted alias is held by an item other than the Discogs one; the item rolls back."""


@dataclass(slots=True)
class ItemOutcome:
    """What one item's transaction did."""

    status: str
    reasons: tuple[str, ...] = ()
    split_id: UUID | None = None
    discogs_native_id: UUID | None = None
    aliases_moved: int = 0
    identifier_collisions: int = 0


@dataclass(slots=True)
class KindTally:
    """Per-kind counts of an applying run."""

    reattached: int = 0
    guarded: int = 0
    unchanged: int = 0
    failed: int = 0
    aliases_moved: int = 0
    identifier_collisions: int = 0
    guard_reasons: dict[str, int] = field(default_factory=lambda: dict.fromkeys(GUARD_REASONS, 0))

    def add(self, outcome: ItemOutcome) -> None:
        setattr(self, outcome.status, getattr(self, outcome.status) + 1)
        self.aliases_moved += outcome.aliases_moved
        self.identifier_collisions += outcome.identifier_collisions
        for reason in outcome.reasons:
            self.guard_reasons[reason] += 1

    def as_dict(self) -> dict[str, Any]:
        return {
            **{status: getattr(self, status) for status in OUTCOMES},
            "aliases_moved": self.aliases_moved,
            "identifier_collisions": self.identifier_collisions,
            "guard_reasons": dict(self.guard_reasons),
        }


def _guard_reasons(flags: tuple[Any, ...]) -> tuple[str, ...]:
    """Map one row of `_GUARDS` flags (dependents..., shared, non_catalog) to reason names."""
    dependents = flags[: len(DEPENDENT_TABLES)]
    shared, non_catalog = flags[len(DEPENDENT_TABLES) :]
    reasons: list[str] = []
    if any(dependents):
        reasons.append("dependents")
    if shared:
        reasons.append("shared_native_id")
    if non_catalog:
        reasons.append("non_catalog_alias")
    return tuple(reasons)


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


async def reattach_item(cur: Any, kind: SplitKind, mbid: str) -> ItemOutcome:
    """Re-attach one MusicBrainz row inside the caller's open transaction.

    Every decision is re-made under lock from the rows as they are now, not as the candidate
    page saw them, so a stale page, a concurrent run, or an already-repaired item is a no-op.
    A guarded item returns before any write. See the module docstring for the lock order and
    the identifier-alias rule.
    """
    await cur.execute(_LOCK_ROW.format(table=kind.table, column=kind.discogs_column), (mbid,))
    row = await cur.fetchone()
    if row is None or row[0] is None:
        return ItemOutcome("unchanged")
    discogs_id, gm_item_id = row

    await cur.execute(_LOCK_ALIASES, (kind.entity_kind, mbid, discogs_id))
    resolved = dict(await cur.fetchall())
    musicbrainz_native_id = resolved.get("musicbrainz")
    discogs_native_id = resolved.get("discogs")
    if musicbrainz_native_id is None or discogs_native_id is None:
        return ItemOutcome("unchanged")
    if musicbrainz_native_id != discogs_native_id:
        split_id = musicbrainz_native_id
    elif gm_item_id is not None and gm_item_id != discogs_native_id:
        split_id = gm_item_id
    else:
        return ItemOutcome("unchanged")

    await cur.execute(_LOCK_ITEM, (split_id,))
    await cur.execute(_GUARD_ITEM.format(entity_kind=kind.entity_kind), {"split_id": split_id, "mbid": mbid})
    reasons = _guard_reasons(tuple(await cur.fetchone() or ()))
    if reasons:
        return ItemOutcome("guarded", reasons, split_id, discogs_native_id)

    await cur.execute(_CLOSE_ALIASES, (split_id,))
    closed = await cur.fetchall()
    collisions = 0
    if closed:
        providers, entity_kinds, external_ids, confidences = (list(column) for column in zip(*closed, strict=True))
        await cur.execute(_REINSERT_ALIASES, (discogs_native_id, providers, entity_kinds, external_ids, confidences))
        inserted = {tuple(key) for key in await cur.fetchall()}
        missing = [key for key in zip(providers, entity_kinds, external_ids, strict=True) if key not in inserted]
        if missing:
            await cur.execute(_CURRENT_HOLDERS, tuple(list(column) for column in zip(*missing, strict=True)))
            holders = {(provider, entity_kind, external_id): native_id for provider, entity_kind, external_id, native_id in await cur.fetchall()}
            foreign = [key for key in missing if holders.get(key) != discogs_native_id]
            if foreign:
                raise ReattachConflictError(f"aliases held by another item: {foreign}")
            collisions = len(missing)

    await cur.execute(_SET_GM_ITEM_ID.format(table=kind.table), (discogs_native_id, mbid))
    return ItemOutcome("reattached", (), split_id, discogs_native_id, len(closed) - collisions, collisions)


async def _reattach_one(pool: Any, kind: SplitKind, mbid: str) -> ItemOutcome:
    async with pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
        return await reattach_item(cur, kind, mbid)


async def run_reattachment(pool: Any, *, apply: bool = False, batch_size: int = DEFAULT_BATCH_SIZE) -> dict[str, Any]:
    """Run the census and, only when ``apply`` is true, re-attach every eligible item.

    Dry run is the default. An applying run pages candidates per kind with a keyset cursor and
    repairs each in its own transaction; a guarded item is logged and skipped, an identifier
    conflict rolls that item back and is counted as failed, and any other database error ends
    the run with every committed item still committed.

    Returns:
        ``{"apply": bool, "census": {...}}`` plus, for an applying run, ``"outcomes"``: per
        kind, ``reattached``, ``guarded``, ``unchanged``, ``failed``, ``aliases_moved``,
        ``identifier_collisions``, and ``guard_reasons``.
    """
    report: dict[str, Any] = {"apply": apply, "census": await run_census(pool)}
    if not apply:
        return report

    outcomes: dict[str, dict[str, Any]] = {}
    for name, kind in KINDS.items():
        tally = KindTally()
        page_sql = candidate_page_sql(kind)
        cursor_key = _FIRST_MBID
        while True:
            async with pool.connection() as conn, conn.cursor() as cur:
                await cur.execute(page_sql, (cursor_key, batch_size))
                page = await cur.fetchall()
            if not page:
                break
            for mbid, discogs_id, key in page:
                try:
                    outcome = await _reattach_one(pool, kind, mbid)
                except ReattachConflictError as exc:
                    outcome = ItemOutcome("failed")
                    logger.error("❌ Re-attachment rolled back", kind=name, mbid=mbid, discogs_id=discogs_id, error=str(exc))
                else:
                    logger.info(
                        "🔗 Re-attachment item",
                        kind=name,
                        mbid=mbid,
                        discogs_id=discogs_id,
                        outcome=outcome.status,
                        reasons=list(outcome.reasons),
                        split_id=str(outcome.split_id) if outcome.split_id else None,
                        discogs_native_id=str(outcome.discogs_native_id) if outcome.discogs_native_id else None,
                        aliases_moved=outcome.aliases_moved,
                        identifier_collisions=outcome.identifier_collisions,
                    )
                tally.add(outcome)
                cursor_key = key
        outcomes[name] = tally.as_dict()
        logger.info(
            "✅ Re-attachment finished for kind", kind=name, **{key: value for key, value in outcomes[name].items() if isinstance(value, int)}
        )

    report["outcomes"] = outcomes
    logger.info("➡️ Re-attachment applied; run the gm_id projection next (POST /api/admin/identity/project or catalog-identity-projection)")
    return report


def audit_details(report: dict[str, Any], job_id: str) -> dict[str, Any]:
    """The `admin_audit_log` details for an applying run: the job id and per-kind outcomes."""
    return {"job_id": job_id, "apply": report["apply"], "outcomes": report.get("outcomes", {})}


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


_SELECT_ADMIN: Final = "SELECT is_admin FROM users WHERE id = %s::uuid AND is_active = true"


async def _is_admin(pool: Any, admin_id: str) -> bool:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(_SELECT_ADMIN, (admin_id,))
        row = await cur.fetchone()
    return bool(row and row[0])


async def _run_once(*, apply: bool, admin_id: str | None, batch_size: int, job_id: str) -> dict[str, Any] | None:
    """Build a pool from the environment, run the job once, audit an applying run, and close it."""
    from common import AsyncPostgreSQLPool  # noqa: PLC0415

    pool = AsyncPostgreSQLPool(connection_params=_connection_params(), max_connections=2, min_connections=1)
    await pool.initialize()
    try:
        if apply and not await _is_admin(pool, str(admin_id)):
            print(f"❌ --admin-id {admin_id} is not an active admin; nothing was changed.", file=sys.stderr)
            return None
        report = await run_reattachment(pool, apply=apply, batch_size=batch_size)
        if apply:
            await record_audit_entry(
                pool=pool, admin_id=str(admin_id), action="identity.reattach.apply", target=job_id, details=audit_details(report, job_id)
            )
        return report
    finally:
        await pool.close()


def _print_report(report: dict[str, Any]) -> None:
    for name, counts in report["census"].items():
        print(
            f"  {name}: {counts['split']} split, {counts['stale_gm_item_id']} stale, {counts['eligible']} eligible, "
            f"{counts['guarded']} guarded {counts['guard_reasons']}, dependents {counts['dependents']}, "
            f"{counts['unresolved']} unresolved, identifiers {counts['identifier_aliases']}"
        )
    for name, tally in report.get("outcomes", {}).items():
        print(
            f"  {name}: {tally['reattached']} re-attached, {tally['guarded']} guarded, {tally['unchanged']} unchanged, "
            f"{tally['failed']} failed, {tally['aliases_moved']} alias(es) moved, {tally['identifier_collisions']} identifier collision(s)"
        )


def main() -> None:
    """Entry point for the catalog-identity-reattach CLI tool."""
    parser = argparse.ArgumentParser(
        prog="catalog-identity-reattach",
        description=(
            "Catalog re-attachment of load-order-split items (ADR 0014 section 8). Without "
            "--apply it only prints the read-only census. With --apply it re-attaches each "
            "eligible MusicBrainz release, release group, artist, and label to its Discogs "
            "native id and records one admin audit entry."
        ),
        epilog="Reads DB connection from environment variables: POSTGRES_HOST, POSTGRES_USERNAME, POSTGRES_PASSWORD, POSTGRES_DATABASE",
    )
    parser.add_argument("--apply", action="store_true", help="Write the re-attachment (default: dry run, census only)")
    parser.add_argument("--admin-id", type=UUID, metavar="UUID", help="The admin user the audit entry is recorded against (required with --apply)")
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        metavar="N",
        help=f"Candidates read per page, per kind (default: {DEFAULT_BATCH_SIZE})",
    )
    args = parser.parse_args()

    if args.batch_size < 1:
        parser.error("--batch-size must be at least 1")
    if args.apply and args.admin_id is None:
        parser.error("--apply requires --admin-id")

    _connection_params()

    job_id = str(uuid4())
    print(f"📋 Running catalog re-attachment ({'apply' if args.apply else 'dry run'}), job {job_id}…")
    report = asyncio.run(
        _run_once(apply=args.apply, admin_id=str(args.admin_id) if args.admin_id else None, batch_size=args.batch_size, job_id=job_id)
    )
    if report is None:
        sys.exit(1)
    _print_report(report)
    if args.apply:
        print("✅ Done. Run catalog-identity-projection next so the graph's gm_id follows the alias table.")
    else:
        print("✅ Dry run only; nothing was written. Re-run with --apply --admin-id <uuid> to re-attach.")


if __name__ == "__main__":
    main()
