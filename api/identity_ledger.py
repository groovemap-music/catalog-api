"""The Identity Ledger: one work's editions, their identity states, and their events over transaction time.

ADR 0016 section 7 and its 2026-10-24 amendment (concept D). The ledger is a read of
PostgreSQL, the identity authority under ADR 0009, not of Neo4j, which holds only the current
`gm_id` projection and no history. ADR 0016 section 3 records that departure.

The rules are ported from design spike `gm-design-loj9` (`scripts/payload.py` and
`scripts/reference.py`); nothing is imported from the design repository.
`api/queries/identity_ledger_queries.py` fetches the rows one work touches and this module
builds the payload from them, so the state machine can be tested without a database.

**States.** An edition is `not_recorded`, `discogs_only`, `musicbrainz_only`, `unresolved`,
`split`, or `linked` (spike section 4). A MusicBrainz release row names its Discogs release
in `discogs_release_id`, and the row keeps no history, so a past state reads today's link.

**Events.** An edition's events are everything recorded about it or about an item
superseded into it: mints, aliases opened or closed later, supersessions, and the dated
changes of its MusicBrainz rows' Discogs links in `identity_link_events`. Every event names
where its date comes from in `dated_from`: `groovemap` for GrooveMap's own tables and for
link changes `musicbrainz-sql-loader` recorded at load, and `musicbrainz_fullexport` for the
links the one-time backfill dated from the MusicBrainz full export. About 9% of those dates
belong to an older link MusicBrainz replaced (spike `gm-design-6b8u`), so a reader must be
able to tell a dated event from a recorded one.

**Work parents.** An edition's works come from each catalog's own release row: a Discogs
release names its master, a MusicBrainz release its release group. When the two differ, the
edition's primary parent is the Discogs master, because the survivor of a re-attachment is
always on the Discogs side, and `work_disagreement` is set (ADR 0016 section 4).

**No user data.** Moves are counted per supersession and table. No user id, owned-copy id,
artifact id, or admin id is read or emitted; `decision_ref` names the audit entry.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


STATES = ("not_recorded", "discogs_only", "musicbrainz_only", "unresolved", "split", "linked")

RULES = {
    "minted": "ADR 0009: minted at ingest through common.identity",
    "linked_at_ingest": "ADR 0014 Context: musicbrainz-sql-loader attaches to a Discogs item that already resolves",
    "reattached": "ADR 0014 section 8; ADR 0009 2026-09-25 amendment, sections 1 to 3",
    "promoted": "ADR 0014 section 3, gated by section 6",
    "reverted": "ADR 0009 2026-09-25 amendment, section 1 (reversal)",
    "evidence_attached": "ADR 0011 identifier aliases; attach_aliases never overwrites",
    "alias_closed": "no writer closes an alias outside a supersession today",
    "link_added": "ADR 0016 2026-10-24 amendment: a MusicBrainz row gained its Discogs link",
    "link_changed": "ADR 0016 2026-10-24 amendment: a MusicBrainz row's Discogs link changed",
    "link_removed": "ADR 0016 2026-10-24 amendment: a MusicBrainz row lost its Discogs link",
}

# Event types an edition can carry after its mint; the works list filters on these.
EVENT_TYPES = tuple(event for event in RULES if event != "minted")

# Event types that repair an edition: a supersession opening or closing.
REPAIR_EVENTS = ("reattached", "promoted", "reverted")

# Catalog kinds whose events the density strip counts. A MusicBrainz release group aliases
# as `master`, as it does in provider_aliases and identity_link_events.
KINDS = ("release", "master", "artist", "label")

# `dated_from` on a payload event, keyed by identity_link_events.dated_from. A load row is
# dated by the loader's own transaction, so it reads as GrooveMap's record.
GROOVEMAP = "groovemap"
LINK_DATED_FROM = {"musicbrainz_fullexport": "musicbrainz_fullexport", "groovemap_load": GROOVEMAP}

DEFAULT_EDITION_LIMIT = 100
MAX_EDITION_LIMIT = 500


def iso(t: datetime | None) -> str | None:
    """RFC 3339 in UTC with a `Z` suffix, or None."""
    return None if t is None else t.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _valid(alias: dict[str, Any], t: datetime | None) -> bool:
    if t is None:
        return alias["valid_to"] is None
    return alias["valid_from"] <= t and (alias["valid_to"] is None or alias["valid_to"] > t)


def _visible(at: datetime | None, as_of: datetime | None) -> bool:
    return at is not None and (as_of is None or at <= as_of)


@dataclass(frozen=True)
class LedgerRows:
    """The rows one work's payload reads.

    Attributes:
        aliases: Every `provider_aliases` row (any validity) on the work's and its shown
            editions' items, on the items superseded into them, and on the current holders
            of the Discogs releases their MusicBrainz rows name.
        supersessions: Every `catalog_item_supersessions` row either side of which is one
            of those items.
        move_counts: `catalog_item_moves` counted per supersession id and table name.
        discogs_releases: `{data_id, master_id}` for the Discogs releases those items hold.
        musicbrainz_releases: `{mbid, release_group_mbid, discogs_release_id}` likewise.
        master_works: `(provider, external_id)` of a Discogs master or MusicBrainz release
            group with a current alias, to the work it resolves to.
        link_events: `identity_link_events` rows for the MusicBrainz rows those items hold.
    """

    aliases: list[dict[str, Any]]
    supersessions: list[dict[str, Any]]
    move_counts: dict[str, dict[str, int]] = field(default_factory=dict)
    discogs_releases: list[dict[str, Any]] = field(default_factory=list)
    musicbrainz_releases: list[dict[str, Any]] = field(default_factory=list)
    master_works: dict[tuple[str, str], str] = field(default_factory=dict)
    link_events: list[dict[str, Any]] = field(default_factory=list)


class _Ledger:
    """Lookups over one work's rows, ported from the spike's `_Index` and `_Ledger`."""

    def __init__(self, rows: LedgerRows) -> None:
        self.rows = rows
        self.survivor: dict[str, str] = {s["superseded_id"]: s["survivor_id"] for s in rows.supersessions if s["valid_to"] is None}
        self.current_alias = {(a["provider"], a["entity_kind"], a["external_id"]): a["native_id"] for a in rows.aliases if a["valid_to"] is None}
        self.by_key: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
        self.by_item: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.mint: dict[str, datetime] = {}
        for a in rows.aliases:
            self.by_key[(a["provider"], a["entity_kind"], a["external_id"])].append(a)
            self.by_item[a["native_id"]].append(a)
            first = self.mint.get(a["native_id"])
            if first is None or a["valid_from"] < first:
                self.mint[a["native_id"]] = a["valid_from"]
        # An alias opened on an item at the instant a supersession into it opened (or one out of
        # it closed) was moved by that supersession; closes mirror it.
        self.opened_instants: set[tuple[str, datetime]] = set()
        self.closed_instants: set[tuple[str, datetime]] = set()
        for s in rows.supersessions:
            self.opened_instants.add((s["survivor_id"], s["valid_from"]))
            self.closed_instants.add((s["superseded_id"], s["valid_from"]))
            if s["valid_to"] is not None:
                self.opened_instants.add((s["superseded_id"], s["valid_to"]))
                self.closed_instants.add((s["survivor_id"], s["valid_to"]))

    def resolve(self, item: str) -> str:
        return self.survivor.get(item, item)

    def opened_by_supersession(self, alias: dict[str, Any]) -> bool:
        return (alias["native_id"], alias["valid_from"]) in self.opened_instants

    def closed_by_supersession(self, alias: dict[str, Any]) -> bool:
        return alias["valid_to"] is not None and (alias["native_id"], alias["valid_to"]) in self.closed_instants

    def group(self, item: str) -> list[str]:
        """The item, then every item currently superseded into it."""
        former = sorted({s["superseded_id"] for s in self.rows.supersessions if s["survivor_id"] == item and s["valid_to"] is None})
        return [item, *former]

    def holder(self, provider: str, kind: str, external_id: str, t: datetime | None) -> str | None:
        for a in self.by_key.get((provider, kind, external_id), ()):
            if _valid(a, t):
                return str(a["native_id"])
        return None

    def _resolves_to(self, key: tuple[str, str, str], edition: str) -> bool:
        item = self.current_alias.get(key)
        return item is not None and self.resolve(item) == edition

    def musicbrainz_rows(self, group: list[str]) -> list[dict[str, Any]]:
        return [row for row in self.rows.musicbrainz_releases if self._resolves_to(("musicbrainz", "release", row["mbid"]), group[0])]

    def discogs_rows(self, group: list[str]) -> list[dict[str, Any]]:
        return [row for row in self.rows.discogs_releases if self._resolves_to(("discogs", "release", row["data_id"]), group[0])]

    def works(self, group: list[str]) -> tuple[str | None, list[str]]:
        """The edition's primary work and all its works, primary first.

        The primary parent is the Discogs master's work; without one, the release group's.
        """
        discogs = sorted(
            {
                work
                for row in self.discogs_rows(group)
                if row.get("master_id") is not None and (work := self.rows.master_works.get(("discogs", row["master_id"].strip()))) is not None
            }
        )
        musicbrainz = sorted(
            {
                work
                for row in self.musicbrainz_rows(group)
                if row.get("release_group_mbid") is not None
                and (work := self.rows.master_works.get(("musicbrainz", row["release_group_mbid"]))) is not None
            }
        )
        ordered = [*discogs, *(work for work in musicbrainz if work not in discogs)]
        return (ordered[0] if ordered else None), ordered

    def state(self, item: str, group: list[str], t: datetime | None, kind: str) -> str:
        held = {a["provider"] for a in self.by_item.get(item, ()) if a["entity_kind"] == kind and _valid(a, t)}
        if {"discogs", "musicbrainz"} <= held:
            return "linked"
        if kind == "release":
            for row in self.musicbrainz_rows(group):
                if row.get("discogs_release_id") is None:
                    continue
                discogs_item = self.holder("discogs", "release", str(row["discogs_release_id"]), t)
                musicbrainz_item = self.holder("musicbrainz", "release", row["mbid"], t)
                if musicbrainz_item is None:
                    continue
                if discogs_item is None:
                    return "unresolved"
                if discogs_item != musicbrainz_item:
                    return "split"
        if "discogs" in held:
            return "discogs_only"
        if "musicbrainz" in held or any(a["provider"] == "musicbrainz" and _valid(a, t) for member in group for a in self.by_item.get(member, ())):
            return "musicbrainz_only"
        return "not_recorded"

    def events(self, group: list[str], as_of: datetime | None, kind: str) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for item in group:
            own = self.by_item.get(item, [])
            if not own:
                continue
            mint = self.mint[item]
            events.append(
                {
                    "at": mint,
                    "type": "minted",
                    "item": item,
                    "providers": sorted({a["provider"] for a in own if a["valid_from"] == mint}),
                }
            )
            discogs_at = min((a["valid_from"] for a in own if a["provider"] == "discogs"), default=None)
            later = sorted(
                (a for a in own if a["valid_from"] > mint and not self.opened_by_supersession(a)),
                key=lambda a: (a["valid_from"], a["provider"], a["external_id"]),
            )
            linked = False
            for a in later:
                if a["provider"] == "musicbrainz" and discogs_at is not None and not linked:
                    linked = True
                    events.append({"at": a["valid_from"], "type": "linked_at_ingest", "item": item})
                else:
                    events.append({"at": a["valid_from"], "type": "evidence_attached", "provider": a["provider"], "source": a["source"]})
            for a in own:
                if a["valid_to"] is not None and not self.closed_by_supersession(a):
                    events.append({"at": a["valid_to"], "type": "alias_closed", "provider": a["provider"]})
        for s in self.rows.supersessions:
            if s["survivor_id"] not in group and s["superseded_id"] not in group:
                continue
            events.append(
                {
                    "at": s["valid_from"],
                    "type": "reattached" if s["cause"] == "catalog_reattachment" else "promoted",
                    "superseded": s["superseded_id"],
                    "survivor": s["survivor_id"],
                    "decision_ref": s["decision_ref"],
                    "compressed": s["via_id"] is not None,
                    "moves": dict(self.rows.move_counts.get(s["id"], {})),
                }
            )
            if s["valid_to"] is not None:
                events.append({"at": s["valid_to"], "type": "reverted", "superseded": s["superseded_id"], "survivor": s["survivor_id"]})
        for event in events:
            event["dated_from"] = GROOVEMAP
        events.extend(self.link_events(group, kind))
        visible = [e for e in events if _visible(e["at"], as_of)]
        visible.sort(key=lambda e: (e["at"], e["type"]))
        return [{**e, "at": iso(e["at"]), "rule": RULES[e["type"]]} for e in visible]

    def link_events(self, group: list[str], kind: str) -> list[dict[str, Any]]:
        """Dated link changes of every MusicBrainz row of this kind the group's items have held."""
        external_ids = {
            a["external_id"] for member in group for a in self.by_item.get(member, ()) if a["provider"] == "musicbrainz" and a["entity_kind"] == kind
        }
        return [
            {
                "at": e["event_at"],
                "type": e["event"],
                "provider": e["provider"],
                "external_id": e["external_id"],
                "discogs_id_before": None if e["discogs_id_before"] is None else str(e["discogs_id_before"]),
                "discogs_id_after": None if e["discogs_id_after"] is None else str(e["discogs_id_after"]),
                "source_ref": e["source_ref"],
                "decision_ref": e["decision_ref"],
                "dated_from": LINK_DATED_FROM[e["dated_from"]],
            }
            for e in self.rows.link_events
            if e["entity_kind"] == kind and e["external_id"] in external_ids
        ]

    def evidence(self, group: list[str], as_of: datetime | None) -> list[dict[str, Any]]:
        rows = []
        aliases = [a for member in group for a in self.by_item.get(member, ())]
        for a in sorted(aliases, key=lambda a: (a["valid_from"], a["provider"], a["external_id"])):
            if not _visible(a["valid_from"], as_of):
                continue
            closed = _visible(a["valid_to"], as_of)
            if self.opened_by_supersession(a):
                opened_by = "supersession"
            elif a["valid_from"] == self.mint[a["native_id"]]:
                opened_by = "mint"
            else:
                opened_by = "later"
            rows.append(
                {
                    "provider": a["provider"],
                    "external_id": a["external_id"],
                    "source": a["source"],
                    "held_by": a["native_id"],
                    "valid_from": iso(a["valid_from"]),
                    "valid_to": iso(a["valid_to"]) if closed else None,
                    "opened_by": opened_by,
                    "closed_by": None if not closed else "supersession" if self.closed_by_supersession(a) else "other",
                }
            )
        return rows


