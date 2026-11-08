"""Reads behind the ``admin_identity_ledger_*`` routes: the Identity Ledger over PostgreSQL.

ADR 0016 section 3 records the departure: the ledger's history lives only in PostgreSQL
(alias validity windows, supersessions, and dated link changes), so these are read-only
PostgreSQL queries behind ``require_admin`` rather than Neo4j reads. Every function runs in
one ``REPEATABLE READ, READ ONLY`` transaction, so each answer describes one snapshot and the
server refuses any write.

The definitions are spike ``gm-design-loj9``'s (``sql/identity_history.sql``), with the dated
link changes of ``identity_link_events`` added as events (``api/identity_ledger.py``):

- An **edition** is a release-kind native item resolved one hop through an open supersession.
  A **work** is a master-kind item: a Discogs master or a MusicBrainz release group.
- An edition's **works** come from each catalog's own release row: a Discogs release's master
  (``graph.derived_from``, the relational form of ``data ->> 'master_id'``) and a MusicBrainz
  release's release group, each resolved through its current alias.
- An edition's **events** are everything after its items' mints: aliases opened later (the
  first MusicBrainz one on an item that already holds a Discogs alias is ``linked_at_ingest``),
  aliases closed other than by a supersession, supersessions opening and closing, and the
  ``identity_link_events`` rows of the MusicBrainz releases it currently holds. An alias opened
  or closed at the instant of a supersession of its item was moved by it, not attached.

The works list and density read database-schema's precomputed views (epic
``gm-database-schema-k8e9``): ``identity_ledger_edition_events``,
``identity_ledger_edition_parents``, ``identity_ledger_work_summary``, and
``identity_ledger_density`` hold what those two statements computed per request from the whole
history, refreshed off the request path by ``identity_ledger_refresh()``. Every payload carries
``refreshed_at``, when the last refresh started. Until the first refresh the views are
unpopulated, and the list and density raise ``LedgerNotRefreshed`` rather than answer an empty
list. One work's planes keep reading the base tables live, because ``as_of`` needs the alias
history itself.

Nothing here reads ``catalog_item_moves.user_id`` or ``row_id``, or any admin id: moves are
counted per supersession and table.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from common.query_debug import execute_sql
from psycopg.errors import ObjectNotInPrerequisiteState

from api.identity_ledger import DEFAULT_EDITION_LIMIT, LedgerRows, build_work, iso


_READ_ONLY = "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"


class LedgerNotRefreshed(Exception):
    """The precomputed views a read needs have not been refreshed yet; never an empty answer."""


# Open supersessions name each superseded item's one survivor; COALESCE through this join is
# public.resolve_catalog_item without a function call per row.
_RESOLVED = "LEFT JOIN catalog_item_supersessions AS {alias} ON {alias}.superseded_id = {item} AND {alias}.valid_to IS NULL"

# The editions of every work in `work_member (work_id, item_id)`: the work's items' current
# Discogs master and MusicBrainz release group aliases, each catalog's release rows naming
# them, and those rows' current release aliases, resolved one hop.
_EDITIONS_OF_WORK = f"""
work_master AS MATERIALIZED (
    SELECT work_member.work_id, alias.provider, alias.external_id
    FROM work_member
    JOIN provider_aliases AS alias ON alias.native_id = work_member.item_id
    WHERE alias.entity_kind = 'master' AND alias.valid_to IS NULL AND alias.provider IN ('discogs', 'musicbrainz')
),
work_release_group AS MATERIALIZED (
    SELECT work_id, external_id::uuid AS release_group_mbid
    FROM work_master
    WHERE provider = 'musicbrainz'
),
edition_of AS (
    SELECT work_master.work_id, COALESCE(edition_open.survivor_id, release.native_id) AS edition_id
    FROM work_master
    JOIN graph.derived_from AS derived ON derived.master_id = work_master.external_id
    JOIN provider_aliases AS release
      ON release.provider = 'discogs' AND release.entity_kind = 'release'
     AND release.external_id = derived.release_id AND release.valid_to IS NULL
    {_RESOLVED.format(alias="edition_open", item="release.native_id")}
    WHERE work_master.provider = 'discogs'
    UNION
    SELECT work_release_group.work_id, COALESCE(edition_open.survivor_id, release.native_id)
    FROM work_release_group
    JOIN musicbrainz.releases AS mb_release ON mb_release.release_group_mbid = work_release_group.release_group_mbid
    JOIN provider_aliases AS release
      ON release.provider = 'musicbrainz' AND release.entity_kind = 'release'
     AND release.external_id = mb_release.mbid::text AND release.valid_to IS NULL
    {_RESOLVED.format(alias="edition_open", item="release.native_id")}
),
member AS (
    SELECT DISTINCT edition_id, edition_id AS item_id FROM edition_of
    UNION
    SELECT supersession.survivor_id, supersession.superseded_id
    FROM catalog_item_supersessions AS supersession
    WHERE supersession.valid_to IS NULL AND supersession.survivor_id IN (SELECT edition_id FROM edition_of)
)"""  # noqa: S608 — composed of module constants only

# Every work parent of every edition in `member (edition_id, item_id)`: the Discogs master its
# current Discogs release aliases' rows name, and the release group its current MusicBrainz
# release aliases' rows name, each through that master's current alias, resolved one hop.
_PARENTS_OF_MEMBER = f"""
member_mbid AS MATERIALIZED (
    SELECT member.edition_id, release.external_id::uuid AS mbid
    FROM member
    JOIN provider_aliases AS release ON release.native_id = member.item_id
    WHERE release.provider = 'musicbrainz' AND release.entity_kind = 'release' AND release.valid_to IS NULL
),
edition_parent AS (
    SELECT member.edition_id, COALESCE(work_open.survivor_id, master.native_id) AS work_id
    FROM member
    JOIN provider_aliases AS release
      ON release.native_id = member.item_id AND release.provider = 'discogs'
     AND release.entity_kind = 'release' AND release.valid_to IS NULL
    JOIN graph.derived_from AS derived ON derived.release_id = release.external_id
    JOIN provider_aliases AS master
      ON master.provider = 'discogs' AND master.entity_kind = 'master'
     AND master.external_id = derived.master_id AND master.valid_to IS NULL
    {_RESOLVED.format(alias="work_open", item="master.native_id")}
    UNION
    SELECT member_mbid.edition_id, COALESCE(work_open.survivor_id, master.native_id)
    FROM member_mbid
    JOIN musicbrainz.releases AS mb_release ON mb_release.mbid = member_mbid.mbid
    JOIN provider_aliases AS master
      ON master.provider = 'musicbrainz' AND master.entity_kind = 'master'
     AND master.external_id = mb_release.release_group_mbid::text AND master.valid_to IS NULL
    {_RESOLVED.format(alias="work_open", item="master.native_id")}
)"""  # noqa: S608 — composed of module constants only

_REPAIRS = "('reattached', 'promoted', 'reverted')"

# The precomputed views (database-schema, epic gm-database-schema-k8e9) that the works list and
# density read, as `pg_class` names them. Each route checks its own before reading.
_WORKS_VIEWS = ("identity_ledger_edition_events", "identity_ledger_edition_parents", "identity_ledger_work_summary")
_DENSITY_VIEWS = ("identity_ledger_density",)

# Whether every named view has been refreshed at least once, and when the last refresh started.
# The relation is found by name, whatever its kind: the views are materialized views through
# database-schema v0.6.3 and plain tables from gm-database-schema-xen5, which keeps the same names.
# A missing state row leaves refreshed_at NULL, which is also not refreshed.
# `pg_class.relispopulated` takes no lock on a view, so a request that arrives during the first
# build (a plain REFRESH under ACCESS EXCLUSIVE) is answered at once instead of queuing behind it.
_FRESHNESS = """
SELECT count(*) FILTER (WHERE view.relispopulated) = cardinality(%(views)s::text[]),
       (SELECT refreshed_at FROM identity_ledger_refresh_state)
