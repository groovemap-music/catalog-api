"""Tests for the catalog re-attachment job (api/reattach.py) — ADR 0014 section 8.

The SQL itself is proved against the real schema in `tests/test_reattach_integration.py`;
these tests pin what each step sends, in what order, and what it does with the rows back.
"""

from __future__ import annotations

import re
import sys
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from api.catalog_merge import CACHE_TABLE_NAMES, MergeConflictError
from api.reattach import (
    DEPENDENT_TABLES,
    GUARD_REASONS,
    IDENTIFIER_PROVIDERS,
    KINDS,
    ItemOutcome,
    KindTally,
    ReattachConflictError,
    audit_details,
    candidate_page_sql,
    census_sql,
    failure_details,
    identifier_sql,
    reattach_item,
    run_census,
    run_reattachment,
)
from tests.fake_postgres import FakePool
from tests.test_admin_endpoints import _admin_auth_headers


if TYPE_CHECKING:
    from fastapi.testclient import TestClient


MBID = "0f2a8c4e-1111-4222-8333-444455556666"
DISCOGS_ID = "4828001"
SPLIT_ID = UUID("00000000-0000-7000-8000-00000000000a")
DISCOGS_NATIVE_ID = UUID("00000000-0000-7000-8000-00000000000d")
OTHER_ID = UUID("00000000-0000-7000-8000-00000000000f")
SUPERSESSION_ID = UUID("00000000-0000-7000-8000-0000000000aa")
DECISION_REF = UUID("00000000-0000-4000-8000-0000000000dd")

RELEASE = KINDS["release"]

_WRITE = re.compile(r"\b(INSERT|UPDATE|DELETE|TRUNCATE|MERGE|ALTER|CREATE|DROP)\b")

NO_GUARDS: list[tuple[Any, ...]] = [(False,) * len(GUARD_REASONS)]

# The rows merge step 4 reads back: both items locked, in id order (the split id sorts first).
LOCKED_ITEMS: list[list[tuple[Any, ...]]] = [[("release",)], [("release",)]]

# Merge steps 5-7 for a release with nothing to compress: open, close-survived, one re-point
# count per moved table, one recompute count per release cache table.
MERGE_STEPS: list[list[tuple[Any, ...]]] = [[(SUPERSESSION_ID,)], [], [(0,)], [(0,)], [(0,)], [(0,)], [(0,)], [(0,)]]


def _statements(pool: FakePool) -> list[str]:
    return [call.sql for call in pool.calls]


def _is_read(sql: str) -> bool:
    """A statement that can only read: a SELECT (not locking) or the transaction's own mode."""
    stripped = sql.strip()
    if stripped in ("BEGIN", "COMMIT", "ROLLBACK") or stripped.startswith("SET TRANSACTION"):
        return True
    return stripped.startswith("SELECT") and not _WRITE.search(stripped.replace("FOR NO KEY UPDATE", "").replace("FOR UPDATE", ""))


async def _run_item(results: list[list[tuple[Any, ...]]]) -> tuple[ItemOutcome, FakePool]:
    pool = FakePool(results)
    async with pool.connection() as conn:
        outcome = await reattach_item(conn.cursor(), RELEASE, MBID, decision_ref=DECISION_REF)
    return outcome, pool


class TestKindMapping:
    """The four kinds, their `musicbrainz.*` tables, Discogs columns, and alias entity kinds."""

    def test_columns_match_database_schema(self) -> None:
        assert {name: (kind.table, kind.discogs_column, kind.entity_kind) for name, kind in KINDS.items()} == {
            "release": ("releases", "discogs_release_id", "release"),
            "release_group": ("release_groups", "discogs_master_id", "master"),
            "artist": ("artists", "discogs_artist_id", "artist"),
            "label": ("labels", "discogs_label_id", "label"),
        }

    @pytest.mark.parametrize("name", list(KINDS))
    def test_every_statement_reads_only_current_aliases(self, name: str) -> None:
        kind = KINDS[name]
        for sql in (census_sql(kind), identifier_sql(kind), candidate_page_sql(kind)):
            assert _is_read(sql)
            assert f"FROM musicbrainz.{kind.table} AS mb" in sql
            assert f"mb.{kind.discogs_column}::text" in sql
            # Both alias joins go through the partial unique index.
            assert "discogs_alias.valid_to IS NULL" in sql
            assert "mb_alias.valid_to IS NULL" in sql
            assert f"entity_kind = '{kind.entity_kind}'" in sql

    def test_only_releases_are_contested_against_the_discogs_document(self) -> None:
        assert "identifiers' -> 'aliases'" in identifier_sql(KINDS["release"])
        for name in ("release_group", "artist", "label"):
            assert "FILTER (WHERE FALSE)" in identifier_sql(KINDS[name])

    def test_candidate_page_is_keyset_on_the_primary_key(self) -> None:
        sql = candidate_page_sql(RELEASE)
        assert "linked.mbid_key > %s::uuid" in sql
        assert "ORDER BY linked.mbid_key" in sql
        assert "LIMIT %s" in sql


