"""An invented catalog for the Identity Ledger tests: spike `gm-design-loj9`'s synthetic story.

No Discogs or MusicBrainz data: ids and times are made up. Native ids are
`00000000-0000-4000-8000-…` and MBIDs `00000000-0000-4000-9000-…`. The story is cut to the
paths the ledger routes must show, plus `identity_link_events` rows of both provenances:

- 2026-08-01, Discogs load 1: masters 900101 (W1) and 900102 (W2); releases 900001, 900002,
  900004 under 900101 and 900005, 900006 under 900102.
- 2026-08-03, MusicBrainz load 1: release group B1 names 900101 and attaches to W1; B2 names
  no master and mints G2; B5 names 900105, not yet loaded, and mints G5X. r1 names 900001 and
  attaches to E1; r3 names 900003, not yet loaded, and mints X3; r5 names nothing (E5); r6
  and r7 name 900005 and 900006 and attach under B2, so E6 and E7 have two work parents.
- 2026-09-01, Discogs load 2: release 900003 (E3) under 900101. X3 and E3 are now split.
- 2026-09-02, re-attachment run RUN1: X3 is superseded into E3; r3's aliases move, and one
  owned copy (a user's) moves with them.
- 2026-09-10, MusicBrainz load 2: r2 attaches to E2, and the loader records r7's link change.
- 2026-10-01, Discogs load 3: master 900105 (W5); 2026-10-02, G5X is superseded into W5.
- Before any of that, the full-export backfill dated r1's and r3's links and B1's master link.

`tests/test_identity_ledger.py` builds payloads from these rows in memory, and
`tests/test_identity_ledger_integration.py` seeds them into real PostgreSQL.
"""

from __future__ import annotations

from typing import Any


def native(n: int) -> str:
    return f"00000000-0000-4000-8000-{n:012x}"


def mbid(n: int) -> str:
    return f"00000000-0000-4000-9000-{n:012x}"


def mb_load(seconds: int) -> str:
    return f"2026-08-03T05:00:{seconds:02d}+00:00"


D1, D2, D3 = "2026-08-01T02:00:00+00:00", "2026-09-01T02:00:00+00:00", "2026-10-01T02:00:00+00:00"
R1, R2 = "2026-09-02T10:00:01+00:00", "2026-10-02T10:00:01+00:00"
MB2 = "2026-09-10T05:00:00+00:00"
RUN1, RUN2, BACKFILL = native(0xA001), native(0xA002), native(0xA0F1)
EXPORT = "20261024-001001"

W1, W2, W5, G2, G5X = native(0x101), native(0x102), native(0x105), native(0x202), native(0x205)
E1, E2, E3, E4, E5, E6, E7, X3 = (native(n) for n in (0x1, 0x2, 0x3, 0x4, 0x5, 0x6, 0x7, 0x13))
S3, S5 = native(0x5003), native(0x5005)
OWNER = native(0xE001)
MOVED_ROW = native(0x7001)