FROM pg_class AS view
WHERE view.relnamespace = 'public'::regnamespace AND view.relname = ANY(%(views)s::text[])
"""

_REFRESHED_AT = "SELECT refreshed_at FROM identity_ledger_refresh_state"

# Works with evented editions, read from the views. Without a window it pages the per-work
# summary for the event filter ('any' is every event type). A window reads the edition events in
# its range and counts them per work through the editions' parents, as the live statement did.
# per_work is NOT MATERIALIZED so the unwindowed count and page each plan alone (the page walks
# idx_identity_ledger_work_summary_page). _WORKS_WINDOWED materializes `windowed`, so a window's
# aggregation runs once and feeds both the count and the page. Inlined, it ran twice (count and page),
# which doubled the one-month window's time. Materializing it for the unwindowed read made that read
# slower (320 ms to about 1.9 s on the real catalog), so only a window takes this variant.
_WORKS = f"""
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
    SELECT edition_id, min(at) AS first_at, max(at) AS last_at, bool_or(type IN {_REPAIRS}) AS repaired
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

_WORKS_WINDOWED_RAW = _WORKS.replace("windowed AS (", "windowed AS MATERIALIZED (", 1)

# The windowed read from database-schema's per-day rollup (`identity_ledger_work_summary_day`,
# v0.11.0): the window's full UTC days [first_day, end_day) come from the rollup and only the
# partial edge days (`since` up to the first midnight, the last midnight up to `until`) from raw
# edition events. The rollup's grain is (event, day, prev_day, work_id), `prev_day` being that
# edition's previous event day of the same filter ('-infinity' for its first), so an edition counts
# once, at its first day in the range: `prev_day < first_day`. An edge-day edition that already has
# an event of the same kind inside the full-day range is skipped, or the work counts it twice
# (once here, once in the rollup); `covered_any` and `covered_repaired` decide that separately, as an
# edition's evented and repaired counts come from different rows. first_event and last_event take
# every rollup row in range and every edge event. Same results as `_WORKS_WINDOWED_RAW` (the live
# aggregation, kept for a rollup that is not populated yet).
#   full: whether any full day exists; head_end/tail_start: the edge ranges' bounds, NULL for none;
#   first_day/end_day: the full days' bounds as dates, NULL for unbounded; first_at/end_at: the same as instants.
_WORKS_WINDOWED = f"""
WITH day_row AS (
    SELECT work_id, event, editions, first_event, last_event,
           (prev_day IS NULL OR prev_day = '-infinity' OR prev_day < %(first_day)s::date) AS first_in_range
    FROM identity_ledger_work_summary_day
    WHERE %(full)s::boolean
      AND event IN (COALESCE(%(event)s::text, 'any'), 'repaired')
      AND (%(first_day)s::date IS NULL OR day >= %(first_day)s::date)
      AND (%(end_day)s::date IS NULL OR day < %(end_day)s::date)
),
rolled AS (
    SELECT work_id,
           COALESCE(sum(editions) FILTER (WHERE event = COALESCE(%(event)s::text, 'any') AND first_in_range), 0) AS evented_editions,
           COALESCE(sum(editions) FILTER (WHERE first_in_range AND event = CASE
               WHEN %(event)s::text IS NULL THEN 'repaired'
               WHEN %(event)s::text IN {_REPAIRS} THEN %(event)s::text END), 0) AS repaired_editions,
           min(first_event) FILTER (WHERE event = COALESCE(%(event)s::text, 'any')) AS first_event,
           max(last_event) FILTER (WHERE event = COALESCE(%(event)s::text, 'any')) AS last_event
    FROM day_row
    GROUP BY work_id
    HAVING bool_or(event = COALESCE(%(event)s::text, 'any'))
),
edge_event AS (
    SELECT edition_id, at, type
    FROM identity_ledger_edition_events
    WHERE %(head_end)s::timestamptz IS NOT NULL
      AND (%(event)s::text IS NULL OR type = %(event)s::text)
      AND (%(since)s::timestamptz IS NULL OR at >= %(since)s::timestamptz)
      AND at < %(head_end)s::timestamptz
    UNION ALL
    SELECT edition_id, at, type
    FROM identity_ledger_edition_events
    WHERE %(tail_start)s::timestamptz IS NOT NULL
      AND (%(event)s::text IS NULL OR type = %(event)s::text)
      AND at >= %(tail_start)s::timestamptz
      AND (%(until)s::timestamptz IS NULL OR at < %(until)s::timestamptz)
),
edge_edition AS (
    SELECT edition_id, min(at) AS first_at, max(at) AS last_at, bool_or(type IN {_REPAIRS}) AS repaired
    FROM edge_event
    GROUP BY edition_id
),
edge_flagged AS (
    SELECT edge.*,
           %(full)s::boolean AND EXISTS (
               SELECT 1 FROM identity_ledger_edition_events AS inside
               WHERE inside.edition_id = edge.edition_id
                 AND (%(event)s::text IS NULL OR inside.type = %(event)s::text)
                 AND (%(first_at)s::timestamptz IS NULL OR inside.at >= %(first_at)s::timestamptz)
                 AND (%(end_at)s::timestamptz IS NULL OR inside.at < %(end_at)s::timestamptz)
           ) AS covered_any,
           %(full)s::boolean AND edge.repaired AND EXISTS (
               SELECT 1 FROM identity_ledger_edition_events AS inside
               WHERE inside.edition_id = edge.edition_id AND inside.type IN {_REPAIRS}
                 AND (%(event)s::text IS NULL OR inside.type = %(event)s::text)
                 AND (%(first_at)s::timestamptz IS NULL OR inside.at >= %(first_at)s::timestamptz)
                 AND (%(end_at)s::timestamptz IS NULL OR inside.at < %(end_at)s::timestamptz)
           ) AS covered_repaired
    FROM edge_edition AS edge
),
edge_work AS (
    SELECT parent.work_id,
           count(*) FILTER (WHERE NOT edge.covered_any) AS evented_editions,
           count(*) FILTER (WHERE edge.repaired AND NOT edge.covered_repaired) AS repaired_editions,
           min(edge.first_at) AS first_event, max(edge.last_at) AS last_event
    FROM identity_ledger_edition_parents AS parent
    JOIN edge_flagged AS edge ON edge.edition_id = parent.edition_id
    GROUP BY parent.work_id
),
windowed AS MATERIALIZED (
    SELECT work_id,
           (COALESCE(rolled.evented_editions, 0) + COALESCE(edge_work.evented_editions, 0))::bigint AS evented_editions,
           (COALESCE(rolled.repaired_editions, 0) + COALESCE(edge_work.repaired_editions, 0))::bigint AS repaired_editions,
           LEAST(rolled.first_event, edge_work.first_event) AS first_event,
           GREATEST(rolled.last_event, edge_work.last_event) AS last_event
    FROM rolled
    FULL JOIN edge_work USING (work_id)
)
SELECT total.works, page.work_id, page.evented_editions, page.repaired_editions, page.first_event, page.last_event
FROM (SELECT count(*) AS works FROM windowed) AS total
LEFT JOIN LATERAL (
    SELECT * FROM windowed ORDER BY last_event DESC, work_id LIMIT %(limit)s OFFSET %(offset)s
) AS page ON TRUE
ORDER BY page.last_event DESC, page.work_id
"""  # noqa: S608 — composed of module constants only