def _census_row(**overrides: int) -> tuple[int, ...]:
    columns = [
        "linked",
        "unresolved",
        "split",
        "stale_gm_item_id",
        *DEPENDENT_TABLES,
        "shared_native_id",
        "non_catalog_alias",
        "guarded",
        "will_move_items",
        "will_move_artifacts",
        "will_move_owned_copies",
    ]
    return tuple(overrides.get(column, 0) for column in columns)


class TestRunCensus:
    """run_census: read-only, bounded, and the report shape."""

    @pytest.mark.asyncio
    async def test_issues_only_selects_in_one_read_only_transaction(self) -> None:
        pool = FakePool()

        await run_census(pool)

        statements = _statements(pool)
        assert statements[0] == "BEGIN"
        assert statements[1] == "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
        assert statements[-1] == "COMMIT"
        assert all(_is_read(sql) for sql in statements)
        assert not any("FOR UPDATE" in sql for sql in statements)
        # Two set-based statements per kind, whatever the data: no per-row round trips.
        assert len(statements) == 3 + 2 * len(KINDS)

    @pytest.mark.asyncio
    async def test_report_shape_and_eligible_count(self) -> None:
        release_counts = _census_row(
            linked=10,
            unresolved=3,
            split=4,
            stale_gm_item_id=1,
            artifacts=1,
            user_collections=1,
            shared_native_id=1,
            guarded=1,
            will_move_items=1,
            will_move_artifacts=2,
        )
        pool = FakePool(
            [
                [],  # SET TRANSACTION
                [release_counts],
                [("barcode", 2, 1)],
            ]
        )

        census = await run_census(pool)

        assert list(census) == list(KINDS)
        assert census["release"] == {
            "linked": 10,
            "unresolved": 3,
            "split": 4,
            "stale_gm_item_id": 1,
            "eligible": 4,
            "guarded": 1,
            "guard_reasons": {"shared_native_id": 1, "non_catalog_alias": 0},
            "dependents": {"artifacts": 1, "owned_copies": 0, "observations": 0, "user_collections": 1, "user_wantlists": 0},
            "will_move": {"items": 1, "artifacts": 2, "owned_copies": 0},
            "identifier_aliases": {
                "barcode": {"held": 2, "contested": 1},
                "catalog_number": {"held": 0, "contested": 0},
                "isrc": {"held": 0, "contested": 0},
                "matrix": {"held": 0, "contested": 0},
            },
        }
        # A kind with no rows reports zeros rather than failing.
        assert census["label"]["split"] == 0
        assert census["label"]["eligible"] == 0

    @pytest.mark.asyncio
    @pytest.mark.parametrize("table", DEPENDENT_TABLES)
    async def test_a_dependent_is_reported_as_will_move_and_never_guards(self, table: str) -> None:
        pool = FakePool([[], [_census_row(split=1, will_move_items=1, will_move_owned_copies=1, **{table: 1})]])

        census = await run_census(pool)

        assert census["release"]["dependents"] == {name: int(name == table) for name in DEPENDENT_TABLES}
        assert census["release"]["will_move"] == {"items": 1, "artifacts": 0, "owned_copies": 1}
        assert census["release"]["guarded"] == 0
        assert census["release"]["eligible"] == 1

    def test_census_no_longer_guards_on_dependents(self) -> None:
        sql = census_sql(RELEASE)
        guarded = sql.split("AS non_catalog_alias,", 1)[1].split("AS guarded", 1)[0]
        assert "flags.artifacts" not in guarded
        assert "flags.shared_native_id OR flags.non_catalog_alias" in guarded
        # The rows the merge would re-point are counted only among items that are not guarded.
        assert "sum(flags.artifact_rows) FILTER (WHERE NOT (flags.shared_native_id OR flags.non_catalog_alias))" in sql


