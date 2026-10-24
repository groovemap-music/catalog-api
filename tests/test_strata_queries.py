"""The strata reads (api/queries/strata_queries.py) against mocked Neo4j and PostgreSQL."""

from __future__ import annotations

import re
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.queries import strata_queries
from api.queries.strata_queries import (
    STRATA_READ_TIMEOUT_SECONDS,
    get_musicbrainz_only_releases,
    get_shared_personnel,
    get_strata_catalog,
    get_strata_labels,
    get_strata_ladder,
    get_strata_marks,
    get_strata_root,
)
from api.strata import Mark, Release


pytestmark = pytest.mark.asyncio

_CYPHER = {name: getattr(strata_queries, name) for name in ("_ROOT", "_LADDER", "_MARKS", "_LABELS", "_SHARED_PERSONNEL", "_SIBLING_MASTERS")}


class TestQueryText:
    """Guards on what the reads may and may not depend on."""

    @pytest.mark.parametrize("name", sorted(_CYPHER))
    async def test_no_strata_read_filters_the_master_sentinel(self, name: str) -> None:
        # The Discogs "0" master was removed at its source on 2026-10-23. A read that
        # filtered it would hide a regression, so none may name it.
        cypher = _CYPHER[name]
        assert not re.search(r"""['"]0['"]""", cypher)
        assert not re.search(r"<>\s*0\b|!=\s*0\b|NOT\s+\w+\.id\s*=", cypher)

    async def test_the_ladder_reads_masters_only_through_derived_from(self) -> None:
        assert "OPTIONAL MATCH (r)-[:DERIVED_FROM]->(m:Master)" in strata_queries._LADDER
        assert "WHERE" not in strata_queries._LADDER

    async def test_personnel_edges_key_on_category_never_on_the_raw_role(self) -> None:
        assert "credit.category" in strata_queries._SHARED_PERSONNEL
        assert ".role" not in strata_queries._SHARED_PERSONNEL

    async def test_the_root_counts_its_releases_from_the_degree_alone(self) -> None:
        # No label or property on the far side, so the planner reads the node's degree
        # instead of expanding a 1,356,055-release root before the size check.
        assert "COUNT { (a)<-[:BY]-() } AS releases" in strata_queries._ROOT

    async def test_marks_read_the_callers_collected_and_wants_edges(self) -> None:
        assert "(:User {id: $user_id})-[edge:COLLECTED|WANTS]->" in strata_queries._MARKS
        assert "date_added" in strata_queries._MARKS


async def test_root_reads_the_artist_by_id() -> None:
    driver = MagicMock()
    root = {"id": "1", "name": "Root", "releases": 3}
    with patch.object(strata_queries, "run_single", new=AsyncMock(return_value=root)) as run:
        assert await get_strata_root(driver, "1") == root
    run.assert_awaited_once_with(driver, strata_queries._ROOT, timeout=STRATA_READ_TIMEOUT_SECONDS, artist_id="1")


async def test_ladder_rows_become_releases_and_master_years() -> None:
    rows = [
        {"id": 10, "year": 1971, "master_id": 500, "master_year": 1970, "credit": ["12", "1", "3"]},
        {"id": "11", "year": None, "master_id": None, "master_year": None, "credit": ["1"]},
    ]
    with patch.object(strata_queries, "run_query", new=AsyncMock(return_value=rows)):
        releases, master_years = await get_strata_ladder(MagicMock(), "1")
    assert releases == [Release("10", "500", 1971, ("1", "3", "12")), Release("11", None, None, ("1",))]
    assert master_years == {"500": 1970}