# What a windowed read checks before it picks a statement: the first and last event of the filter
# (both are index-only reads, under a millisecond) and whether the rollup is populated. A window
# that holds every event of the filter answers exactly what the unwindowed read does, from the
# per-work summary's index walk (about 0.3 s on the real catalog) instead of aggregating a rollup
# row per work: the real catalog's 2.09M events all fall on one UTC day, so a window over that day
# reads a rollup row per work and no less than the raw aggregation did.
_WINDOW_FACTS = """
SELECT COALESCE((SELECT min(at) FROM identity_ledger_edition_events WHERE %(event)s::text IS NULL),
                (SELECT min(at) FROM identity_ledger_edition_events WHERE type = %(event)s::text)),
       COALESCE((SELECT max(at) FROM identity_ledger_edition_events WHERE %(event)s::text IS NULL),
                (SELECT max(at) FROM identity_ledger_edition_events WHERE type = %(event)s::text)),
       EXISTS (SELECT 1 FROM identity_ledger_work_summary_day)
"""

_DAY = timedelta(days=1)


def _window_params(since: datetime | None, until: datetime | None) -> dict[str, Any]:
    """Split a window into the rollup's full UTC days and the raw partial edge days.

    ``since`` rounds up to the next midnight and ``until`` down to the previous one. When no
    whole day fits between them the whole window is one raw edge. Unwindowed reads use neither.
    """
    none: dict[str, Any] = {"full": False, "first_day": None, "end_day": None, "first_at": None, "end_at": None, "head_end": None, "tail_start": None}
    if since is None and until is None:
        return none
    first_at = end_at = None
    if since is not None:
        since_utc = since.astimezone(UTC)
        first_at = since_utc.replace(hour=0, minute=0, second=0, microsecond=0)
        if first_at < since_utc:
            first_at += _DAY
    if until is not None:
        end_at = until.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    if first_at is not None and end_at is not None and first_at >= end_at:
        return {**none, "head_end": until}
    return {
        "full": True,
        "first_day": first_at.date() if first_at else None,
        "end_day": end_at.date() if end_at else None,
        "first_at": first_at,
        "end_at": end_at,
        "head_end": first_at if since is not None and since < first_at else None,  # type: ignore[operator]
        "tail_start": end_at if until is not None and until > end_at else None,  # type: ignore[operator]
    }


