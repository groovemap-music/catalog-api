"""The paged rarity walk: each page is its own unit of work and adds up to the streamed walk."""

from __future__ import annotations

from collections import Counter
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.queries import rarity_queries
from api.queries.rarity_queries import _Distribution, _percentile_rank, fetch_rarity_page, fetch_rarity_quality_counts, iter_rarity_signals


_IDS = [f"{i:02d}" for i in range(1, 8)]
_ARTIST_DEGREE = {"01": 5, "02": 5, "03": 9, "04": 0, "05": 2, "06": 7, "07": 2}
_LABEL_SIZE = {"01": 40, "02": 3000, "03": 40, "05": 7, "06": 7}
_GENRE_COUNT = {"01": 100, "02": 100, "03": 100, "04": 900, "05": 100, "06": 900}


async def _graph(_driver: Any, cypher: str, **params: Any) -> list[dict[str, Any]]:
    """Answer each statement the walk issues from the tables above."""
    if cypher == rarity_queries._RELEASE_ID_PAGE_QUERY:
        return [{"release_id": rid} for rid in _IDS if rid > params["cursor"]][: params["limit"]]
    if cypher == rarity_queries._RELEASE_COUNT_QUERY:
        return [{"total": len(_IDS)}]
    ids = params["ids"]
    if cypher == rarity_queries._RELEASE_QUERY:
        return [{"release_id": rid, "title": f"T{rid}", "artist_name": "A", "year": 1990} for rid in ids]
    if cypher == rarity_queries._ARTIST_DEGREE_QUERY:
        return [{"release_id": rid, "artist_max_degree": _ARTIST_DEGREE[rid]} for rid in ids]
    if cypher == rarity_queries._LABEL_SIZE_QUERY:
        return [{"release_id": rid, "label_max_catalog": _LABEL_SIZE[rid]} for rid in ids if rid in _LABEL_SIZE]
    if cypher == rarity_queries._GENRE_COUNT_QUERY:
        return [{"release_id": rid, "genre_max_release_count": _GENRE_COUNT[rid]} for rid in ids if rid in _GENRE_COUNT]
    return []


def _pool(rows: list[dict[str, Any]] | Exception) -> MagicMock:
    cursor = MagicMock()
    if isinstance(rows, Exception):
        cursor.execute = AsyncMock(side_effect=rows)
    else:
        cursor.execute = AsyncMock()
        cursor.fetchall = AsyncMock(return_value=rows)
    cursor.__aenter__ = AsyncMock(return_value=cursor)
    cursor.__aexit__ = AsyncMock(return_value=False)
    connection = MagicMock()
    connection.cursor = MagicMock(return_value=cursor)
    connection.__aenter__ = AsyncMock(return_value=connection)
    connection.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.connection = MagicMock(return_value=connection)
    pool.cursor = cursor
    return pool


async def _sum_quality_counts(page_size: int) -> tuple[dict[str, list[tuple[float, int]]], list[dict[str, Any]]]:
    """Walk the first pass as a client does, and return the summed counts and every page."""
    totals: dict[str, Counter[float]] = {"artist": Counter(), "label": Counter(), "genre": Counter()}
    pages = []
    after: str | None = ""
    while after is not None:
        page = await fetch_rarity_quality_counts(MagicMock(), after=after, limit=page_size)
        pages.append(page)
        for name, counter in totals.items():
            counter.update(dict(page[name]))
        after = page["next_after"]
    return {name: sorted(counter.items()) for name, counter in totals.items()}, pages


async def _walk_pages(page_size: int, pool: Any) -> list[dict[str, Any]]:
    """Walk both passes as a client does."""
    distributions, _pages = await _sum_quality_counts(page_size)
    rows: list[dict[str, Any]] = []
    after: str | None = ""
    while after is not None:
        page = await fetch_rarity_page(MagicMock(), pool, after=after, limit=page_size, distributions=distributions)
        rows.extend(page["items"])
        after = page["next_after"]
    return rows


@pytest.fixture(autouse=True)
def _in_memory_graph() -> Any:
    with patch("api.queries.rarity_queries.run_query", new=_graph):
        yield


@pytest.mark.parametrize("page_size", [1, 2, 7, 20])
@pytest.mark.asyncio
async def test_pages_return_the_rows_the_streamed_walk_returns(page_size: int) -> None:
    community = [{"release_id": 2, "have_count": 40, "want_count": 90}]

    paged = await _walk_pages(page_size, _pool(community))
    streamed = [row async for row in iter_rarity_signals(MagicMock(), _pool(community))]

    assert paged == streamed
    assert [row["release_id"] for row in paged] == _IDS
    assert len({row["hidden_gem_score"] for row in paged}) > 1


@pytest.mark.parametrize("page_size", [1, 3, 7])
@pytest.mark.asyncio
async def test_summed_counts_rank_exactly_as_one_sorted_list(page_size: int) -> None:
    distributions, _pages = await _sum_quality_counts(page_size)

    rebuilt = _Distribution.from_counts(distributions["artist"])
    positive = sorted(value for value in _ARTIST_DEGREE.values() if value)

    assert [rebuilt.percentile_rank(value) for value in range(11)] == [_percentile_rank(value, positive) for value in range(11)]


@pytest.mark.asyncio
async def test_the_last_short_page_ends_the_walk() -> None:
    _distributions, pages = await _sum_quality_counts(3)

    assert [page["releases"] for page in pages] == [3, 3, 1]
    assert [page["next_after"] for page in pages] == ["03", "06", None]
    assert {page["release_total"] for page in pages} == {len(_IDS)}


@pytest.mark.asyncio
async def test_an_exact_multiple_ends_on_an_empty_page() -> None:
    """Nothing tells a full last page from a full middle one, so the walk asks once more."""
    _distributions, pages = await _sum_quality_counts(7)

    assert [page["releases"] for page in pages] == [7, 0]
    assert pages[-1]["next_after"] is None
    assert await fetch_rarity_page(MagicMock(), _pool([]), after="07", limit=7, distributions={}) == {"items": [], "next_after": None}


@pytest.mark.asyncio
async def test_a_page_reads_only_its_own_community_counts() -> None:
    pool = _pool([])

    await fetch_rarity_page(MagicMock(), pool, after="", limit=2, distributions={"artist": [], "label": [], "genre": []})

    assert pool.cursor.execute.await_args.args[1] == ([1, 2],)


@pytest.mark.asyncio
async def test_a_community_count_failure_fails_the_page_instead_of_scoring_it_neutral() -> None:
    """The streamed walk falls back to a neutral score; a retried page must not store that."""
    with pytest.raises(RuntimeError, match="gone"):
        await fetch_rarity_page(MagicMock(), _pool(RuntimeError("gone")), after="", limit=2, distributions={"artist": [], "label": [], "genre": []})
