"""Query shape of api/queries/identity_ledger_queries.py against a fake pool.

The statements themselves are parsed and run by PostgreSQL in
`tests/test_identity_ledger_integration.py`; here the fake pool proves the read-only
transaction, the order of reads, the parameters each read binds, and how rows become the
payload.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest

from api.queries import identity_ledger_queries as queries
from api.queries.identity_ledger_queries import get_ledger_density, get_ledger_work, get_ledger_works
from tests.fake_postgres import FakePool


pytestmark = pytest.mark.asyncio

W = "00000000-0000-4000-8000-000000000101"
E = "00000000-0000-4000-8000-000000000001"
H = "00000000-0000-4000-8000-000000000002"
M1 = "00000000-0000-4000-9000-000000000001"
G1 = "00000000-0000-4000-9000-0000000000b1"
RUN = "00000000-0000-4000-8000-00000000a0f1"
T0 = datetime(2026, 8, 1, 2, tzinfo=UTC)
T1 = datetime(2026, 8, 3, 5, tzinfo=UTC)
BACKFILLED = datetime(2025, 11, 14, tzinfo=UTC)


def _alias(provider: str, kind: str, external_id: str, native_id: str, valid_from: datetime) -> tuple[Any, ...]:
    return (provider, kind, external_id, UUID(native_id), valid_from, None, "catalog")


def _work_results(*, named: int = 1, holder: str = E) -> list[list[tuple[Any, ...]]]:
    """One linked edition under one work; the MusicBrainz row names Discogs release `named`."""
    results: list[list[tuple[Any, ...]]] = [
        [],  # SET TRANSACTION
        [(UUID(W), "master")],
        [(UUID(E), T0)],
        [],  # no item superseded into the work or the edition
        [
            _alias("discogs", "master", "10", W, T0),
            _alias("musicbrainz", "master", G1, W, T1),
            _alias("discogs", "release", "1", E, T0),
            _alias("musicbrainz", "release", M1, E, T1),
            _alias("musicbrainz", "release", "not-a-uuid", E, T1),
        ],
        [("1", "10")],
        [(UUID(M1), UUID(G1), named)],
        [(UUID(holder),)],
    ]
    if holder != E:
        results.append([_alias("discogs", "release", str(named), holder, T0)])
    results += [
        [("discogs", "10", UUID(W)), ("musicbrainz", G1, UUID(W))],
        [],  # no supersession
        [],  # no move
        [("musicbrainz", "release", M1, "link_added", None, named, BACKFILLED, "musicbrainz_fullexport", "20261024-001001", UUID(RUN))],
    ]
    return results


def _statements(pool: FakePool) -> list[str]:
    return [call.sql for call in pool.calls]


async def test_work_reads_in_one_read_only_transaction() -> None:
    pool = FakePool(_work_results())

    payload = await get_ledger_work(pool, W)

    statements = _statements(pool)
    assert statements[0] == "BEGIN"
    assert statements[1] == "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
    assert statements[-1] == "COMMIT"
    assert statements[2:-1] == [
        queries._RESOLVE,
        queries._EDITIONS,
        queries._GROUPS,
        queries._ALIASES,
        queries._DISCOGS_RELEASES,
        queries._MUSICBRAINZ_RELEASES,
        queries._DISCOGS_HOLDERS,
        queries._MASTER_WORKS,
        queries._SUPERSESSIONS,
        queries._MOVE_COUNTS,
        queries._LINK_EVENTS,
    ]
    assert payload is not None
    [edition] = payload["editions"]
    assert edition["state"] == "linked"
    assert (edition["primary_work"], edition["works"], edition["work_disagreement"]) == (W, [W], False)
    assert [(event["type"], event["dated_from"]) for event in edition["events"]] == [
        ("link_added", "musicbrainz_fullexport"),
        ("minted", "groovemap"),
        # Both MusicBrainz aliases opened at T1; the first in id order is the link.
        ("evidence_attached", "groovemap"),
        ("linked_at_ingest", "groovemap"),
    ]
    assert payload["work"]["gm_id"] == W
    assert "requested_id" not in payload["work"]


async def test_work_binds_each_read_to_the_rows_before_it() -> None:
    pool = FakePool(_work_results())

    await get_ledger_work(pool, W, edition_limit=5)

    params = {call.sql: call.params for call in pool.calls}
    assert params[queries._RESOLVE] == {"id": W}
    assert params[queries._EDITIONS] == {"work": W}
    assert params[queries._GROUPS] == {"ids": [W, E]}
    assert params[queries._ALIASES] == {"ids": sorted([W, E])}
    assert params[queries._DISCOGS_RELEASES] == {"ids": ["1"]}
    # A MusicBrainz alias that is not a UUID cannot name a row and is not bound.
    assert params[queries._MUSICBRAINZ_RELEASES] == {"ids": [M1]}
    assert params[queries._DISCOGS_HOLDERS] == {"ids": ["1"]}
    assert params[queries._MASTER_WORKS] == {"discogs": ["10"], "musicbrainz": [G1]}
    assert params[queries._SUPERSESSIONS] == {"ids": sorted([W, E])}
    assert params[queries._MOVE_COUNTS] == {"ids": []}
    assert params[queries._LINK_EVENTS] == {"releases": sorted([M1, "not-a-uuid"]), "masters": [G1]}


async def test_work_reads_the_item_holding_a_named_discogs_release() -> None:
    pool = FakePool(_work_results(named=2, holder=H))

    payload = await get_ledger_work(pool, W)

    assert [call.params for call in pool.calls if call.sql == queries._ALIASES][1] == {"ids": [H]}
    assert payload is not None
    assert payload["editions"][0]["state"] == "linked"
    assert payload["editions"][0]["provider_rows"][1] == {"provider": "musicbrainz", "external_id": M1, "names_discogs_release": "2"}


async def test_unknown_or_non_work_ids_are_none() -> None:
    for resolved in ([], [(UUID(E), "release")]):
        pool = FakePool([[], resolved])
        assert await get_ledger_work(pool, E) is None
        assert _statements(pool)[2:-1] == [queries._RESOLVE]


async def test_as_of_reads_only_the_editions_recorded_by_then() -> None:
    results = _work_results()
    results[2] = [(UUID(E), T1)]
    # Only the work's own items are read: its aliases, and no release row or holder.
    results[4] = results[4][:2]
    results[5:8] = [[], [], []]
    pool = FakePool(results)

    payload = await get_ledger_work(pool, W, as_of=T0)

    assert next(call.params for call in pool.calls if call.sql == queries._GROUPS) == {"ids": [W]}
    assert payload is not None
    assert (payload["editions"], payload["edition_count"], payload["truncated"]) == ([], 0, False)
    assert payload["as_of"] == "2026-08-01T02:00:00Z"


async def test_groups_name_the_items_superseded_into_each_survivor() -> None:
    pool = FakePool([[(UUID(E), UUID(H)), (UUID(E), UUID(W))]])
    async with pool.connection() as conn, conn.cursor() as cur:
        groups = await queries._groups(cur, [E, W])
    assert groups == {E: [E, H, W], W: [W]}


async def test_works_list_pages_and_joins_each_works_summary() -> None:
    since, until = datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 10, 1, tzinfo=UTC)
    pool = FakePool(
        [
            [],
            [(3, UUID(W), 3, 1, T0, T1), (3, UUID(E), 1, 0, T1, T1)],
            [(UUID(W), 5, 0)],
            [(UUID(W), "discogs", "10"), (UUID(W), "musicbrainz", G1)],
        ]
    )

    data = await get_ledger_works(pool, event="reattached", since=since, until=until, page=2, page_size=2)

    works_call = next(call for call in pool.calls if call.sql == queries._WORKS)
    assert works_call.params == {"kind": "release", "event": "reattached", "since": since, "until": until, "limit": 2, "offset": 2}
    assert next(call.params for call in pool.calls if call.sql == queries._WORK_SUMMARIES) == {"works": [W, E]}
    assert data == {
        "works": [
            {
                "gm_id": W,
                "providers": [{"provider": "discogs", "external_id": "10"}, {"provider": "musicbrainz", "external_id": G1}],
                "editions": 5,
                "evented_editions": 3,
                "repaired_editions": 1,
                "work_disagreements": 0,
                "first_event": "2026-08-01T02:00:00Z",
                "last_event": "2026-08-03T05:00:00Z",
            },
            {
                "gm_id": E,
                "providers": [],
                "editions": 0,
                "evented_editions": 1,
                "repaired_editions": 0,
                "work_disagreements": 0,
                "first_event": "2026-08-03T05:00:00Z",
                "last_event": "2026-08-03T05:00:00Z",
            },
        ],
        "total": 3,
        "page": 2,
        "page_size": 2,
    }


async def test_an_empty_page_still_reports_the_total() -> None:
    pool = FakePool([[], [(4, None, None, None, None, None)]])

    data = await get_ledger_works(pool, page=9, page_size=50)

    assert data == {"works": [], "total": 4, "page": 9, "page_size": 50}
    assert queries._WORK_SUMMARIES not in _statements(pool)


async def test_works_list_has_no_default_window() -> None:
    assert "90 days" not in queries._WORKS
    assert "INTERVAL" not in queries._WORKS.upper()


async def test_density_buckets_name_their_provenance() -> None:
    pool = FakePool([[], [("2025-11", "link_added", "musicbrainz_fullexport", 1), ("2026-08", "minted", "groovemap", 7)]])

    data = await get_ledger_density(pool, "master")

    assert next(call.params for call in pool.calls if call.sql == queries._DENSITY) == {"kind": "master"}
    assert data == {
        "kind": "master",
        "buckets": [
            {"month": "2025-11", "type": "link_added", "dated_from": "musicbrainz_fullexport", "events": 1},
            {"month": "2026-08", "type": "minted", "dated_from": "groovemap", "events": 7},
        ],
    }
    assert _statements(pool)[1] == "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"


async def test_no_statement_reads_a_user_row_or_an_admin_id() -> None:
    statements = [value for name, value in vars(queries).items() if name.isupper() and isinstance(value, str)]
    text = "\n".join(statements)
    for column in ("user_id", "row_id", "admin_id", "owned_copies", "artifacts"):
        assert column not in text


async def test_moves_are_counted_per_supersession_and_table() -> None:
    supersession = "00000000-0000-4000-8000-000000005003"
    results = _work_results()
    results[-3] = [(UUID(supersession), UUID(H), UUID(E), "catalog_reattachment", UUID(RUN), T1, None, None)]
    results[-2] = [(UUID(supersession), "owned_copies", 2), (UUID(supersession), "artifacts", 1)]
    pool = FakePool(results)

    payload = await get_ledger_work(pool, W)

    assert next(call.params for call in pool.calls if call.sql == queries._MOVE_COUNTS) == {"ids": [supersession]}
    assert payload is not None
    [reattached] = [event for event in payload["editions"][0]["events"] if event["type"] == "reattached"]
    assert reattached["moves"] == {"owned_copies": 2, "artifacts": 1}
    assert (reattached["superseded"], reattached["survivor"], reattached["decision_ref"]) == (H, E, RUN)