# Each page work's edition count and how many of those editions have two or more works.
_WORK_SUMMARIES = f"""
WITH work_member AS (
    SELECT work_id, work_id AS item_id FROM unnest(%(works)s::uuid[]) AS work (work_id)
    UNION
    SELECT supersession.survivor_id, supersession.superseded_id
    FROM catalog_item_supersessions AS supersession
    WHERE supersession.valid_to IS NULL AND supersession.survivor_id = ANY(%(works)s::uuid[])
),
{_EDITIONS_OF_WORK},
{_PARENTS_OF_MEMBER},
parents AS (
    SELECT edition_id, count(DISTINCT work_id) AS works FROM edition_parent GROUP BY edition_id
)
SELECT edition_of.work_id,
       count(DISTINCT edition_of.edition_id) AS editions,
       count(DISTINCT edition_of.edition_id) FILTER (WHERE parents.works > 1) AS work_disagreements
FROM edition_of
LEFT JOIN parents ON parents.edition_id = edition_of.edition_id
GROUP BY edition_of.work_id
"""  # noqa: S608 — composed of module constants only

_WORK_PROVIDERS = """
SELECT native_id, provider, external_id
FROM provider_aliases
WHERE native_id = ANY(%(works)s::uuid[]) AND valid_to IS NULL
ORDER BY native_id, provider, external_id
"""

