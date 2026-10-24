"""The Identity Ledger payload (api/identity_ledger.py), built in memory from the shared story.

Ports spike `gm-design-loj9`'s `tests/test_payload.py` rules: history-only fields, states that
move with transaction time, one-hop resolution, the primary-parent rule, closed vocabularies,
and no user data. Adds the provenance label every event carries.
"""

from __future__ import annotations

import copy
import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from api.identity_ledger import EVENT_TYPES, LINK_DATED_FROM, REPAIR_EVENTS, RULES, STATES, LedgerRows, build_work, iso
from tests.identity_ledger_story import (
    BACKFILL,
    E1,
    E2,
    E3,
    E4,
    E5,
    E6,
    E7,
    EXPORT,
    G2,
    G5X,
    MOVED_ROW,
    OWNER,
    R1,
    RUN1,
    S3,
    W1,
    W2,
    W5,
    X3,
    mbid,
    native,
    story,
)


_TIMES = ("created_at", "valid_from", "valid_to", "moved_at", "event_at")


def _parse(row: dict[str, Any]) -> dict[str, Any]:
    return {key: datetime.fromisoformat(value) if key in _TIMES and value is not None else value for key, value in row.items()}


class _Catalog:
    """The story's tables, parsed, with the reads the SQL performs done in Python."""

    def __init__(self, tables: dict[str, list[dict[str, Any]]]) -> None:
        self.tables = {name: [_parse(row) for row in rows] for name, rows in tables.items()}
        self.survivor = {s["superseded_id"]: s["survivor_id"] for s in self.tables["catalog_item_supersessions"] if s["valid_to"] is None}
        self.current = {
            (a["provider"], a["entity_kind"], a["external_id"]): a["native_id"] for a in self.tables["provider_aliases"] if a["valid_to"] is None
        }

    def resolve(self, item: str) -> str:
        return self.survivor.get(item, item)

    def rows(self) -> LedgerRows:
        moves: Counter[tuple[str, str]] = Counter((m["supersession_id"], m["table_name"]) for m in self.tables["catalog_item_moves"])
        move_counts: dict[str, dict[str, int]] = {}
        for (supersession, table), count in moves.items():
            move_counts.setdefault(supersession, {})[table] = count
        return LedgerRows(
            aliases=self.tables["provider_aliases"],
            supersessions=self.tables["catalog_item_supersessions"],
            move_counts=move_counts,
            discogs_releases=self.tables["discogs_releases"],
            musicbrainz_releases=self.tables["musicbrainz_releases"],
            master_works={
                (provider, external_id): self.resolve(item) for (provider, kind, external_id), item in self.current.items() if kind == "master"
            },
            link_events=self.tables["identity_link_events"],
        )

    def editions(self, work: str) -> list[tuple[str, datetime]]:
        """`_EDITIONS`: the editions whose rows name the work, by first instant, then id."""
        editions = set()
        for row in self.tables["discogs_releases"]:
            master = self.current.get(("discogs", "master", row["master_id"]))
            release = self.current.get(("discogs", "release", row["data_id"]))
            if master is not None and release is not None and self.resolve(master) == work:
                editions.add(self.resolve(release))
        for row in self.tables["musicbrainz_releases"]:
            group = self.current.get(("musicbrainz", "master", row["release_group_mbid"]))
            release = self.current.get(("musicbrainz", "release", row["mbid"]))
            if group is not None and release is not None and self.resolve(group) == work:
                editions.add(self.resolve(release))
        first = {
            edition: min(
                a["valid_from"] for a in self.tables["provider_aliases"] if self.resolve(a["native_id"]) == edition or a["native_id"] == edition
            )
            for edition in editions
        }
        return sorted(first.items(), key=lambda pair: (pair[1], pair[0]))

    def build(self, work: str, *, requested_id: str | None = None, as_of: str | None = None, edition_limit: int = 100) -> dict[str, Any]:
        resolved = self.resolve(work)
        return build_work(
            self.rows(),
            resolved,
            self.editions(resolved),
            requested_id=requested_id or work,
            as_of=None if as_of is None else datetime.fromisoformat(as_of),
            edition_limit=edition_limit,
        )


@pytest.fixture(scope="module")
def catalog() -> _Catalog:
    return _Catalog(story())


def _edition(payload: dict[str, Any], gm_id: str) -> dict[str, Any]:
    return next(edition for edition in payload["editions"] if edition["gm_id"] == gm_id)


