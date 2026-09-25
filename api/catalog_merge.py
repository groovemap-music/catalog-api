"""The native-id merge of two catalog items — ADR 0009's 2026-09-25 amendment.

When two native catalog items turn out to be one, the superseded item is kept, never deleted:
a `catalog_item_supersessions` row records that it now resolves to the survivor. The merge is
not a job of its own. It is steps 4-7 appended to each ADR 0014 transaction that moves a
superseded item's aliases, run by the caller inside that same open transaction after the
alias steps:

4. :func:`lock_items` locks both `catalog_items` rows, in id order, and checks they can merge.
5. :func:`merge_items` opens the supersession row and compresses chains,
6. re-points `artifacts.item_id` and `owned_copies.item_id` from the superseded item to the
   survivor, writing each re-pointed row to the `catalog_item_moves` ledger, and
7. recomputes the alias-table caches (`gm_item_id`) from the moved aliases.

:func:`revert_supersession` is the exact reversal, for ADR 0014 section 3's revert and the
section 8 contradiction revert. It runs inside the transaction that has already moved the
aliases back, closes the row, moves back exactly the ledgered rows that still point at the
survivor, re-opens the rows the merge closed by chain compression, and recomputes the caches.
Only the section 8 re-attachment (`api/reattach.py`) calls into this module today; the
promotion path will call the same two functions when it is built.

**What is and is not written.** A merge never deletes a row. On a user-owned row it writes
only the catalog-item reference: not `updated_at`, not `user_id`, not the row id, and no
user-authored value. It emits no event and needs no consent purpose. Observations and
snapshots are not touched, because owned copies and artifacts keep their ids. Activity rows
are immutable history and are read through `public.resolve_catalog_item`. The caches are not
ledgered, because the alias table can always reproduce them.

**Chains.** Resolution is always one hop: a current survivor is never itself currently
superseded. When *X*, which survives *A*, is superseded into *D*, the same transaction closes
*A → X* and opens *A → D* with `via_id` naming the *X → D* row (keeping *A → X*'s cause and
decision). A revert of *X → D* closes every open row whose `via_id` is *X → D* and re-opens its
predecessor. A compressed row is never reverted directly: the merge that compressed it is
reverted first, in reverse order, which re-opens the row it replaced.

**Locks.** Both items are locked in id order so two merges or reverts over overlapping pairs
cannot deadlock. The superseded item is locked `FOR UPDATE`, which conflicts with the
`FOR KEY SHARE` a foreign-key insert into `artifacts` or `owned_copies` takes: no copy or
artifact can be created against it while its dependents move and be left behind. The survivor
is locked `FOR NO KEY UPDATE` by a merge, which serializes it against any other merge or revert
of that item but not against a loader attaching an alias to it or a copy created against it,
both of which are correct against a survivor. A revert locks both `FOR UPDATE`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final
from uuid import UUID


CAUSES: Final[tuple[str, ...]] = ("edition_promotion", "catalog_reattachment")

# The asserted references a merge re-points and ledgers, and the column naming each row's
# owner. An artifact's owner is its creator; the ledger admits NULL for one nobody created.
MOVED_TABLES: Final[dict[str, str]] = {"artifacts": "created_by", "owned_copies": "user_id"}


@dataclass(frozen=True, slots=True)
class CacheTable:
    """One table carrying a `gm_item_id` cache, and the alias its value is resolved through."""

    table: str
    key: str
    provider: str
    entity_kind: str


# Every `gm_item_id` cache per catalog kind: the Discogs entity tables, the MusicBrainz tables
# (a release group aliases as `master`), and the collection and wantlist rows the sync fills
# from the Discogs release alias. Each column is indexed, so a recompute is index probes.
# Fixed and closed: every name interpolated into the SQL below is one of these literals.
CACHE_TABLES: Final[dict[str, tuple[CacheTable, ...]]] = {
    "release": (
        CacheTable("public.releases", "data_id", "discogs", "release"),
        CacheTable("musicbrainz.releases", "mbid", "musicbrainz", "release"),
        CacheTable("user_collections", "release_id", "discogs", "release"),
        CacheTable("user_wantlists", "release_id", "discogs", "release"),
    ),
    "master": (
        CacheTable("public.masters", "data_id", "discogs", "master"),
        CacheTable("musicbrainz.release_groups", "mbid", "musicbrainz", "master"),
    ),
    "artist": (
        CacheTable("public.artists", "data_id", "discogs", "artist"),
        CacheTable("musicbrainz.artists", "mbid", "musicbrainz", "artist"),
    ),
    "label": (
        CacheTable("public.labels", "data_id", "discogs", "label"),
        CacheTable("musicbrainz.labels", "mbid", "musicbrainz", "label"),
    ),
}

CACHE_TABLE_NAMES: Final[tuple[str, ...]] = tuple(dict.fromkeys(cache.table for caches in CACHE_TABLES.values() for cache in caches))

_LOCK_ITEM: Final = "SELECT kind FROM catalog_items WHERE id = %s FOR {strength}"

_CURRENT_SURVIVORS: Final = """
SELECT superseded_id, id, survivor_id
FROM catalog_item_supersessions
WHERE superseded_id = ANY(%s::uuid[]) AND valid_to IS NULL
"""

_OPEN: Final = """
INSERT INTO catalog_item_supersessions (superseded_id, survivor_id, cause, decision_ref, valid_from)
VALUES (%s, %s, %s, %s, now())
RETURNING id
"""

# Chain compression, first half: every row that X currently survives is closed. `now()` is
# the transaction's start, so each closed row's `valid_to` equals its successor's `valid_from`,
# which is how a revert finds the predecessor again.
_CLOSE_SURVIVED: Final = """
UPDATE catalog_item_supersessions
SET valid_to = now()
WHERE survivor_id = %s AND valid_to IS NULL
RETURNING superseded_id, cause, decision_ref
"""

_OPEN_COMPRESSED: Final = """
INSERT INTO catalog_item_supersessions (superseded_id, survivor_id, cause, decision_ref, valid_from, via_id)
SELECT compressed.superseded_id, %s, compressed.cause, compressed.decision_ref, now(), %s
FROM unnest(%s::uuid[], %s::text[], %s::uuid[]) AS compressed(superseded_id, cause, decision_ref)
"""

# Step 6, one statement per table: re-point, ledger each re-pointed row with its owner, and
# count. The ledger's unique key admits a row once per supersession, so a row that somehow
# returned to the superseded item and moves again keeps its first entry, which already
# records the move a revert has to undo.
_REPOINT: Final = """
WITH moved AS (
    UPDATE {table} SET item_id = %(survivor)s WHERE item_id = %(superseded)s RETURNING id, {owner} AS user_id
), ledgered AS (
    INSERT INTO catalog_item_moves (supersession_id, table_name, row_id, from_item_id, to_item_id, user_id, moved_at)
    SELECT %(supersession)s, '{table}', moved.id, %(superseded)s, %(survivor)s, moved.user_id, now() FROM moved
    ON CONFLICT (supersession_id, table_name, row_id) DO NOTHING
)
SELECT count(*) FROM moved
"""

# Step 7: a cached row on either item takes whatever its own alias resolves to now. A row with
# no current alias is left alone: there is nothing to recompute it from.
_RECOMPUTE: Final = """
WITH recomputed AS (
    UPDATE {table} AS cached
    SET gm_item_id = alias.native_id
    FROM provider_aliases AS alias
    WHERE cached.gm_item_id = ANY(%s::uuid[])
      AND alias.provider = '{provider}'
      AND alias.entity_kind = '{entity_kind}'
      AND alias.external_id = cached.{key}::text
      AND alias.valid_to IS NULL
      AND alias.native_id <> cached.gm_item_id
    RETURNING 1
)
SELECT count(*) FROM recomputed
"""

_LOCK_SUPERSESSION: Final = "SELECT superseded_id, survivor_id, valid_to IS NULL, via_id FROM catalog_item_supersessions WHERE id = %s"

_CLOSE: Final = "UPDATE catalog_item_supersessions SET valid_to = now() WHERE id = %s"

_CLOSE_COMPRESSED: Final = """
UPDATE catalog_item_supersessions
SET valid_to = now()
WHERE via_id = %s AND valid_to IS NULL
RETURNING superseded_id, valid_from
"""

# The predecessor of a compressed row A → C (via B → C) is the A → B row closed in the same
# transaction that opened A → C, so its `valid_to` is exactly A → C's `valid_from`.
_REOPEN_PREDECESSORS: Final = """
UPDATE catalog_item_supersessions AS predecessor
SET valid_to = NULL
FROM unnest(%s::uuid[], %s::timestamptz[]) AS compressed(superseded_id, valid_from)
WHERE predecessor.superseded_id = compressed.superseded_id
  AND predecessor.survivor_id = %s
  AND predecessor.valid_to = compressed.valid_from