_DENSITY = """
SELECT month, type, dated_from, events
FROM identity_ledger_density
WHERE kind = %(kind)s
ORDER BY 1, 2, 3
"""

_RESOLVE = """
SELECT item.id, item.kind
FROM catalog_items AS item
WHERE item.id = public.resolve_catalog_item(%(id)s::uuid)
"""

# The work's editions, each with its first recorded instant, in ledger order.
_EDITIONS = f"""
WITH work_member AS (
    SELECT %(work)s::uuid AS work_id, %(work)s::uuid AS item_id
    UNION
    SELECT survivor_id, superseded_id
    FROM catalog_item_supersessions
    WHERE survivor_id = %(work)s::uuid AND valid_to IS NULL
),
{_EDITIONS_OF_WORK}
SELECT member.edition_id, min(alias.valid_from) AS first_at
FROM member
JOIN provider_aliases AS alias ON alias.native_id = member.item_id
GROUP BY member.edition_id
ORDER BY first_at, member.edition_id
"""  # noqa: S608 — composed of module constants only

_GROUPS = """
SELECT survivor_id, superseded_id
FROM catalog_item_supersessions
WHERE survivor_id = ANY(%(ids)s::uuid[]) AND valid_to IS NULL
"""

_ALIASES = """
SELECT provider, entity_kind, external_id, native_id, valid_from, valid_to, source
FROM provider_aliases
WHERE native_id = ANY(%(ids)s::uuid[])
ORDER BY valid_from, provider, external_id
"""

_SUPERSESSIONS = """
SELECT id, superseded_id, survivor_id, cause, decision_ref, valid_from, valid_to, via_id
FROM catalog_item_supersessions
WHERE superseded_id = ANY(%(ids)s::uuid[]) OR survivor_id = ANY(%(ids)s::uuid[])
"""

_MOVE_COUNTS = """
SELECT supersession_id, table_name, count(*)
FROM catalog_item_moves
WHERE supersession_id = ANY(%(ids)s::uuid[])
GROUP BY supersession_id, table_name
"""