class TestReattachItem:
    """reattach_item: re-verify under lock, guard, close, re-insert, repoint the cache, merge."""

    @pytest.mark.asyncio
    async def test_missing_row_is_unchanged(self) -> None:
        outcome, pool = await _run_item([[]])

        assert outcome.status == "unchanged"
        assert len(pool.calls) == 1

    @pytest.mark.asyncio
    async def test_row_whose_discogs_link_was_removed_is_unchanged(self) -> None:
        outcome, pool = await _run_item([[(None, SPLIT_ID)]])

        assert outcome.status == "unchanged"
        assert len(pool.calls) == 1

    @pytest.mark.asyncio
    async def test_unresolved_discogs_id_is_unchanged_and_writes_nothing(self) -> None:
        outcome, pool = await _run_item([[(DISCOGS_ID, SPLIT_ID)], [("musicbrainz", SPLIT_ID)]])

        assert outcome.status == "unchanged"
        assert all(_is_read(sql) for sql in _statements(pool))

    @pytest.mark.asyncio
    async def test_not_split_is_unchanged_and_writes_nothing(self) -> None:
        outcome, pool = await _run_item([[(DISCOGS_ID, DISCOGS_NATIVE_ID)], [("musicbrainz", DISCOGS_NATIVE_ID), ("discogs", DISCOGS_NATIVE_ID)]])

        assert outcome.status == "unchanged"
        assert all(_is_read(sql) for sql in _statements(pool))

    @pytest.mark.asyncio
    async def test_split_is_reattached_and_merged_in_lock_order(self) -> None:
        outcome, pool = await _run_item(
            [
                [(DISCOGS_ID, SPLIT_ID)],
                [("musicbrainz", SPLIT_ID), ("discogs", DISCOGS_NATIVE_ID)],
                *LOCKED_ITEMS,
                NO_GUARDS,
                [],  # neither item is superseded
                [("musicbrainz", "release", MBID, 1.0), ("barcode", "release", "5012345678900", 1.0)],
                [("musicbrainz", "release", MBID), ("barcode", "release", "5012345678900")],
                [],
                [(SUPERSESSION_ID,)],
                [],  # nothing to compress
                [(1,)],  # artifacts
                [(2,)],  # owned copies
                [(1,)],
                [(0,)],
                [(3,)],
                [(0,)],
            ]
        )

        assert outcome.status == "reattached"
        assert (outcome.split_id, outcome.discogs_native_id, outcome.aliases_moved, outcome.identifier_collisions) == (
            SPLIT_ID,
            DISCOGS_NATIVE_ID,
            2,
            0,
        )
        assert outcome.supersession_opened is True
        assert outcome.moved == {"artifacts": 1, "owned_copies": 2}
        assert outcome.caches_recomputed == {"public.releases": 1, "musicbrainz.releases": 0, "user_collections": 3, "user_wantlists": 0}
        (
            lock_row,
            lock_aliases,
            lock_split,
            lock_discogs,
            guard,
            survivors,
            close,
            reinsert,
            set_cache,
            open_row,
            close_survived,
            repoint_artifacts,
            repoint_copies,
            *recomputes,
        ) = pool.calls
        assert lock_row.sql == "SELECT discogs_release_id::text, gm_item_id FROM musicbrainz.releases WHERE mbid = %s::uuid FOR UPDATE"
        assert lock_row.params == (MBID,)
        assert lock_aliases.sql.rstrip().endswith("FOR UPDATE")
        assert lock_aliases.params == ("release", MBID, DISCOGS_ID)
        # Merge step 4 comes before any write: the split item blocks foreign-key inserts, the
        # survivor only other merges and reverts.
        assert lock_split.sql == "SELECT kind FROM catalog_items WHERE id = %s FOR UPDATE"
        assert lock_split.params == (SPLIT_ID,)
        assert lock_discogs.sql == "SELECT kind FROM catalog_items WHERE id = %s FOR NO KEY UPDATE"
        assert lock_discogs.params == (DISCOGS_NATIVE_ID,)
        assert guard.params == {"split_id": SPLIT_ID, "mbid": MBID}
        assert "held.entity_kind = 'release'" in guard.sql
        assert "artifacts" not in guard.sql, "the dependents guard is gone"
        assert survivors.params == ([SPLIT_ID, DISCOGS_NATIVE_ID],)
        assert "SET valid_to = now()" in close.sql
        assert close.params == (SPLIT_ID,)
        assert "'catalog'" in reinsert.sql
        assert "ON CONFLICT (provider, entity_kind, external_id) WHERE valid_to IS NULL DO NOTHING" in reinsert.sql
        assert reinsert.params == (
            DISCOGS_NATIVE_ID,
            ["musicbrainz", "barcode"],
            ["release", "release"],
            [MBID, "5012345678900"],
            [1.0, 1.0],
        )
        assert set_cache.sql == "UPDATE musicbrainz.releases SET gm_item_id = %s WHERE mbid = %s::uuid"
        assert set_cache.params == (DISCOGS_NATIVE_ID, MBID)
        assert "INSERT INTO catalog_item_supersessions" in open_row.sql
        assert open_row.params == (SPLIT_ID, DISCOGS_NATIVE_ID, "catalog_reattachment", DECISION_REF)
        assert close_survived.params == (SPLIT_ID,)
        assert "UPDATE artifacts SET item_id" in repoint_artifacts.sql
        assert "UPDATE owned_copies SET item_id" in repoint_copies.sql
        assert repoint_copies.params == {"survivor": DISCOGS_NATIVE_ID, "superseded": SPLIT_ID, "supersession": SUPERSESSION_ID}
        assert [call.params for call in recomputes] == [([SPLIT_ID, DISCOGS_NATIVE_ID],)] * 4
        # The former native id is never deleted.
        assert not any("DELETE" in sql for sql in _statements(pool))

    @pytest.mark.asyncio
    async def test_stale_cache_moves_the_orphan_identifiers_and_repoints_the_row(self) -> None:
        outcome, pool = await _run_item(
            [
                [(DISCOGS_ID, SPLIT_ID)],
                [("musicbrainz", DISCOGS_NATIVE_ID), ("discogs", DISCOGS_NATIVE_ID)],
                *LOCKED_ITEMS,
                NO_GUARDS,
                [],
                [],  # nothing left on the orphan
                [],
                *MERGE_STEPS,
            ]
        )

        assert outcome.status == "reattached"
        assert outcome.split_id == SPLIT_ID
        assert outcome.aliases_moved == 0
        # No alias re-insert when nothing was closed; the cache still follows the alias.
        assert not any(sql.lstrip().startswith("INSERT INTO provider_aliases") for sql in _statements(pool))
        assert pool.calls[7].params == (DISCOGS_NATIVE_ID, MBID)

    @pytest.mark.asyncio
    async def test_an_item_already_superseded_into_the_discogs_item_reuses_its_supersession(self) -> None:
        """The loader race's residue: the re-run moves what is left under the open row, opening none."""
        outcome, pool = await _run_item(
            [
                [(DISCOGS_ID, SPLIT_ID)],
                [("musicbrainz", DISCOGS_NATIVE_ID), ("discogs", DISCOGS_NATIVE_ID)],
                *LOCKED_ITEMS,
                NO_GUARDS,
                [(SPLIT_ID, SUPERSESSION_ID, DISCOGS_NATIVE_ID)],
                [],
                [],
                [(0,)],
                [(1,)],
                *[[(0,)]] * 4,
            ]
        )

        assert outcome.status == "reattached"
        assert outcome.supersession_opened is False
        assert outcome.moved == {"artifacts": 0, "owned_copies": 1}
        assert not any("INSERT INTO catalog_item_supersessions" in sql for sql in _statements(pool))
        assert pool.calls[9].params["supersession"] == SUPERSESSION_ID

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("survivors", "match"),
        [
            ([(DISCOGS_NATIVE_ID, SUPERSESSION_ID, OTHER_ID)], "itself superseded"),
            ([(SPLIT_ID, SUPERSESSION_ID, OTHER_ID)], "already superseded"),
        ],
    )
    async def test_a_merge_that_cannot_happen_raises_before_any_write(self, survivors: list[tuple[Any, ...]], match: str) -> None:
        pool = FakePool([[(DISCOGS_ID, SPLIT_ID)], [("musicbrainz", SPLIT_ID), ("discogs", DISCOGS_NATIVE_ID)], *LOCKED_ITEMS, NO_GUARDS, survivors])

        with pytest.raises(MergeConflictError, match=match):
            async with pool.connection() as conn, conn.transaction():
                await reattach_item(conn.cursor(), RELEASE, MBID, decision_ref=DECISION_REF)

        assert all(_is_read(sql) for sql in _statements(pool))
        assert pool.calls[-1].sql == "ROLLBACK"

    @pytest.mark.asyncio
    async def test_items_of_different_kinds_never_merge(self) -> None:
        pool = FakePool([[(DISCOGS_ID, SPLIT_ID)], [("musicbrainz", SPLIT_ID), ("discogs", DISCOGS_NATIVE_ID)], [("release",)], [("master",)]])

        with pytest.raises(MergeConflictError, match="kinds differ"):
            async with pool.connection() as conn:
                await reattach_item(conn.cursor(), RELEASE, MBID, decision_ref=DECISION_REF)

        assert all(_is_read(sql) for sql in _statements(pool))

    @pytest.mark.asyncio
    @pytest.mark.parametrize("reason", GUARD_REASONS)
    async def test_guarded_item_is_skipped_before_any_write(self, reason: str) -> None:
        flags = tuple(name == reason for name in GUARD_REASONS)
        outcome, pool = await _run_item(
            [
                [(DISCOGS_ID, SPLIT_ID)],
                [("musicbrainz", SPLIT_ID), ("discogs", DISCOGS_NATIVE_ID)],
                *LOCKED_ITEMS,
                [flags],
            ]
        )

        assert outcome.status == "guarded"
        assert outcome.reasons == (reason,)
        assert outcome.split_id == SPLIT_ID
        assert all(_is_read(sql) for sql in _statements(pool))

    def test_only_the_two_alias_guards_remain(self) -> None:
        assert GUARD_REASONS == ("shared_native_id", "non_catalog_alias")

    @pytest.mark.asyncio
    async def test_identifier_already_on_the_discogs_item_is_not_duplicated(self) -> None:
        outcome, pool = await _run_item(
            [
                [(DISCOGS_ID, SPLIT_ID)],
                [("musicbrainz", SPLIT_ID), ("discogs", DISCOGS_NATIVE_ID)],
                *LOCKED_ITEMS,
                NO_GUARDS,
                [],
                [("musicbrainz", "release", MBID, 1.0), ("barcode", "release", "5012345678900", 1.0)],
                [("musicbrainz", "release", MBID)],
                [("barcode", "release", "5012345678900", DISCOGS_NATIVE_ID)],
                [],
                *MERGE_STEPS,
            ]
        )

        assert outcome.status == "reattached"
        assert outcome.aliases_moved == 1
        assert outcome.identifier_collisions == 1
        holders = pool.calls[8]
        assert holders.params == (["barcode"], ["release"], ["5012345678900"])

    @pytest.mark.asyncio
    async def test_identifier_held_by_a_third_item_rolls_the_item_back(self) -> None:
        pool = FakePool(
            [
                [(DISCOGS_ID, SPLIT_ID)],
                [("musicbrainz", SPLIT_ID), ("discogs", DISCOGS_NATIVE_ID)],
                *LOCKED_ITEMS,
                NO_GUARDS,
                [],
                [("barcode", "release", "5012345678900", 1.0)],
                [],
                [("barcode", "release", "5012345678900", OTHER_ID)],
            ]
        )

        with pytest.raises(ReattachConflictError):
            async with pool.connection() as conn, conn.transaction():
                await reattach_item(conn.cursor(), RELEASE, MBID, decision_ref=DECISION_REF)

        assert pool.calls[-1].sql == "ROLLBACK"
        assert not any("catalog_item_supersessions (" in sql for sql in _statements(pool))