def story(owner: str = OWNER) -> dict[str, list[dict[str, Any]]]:
    """The story's rows, table by table, with database-schema's column names."""
    items: list[dict[str, Any]] = []
    aliases: list[dict[str, Any]] = []

    def item(item_id: str, kind: str, created_at: str) -> None:
        items.append({"id": item_id, "kind": kind, "created_at": created_at})

    def alias(provider: str, kind: str, external_id: str, native_id: str, valid_from: str, valid_to: str | None = None) -> None:
        aliases.append(
            {
                "provider": provider,
                "entity_kind": kind,
                "external_id": external_id,
                "native_id": native_id,
                "valid_from": valid_from,
                "valid_to": valid_to,
                "source": "catalog",
            }
        )

    # Discogs load 1.
    for item_id, external in ((W1, "900101"), (W2, "900102")):
        item(item_id, "master", D1)
        alias("discogs", "master", external, item_id, D1)
    for item_id, external in ((E1, "900001"), (E2, "900002"), (E4, "900004"), (E6, "900005"), (E7, "900006")):
        item(item_id, "release", D1)
        alias("discogs", "release", external, item_id, D1)
    alias("barcode", "release", "synthetic-barcode-1", E1, D1)

    # MusicBrainz load 1.
    alias("musicbrainz", "master", mbid(0xB1), W1, mb_load(1))
    item(G2, "master", mb_load(3))
    alias("musicbrainz", "master", mbid(0xB2), G2, mb_load(3))
    item(G5X, "master", mb_load(5))
    alias("musicbrainz", "master", mbid(0xB5), G5X, mb_load(5), valid_to=R2)
    alias("musicbrainz", "release", mbid(0x1), E1, mb_load(10))
    item(X3, "release", mb_load(11))
    alias("musicbrainz", "release", mbid(0x3), X3, mb_load(11), valid_to=R1)
    alias("barcode", "release", "synthetic-barcode-3", X3, mb_load(11), valid_to=R1)
    item(E5, "release", mb_load(12))
    alias("musicbrainz", "release", mbid(0x5), E5, mb_load(12))
    alias("musicbrainz", "release", mbid(0x6), E6, mb_load(13))
    alias("musicbrainz", "release", mbid(0x7), E7, mb_load(14))

    # Discogs load 2, then re-attachment run 1 moves r3's aliases in one transaction.
    item(E3, "release", D2)
    alias("discogs", "release", "900003", E3, D2)
    alias("musicbrainz", "release", mbid(0x3), E3, R1)
    alias("barcode", "release", "synthetic-barcode-3", E3, R1)

    # MusicBrainz load 2; Discogs load 3 and re-attachment run 2.
    alias("musicbrainz", "release", mbid(0x2), E2, MB2)
    item(W5, "master", D3)
    alias("discogs", "master", "900105", W5, D3)
    alias("musicbrainz", "master", mbid(0xB5), W5, R2)

    def link(kind: str, external_id: str, event: str, before: int | None, after: int | None, at: str, dated_from: str) -> dict[str, Any]:
        backfilled = dated_from == "musicbrainz_fullexport"
        return {
            "provider": "musicbrainz",
            "entity_kind": kind,
            "external_id": external_id,
            "event": event,
            "discogs_id_before": before,
            "discogs_id_after": after,
            "event_at": at,
            "dated_from": dated_from,
            "source_ref": EXPORT if backfilled else "20260909-001001",
            "decision_ref": BACKFILL if backfilled else None,
        }

    return {
        "catalog_items": items,
        "provider_aliases": aliases,
        "catalog_item_supersessions": [
            {
                "id": S3,
                "superseded_id": X3,
                "survivor_id": E3,
                "cause": "catalog_reattachment",
                "decision_ref": RUN1,
                "valid_from": R1,
                "valid_to": None,
                "via_id": None,
            },
            {
                "id": S5,
                "superseded_id": G5X,
                "survivor_id": W5,
                "cause": "catalog_reattachment",
                "decision_ref": RUN2,
                "valid_from": R2,
                "valid_to": None,
                "via_id": None,
            },
        ],
        "catalog_item_moves": [
            {
                "supersession_id": S3,
                "table_name": "owned_copies",
                "row_id": MOVED_ROW,
                "from_item_id": X3,
                "to_item_id": E3,
                "user_id": owner,
                "moved_at": R1,
            },
        ],
        "discogs_releases": [
            {"data_id": data_id, "master_id": master_id}
            for data_id, master_id in (
                ("900001", "900101"),
                ("900002", "900101"),
                ("900003", "900101"),
                ("900004", "900101"),
                ("900005", "900102"),
                ("900006", "900102"),
            )
        ],
        "musicbrainz_releases": [
            {"mbid": mbid(0x1), "release_group_mbid": mbid(0xB1), "discogs_release_id": 900001},
            {"mbid": mbid(0x2), "release_group_mbid": mbid(0xB1), "discogs_release_id": 900002},
            {"mbid": mbid(0x3), "release_group_mbid": mbid(0xB1), "discogs_release_id": 900003},
            {"mbid": mbid(0x5), "release_group_mbid": mbid(0xB1), "discogs_release_id": None},
            {"mbid": mbid(0x6), "release_group_mbid": mbid(0xB2), "discogs_release_id": 900005},
            {"mbid": mbid(0x7), "release_group_mbid": mbid(0xB2), "discogs_release_id": 900006},
        ],
        "identity_link_events": [
            link("release", mbid(0x1), "link_added", None, 900001, "2025-11-14T00:00:00+00:00", "musicbrainz_fullexport"),
            link("release", mbid(0x3), "link_added", None, 900003, "2026-03-02T00:00:00+00:00", "musicbrainz_fullexport"),
            link("master", mbid(0xB1), "link_added", None, 900101, "2025-12-01T00:00:00+00:00", "musicbrainz_fullexport"),
            link("release", mbid(0x7), "link_changed", 900099, 900006, MB2, "groovemap_load"),
        ],
    }