_DISCOGS_RELEASES = """
SELECT release.data_id, derived.master_id
FROM public.releases AS release
LEFT JOIN graph.derived_from AS derived ON derived.release_id = release.data_id
WHERE release.data_id = ANY(%(ids)s::text[])
"""

_MUSICBRAINZ_RELEASES = """
SELECT mbid, release_group_mbid, discogs_release_id
FROM musicbrainz.releases
WHERE mbid = ANY(%(ids)s::uuid[])
"""

_DISCOGS_HOLDERS = """
SELECT native_id
FROM provider_aliases
WHERE provider = 'discogs' AND entity_kind = 'release' AND valid_to IS NULL AND external_id = ANY(%(ids)s::text[])
"""

_MASTER_WORKS = f"""
SELECT master.provider, master.external_id, COALESCE(work_open.survivor_id, master.native_id)
FROM provider_aliases AS master
{_RESOLVED.format(alias="work_open", item="master.native_id")}
WHERE master.entity_kind = 'master' AND master.valid_to IS NULL
  AND ((master.provider = 'discogs' AND master.external_id = ANY(%(discogs)s::text[]))
    OR (master.provider = 'musicbrainz' AND master.external_id = ANY(%(musicbrainz)s::text[])))
"""  # noqa: S608 — composed of module constants only

_LINK_EVENTS = """
SELECT provider, entity_kind, external_id, event, discogs_id_before, discogs_id_after,
       event_at, dated_from, source_ref, decision_ref
FROM identity_link_events
WHERE provider = 'musicbrainz'
  AND ((entity_kind = 'release' AND external_id = ANY(%(releases)s::text[]))
    OR (entity_kind = 'master' AND external_id = ANY(%(masters)s::text[])))
ORDER BY event_at, id
"""


def _text(value: Any) -> str | None:
    return None if value is None else str(value)


def _uuids(values: set[str]) -> list[str]:
    """The values that parse as UUIDs, sorted; a MusicBrainz alias that does not is skipped."""
    parsed = []
    for value in values:
        try:
            parsed.append(str(uuid.UUID(value)))
        except ValueError:
            continue
    return sorted(parsed)


async def _rows(cur: Any, sql: str, params: dict[str, Any]) -> list[tuple[Any, ...]]:
    await execute_sql(cur, sql, params)
    return list(await cur.fetchall())


async def _refreshed_at(cur: Any, views: tuple[str, ...]) -> datetime:
    """When the views were last refreshed; raises ``LedgerNotRefreshed`` if any is unpopulated."""
    rows = await _rows(cur, _FRESHNESS, {"views": list(views)})
    refreshed_at: datetime | None = rows[0][1] if rows and rows[0][0] else None
    if refreshed_at is None:
        raise LedgerNotRefreshed
    return refreshed_at


def _alias_rows(rows: list[tuple[Any, ...]]) -> list[dict[str, Any]]:
    return [
        {
            "provider": provider,
            "entity_kind": entity_kind,
            "external_id": external_id,
            "native_id": str(native_id),
            "valid_from": valid_from,
            "valid_to": valid_to,
            "source": source,
        }
        for provider, entity_kind, external_id, native_id, valid_from, valid_to, source in rows
    ]


async def _groups(cur: Any, items: list[str]) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {item: [item] for item in items}
    for survivor, superseded in await _rows(cur, _GROUPS, {"ids": items}):
        groups[str(survivor)].append(str(superseded))
    return {item: [item, *sorted(members[1:])] for item, members in groups.items()}