class TestRunReattachment:
    """run_reattachment: dry run by default; an applying run pages and tallies."""

    @pytest.mark.asyncio
    async def test_dry_run_is_the_default_and_writes_nothing(self) -> None:
        pool = FakePool()

        report = await run_reattachment(pool)

        assert report["apply"] is False
        assert "outcomes" not in report
        assert set(report["census"]) == set(KINDS)
        assert all(_is_read(sql) for sql in _statements(pool))

    @pytest.mark.asyncio
    async def test_apply_pages_every_kind_and_tallies_outcomes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import api.reattach as reattach_module

        monkeypatch.setattr(reattach_module, "run_census", AsyncMock(return_value={"release": {}}))
        release_page = [
            (MBID, DISCOGS_ID, MBID),
            ("1f2a8c4e-1111-4222-8333-444455556666", "2", "1f2a8c4e-1111-4222-8333-444455556666"),
            ("2f2a8c4e-1111-4222-8333-444455556666", "3", "2f2a8c4e-1111-4222-8333-444455556666"),
        ]
        # release: one page then the empty page; every other kind: one empty page.
        pool = FakePool([release_page, [], [], [], []])
        outcomes = [
            ItemOutcome(
                "reattached",
                (),
                SPLIT_ID,
                DISCOGS_NATIVE_ID,
                aliases_moved=2,
                identifier_collisions=1,
                supersession_opened=True,
                moved={"artifacts": 0, "owned_copies": 2},
                caches_recomputed={"musicbrainz.releases": 1, "user_collections": 2},
            ),
            ItemOutcome("guarded", ("shared_native_id", "non_catalog_alias"), SPLIT_ID, DISCOGS_NATIVE_ID),
            ReattachConflictError("aliases held by another item"),
            MergeConflictError("kinds differ"),
        ]
        release_page.append(("3f2a8c4e-1111-4222-8333-444455556666", "4", "3f2a8c4e-1111-4222-8333-444455556666"))
        reattach_one = AsyncMock(side_effect=outcomes)
        monkeypatch.setattr(reattach_module, "_reattach_one", reattach_one)

        report = await run_reattachment(pool, apply=True, batch_size=4, decision_ref=DECISION_REF)

        assert report["apply"] is True
        assert report["outcomes"]["release"] == {
            "reattached": 1,
            "guarded": 1,
            "unchanged": 0,
            "failed": 2,
            "aliases_moved": 2,
            "identifier_collisions": 1,
            "guard_reasons": {"shared_native_id": 1, "non_catalog_alias": 1},
            "supersessions_opened": 1,
            "chains_compressed": 0,
            "merged_with_dependents": 1,
            "moved": {"artifacts": 0, "owned_copies": 2},
            "caches_recomputed": dict.fromkeys(CACHE_TABLE_NAMES, 0) | {"musicbrainz.releases": 1, "user_collections": 2},
        }
        for name in ("release_group", "artist", "label"):
            assert report["outcomes"][name]["reattached"] == 0
        # The cursor advances past the last key of the page, guarded or not.
        assert pool.calls[0].params == ("00000000-0000-0000-0000-000000000000", 4)
        assert pool.calls[1].params == ("3f2a8c4e-1111-4222-8333-444455556666", 4)
        assert [call.args[2] for call in reattach_one.await_args_list] == [row[0] for row in release_page]
        # Every item's supersession names the run's audit entry.
        assert {call.args[3] for call in reattach_one.await_args_list} == {DECISION_REF}

    @pytest.mark.asyncio
    async def test_apply_without_a_decision_ref_is_refused_before_anything_runs(self) -> None:
        pool = FakePool()

        with pytest.raises(ValueError, match="decision_ref"):
            await run_reattachment(pool, apply=True)

        assert pool.calls == []

    @pytest.mark.asyncio
    async def test_each_item_runs_in_its_own_transaction(self) -> None:
        from api.reattach import _reattach_one

        pool = FakePool([[]])

        outcome = await _reattach_one(pool, RELEASE, MBID, DECISION_REF)

        assert outcome.status == "unchanged"
        assert _statements(pool)[0] == "BEGIN"
        assert _statements(pool)[-1] == "COMMIT"


