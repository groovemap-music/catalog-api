"""Tests for the native-id merge steps (api/catalog_merge.py) — ADR 0009's 2026-09-25 amendment.

These pin what each step sends, in what order, and what it does with the rows back. The SQL
itself — the lock conflicts, chain compression, the ledger, the revert, and the caches — is
proved against the real schema in `tests/test_reattach_integration.py`.
"""

from __future__ import annotations

import re
from typing import Any
from uuid import UUID

import pytest

from api.catalog_merge import (
    CACHE_TABLES,
    MOVED_TABLES,
    MergeConflictError,
    MergeOutcome,
    current_supersession,
    lock_items,
    merge_items,
    recompute_caches,
    revert_supersession,
)
from tests.fake_postgres import FakePool


LOW = UUID("00000000-0000-7000-8000-000000000001")
HIGH = UUID("00000000-0000-7000-8000-000000000002")
THIRD = UUID("00000000-0000-7000-8000-000000000003")
EARLIER = UUID("00000000-0000-7000-8000-000000000009")
SUPERSESSION = UUID("00000000-0000-7000-8000-0000000000aa")
DECISION = UUID("00000000-0000-4000-8000-0000000000dd")
OPENED_AT = "2026-09-25T00:00:00+00:00"


async def _run(results: list[list[tuple[Any, ...]]], step: Any, *args: Any, **kwargs: Any) -> tuple[Any, FakePool]:
    pool = FakePool(results)
    async with pool.connection() as conn:
        outcome = await step(conn.cursor(), *args, **kwargs)
    return outcome, pool


def _sql(pool: FakePool) -> list[str]:
    return [" ".join(call.sql.split()) for call in pool.calls]


class TestTables:
    def test_moved_tables_and_their_owner_columns(self) -> None:
        assert MOVED_TABLES == {"artifacts": "created_by", "owned_copies": "user_id"}

    def test_every_catalog_kind_has_its_caches(self) -> None:
        assert {kind: [cache.table for cache in caches] for kind, caches in CACHE_TABLES.items()} == {
            "release": ["public.releases", "musicbrainz.releases", "user_collections", "user_wantlists"],
            "master": ["public.masters", "musicbrainz.release_groups"],
            "artist": ["public.artists", "musicbrainz.artists"],
            "label": ["public.labels", "musicbrainz.labels"],
        }


class TestLockItems:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(("superseded", "survivor"), [(LOW, HIGH), (HIGH, LOW)])
    async def test_locks_in_id_order_and_only_the_superseded_item_for_update(self, superseded: UUID, survivor: UUID) -> None:
        kind, pool = await _run([[("artist",)], [("artist",)]], lock_items, superseded, survivor)

        assert kind == "artist"
        assert [call.params for call in pool.calls] == [(LOW,), (HIGH,)]
        strengths = {call.params[0]: call.sql.rsplit("FOR ", 1)[1] for call in pool.calls}
        assert strengths == {superseded: "UPDATE", survivor: "NO KEY UPDATE"}

    @pytest.mark.asyncio
    async def test_a_revert_locks_both_for_update(self) -> None:
        _kind, pool = await _run([[("release",)], [("release",)]], lock_items, HIGH, LOW, revert=True)

        assert all(call.sql.endswith("FOR UPDATE") and "NO KEY" not in call.sql for call in pool.calls)

    @pytest.mark.asyncio
    async def test_refuses_the_same_item_before_any_statement(self) -> None:
        with pytest.raises(MergeConflictError, match="itself"):
            await _run([], lock_items, LOW, LOW)

    @pytest.mark.asyncio
    async def test_refuses_a_missing_item(self) -> None:
        with pytest.raises(MergeConflictError, match="does not exist"):
            await _run([[("release",)], []], lock_items, LOW, HIGH)

    @pytest.mark.asyncio
    async def test_refuses_different_kinds(self) -> None:
        with pytest.raises(MergeConflictError, match="kinds differ"):
            await _run([[("release",)], [("master",)]], lock_items, LOW, HIGH)