def build_work(
    rows: LedgerRows,
    work: str,
    editions: list[tuple[str, datetime]],
    *,
    requested_id: str | None = None,
    as_of: datetime | None = None,
    edition_limit: int = DEFAULT_EDITION_LIMIT,
    refreshed_at: datetime | None = None,
) -> dict[str, Any]:
    """One work's three planes at `as_of` (transaction time; None means now).

    Args:
        rows: The rows the work and its shown editions read (see `LedgerRows`).
        work: The work's native id, already resolved one hop.
        editions: Every edition of the work with its first recorded instant, in ledger
            order (first instant, then id).
        requested_id: The id the caller asked for, when resolution moved it.
        as_of: Transaction time to read the ledger at.
        edition_limit: How many editions to emit; `truncated` reports a cut.
        refreshed_at: When the precomputed works list and density were last refreshed, None
            before the first refresh. The work's own planes are read live; this names the age
            of the list the work was reached from.
    """
    ledger = _Ledger(rows)
    visible = [edition for edition, first_at in editions if as_of is None or first_at <= as_of]
    shown = visible[:edition_limit]

    payload_editions = []
    for edition in shown:
        group = ledger.group(edition)
        former = [
            {"gm_id": s["superseded_id"], "superseded_at": iso(s["valid_from"]), "cause": s["cause"], "decision_ref": s["decision_ref"]}
            for s in sorted(rows.supersessions, key=lambda s: (s["valid_from"], s["superseded_id"]))
            if s["survivor_id"] == edition and s["superseded_id"] in group and _visible(s["valid_from"], as_of)
        ]
        primary, works = ledger.works(group)
        payload_editions.append(
            {
                "gm_id": edition,
                "state": ledger.state(edition, group, as_of, "release"),
                "primary_work": primary,
                "works": works,
                "work_disagreement": len(works) > 1,
                "provider_rows": [{"provider": "discogs", "external_id": row["data_id"]} for row in ledger.discogs_rows(group)]
                + [
                    {
                        "provider": "musicbrainz",
                        "external_id": row["mbid"],
                        "names_discogs_release": None if row.get("discogs_release_id") is None else str(row["discogs_release_id"]),
                    }
                    for row in ledger.musicbrainz_rows(group)
                ],
                "former_items": former,
                "evidence": ledger.evidence(group, as_of),
                "events": ledger.events(group, as_of, "release"),
            }
        )

    work_aliases = [a for a in ledger.by_item.get(work, ()) if _visible(a["valid_from"], as_of)]
    work_payload: dict[str, Any] = {
        "gm_id": work,
        "state": ledger.state(work, [work], as_of, "master"),
        "providers": [
            {
                "provider": a["provider"],
                "external_id": a["external_id"],
                "valid_from": iso(a["valid_from"]),
                "valid_to": iso(a["valid_to"]) if _visible(a["valid_to"], as_of) else None,
            }
            for a in sorted(work_aliases, key=lambda a: (a["valid_from"], a["provider"], a["external_id"]))
        ],
        "events": ledger.events(ledger.group(work), as_of, "master"),
    }
    if requested_id is not None and requested_id != work:
        work_payload["requested_id"] = requested_id
    return {
        "work": work_payload,
        "as_of": iso(as_of),
        "editions": payload_editions,
        "edition_count": len(visible),
        "truncated": len(visible) > len(shown),
        "refreshed_at": iso(refreshed_at),
    }