class TestTallyAndAudit:
    def test_tally_counts_statuses_and_reasons(self) -> None:
        tally = KindTally()
        tally.add(ItemOutcome("unchanged"))
        tally.add(ItemOutcome("guarded", ("shared_native_id",)))

        tally.add(ItemOutcome("reattached", chains_compressed=2, moved={"artifacts": 1, "owned_copies": 0}))
        tally.add(ItemOutcome("reattached", supersession_opened=True))

        assert tally.as_dict() == {
            "reattached": 2,
            "guarded": 1,
            "unchanged": 1,
            "failed": 0,
            "aliases_moved": 0,
            "identifier_collisions": 0,
            "guard_reasons": dict.fromkeys(GUARD_REASONS, 0) | {"shared_native_id": 1},
            "supersessions_opened": 1,
            "chains_compressed": 2,
            "merged_with_dependents": 1,
            "moved": {"artifacts": 1, "owned_copies": 0},
            "caches_recomputed": dict.fromkeys(CACHE_TABLE_NAMES, 0),
        }

    def test_the_tally_carries_no_ids(self) -> None:
        """What reaches admin_audit_log, which outlives erasure, is counts keyed by name only."""
        tally = KindTally()
        tally.add(ItemOutcome("reattached", (), SPLIT_ID, DISCOGS_NATIVE_ID, moved={"artifacts": 1, "owned_copies": 1}))

        rendered = repr(tally.as_dict())
        assert str(SPLIT_ID) not in rendered
        assert "UUID" not in rendered

    def test_audit_details_carry_job_id_and_outcomes(self) -> None:
        report = {"apply": True, "census": {}, "outcomes": {"release": {"reattached": 1}}}

        assert audit_details(report, "job-1") == {"job_id": "job-1", "apply": True, "outcomes": {"release": {"reattached": 1}}}

    def test_failure_details_name_the_error_class_only(self) -> None:
        assert failure_details("job-1", RuntimeError("password=secret")) == {"job_id": "job-1", "apply": True, "error": "RuntimeError"}

    def test_identifier_providers_are_the_alias_namespaces(self) -> None:
        assert IDENTIFIER_PROVIDERS == ("barcode", "catalog_number", "isrc", "matrix")