async def test_marks_fold_both_edge_kinds_and_their_recorded_times() -> None:
    class Temporal:
        def iso_format(self) -> str:
            return "2019-05-01T12:00:00-07:00"

    rows = [
        {"id": "10", "kind": "COLLECTED", "recorded_at": Temporal()},
        {"id": "10", "kind": "WANTS", "recorded_at": "2018-01-01T00:00:00-08:00"},
        {"id": 12, "kind": "WANTS", "recorded_at": None},
    ]
    with patch.object(strata_queries, "run_query", new=AsyncMock(return_value=rows)) as run:
        marks = await get_strata_marks(MagicMock(), "user-1", "1")
    assert marks == {
        "10": Mark(owned=True, wanted=True, owned_at="2019-05-01T12:00:00-07:00", wanted_at="2018-01-01T00:00:00-08:00"),
        "12": Mark(wanted=True),
    }
    assert run.await_args.kwargs == {"timeout": STRATA_READ_TIMEOUT_SECONDS, "user_id": "user-1", "artist_id": "1"}


class TestOwnedCopies:
    async def test_the_owned_releases_carry_their_copy_ids_in_acquired_order(self, mock_pool: MagicMock, mock_cur: MagicMock) -> None:
        rows = [
            {"id": "10", "kind": "COLLECTED", "recorded_at": "2019-05-01T12:00:00-07:00"},
            {"id": "10", "kind": "WANTS", "recorded_at": None},
            {"id": "11", "kind": "WANTS", "recorded_at": None},
            {"id": "12", "kind": "COLLECTED", "recorded_at": None},
        ]
        mock_cur.fetchall.side_effect = [[{"release_id": 10, "owned_copy_id": "copy-a"}, {"release_id": 10, "owned_copy_id": "copy-b"}]]
        with patch.object(strata_queries, "run_query", new=AsyncMock(return_value=rows)):
            marks = await get_strata_marks(MagicMock(), "user-1", "1", mock_pool)
        assert marks["10"].owned_copy_ids == ("copy-a", "copy-b")
        assert marks["11"] == Mark(wanted=True)
        # Owned, but the sync has not minted a copy for it.
        assert marks["12"] == Mark(owned=True)
        # One read, for the caller's owned releases only: a want-only release is not asked about.
        assert mock_cur.execute.await_args_list[0].args == (strata_queries._STATEMENT_TIMEOUT, {"timeout": "10000ms"})
        sql, params = mock_cur.execute.await_args_list[1].args
        assert "FROM user_collections" in sql
        assert params == {"user_id": "user-1", "release_ids": [10, 12]}

    async def test_the_copy_read_is_scoped_to_the_caller(self) -> None:
        sql = strata_queries._OWNED_COPIES
        assert "user_id = %(user_id)s::uuid" in sql
        assert "owned_copy_id IS NOT NULL" in sql
        assert "date_added NULLS LAST" in sql

    async def test_without_postgres_or_owned_releases_nothing_is_read(self, mock_pool: MagicMock, mock_cur: MagicMock) -> None:
        wants = [{"id": "11", "kind": "WANTS", "recorded_at": None}]
        with patch.object(strata_queries, "run_query", new=AsyncMock(return_value=wants)):
            assert await get_strata_marks(MagicMock(), "user-1", "1", mock_pool) == {"11": Mark(wanted=True)}
        mock_cur.execute.assert_not_awaited()
        owned = [{"id": "10", "kind": "COLLECTED", "recorded_at": None}]
        with patch.object(strata_queries, "run_query", new=AsyncMock(return_value=owned)):
            assert await get_strata_marks(MagicMock(), "user-1", "1") == {"10": Mark(owned=True)}
        mock_cur.execute.assert_not_awaited()

    async def test_a_failed_copy_read_degrades_to_no_copy_ids(self, mock_pool: MagicMock, mock_cur: MagicMock) -> None:
        mock_cur.execute.side_effect = RuntimeError("connection reset")
        owned = [{"id": "10", "kind": "COLLECTED", "recorded_at": None}]
        with patch.object(strata_queries, "run_query", new=AsyncMock(return_value=owned)):
            assert await get_strata_marks(MagicMock(), "user-1", "1", mock_pool) == {"10": Mark(owned=True)}