class TestCurrentSupersession:
    @pytest.mark.asyncio
    async def test_neither_superseded(self) -> None:
        existing, pool = await _run([[]], current_supersession, LOW, HIGH)

        assert existing is None
        assert pool.params == ([LOW, HIGH],)
        assert "valid_to IS NULL" in pool.sql

    @pytest.mark.asyncio
    async def test_already_superseded_into_this_survivor_is_reused(self) -> None:
        existing, _pool = await _run([[(LOW, SUPERSESSION, HIGH)]], current_supersession, LOW, HIGH)

        assert existing == SUPERSESSION

    @pytest.mark.asyncio
    async def test_a_superseded_survivor_would_take_two_hops(self) -> None:
        with pytest.raises(MergeConflictError, match="itself superseded"):
            await _run([[(HIGH, SUPERSESSION, THIRD)]], current_supersession, LOW, HIGH)

    @pytest.mark.asyncio
    async def test_superseded_into_another_item(self) -> None:
        with pytest.raises(MergeConflictError, match="already superseded"):
            await _run([[(LOW, SUPERSESSION, THIRD)]], current_supersession, LOW, HIGH)


class TestMergeItems:
    @pytest.mark.asyncio
    async def test_opens_compresses_repoints_ledgers_and_recomputes(self) -> None:
        outcome, pool = await _run(
            [
                [(SUPERSESSION,)],
                [(EARLIER, "catalog_reattachment", DECISION)],  # EARLIER currently resolves to LOW
                [],
                [(1,)],
                [(2,)],
                [(0,)],
                [(1,)],
            ],
            merge_items,
            LOW,
            HIGH,
            kind="artist",
            cause="catalog_reattachment",
            decision_ref=DECISION,
        )

        assert outcome == MergeOutcome(
            SUPERSESSION,
            opened=True,
            chains_compressed=1,
            moved={"artifacts": 1, "owned_copies": 2},
            caches_recomputed={"public.artists": 0, "musicbrainz.artists": 1},
        )
        open_row, close_survived, open_compressed, artifacts, copies, *recomputes = pool.calls
        assert open_row.params == (LOW, HIGH, "catalog_reattachment", DECISION)
        assert "SET valid_to = now() WHERE survivor_id = %s AND valid_to IS NULL" in " ".join(close_survived.sql.split())
        assert close_survived.params == (LOW,)
        # The compressed row points at the new survivor, keeps its own cause and decision, and
        # names the row that compressed it.
        assert open_compressed.params == (HIGH, SUPERSESSION, [EARLIER], ["catalog_reattachment"], [DECISION])
        assert "via_id" in open_compressed.sql
        for call, table, owner in ((artifacts, "artifacts", "created_by"), (copies, "owned_copies", "user_id")):
            sql = " ".join(call.sql.split())
            assert f"UPDATE {table} SET item_id = %(survivor)s WHERE item_id = %(superseded)s RETURNING id, {owner} AS user_id" in sql  # noqa: S608 — test assertion text
            assert "INSERT INTO catalog_item_moves" in sql
            assert f"'{table}'" in sql
            assert call.params == {"survivor": HIGH, "superseded": LOW, "supersession": SUPERSESSION}
        assert [call.params for call in recomputes] == [([LOW, HIGH],)] * 2

    @pytest.mark.asyncio
    async def test_writes_only_the_item_reference_and_deletes_nothing(self) -> None:
        _outcome, pool = await _run(
            [[(SUPERSESSION,)], [], [(0,)], [(0,)], *[[(0,)]] * 4],
            merge_items,
            LOW,
            HIGH,
            kind="release",
            cause="catalog_reattachment",
            decision_ref=DECISION,
        )

        sql = _sql(pool)
        assert not any("DELETE" in statement for statement in sql)
        for statement in sql:
            for table in MOVED_TABLES:
                match = re.search(rf"UPDATE {table} SET (.+?) WHERE", statement)  # noqa: S608 — test assertion text
                if match:
                    assert match.group(1) == "item_id = %(survivor)s", "no column but the item reference is written"
        # Nothing to compress: no compressed row is opened.
        assert sum("INSERT INTO catalog_item_supersessions" in statement for statement in sql) == 1

    @pytest.mark.asyncio
    async def test_an_open_row_is_reused_without_reopening_or_recompressing(self) -> None:
        outcome, pool = await _run(
            [[(0,)], [(1,)], [(0,)], [(0,)]],
            merge_items,
            LOW,
            HIGH,
            kind="label",
            cause="catalog_reattachment",
            decision_ref=DECISION,
            existing=SUPERSESSION,
        )

        assert outcome.opened is False
        assert outcome.supersession_id == SUPERSESSION
        assert outcome.moved == {"artifacts": 0, "owned_copies": 1}
        assert not any("catalog_item_supersessions" in statement.split("catalog_item_moves")[0] for statement in _sql(pool))

    @pytest.mark.asyncio
    async def test_the_cause_vocabulary_is_closed(self) -> None:
        with pytest.raises(ValueError, match="cause"):
            await _run([], merge_items, LOW, HIGH, kind="release", cause="provider_merge", decision_ref=DECISION)