async def get_ledger_work(
    pool: Any,
    work_id: str,
    *,
    as_of: datetime | None = None,
    edition_limit: int = DEFAULT_EDITION_LIMIT,
) -> dict[str, Any] | None:
    """One work's three planes at ``as_of``, or None when the id is not a work.

    A superseded work id answers as its survivor, with ``requested_id`` (one hop, ADR 0009
    amendment section 4). An id that is not a catalog item, or resolves to an item of another
    kind, is None, which the route answers 404. The planes are read live; ``refreshed_at`` is
    the last refresh of the list the work was reached from, None before the first.
    """
    async with pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
        await cur.execute(_READ_ONLY)
        resolved = await _rows(cur, _RESOLVE, {"id": work_id})
        if not resolved or resolved[0][1] != "master":
            return None
        work = str(resolved[0][0])

        editions = [(str(edition), first_at) for edition, first_at in await _rows(cur, _EDITIONS, {"work": work})]
        visible = [edition for edition, first_at in editions if as_of is None or first_at <= as_of]
        groups = await _groups(cur, [work, *visible[:edition_limit]])
        items = sorted({member for members in groups.values() for member in members})
        aliases = _alias_rows(await _rows(cur, _ALIASES, {"ids": items}))

        current = [a for a in aliases if a["valid_to"] is None and a["entity_kind"] == "release"]
        discogs = sorted({a["external_id"] for a in current if a["provider"] == "discogs"})
        musicbrainz = _uuids({a["external_id"] for a in current if a["provider"] == "musicbrainz"})
        discogs_releases = [
            {"data_id": data_id, "master_id": master_id} for data_id, master_id in await _rows(cur, _DISCOGS_RELEASES, {"ids": discogs})
        ]
        musicbrainz_releases = [
            {"mbid": str(mbid), "release_group_mbid": _text(group), "discogs_release_id": discogs_release_id}
            for mbid, group, discogs_release_id in await _rows(cur, _MUSICBRAINZ_RELEASES, {"ids": musicbrainz})
        ]

        # A MusicBrainz row's state reads the item holding the Discogs release it names.
        named = sorted({str(row["discogs_release_id"]) for row in musicbrainz_releases if row["discogs_release_id"] is not None})
        holders = sorted({str(native_id) for (native_id,) in await _rows(cur, _DISCOGS_HOLDERS, {"ids": named})} - set(items))
        if holders:
            aliases += _alias_rows(await _rows(cur, _ALIASES, {"ids": holders}))

        master_works = {
            (provider, external_id): str(resolved_work)
            for provider, external_id, resolved_work in await _rows(
                cur,
                _MASTER_WORKS,
                {
                    "discogs": sorted({row["master_id"].strip() for row in discogs_releases if row["master_id"] is not None}),
                    "musicbrainz": sorted({row["release_group_mbid"] for row in musicbrainz_releases if row["release_group_mbid"] is not None}),
                },
            )
        }
        supersessions = [
            {
                "id": str(row_id),
                "superseded_id": str(superseded),
                "survivor_id": str(survivor),
                "cause": cause,
                "decision_ref": str(decision_ref),
                "valid_from": valid_from,
                "valid_to": valid_to,
                "via_id": _text(via),
            }
            for row_id, superseded, survivor, cause, decision_ref, valid_from, valid_to, via in await _rows(cur, _SUPERSESSIONS, {"ids": items})
        ]
        move_counts: dict[str, dict[str, int]] = {}
        for supersession_id, table_name, moved in await _rows(cur, _MOVE_COUNTS, {"ids": [s["id"] for s in supersessions]}):
            move_counts.setdefault(str(supersession_id), {})[table_name] = int(moved)

        held = [a for a in aliases if a["native_id"] in items and a["provider"] == "musicbrainz"]
        link_events = [
            {
                "provider": provider,
                "entity_kind": entity_kind,
                "external_id": external_id,
                "event": event,
                "discogs_id_before": before,
                "discogs_id_after": after,
                "event_at": event_at,
                "dated_from": dated_from,
                "source_ref": source_ref,
                "decision_ref": _text(decision_ref),
            }
            for provider, entity_kind, external_id, event, before, after, event_at, dated_from, source_ref, decision_ref in await _rows(
                cur,
                _LINK_EVENTS,
                {
                    "releases": sorted({a["external_id"] for a in held if a["entity_kind"] == "release"}),
                    "masters": sorted({a["external_id"] for a in held if a["entity_kind"] == "master"}),
                },
            )
        ]
        refreshed = await _rows(cur, _REFRESHED_AT, {})

    rows = LedgerRows(
        aliases=aliases,
        supersessions=supersessions,
        move_counts=move_counts,
        discogs_releases=discogs_releases,
        musicbrainz_releases=musicbrainz_releases,
        master_works=master_works,
        link_events=link_events,
    )
    return build_work(
        rows,
        work,
        editions,
        requested_id=work_id,
        as_of=as_of,
        edition_limit=edition_limit,
        refreshed_at=refreshed[0][0] if refreshed else None,
    )