class TestCli:
    """The catalog-identity-reattach entry point: dry run by default, apply needs an admin."""

    @pytest.fixture
    def postgres_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("POSTGRES_HOST", "db:5433")
        monkeypatch.setenv("POSTGRES_USERNAME", "user")
        monkeypatch.setenv("POSTGRES_PASSWORD", "pass")
        monkeypatch.setenv("POSTGRES_DATABASE", "mydb")

    @pytest.mark.usefixtures("postgres_env")
    def test_connection_params_from_env(self) -> None:
        from api.reattach import _connection_params

        assert _connection_params() == {"host": "db", "port": 5433, "dbname": "mydb", "user": "user", "password": "pass"}

    def test_missing_env_exits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from api.reattach import _connection_params

        for var in ("POSTGRES_HOST", "POSTGRES_USERNAME", "POSTGRES_PASSWORD", "POSTGRES_DATABASE"):
            monkeypatch.delenv(var, raising=False)

        with pytest.raises(SystemExit) as exc_info:
            _connection_params()
        assert exc_info.value.code == 1

    @pytest.mark.parametrize("argv", [["--apply"], ["--batch-size", "0"]])
    def test_argument_errors(self, monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> None:
        import api.reattach as reattach_module

        monkeypatch.setattr(sys, "argv", ["catalog-identity-reattach", *argv])

        with pytest.raises(SystemExit) as exc_info:
            reattach_module.main()
        assert exc_info.value.code == 2

    @pytest.mark.usefixtures("postgres_env")
    @pytest.mark.parametrize("apply", [False, True])
    def test_main_prints_the_report(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], apply: bool) -> None:
        import api.reattach as reattach_module

        census_counts = {
            "split": 2,
            "stale_gm_item_id": 0,
            "eligible": 1,
            "guarded": 1,
            "guard_reasons": {},
            "dependents": {},
            "will_move": {"items": 1, "artifacts": 0, "owned_copies": 2},
            "unresolved": 5,
            "identifier_aliases": {},
        }
        report: dict[str, Any] = {"apply": apply, "census": {"release": census_counts}}
        if apply:
            report["outcomes"] = {"release": KindTally(reattached=1, guarded=1).as_dict()}
        seen: dict[str, Any] = {}

        def fake_run(coro: Any) -> dict[str, Any]:
            seen["frame"] = coro.cr_frame.f_locals
            coro.close()
            return report

        monkeypatch.setattr(reattach_module.asyncio, "run", fake_run)
        admin = str(uuid4())
        monkeypatch.setattr(sys, "argv", ["catalog-identity-reattach", *(["--apply", "--admin-id", admin] if apply else [])])

        reattach_module.main()

        out = capsys.readouterr().out
        assert "release: 2 split, 0 stale, 1 eligible, 1 guarded" in out
        assert "will move {'items': 1, 'artifacts': 0, 'owned_copies': 2}" in out
        assert seen["frame"]["apply"] is apply
        if apply:
            assert seen["frame"]["admin_id"] == admin
            assert "release: 1 re-attached, 1 guarded" in out
            assert "0 merged with dependents" in out
            assert "catalog-identity-projection" in out
        else:
            assert seen["frame"]["admin_id"] is None
            assert "Dry run only; nothing was written" in out

    @pytest.mark.usefixtures("postgres_env")
    def test_main_exits_when_the_run_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import api.reattach as reattach_module

        def fake_run(coro: Any) -> None:
            coro.close()

        monkeypatch.setattr(reattach_module.asyncio, "run", fake_run)
        monkeypatch.setattr(sys, "argv", ["catalog-identity-reattach", "--apply", "--admin-id", str(uuid4())])

        with pytest.raises(SystemExit) as exc_info:
            reattach_module.main()
        assert exc_info.value.code == 1