async def test_labels_are_keyed_by_id_and_tolerate_no_row() -> None:
    row = {"releases": [[10, "First"]], "masters": [["500", "Master"]], "artists": [["1", "Root"]]}
    with patch.object(strata_queries, "run_single", new=AsyncMock(return_value=row)):
        assert await get_strata_labels(MagicMock(), ["10"], ["500"], ["1"]) == ({"10": "First"}, {"500": "Master"}, {"1": "Root"})
    with patch.object(strata_queries, "run_single", new=AsyncMock(return_value=None)):
        assert await get_strata_labels(MagicMock(), [], [], []) == ({}, {}, {})


async def test_shared_personnel_skips_the_read_below_two_masters() -> None:
    edges = [{"source": "500", "target": "501", "categories": ["Production"], "people": 2}]
    with patch.object(strata_queries, "run_query", new=AsyncMock(return_value=edges)) as run:
        assert await get_shared_personnel(MagicMock(), ["500"], limit=8) == []
        run.assert_not_awaited()
        assert await get_shared_personnel(MagicMock(), ["500", "501"], limit=8) == edges
    assert run.await_args.kwargs == {"timeout": STRATA_READ_TIMEOUT_SECONDS, "master_ids": ["500", "501"], "limit": 8}


class TestCatalog:
    async def test_without_postgres_the_catalog_is_the_graph_alone(self) -> None:
        ladder = ([Release("10", "500", 1971, ("1",))], {"500": 1971})
        with (
            patch.object(strata_queries, "get_strata_ladder", new=AsyncMock(return_value=ladder)),
            patch.object(strata_queries, "get_musicbrainz_only_releases", new=AsyncMock()) as mb,
        ):
            catalog = await get_strata_catalog(MagicMock(), None, "1", "Root")
        mb.assert_not_awaited()
        assert (catalog.root_id, catalog.root_name, catalog.releases, dict(catalog.master_years)) == ("1", "Root", tuple(ladder[0]), {"500": 1971})

    async def test_a_root_with_no_masters_skips_the_musicbrainz_read(self, mock_pool: MagicMock) -> None:
        ladder = ([Release("10", None, 1971, ("1",))], {})
        with (
            patch.object(strata_queries, "get_strata_ladder", new=AsyncMock(return_value=ladder)),
            patch.object(strata_queries, "get_musicbrainz_only_releases", new=AsyncMock()) as mb,
        ):
            catalog = await get_strata_catalog(MagicMock(), mock_pool, "1", "Root")
        mb.assert_not_awaited()
        mock_pool.connection.assert_not_called()
        assert catalog.releases == tuple(ladder[0])

    async def test_musicbrainz_only_releases_join_the_catalog(self, mock_pool: MagicMock) -> None:
        ladder = ([Release("10", "500", 1971, ("1",))], {"500": 1971})
        joined = [Release("mbid", "500", 1972, (), "musicbrainz", "Reissue")]
        with (
            patch.object(strata_queries, "get_strata_ladder", new=AsyncMock(return_value=ladder)),
            patch.object(strata_queries, "get_musicbrainz_only_releases", new=AsyncMock(return_value=joined)) as mb,
        ):
            catalog = await get_strata_catalog(MagicMock(), mock_pool, "1", None)
        assert catalog.releases == (*ladder[0], *joined)
        assert mb.await_args.args[3] == {"500"}


def _candidate(mbid: str, group: str, master: int | None, events: Any = None, title: str = "T") -> dict[str, Any]:
    return {"mbid": mbid, "title": title, "group_mbid": group, "group_master_id": master, "release_events": events}