async def get_ledger_works(
    pool: Any,
    *,
    event: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    page: int = 1,
    page_size: int = 50,
) -> dict[str, Any]:
    """Works with evented editions, most recent event first, paged like the audit log.

    ``event`` keeps only that event type; ``since`` (inclusive) and ``until`` (exclusive)
    bound the events' times. ``evented_editions``, ``repaired_editions``, ``first_event``, and
    ``last_event`` count the matching events only. ``editions`` and ``work_disagreements``
    describe the whole work, read live. There is no default window: the ledger spans every
    month. Raises ``LedgerNotRefreshed`` until the views' first refresh.
    """
    params = {"event": event, "since": since, "until": until, "limit": page_size, "offset": (page - 1) * page_size}
    try:
        async with pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            await cur.execute(_READ_ONLY)
            refreshed_at = await _refreshed_at(cur, _WORKS_VIEWS)
            statement = _WORKS
            if since is not None or until is not None:
                first, last, rolled = (await _rows(cur, _WINDOW_FACTS, {"event": event}))[0]
                if first is not None and (since is None or since <= first) and (until is None or last < until):
                    params = {**params, "since": None, "until": None}  # holds every event: same as no window
                else:
                    # A rollup the last rebuild has not filled would read as no works at all; the
                    # live aggregation answers until it is populated.
                    statement = _WORKS_WINDOWED if rolled else _WORKS_WINDOWED_RAW
                    if rolled:
                        params = {**params, **_window_params(since, until)}
            page_rows = await _rows(cur, statement, params)
            total = int(page_rows[0][0]) if page_rows else 0
            listed = [row[1:] for row in page_rows if row[1] is not None]
            works = [str(row[0]) for row in listed]
            summaries: dict[str, tuple[int, int]] = {}
            providers: dict[str, list[dict[str, str]]] = {work: [] for work in works}
            if works:
                for work_id, editions, disagreements in await _rows(cur, _WORK_SUMMARIES, {"works": works}):
                    summaries[str(work_id)] = (int(editions), int(disagreements))
                for native_id, provider, external_id in await _rows(cur, _WORK_PROVIDERS, {"works": works}):
                    providers[str(native_id)].append({"provider": provider, "external_id": external_id})
    except ObjectNotInPrerequisiteState as exc:
        # A view dropped and recreated unpopulated after the check above (database-schema's
        # definition-change migration) reads as not refreshed, like one never refreshed.
        raise LedgerNotRefreshed from exc

    entries = []
    for work_id, evented_editions, repaired_editions, first_event, last_event in listed:
        work = str(work_id)
        editions, disagreements = summaries.get(work, (0, 0))
        entries.append(
            {
                "gm_id": work,
                "providers": providers[work],
                "editions": editions,
                "evented_editions": int(evented_editions),
                "repaired_editions": int(repaired_editions),
                "work_disagreements": disagreements,
                "first_event": iso(first_event),
                "last_event": iso(last_event),
            }
        )
    return {"works": entries, "total": total, "page": page, "page_size": page_size, "refreshed_at": iso(refreshed_at)}


async def get_ledger_density(pool: Any, kind: str = "release") -> dict[str, Any]:
    """Events per month by type and provenance for one catalog kind: the ledger's time axis.

    Months are UTC. Each bucket names its ``dated_from``: the full-export backfill's link
    events are counted apart from GrooveMap's own record. Raises ``LedgerNotRefreshed`` until
    the view's first refresh.
    """
    try:
        async with pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            await cur.execute(_READ_ONLY)
            refreshed_at = await _refreshed_at(cur, _DENSITY_VIEWS)
            rows = await _rows(cur, _DENSITY, {"kind": kind})
    except ObjectNotInPrerequisiteState as exc:
        raise LedgerNotRefreshed from exc
    return {
        "kind": kind,
        "buckets": [{"month": month, "type": event, "dated_from": dated_from, "events": int(count)} for month, event, dated_from, count in rows],
        "refreshed_at": iso(refreshed_at),
    }
