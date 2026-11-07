"""Unit tests for the by-label / by-artist membership statement and its completeness guard.

The real-database behaviour is in ``test_rarity_membership_integration.py``; these pin the branching
(fast path, each fallback, cache) with a scripted pool so the unit lane covers every line.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from api.queries import rarity_queries as rq


class _Cursor:
    def __init__(self, pool: _Pool) -> None:
        self.pool = pool
        self.last: str = ""

    async def __aenter__(self) -> _Cursor:
        return self

    async def __aexit__(self, *_: object) -> bool:
        return False

    async def execute(self, query: str, params: Any = None) -> None:
        self.last = query
        self.pool.executed.append((query, params))

    async def fetchone(self) -> Any:
        return self.pool.answer(self.last, one=True)

    async def fetchall(self) -> Any:
        return self.pool.answer(self.last, one=False)


class _Conn:
    def __init__(self, pool: _Pool) -> None:
        self.pool = pool

    async def __aenter__(self) -> _Conn:
        return self

    async def __aexit__(self, *_: object) -> bool:
        return False

    def cursor(self, **_: Any) -> _Cursor:
        return _Cursor(self.pool)


class _Pool:
    def __init__(self, answer: Callable[[str, bool], Any]) -> None:
        self._answer = answer
        self.executed: list[tuple[str, Any]] = []

    def answer(self, query: str, *, one: bool) -> Any:
        return self._answer(query, one)

    def connection(self) -> _Conn:
        return _Conn(self)


@pytest.fixture(autouse=True)
def _fresh_cache() -> Iterator[None]:
    rq._membership_checked.update(run_id=None, complete={})
    yield
    rq._membership_checked.update(run_id=None, complete={})


def _pool(*, run_id: int | None = 5, label_ok: bool = True, artist_ok: bool = True, page_rows: list[dict[str, Any]] | None = None) -> _Pool:
    def answer(query: str, one: bool) -> Any:
        if "computation_log" in query:
            return {"run_id": run_id}
        if "NOT EXISTS" in query:
            return {"label_ids": label_ok, "artist_ids": artist_ok}
        if "EXISTS (SELECT 1 FROM insights.release_rarity)" in query:
            return {"present": True}
        if "LATERAL" in query:
            return page_rows if page_rows is not None else []
        if "count(*)" in query:
            return {"total": 0}
        return [] if not one else None

    return _Pool(answer)


@pytest.mark.asyncio
async def test_has_rows_reads_the_probe() -> None:
    assert await rq._release_rarity_has_rows(_pool()) is True


@pytest.mark.asyncio
async def test_completeness_is_checked_once_per_run_and_remembered() -> None:
    pool = _pool(label_ok=True, artist_ok=False)
    assert await rq._membership_columns_complete(pool, "label_ids") is True
    assert await rq._membership_columns_complete(pool, "artist_ids") is False
    scans = [q for q, _ in pool.executed if "NOT EXISTS" in q]
    assert len(scans) == 1  # the second call used the remembered answer

    # No timer: the same completed run stays remembered on a later call, with no time having passed.
    await rq._membership_columns_complete(pool, "label_ids")
    assert len([q for q, _ in pool.executed if "NOT EXISTS" in q]) == 1


class _Stop(Exception):
    pass


@pytest.mark.asyncio
async def test_background_refresh_scans_once_per_completed_run() -> None:
    pool = _pool(run_id=5)
    ticks = 0

    async def two_ticks(_seconds: float) -> None:
        nonlocal ticks
        ticks += 1
        if ticks == 2:
            raise _Stop

    with patch("api.queries.rarity_queries.asyncio.sleep", side_effect=two_ticks), pytest.raises(_Stop):
        await rq.run_rarity_membership_refresh(pool, 60.0)
    assert ticks == 2
    assert len([q for q, _ in pool.executed if "NOT EXISTS" in q]) == 1  # the second tick saw the same run


@pytest.mark.asyncio
async def test_a_new_completed_run_forces_a_new_scan() -> None:
    first = _pool(run_id=5)
    await rq._membership_columns_complete(first, "label_ids")
    second = _pool(run_id=6, label_ok=False)
    assert await rq._membership_columns_complete(second, "label_ids") is False


@pytest.mark.asyncio
async def test_no_completed_run_cannot_be_trusted() -> None:
    pool = _pool(run_id=None)
    assert await rq._membership_columns_complete(pool, "label_ids") is False
    assert not [q for q, _ in pool.executed if "NOT EXISTS" in q]


@pytest.mark.asyncio
async def test_page_drops_the_null_row_and_keeps_the_total() -> None:
    empty = _pool(page_rows=[{"release_id": None, "title": None, "total": 42}])
    assert await rq._rarity_page_by_membership(empty, "label_ids", 1, 9, 20) == ([], 42)
    assert "label_ids @>" in empty.executed[0][0]
    assert empty.executed[0][1] == {"key": 1, "limit": 20, "offset": 160}

    none_at_all = _pool(page_rows=[])
    assert await rq._rarity_page_by_membership(none_at_all, "artist_ids", 1, 1, 20) == ([], 0)

    rows = [{"release_id": 3, "title": "t", "total": 2}, {"release_id": 4, "title": "u", "total": 2}]
    items, total = await rq._rarity_page_by_membership(_pool(page_rows=rows), "artist_ids", 1, 1, 20)
    assert total == 2
    assert items == [{"release_id": 3, "title": "t"}, {"release_id": 4, "title": "u"}]


@pytest.mark.parametrize(
    ("value", "expected"),
    [("1818", 1818), ("0", 0), (str(2**63 - 1), 2**63 - 1), (str(2**63), None), ("x1", None), ("", None), ("١٢", None), ("-1", None)],
)
def test_graph_id_must_be_a_plain_bigint(value: str, expected: int | None) -> None:
    assert rq._numeric_graph_id(value) == expected


@pytest.mark.asyncio
async def test_unknown_node_is_not_found_without_touching_postgres() -> None:
    pool = _pool()
    with patch.object(rq, "run_query", new=AsyncMock(return_value=[])):
        assert await rq.get_rarity_by_label(object(), pool, "1", 1, 20) is None
        assert await rq.get_rarity_by_artist(object(), pool, "1", 1, 20) is None
    assert pool.executed == []


@pytest.mark.asyncio
async def test_empty_table_answers_empty_without_the_graph_list() -> None:
    run = AsyncMock(return_value=[{"id": "1", "name": "n"}])
    with patch.object(rq, "run_query", new=run), patch.object(rq, "_release_rarity_has_rows", new=AsyncMock(return_value=False)):
        assert await rq.get_rarity_by_label(object(), _pool(), "1", 1, 20) == ([], 0)
    assert run.await_count == 1


@pytest.mark.asyncio
async def test_complete_columns_use_the_statement_and_one_graph_lookup() -> None:
    run = AsyncMock(return_value=[{"id": "1", "name": "n"}])
    rows = [{"release_id": 8, "title": "t", "total": 1}]
    with patch.object(rq, "run_query", new=run):
        assert await rq.get_rarity_by_label(object(), _pool(page_rows=rows), "1818", 1, 20) == ([{"release_id": 8, "title": "t"}], 1)
        assert await rq.get_rarity_by_artist(object(), _pool(page_rows=rows), "55", 1, 20) == ([{"release_id": 8, "title": "t"}], 1)
    assert run.await_count == 2


@pytest.mark.parametrize("node_id", ["1", "not-a-number"])
@pytest.mark.asyncio
async def test_gap_or_unholdable_id_falls_back_to_the_graph_list(node_id: str) -> None:
    calls: list[str] = []

    async def fake_run(_driver: Any, query: str, **_: Any) -> list[dict[str, Any]]:
        calls.append(query)
        return [{"id": node_id, "name": "n"}] if "RETURN n.id AS id" in query else [{"release_id": "10"}, {"release_id": "x"}]

    def answer(query: str, one: bool) -> Any:
        if "computation_log" in query:
            return {"run_id": 1}
        if "NOT EXISTS" in query:
            return {"label_ids": False, "artist_ids": False}
        if "EXISTS (SELECT 1 FROM insights.release_rarity)" in query:
            return {"present": True}
        return {"total": 1} if one else [{"release_id": 10}]

    with patch.object(rq, "run_query", new=fake_run):
        assert await rq.get_rarity_by_label(object(), _Pool(answer), node_id, 1, 20) == ([{"release_id": 10}], 1)
    assert len(calls) == 2  # existence, then the release id list

    async def only_nonnumeric(_driver: Any, query: str, **_: Any) -> list[dict[str, Any]]:
        return [{"id": node_id}] if "RETURN n.id AS id" in query else [{"release_id": "x"}]

    with patch.object(rq, "run_query", new=only_nonnumeric):
        assert await rq.get_rarity_by_artist(object(), _Pool(answer), node_id, 1, 20) == ([], 0)

    async def no_members(_driver: Any, query: str, **_: Any) -> list[dict[str, Any]]:
        return [{"id": node_id}] if "RETURN n.id AS id" in query else []

    with patch.object(rq, "run_query", new=no_members):
        assert await rq.get_rarity_by_label(object(), _Pool(answer), node_id, 1, 20) == ([], 0)