class TestMusicBrainzOnly:
    async def test_native_id_first_then_one_master_siblings(self, mock_pool: MagicMock, mock_cur: MagicMock) -> None:
        candidates = [
            # Native id: the group names one of the root's masters, whatever its siblings say.
            _candidate("a", "g-native", 500, [{"date": "1999-03-01"}, {"date": "1972"}, {"date": None}, "junk", {"date": "????"}]),
            # Native id naming a master outside this discography: not on this ladder.
            _candidate("b", "g-elsewhere", 999),
            # Release group: linked siblings derive from exactly one master (one known, one read).
            _candidate("c", "g-siblings", None, []),
            # Release group: siblings derive from two masters, so ambiguous.
            _candidate("d", "g-ambiguous", None),
            # Release group: linked siblings have no master.
            _candidate("e", "g-masterless", None),
            # Release group with no linked sibling at all.
            _candidate("f", "g-alone", None),
        ]
        siblings = [
            {"group_mbid": "g-siblings", "release_ids": [10, 77]},
            {"group_mbid": "g-ambiguous", "release_ids": [10, 11]},
            {"group_mbid": "g-masterless", "release_ids": [12, 88]},
        ]
        mock_cur.fetchall.side_effect = [candidates, siblings]
        releases = [Release("10", "500", 1971, ("1",)), Release("11", "501", 1980, ("1",)), Release("12", None, 1990, ("1",))]
        sibling_rows = [{"id": "77", "masters": ["500"]}, {"id": 88, "masters": []}]
        with patch.object(strata_queries, "run_query", new=AsyncMock(return_value=sibling_rows)) as run:
            attached = await get_musicbrainz_only_releases(MagicMock(), mock_pool, releases, {"500", "501"})

        assert attached == [
            Release("a", "500", 1972, (), "musicbrainz", "T"),
            Release("c", "500", None, (), "musicbrainz", "T"),
        ]
        bound, first, second = mock_cur.execute.await_args_list
        assert bound.args == (strata_queries._STATEMENT_TIMEOUT, {"timeout": "10000ms"})
        assert sorted(first.args[1]["master_ids"]) == [500, 501]
        assert first.args[1]["release_ids"] == [10, 11, 12]
        assert second.args[1] == {"group_mbids": ["g-alone", "g-ambiguous", "g-masterless", "g-siblings"]}
        # Only siblings outside the root's own releases are read back from the graph.
        assert run.await_args.kwargs == {"timeout": STRATA_READ_TIMEOUT_SECONDS, "release_ids": ["77", "88"]}

    async def test_no_unlinked_group_reads_no_siblings(self, mock_pool: MagicMock, mock_cur: MagicMock) -> None:
        mock_cur.fetchall.side_effect = [[_candidate("a", "g", 500, None)]]
        with patch.object(strata_queries, "run_query", new=AsyncMock()) as run:
            attached = await get_musicbrainz_only_releases(MagicMock(), mock_pool, [Release("10", "500", 1971, ("1",))], {"500"})
        assert attached == [Release("a", "500", None, (), "musicbrainz", "T")]
        assert mock_cur.execute.await_count == 2
        run.assert_not_awaited()


async def test_every_graph_read_is_bounded_by_the_strata_timeout() -> None:
    reads = {
        "ladder": lambda driver: get_strata_ladder(driver, "1"),
        "marks": lambda driver: get_strata_marks(driver, "user-1", "1"),
        "personnel": lambda driver: get_shared_personnel(driver, ["500", "501"], limit=8),
    }
    for name, read in reads.items():
        with patch.object(strata_queries, "run_query", new=AsyncMock(return_value=[])) as run:
            await read(MagicMock())
        assert run.await_args.kwargs["timeout"] == STRATA_READ_TIMEOUT_SECONDS, name
    with patch.object(strata_queries, "run_single", new=AsyncMock(return_value=None)) as run:
        await get_strata_labels(MagicMock(), ["10"], ["500"], ["1"])
    assert run.await_args.kwargs["timeout"] == STRATA_READ_TIMEOUT_SECONDS


async def test_the_musicbrainz_read_bounds_its_own_transaction(mock_pool: MagicMock, mock_conn: MagicMock, mock_cur: MagicMock) -> None:
    mock_cur.fetchall.side_effect = [[]]
    await get_musicbrainz_only_releases(MagicMock(), mock_pool, [Release("10", "500", 1971, ("1",))], {"500"})
    # set_config(..., true) is transaction-local, so the bound never leaks onto the pooled connection.
    mock_conn.transaction.assert_called_once()
    assert mock_cur.execute.await_args_list[0].args == (strata_queries._STATEMENT_TIMEOUT, {"timeout": "10000ms"})
    assert strata_queries._STATEMENT_TIMEOUT.endswith("true)")