def _events(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [event for edition in payload["editions"] for event in edition["events"]] + payload["work"]["events"]


def test_work_lists_editions_from_both_catalogs_rows(catalog: _Catalog) -> None:
    payload = catalog.build(W1)

    assert [edition["gm_id"] for edition in payload["editions"]] == [E1, E2, E4, E3, E5]
    assert payload["work"]["state"] == "linked"
    assert [provider["provider"] for provider in payload["work"]["providers"]] == ["discogs", "musicbrainz"]
    assert _edition(payload, E1)["state"] == "linked"
    assert _edition(payload, E4)["state"] == "discogs_only"
    assert _edition(payload, E5)["state"] == "musicbrainz_only"
    assert _edition(payload, E5)["provider_rows"] == [{"provider": "musicbrainz", "external_id": mbid(0x5), "names_discogs_release": None}]
    assert payload["as_of"] is None
    assert (payload["edition_count"], payload["truncated"]) == (5, False)


def test_reattached_edition_walks_unresolved_split_linked(catalog: _Catalog) -> None:
    states = [
        _edition(catalog.build(W1, as_of=t), E3)["state"]
        for t in ("2026-08-05T00:00:00+00:00", "2026-09-01T12:00:00+00:00", "2026-09-03T00:00:00+00:00")
    ]
    assert states == ["unresolved", "split", "linked"]


def test_reattachment_is_one_event_citing_its_audit_entry(catalog: _Catalog) -> None:
    e3 = _edition(catalog.build(W1), E3)

    [reattached] = [event for event in e3["events"] if event["type"] == "reattached"]
    assert reattached == {
        "at": "2026-09-02T10:00:01Z",
        "type": "reattached",
        "superseded": X3,
        "survivor": E3,
        "decision_ref": RUN1,
        "compressed": False,
        "moves": {"owned_copies": 1},
        "dated_from": "groovemap",
        "rule": RULES["reattached"],
    }
    assert e3["former_items"] == [{"gm_id": X3, "superseded_at": "2026-09-02T10:00:01Z", "cause": "catalog_reattachment", "decision_ref": RUN1}]
    moved = [row for row in e3["evidence"] if row["opened_by"] == "supersession"]
    assert {row["provider"] for row in moved} == {"musicbrainz", "barcode"}
    closed = [row for row in e3["evidence"] if row["valid_to"] is not None]
    assert {(row["held_by"], row["closed_by"]) for row in closed} == {(X3, "supersession")}
    assert [event["type"] for event in e3["events"] if event["type"] == "minted"] == ["minted", "minted"]


def test_linked_at_ingest_is_the_first_musicbrainz_alias_after_a_discogs_mint(catalog: _Catalog) -> None:
    e1 = _edition(catalog.build(W1), E1)

    assert [(event["type"], event["at"]) for event in e1["events"]] == [
        ("link_added", "2025-11-14T00:00:00Z"),
        ("minted", "2026-08-01T02:00:00Z"),
        ("linked_at_ingest", "2026-08-03T05:00:10Z"),
    ]
    assert e1["events"][1]["providers"] == ["barcode", "discogs"]
    assert {row["opened_by"] for row in e1["evidence"]} == {"mint", "later"}


def test_as_of_hides_what_was_not_yet_recorded(catalog: _Catalog) -> None:
    early = catalog.build(W1, as_of="2026-08-02T00:00:00+00:00")

    assert early["as_of"] == "2026-08-02T00:00:00Z"
    assert [edition["gm_id"] for edition in early["editions"]] == [E1, E2, E4]
    assert early["edition_count"] == 3
    for edition in early["editions"]:
        assert all(row["valid_from"] <= "2026-08-02" for row in edition["evidence"])
        assert all(event["at"] <= "2026-08-02" for event in edition["events"])
    assert [provider["provider"] for provider in early["work"]["providers"]] == ["discogs"]
    assert early["work"]["state"] == "discogs_only"


def test_edition_limit_caps_the_editions_and_reports_truncation(catalog: _Catalog) -> None:
    payload = catalog.build(W1, edition_limit=2)

    assert [edition["gm_id"] for edition in payload["editions"]] == [E1, E2]
    assert (payload["edition_count"], payload["truncated"]) == (5, True)


def test_two_parent_edition_has_the_discogs_master_as_primary(catalog: _Catalog) -> None:
    for work in (W2, G2):
        payload = catalog.build(work)
        assert [edition["gm_id"] for edition in payload["editions"]] == [E6, E7]
        for edition in payload["editions"]:
            assert edition["primary_work"] == W2
            assert edition["works"] == [W2, G2]
            assert edition["work_disagreement"] is True
    single = _edition(catalog.build(W1), E1)
    assert (single["primary_work"], single["works"], single["work_disagreement"]) == (W1, [W1], False)


def test_release_group_alone_is_the_primary_parent_when_there_is_no_master() -> None:
    tables = story()
    tables["discogs_releases"] = [row for row in tables["discogs_releases"] if row["data_id"] != "900005"]
    e6 = _edition(_Catalog(tables).build(G2), E6)

    assert (e6["primary_work"], e6["works"], e6["work_disagreement"]) == (G2, [G2], False)


def test_superseded_work_answers_as_the_survivor(catalog: _Catalog) -> None:
    payload = catalog.build(G5X)

    assert payload["work"]["gm_id"] == W5
    assert payload["work"]["requested_id"] == G5X
    assert [event["type"] for event in payload["work"]["events"]] == ["minted", "minted", "reattached"]
    assert "requested_id" not in catalog.build(W5)["work"]


def test_vocabularies_are_closed(catalog: _Catalog) -> None:
    for work in (W1, W2, G2, W5):
        payload = catalog.build(work)
        for edition in payload["editions"]:
            assert edition["state"] in STATES
        for event in _events(payload):
            assert event["type"] in RULES
            assert event["rule"] == RULES[event["type"]]
            assert event["dated_from"] in ("groovemap", "musicbrainz_fullexport")
    assert set(EVENT_TYPES) == set(RULES) - {"minted"}
    assert set(REPAIR_EVENTS) <= set(EVENT_TYPES)


def test_backfilled_link_events_read_dated_from_the_full_export(catalog: _Catalog) -> None:
    events = _events(catalog.build(W1)) + _events(catalog.build(W2))
    links = [event for event in events if event["type"].startswith("link_")]

    assert {(event["external_id"], event["dated_from"]) for event in links} == {
        (mbid(0x1), "musicbrainz_fullexport"),
        (mbid(0x3), "musicbrainz_fullexport"),
        (mbid(0xB1), "musicbrainz_fullexport"),
        (mbid(0x7), "groovemap"),
    }
    assert all(event["source_ref"] == EXPORT and event["decision_ref"] == BACKFILL for event in links if event["dated_from"] != "groovemap")
    assert all(event["dated_from"] == "groovemap" for event in events if not event["type"].startswith("link_"))
    assert LINK_DATED_FROM == {"musicbrainz_fullexport": "musicbrainz_fullexport", "groovemap_load": "groovemap"}
    [changed] = [event for event in links if event["type"] == "link_changed"]
    assert (changed["discogs_id_before"], changed["discogs_id_after"]) == ("900099", "900006")


def test_link_events_follow_the_musicbrainz_row_through_a_reattachment(catalog: _Catalog) -> None:
    e3 = _edition(catalog.build(W1), E3)

    assert [event["external_id"] for event in e3["events"] if event["type"] == "link_added"] == [mbid(0x3)]
    assert e3["events"][0]["type"] == "link_added"
    assert all(event["type"] != "link_added" for event in _edition(catalog.build(W1), E2)["events"])


def test_no_user_owned_copy_or_admin_id_is_emitted(catalog: _Catalog) -> None:
    text = json.dumps([catalog.build(work) for work in (W1, W2, G2, W5)])

    assert OWNER not in text
    assert MOVED_ROW not in text
    assert S3 not in text


def _with_writers_that_do_not_exist_yet() -> dict[str, list[dict[str, Any]]]:
    """The story plus a promotion with its revert, a compressed chain, and a closed alias.

    No writer exists for any of these today; they prove the events see them when one does.
    """
    tables = copy.deepcopy(story())
    promoted, chained = native(0x5101), native(0x5102)
    tables["catalog_item_supersessions"] += [
        {"id": promoted, "superseded_id": E5, "survivor_id": E4, "cause": "edition_promotion", "decision_ref": native(0xD001),
         "valid_from": "2026-11-05T09:00:00+00:00", "valid_to": "2026-12-05T09:00:00+00:00", "via_id": None},
        {"id": chained, "superseded_id": E2, "survivor_id": E4, "cause": "catalog_reattachment", "decision_ref": native(0xD002),
         "valid_from": "2026-11-06T09:00:00+00:00", "valid_to": None, "via_id": promoted},
    ]  # fmt: skip
    for alias in tables["provider_aliases"]:
        if alias["external_id"] == "synthetic-barcode-1":
            alias["valid_to"] = "2026-10-20T00:00:00+00:00"
    return tables


def test_promotion_revert_chain_and_closed_alias_are_events() -> None:
    payload = _Catalog(_with_writers_that_do_not_exist_yet()).build(W1)
    e4 = _edition(payload, E4)

    types = [event["type"] for event in e4["events"]]
    assert types.count("promoted") == 1
    assert types.count("reverted") == 1
    assert [event["compressed"] for event in e4["events"] if event["type"] == "reattached"] == [True]
    assert [edition["gm_id"] for edition in payload["editions"]] == [E1, E4, E3, E5]
    assert e4["former_items"][0]["gm_id"] == E2
    assert [event["provider"] for event in _edition(payload, E1)["events"] if event["type"] == "alias_closed"] == ["barcode"]
    assert {row["closed_by"] for row in _edition(payload, E1)["evidence"] if row["valid_to"]} == {"other"}


def test_iso_renders_utc_with_z() -> None:
    assert iso(None) is None
    assert iso(datetime(2026, 9, 2, 12, 0, tzinfo=timezone(timedelta(hours=2)))) == "2026-09-02T10:00:00Z"
    assert iso(datetime.fromisoformat(R1)) == "2026-09-02T10:00:01Z"


def test_an_item_without_aliases_has_no_events() -> None:
    tables = story()
    tables["catalog_item_supersessions"].append(
        {"id": native(0x5201), "superseded_id": native(0x9999), "survivor_id": E4, "cause": "catalog_reattachment",
         "decision_ref": RUN1, "valid_from": "2026-11-01T00:00:00+00:00", "valid_to": None, "via_id": None}
    )  # fmt: skip
    e4 = _edition(_Catalog(tables).build(W1), E4)

    assert [event["type"] for event in e4["events"]] == ["minted", "reattached"]
    assert e4["state"] == "discogs_only"