class TestRunOnce:
    """_run_once: the real pool wiring, the admin check, and the one audit entry."""

    @pytest.fixture
    def pool(self, monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
        monkeypatch.setenv("POSTGRES_HOST", "db")
        monkeypatch.setenv("POSTGRES_USERNAME", "user")
        monkeypatch.setenv("POSTGRES_PASSWORD", "pass")
        monkeypatch.setenv("POSTGRES_DATABASE", "mydb")
        pool = AsyncMock()
        monkeypatch.setattr("common.AsyncPostgreSQLPool", MagicMock(return_value=pool))
        return pool

    @pytest.mark.asyncio
    async def test_dry_run_is_not_audited(self, pool: AsyncMock, monkeypatch: pytest.MonkeyPatch) -> None:
        import api.reattach as reattach_module

        run = AsyncMock(return_value={"apply": False, "census": {}})
        audit = AsyncMock()
        monkeypatch.setattr(reattach_module, "run_reattachment", run)
        monkeypatch.setattr(reattach_module, "record_audit_entry", audit)

        report = await reattach_module._run_once(apply=False, admin_id=None, batch_size=10, job_id="job")

        assert report == {"apply": False, "census": {}}
        run.assert_awaited_once_with(pool, apply=False, batch_size=10, decision_ref=None)
        audit.assert_not_awaited()
        pool.initialize.assert_awaited_once()
        pool.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_apply_by_an_admin_is_audited_once(self, pool: AsyncMock, monkeypatch: pytest.MonkeyPatch) -> None:
        import api.reattach as reattach_module

        report = {"apply": True, "census": {}, "outcomes": {"release": {"reattached": 3}}}
        monkeypatch.setattr(reattach_module, "_is_admin", AsyncMock(return_value=True))
        monkeypatch.setattr(reattach_module, "run_reattachment", AsyncMock(return_value=report))
        audit = AsyncMock()
        monkeypatch.setattr(reattach_module, "record_audit_entry", audit)
        admin = str(uuid4())
        job_id = str(uuid4())

        assert await reattach_module._run_once(apply=True, admin_id=admin, batch_size=10, job_id=job_id) == report

        reattach_module.run_reattachment.assert_awaited_once_with(pool, apply=True, batch_size=10, decision_ref=UUID(job_id))
        audit.assert_awaited_once_with(
            pool=pool,
            admin_id=admin,
            action="identity.reattach.apply",
            target=job_id,
            details=audit_details(report, job_id),
            entry_id=job_id,
        )

    @pytest.mark.asyncio
    async def test_a_failed_apply_is_audited_under_its_decision_ref_and_reraised(self, pool: AsyncMock, monkeypatch: pytest.MonkeyPatch) -> None:
        import api.reattach as reattach_module

        monkeypatch.setattr(reattach_module, "_is_admin", AsyncMock(return_value=True))
        monkeypatch.setattr(reattach_module, "run_reattachment", AsyncMock(side_effect=RuntimeError("connection lost")))
        audit = AsyncMock()
        monkeypatch.setattr(reattach_module, "record_audit_entry", audit)
        admin = str(uuid4())
        job_id = str(uuid4())

        with pytest.raises(RuntimeError, match="connection lost"):
            await reattach_module._run_once(apply=True, admin_id=admin, batch_size=10, job_id=job_id)

        audit.assert_awaited_once_with(
            pool=pool,
            admin_id=admin,
            action="identity.reattach.failed",
            target=job_id,
            details=failure_details(job_id, RuntimeError()),
            entry_id=job_id,
        )
        pool.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_apply_by_a_non_admin_changes_nothing(self, pool: AsyncMock, monkeypatch: pytest.MonkeyPatch) -> None:
        import api.reattach as reattach_module

        run = AsyncMock()
        monkeypatch.setattr(reattach_module, "_is_admin", AsyncMock(return_value=False))
        monkeypatch.setattr(reattach_module, "run_reattachment", run)

        assert await reattach_module._run_once(apply=True, admin_id=str(uuid4()), batch_size=10, job_id="job") is None

        run.assert_not_awaited()
        pool.close.assert_awaited_once()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("row", "expected"), [((True,), True), ((False,), False), (None, False)])
    async def test_is_admin(self, row: tuple[bool] | None, expected: bool) -> None:
        from api.reattach import _is_admin

        pool = FakePool([[row]] if row else [])

        assert await _is_admin(pool, "00000000-0000-0000-0000-000000000099") is expected
        assert "is_admin" in pool.sql