class TestRecomputeCaches:
    @pytest.mark.asyncio
    async def test_each_cache_resolves_through_its_own_alias(self) -> None:
        counts, pool = await _run([[(2,)], [(0,)]], recompute_caches, "master", [LOW, HIGH])

        assert counts == {"public.masters": 2, "musicbrainz.release_groups": 0}
        discogs, musicbrainz = _sql(pool)
        assert "UPDATE public.masters AS cached" in discogs
        assert "alias.provider = 'discogs' AND alias.entity_kind = 'master' AND alias.external_id = cached.data_id::text" in discogs
        assert "UPDATE musicbrainz.release_groups AS cached" in musicbrainz
        assert "alias.provider = 'musicbrainz' AND alias.entity_kind = 'master' AND alias.external_id = cached.mbid::text" in musicbrainz
        assert all("alias.valid_to IS NULL" in statement for statement in (discogs, musicbrainz))


class TestRevertSupersession:
    @pytest.mark.asyncio
    async def test_closes_reopens_compressed_rows_moves_back_and_recomputes(self) -> None:
        outcome, pool = await _run(
            [
                [(LOW, HIGH, True, None)],
                [("release",)],
                [("release",)],
                [(LOW, HIGH, True, None)],
                [],  # close
                [(EARLIER, OPENED_AT)],  # one row this merge compressed
                [(UUID(int=77),)],  # its predecessor, re-opened
                [(1,)],
                [(3,)],
                *[[(0,)]] * 4,
            ],
            revert_supersession,
            SUPERSESSION,
        )

        assert outcome.chains_reopened == 1
        assert outcome.moved_back == {"artifacts": 1, "owned_copies": 3}
        assert set(outcome.caches_recomputed) == {cache.table for cache in CACHE_TABLES["release"]}
        read, lock_low, lock_high, relock, close, close_compressed, reopen, artifacts, copies, *recomputes = pool.calls
        assert read.params == (SUPERSESSION,)
        assert [lock_low.params, lock_high.params] == [(LOW,), (HIGH,)]
        assert relock.sql.endswith("FOR UPDATE")
        assert close.params == (SUPERSESSION,)
        assert close_compressed.params == (SUPERSESSION,)
        assert "via_id = %s" in close_compressed.sql
        # The predecessor is the row into the reverted row's superseded item that closed exactly
        # when the compressed row opened.
        assert reopen.params == ([EARLIER], [OPENED_AT], LOW)
        assert "SET valid_to = NULL" in reopen.sql
        for call, table in ((artifacts, "artifacts"), (copies, "owned_copies")):
            sql = " ".join(call.sql.split())
            assert f"UPDATE {table} AS target SET item_id = ledger.from_item_id" in sql  # noqa: S608 — test assertion text
            assert "target.item_id = ledger.to_item_id" in sql, "only rows still on the survivor move back"
            assert call.params == (SUPERSESSION,)
        assert [call.params for call in recomputes] == [([LOW, HIGH],)] * 4
        assert not any("DELETE" in statement for statement in _sql(pool))

    @pytest.mark.asyncio
    async def test_a_missing_row_is_refused(self) -> None:
        with pytest.raises(MergeConflictError, match="does not exist"):
            await _run([[]], revert_supersession, SUPERSESSION)

    @pytest.mark.asyncio
    async def test_a_closed_row_is_refused_before_any_write(self) -> None:
        pool = FakePool([[(LOW, HIGH, True, None)], [("release",)], [("release",)], [(LOW, HIGH, False, None)]])
        with pytest.raises(MergeConflictError, match="not current"):
            async with pool.connection() as conn:
                await revert_supersession(conn.cursor(), SUPERSESSION)
        assert all(" ".join(sql.split()).startswith("SELECT") for sql in _sql(pool))

    @pytest.mark.asyncio
    async def test_a_compressed_row_waits_for_the_later_merge_to_be_reverted(self) -> None:
        with pytest.raises(MergeConflictError, match="revert that merge first"):
            await _run([[(LOW, HIGH, True, THIRD)], [("release",)], [("release",)], [(LOW, HIGH, True, THIRD)]], revert_supersession, SUPERSESSION)

    @pytest.mark.asyncio
    async def test_a_missing_predecessor_is_refused(self) -> None:
        with pytest.raises(MergeConflictError, match="predecessor"):
            await _run(
                [[(LOW, HIGH, True, None)], [("release",)], [("release",)], [(LOW, HIGH, True, None)], [], [(EARLIER, OPENED_AT)], []],
                revert_supersession,
                SUPERSESSION,
            )