RETURNING predecessor.id
"""

_MOVE_BACK: Final = """
WITH moved AS (
    UPDATE {table} AS target
    SET item_id = ledger.from_item_id
    FROM catalog_item_moves AS ledger
    WHERE ledger.supersession_id = %s
      AND ledger.table_name = '{table}'
      AND target.id = ledger.row_id
      AND target.item_id = ledger.to_item_id
    RETURNING 1
)
SELECT count(*) FROM moved
"""


class MergeConflictError(RuntimeError):
    """The two items cannot merge, or the supersession cannot revert, as they stand now.

    The caller lets it propagate out of its transaction, which then rolls back whole: the
    alias steps included, so an item is never left re-attached without its merge.
    """


@dataclass(slots=True)
class MergeOutcome:
    """What steps 5-7 did for one pair: per-table counts only, never a row or user id."""

    supersession_id: UUID
    opened: bool = True
    chains_compressed: int = 0
    moved: dict[str, int] = field(default_factory=lambda: dict.fromkeys(MOVED_TABLES, 0))
    caches_recomputed: dict[str, int] = field(default_factory=dict)


@dataclass(slots=True)
class RevertOutcome:
    """What a revert did: per-table counts only."""

    supersession_id: UUID
    chains_reopened: int = 0
    moved_back: dict[str, int] = field(default_factory=lambda: dict.fromkeys(MOVED_TABLES, 0))
    caches_recomputed: dict[str, int] = field(default_factory=dict)


async def _count(cur: Any, sql: str, params: Any) -> int:
    await cur.execute(sql, params)
    row = await cur.fetchone()
    return int(row[0]) if row else 0


async def lock_items(cur: Any, superseded_id: UUID, survivor_id: UUID, *, revert: bool = False) -> str:
    """Step 4: lock both items in id order and return their shared kind.

    Raises:
        MergeConflictError: The ids are equal, an item does not exist, or the kinds differ.
    """
    if superseded_id == survivor_id:
        raise MergeConflictError(f"an item cannot supersede itself: {superseded_id}")
    kinds: dict[UUID, str] = {}
    for item_id in sorted((superseded_id, survivor_id)):
        strength = "UPDATE" if revert or item_id == superseded_id else "NO KEY UPDATE"
        await cur.execute(_LOCK_ITEM.format(strength=strength), (item_id,))
        row = await cur.fetchone()
        if row is None:
            raise MergeConflictError(f"catalog item {item_id} does not exist")
        kinds[item_id] = row[0]
    if kinds[superseded_id] != kinds[survivor_id]:
        raise MergeConflictError(f"kinds differ: {superseded_id} is a {kinds[superseded_id]}, {survivor_id} is a {kinds[survivor_id]}")
    return kinds[superseded_id]


async def current_supersession(cur: Any, superseded_id: UUID, survivor_id: UUID) -> UUID | None:
    """Check, under the step 4 locks, that the pair can merge; return an already-open row.

    Returns the id of an open *superseded → survivor* row when one exists (a re-run over the
    same pair adds to it rather than opening a second), otherwise None.

    Raises:
        MergeConflictError: The survivor is itself currently superseded (resolution would take
            two hops), or the superseded item currently resolves to a different survivor.
    """
    await cur.execute(_CURRENT_SURVIVORS, ([superseded_id, survivor_id],))
    current = {row[0]: (row[1], row[2]) for row in await cur.fetchall()}
    if survivor_id in current:
        raise MergeConflictError(f"survivor {survivor_id} is itself superseded by {current[survivor_id][1]}")
    if superseded_id in current:
        supersession_id, resolved = current[superseded_id]
        if resolved != survivor_id:
            raise MergeConflictError(f"{superseded_id} is already superseded by {resolved}, not {survivor_id}")
        return UUID(str(supersession_id))
    return None


async def recompute_caches(cur: Any, kind: str, item_ids: list[UUID]) -> dict[str, int]:
    """Step 7: re-resolve every `gm_item_id` cache of this kind that names one of the items."""
    return {cache.table: await _count(cur, _RECOMPUTE.format(**_cache_names(cache)), (item_ids,)) for cache in CACHE_TABLES[kind]}


def _cache_names(cache: CacheTable) -> dict[str, str]:
    return {"table": cache.table, "key": cache.key, "provider": cache.provider, "entity_kind": cache.entity_kind}


async def merge_items(
    cur: Any, superseded_id: UUID, survivor_id: UUID, *, kind: str, cause: str, decision_ref: UUID, existing: UUID | None = None
) -> MergeOutcome:
    """Steps 5-7 inside the caller's transaction, after its alias steps and :func:`lock_items`.

    Args:
        kind: The shared kind :func:`lock_items` returned.
        cause: ``edition_promotion`` or ``catalog_reattachment``.
        decision_ref: What authorized the merge — the `matching` decision row for a promotion,
            the `admin_audit_log` entry id of the run for a re-attachment.
        existing: The open row :func:`current_supersession` returned, if any. Its chains were
            compressed when it opened, so only steps 6 and 7 run again.
    """
    if cause not in CAUSES:
        raise ValueError(f"unknown supersession cause: {cause}")
    if existing is not None:
        outcome = MergeOutcome(existing, opened=False)
    else:
        await cur.execute(_OPEN, (superseded_id, survivor_id, cause, decision_ref))
        row = await cur.fetchone()
        outcome = MergeOutcome(UUID(str(row[0])))
        await cur.execute(_CLOSE_SURVIVED, (superseded_id,))
        survived = await cur.fetchall()
        if survived:
            predecessors, causes, decisions = (list(column) for column in zip(*survived, strict=True))
            await cur.execute(_OPEN_COMPRESSED, (survivor_id, outcome.supersession_id, predecessors, causes, decisions))
            outcome.chains_compressed = len(survived)

    params = {"survivor": survivor_id, "superseded": superseded_id, "supersession": outcome.supersession_id}
    for table, owner in MOVED_TABLES.items():
        outcome.moved[table] = await _count(cur, _REPOINT.format(table=table, owner=owner), params)
    outcome.caches_recomputed = await recompute_caches(cur, kind, [superseded_id, survivor_id])
    return outcome


async def revert_supersession(cur: Any, supersession_id: UUID) -> RevertOutcome:
    """Revert one merge inside the caller's transaction, after it has moved the aliases back.

    Closes the supersession, re-opens every row its chain compression closed, moves back
    exactly the ledgered rows that still point at the survivor (a row created against the
    survivor since, or moved elsewhere since, stays where it is), and recomputes the caches of
    both items. The ledger rows are kept as history.

    Raises:
        MergeConflictError: The row does not exist, is already closed, or is itself a
            compressed row (``via_id`` set): the later merge that compressed it must be reverted
            first, which re-opens the row it replaced.
    """
    await cur.execute(_LOCK_SUPERSESSION, (supersession_id,))
    row = await cur.fetchone()
    if row is None:
        raise MergeConflictError(f"supersession {supersession_id} does not exist")
    superseded_id, survivor_id = row[0], row[1]
    kind = await lock_items(cur, superseded_id, survivor_id, revert=True)
    # Re-read under the item locks: a concurrent merge or revert of either item may have
    # closed or compressed this row between the unlocked read and the locks.
    await cur.execute(_LOCK_SUPERSESSION + " FOR UPDATE", (supersession_id,))
    _superseded, _survivor, is_open, via_id = await cur.fetchone()
    if not is_open:
        raise MergeConflictError(f"supersession {supersession_id} is not current")
    if via_id is not None:
        raise MergeConflictError(f"supersession {supersession_id} was compressed by {via_id}; revert that merge first")

    outcome = RevertOutcome(supersession_id)
    await cur.execute(_CLOSE, (supersession_id,))
    await cur.execute(_CLOSE_COMPRESSED, (supersession_id,))
    compressed = await cur.fetchall()
    if compressed:
        items, opened_at = (list(column) for column in zip(*compressed, strict=True))
        await cur.execute(_REOPEN_PREDECESSORS, (items, opened_at, superseded_id))
        outcome.chains_reopened = len(await cur.fetchall())
        if outcome.chains_reopened != len(compressed):
            raise MergeConflictError(f"supersession {supersession_id}: {len(compressed)} compressed row(s), {outcome.chains_reopened} predecessor(s)")

    for table in MOVED_TABLES:
        outcome.moved_back[table] = await _count(cur, _MOVE_BACK.format(table=table), (supersession_id,))
    outcome.caches_recomputed = await recompute_caches(cur, kind, [superseded_id, survivor_id])
    return outcome