class TestReattachRoute:
    """POST /api/admin/identity/reattach: admin only, 202 + job id, dry run by default, audited."""

    @patch("api.routers.admin.run_reattachment", new_callable=AsyncMock)
    def test_default_is_a_dry_run(self, mock_run: AsyncMock, test_client: TestClient) -> None:
        mock_run.return_value = {"apply": False, "census": {}}

        resp = test_client.post("/api/admin/identity/reattach", headers=_admin_auth_headers())

        assert resp.status_code == 202
        data = resp.json()
        assert data["status"] == "running"
        assert data["apply"] is False
        assert UUID(data["id"])

    @patch("api.routers.admin.run_reattachment", new_callable=AsyncMock)
    def test_apply_requires_the_explicit_flag(self, mock_run: AsyncMock, test_client: TestClient) -> None:
        mock_run.return_value = {"apply": True, "census": {}, "outcomes": {}}

        resp = test_client.post("/api/admin/identity/reattach", params={"apply": "true"}, headers=_admin_auth_headers())

        assert resp.status_code == 202
        assert resp.json()["apply"] is True

    @patch("api.routers.admin.run_reattachment", new_callable=AsyncMock)
    @patch("api.routers.admin.record_audit_entry", new_callable=AsyncMock)
    def test_trigger_is_audited(self, mock_audit: AsyncMock, mock_run: AsyncMock, test_client: TestClient) -> None:
        mock_run.return_value = {"apply": False, "census": {}}

        resp = test_client.post("/api/admin/identity/reattach", headers=_admin_auth_headers())

        job_id = resp.json()["id"]
        trigger_calls = [call for call in mock_audit.call_args_list if call.kwargs["action"] == "identity.reattach.trigger"]
        assert len(trigger_calls) == 1
        assert trigger_calls[0].kwargs["details"] == {"job_id": job_id, "apply": False}

    def test_no_token_is_rejected(self, test_client: TestClient) -> None:
        assert test_client.post("/api/admin/identity/reattach").status_code in (401, 403)

    def test_user_token_is_rejected(self, test_client: TestClient, auth_headers: dict[str, str]) -> None:
        assert test_client.post("/api/admin/identity/reattach", params={"apply": "true"}, headers=auth_headers).status_code in (401, 403)

    def test_not_ready(self, test_client: TestClient) -> None:
        import api.routers.admin as admin_mod

        original_pool = admin_mod._pool
        admin_mod._pool = None
        try:
            assert test_client.post("/api/admin/identity/reattach", headers=_admin_auth_headers()).status_code == 503
        finally:
            admin_mod._pool = original_pool


class TestRunReattachJob:
    """_run_reattach_job: the background task behind the route."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("apply", [False, True])
    @patch("api.routers.admin.record_audit_entry", new_callable=AsyncMock)
    @patch("api.routers.admin.run_reattachment", new_callable=AsyncMock)
    async def test_only_an_applying_run_writes_the_counts_entry(self, mock_run: AsyncMock, mock_audit: AsyncMock, apply: bool) -> None:
        import api.routers.admin as admin_mod

        report = {"apply": apply, "census": {}, **({"outcomes": {"release": {"reattached": 2}}} if apply else {})}
        mock_run.return_value = report
        original_pool = admin_mod._pool
        fake_pool = MagicMock()
        admin_mod._pool = fake_pool
        job_id = str(uuid4())
        admin_mod._reattach_tasks[job_id] = MagicMock()
        try:
            await admin_mod._run_reattach_job(job_id, "00000000-0000-0000-0000-000000000099", apply)
        finally:
            admin_mod._pool = original_pool

        mock_run.assert_awaited_once_with(fake_pool, apply=apply, decision_ref=UUID(job_id) if apply else None)
        if apply:
            mock_audit.assert_awaited_once_with(
                pool=fake_pool,
                admin_id="00000000-0000-0000-0000-000000000099",
                action="identity.reattach.apply",
                target=job_id,
                details={"job_id": job_id, "apply": True, "outcomes": {"release": {"reattached": 2}}},
                entry_id=job_id,
            )
        else:
            mock_audit.assert_not_awaited()
        assert job_id not in admin_mod._reattach_tasks

    @pytest.mark.asyncio
    @patch("api.routers.admin.record_audit_entry", new_callable=AsyncMock)
    @patch("api.routers.admin.run_reattachment", new_callable=AsyncMock)
    async def test_failure_is_swallowed_untracked_and_audited_under_its_decision_ref(self, mock_run: AsyncMock, mock_audit: AsyncMock) -> None:
        import api.routers.admin as admin_mod

        mock_run.side_effect = RuntimeError("postgres unreachable")
        original_pool = admin_mod._pool
        fake_pool = MagicMock()
        admin_mod._pool = fake_pool
        job_id = str(uuid4())
        admin_mod._reattach_tasks[job_id] = MagicMock()
        try:
            await admin_mod._run_reattach_job(job_id, "admin", True)  # must not raise
        finally:
            admin_mod._pool = original_pool

        assert job_id not in admin_mod._reattach_tasks
        mock_audit.assert_awaited_once_with(
            pool=fake_pool,
            admin_id="admin",
            action="identity.reattach.failed",
            target=job_id,
            details={"job_id": job_id, "apply": True, "error": "RuntimeError"},
            entry_id=job_id,
        )

    @pytest.mark.asyncio
    async def test_noop_when_pool_missing(self) -> None:
        import api.routers.admin as admin_mod

        original_pool = admin_mod._pool
        admin_mod._pool = None
        try:
            await admin_mod._run_reattach_job(str(uuid4()), "admin", False)  # must not raise
        finally:
            admin_mod._pool = original_pool
