"""The CrateFit reads: the collection fold, the master hop, and the rarity lookup.

Offline throughout. The driver is a hand-built mock whose ``session().run()`` returns a
scripted result, which is the pattern the other query-module tests use, and the pool is
the shared conftest mock. What each test defends is the *shape* the scoring depends on:
one round trip per read, the ``DERIVED_FROM`` hop in both directions, and a cache key the
collection sync's invalidation already sweeps.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from api.queries.fit_queries import (
    COLLECTION_CACHE_TTL,
    collection_cache_key,
    empty_collection,
    fold_collection,
    get_collection_ids,
    get_release_context,
    get_release_rarity,
    held_title_key,
)


class _AsyncIter:
    def __init__(self, records: list[dict[str, Any]]) -> None:
        self._records = records
        self._index = 0

    def __aiter__(self) -> _AsyncIter:
        return self

    async def __anext__(self) -> dict[str, Any]:
        if self._index >= len(self._records):
            raise StopAsyncIteration
        record = self._records[self._index]
        self._index += 1
        return record


class _MockResult:
    def __init__(self, records: list[dict[str, Any]] | None = None, single: dict[str, Any] | None = None) -> None:
        self._records = records or []
        self._single = single

    def __aiter__(self) -> _AsyncIter:
        return _AsyncIter(self._records)

    async def single(self) -> dict[str, Any] | None:
        return self._single

    async def consume(self) -> MagicMock:
        return MagicMock()


def _driver(records: list[dict[str, Any]] | None = None, single: dict[str, Any] | None = None) -> MagicMock:
    """A driver whose every ``run`` returns one scripted result."""
    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)

    async def _run(*_args: Any, **_kwargs: Any) -> _MockResult:
        return _MockResult(records=records, single=single)

    session.run = AsyncMock(side_effect=_run)
    driver = MagicMock()
    driver.session = MagicMock(return_value=session)
    return driver


def _cypher_of(driver: MagicMock) -> str:
    """The Cypher text of the one query the driver was asked to run."""
    session = driver.session.return_value
    return str(session.run.await_args_list[0].args[0])


def _collection_rows() -> list[dict[str, Any]]:
    """Two jazz records on one label and one techno record that shares nothing with them."""
    return [
        {
            "release_id": "1",
            "title": "Blue Train",
            "artist_ids": ["a-coltrane"],
            "label_ids": ["l-blue-note"],
            "genres": ["Jazz"],
            "styles": ["Hard Bop"],
            "master_ids": ["m-blue-train"],
        },
        {
            "release_id": "2",
            "title": "Moanin'",
            "artist_ids": ["a-blakey"],
            "label_ids": ["l-blue-note"],
            "genres": ["Jazz"],
            "styles": ["Hard Bop"],
            "master_ids": [],
        },
        {
            "release_id": "3",
            "title": "Selected Ambient Works",
            "artist_ids": ["a-aphex"],
            "label_ids": ["l-r-and-s"],
            "genres": ["Electronic"],
            "styles": ["Ambient", "Techno"],
            "master_ids": ["m-saw"],
        },
    ]


# ──────────────────────────────────────────────────────────────────────────────
# fold_collection
# ──────────────────────────────────────────────────────────────────────────────


def test_fold_collects_every_id_set() -> None:
    """Each dimension folds to its own sorted, de-duplicated id set."""
    folded = fold_collection(_collection_rows())

    assert folded["release_ids"] == ["1", "2", "3"]
    assert folded["master_ids"] == ["m-blue-train", "m-saw"]
    assert folded["artist_ids"] == ["a-aphex", "a-blakey", "a-coltrane"]
    assert folded["label_ids"] == ["l-blue-note", "l-r-and-s"]
    assert folded["genres"] == ["Electronic", "Jazz"]
    assert folded["styles"] == ["Ambient", "Hard Bop", "Techno"]


def test_fold_counts_releases_per_facet() -> None:
    """A count is how many held releases carry the facet, not how many edges do."""
    folded = fold_collection(_collection_rows())

    assert folded["label_counts"] == {"l-blue-note": 2, "l-r-and-s": 1}
    assert folded["artist_counts"] == {"a-aphex": 1, "a-blakey": 1, "a-coltrane": 1}
    assert folded["genre_counts"] == {"Electronic": 1, "Jazz": 2}
    assert folded["style_counts"] == {"Ambient": 1, "Hard Bop": 2, "Techno": 1}


def test_fold_counts_a_repeated_artist_on_one_release_once() -> None:
    """A release that names the same artist twice still counts as one held release."""
    folded = fold_collection(
        [{"release_id": "9", "title": "Duets", "artist_ids": ["a-1", "a-1"], "label_ids": [], "genres": [], "styles": [], "master_ids": []}]
    )

    assert folded["artist_counts"] == {"a-1": 1}


def test_fold_indexes_the_neighbourhood_of_each_genre() -> None:
    """Per-genre artist, label, and style sets are what the bridge heuristic reads."""
    folded = fold_collection(_collection_rows())

    assert folded["genre_artists"]["Jazz"] == ["a-blakey", "a-coltrane"]
    assert folded["genre_labels"]["Jazz"] == ["l-blue-note"]
    assert folded["genre_styles"]["Electronic"] == ["Ambient", "Techno"]


def test_fold_indexes_held_titles_per_artist() -> None:
    """The artist-and-title index is case-folded, because Discogs titles are typed by hand."""
    folded = fold_collection(_collection_rows())

    assert folded["held_titles"][held_title_key("a-coltrane", "BLUE TRAIN")] == "Blue Train"


def test_fold_skips_rows_without_a_release_id() -> None:
    """A row the graph could not key is dropped rather than folded under an empty id."""
    folded = fold_collection([{"release_id": None, "title": "orphan", "artist_ids": ["a-1"]}])

    assert folded == empty_collection()


def test_fold_tolerates_null_facets() -> None:
    """A release with no label, genre, or title folds without raising."""
    folded = fold_collection(
        [{"release_id": "7", "title": None, "artist_ids": None, "label_ids": [None], "genres": [], "styles": [], "master_ids": []}]
    )

    assert folded["release_ids"] == ["7"]
    assert folded["label_ids"] == []
    assert folded["held_titles"] == {}


def test_fold_is_deterministic() -> None:
    """The same rows in a different order fold to the same bytes."""
    rows = _collection_rows()

    assert fold_collection(rows) == fold_collection(list(reversed(rows)))


# ──────────────────────────────────────────────────────────────────────────────
# get_collection_ids
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_collection_ids_run_one_query_over_the_collected_edge() -> None:
    """One round trip, off ``(:User)-[:COLLECTED]->(:Release)`` with optional facets."""
    driver = _driver(records=_collection_rows())

    folded = await get_collection_ids(driver, "user-1")

    session = driver.session.return_value
    assert session.run.await_count == 1
    cypher = _cypher_of(driver)
    assert "(u:User {id: $user_id})-[:COLLECTED]->(r:Release)" in cypher
    for pattern in (
        "(r)-[:BY]->(artist:Artist)",
        "(r)-[:ON]->(label:Label)",
        "(r)-[:IS]->(genre:Genre)",
        "(r)-[:IS]->(style:Style)",
        "(r)-[:DERIVED_FROM]->(master:Master)",
    ):
        assert f"OPTIONAL MATCH {pattern}" in cypher
    assert folded["label_counts"] == {"l-blue-note": 2, "l-r-and-s": 1}


@pytest.mark.asyncio
async def test_collection_ids_of_an_empty_collection_is_the_empty_shape() -> None:
    """A collector who holds nothing is a real caller, not a missing value."""
    assert await get_collection_ids(_driver(records=[]), "user-1") == empty_collection()


@pytest.mark.asyncio
async def test_collection_ids_are_cached_under_a_key_the_sync_sweeps() -> None:
    """The fold is written for ten minutes under a ``recommend:explore:{user}:…`` key."""
    cache = AsyncMock()
    cache.get = AsyncMock(return_value=None)
    driver = _driver(records=_collection_rows())

    folded = await get_collection_ids(driver, "user-1", cache=cache)

    key = collection_cache_key("user-1")
    assert key == "recommend:explore:user-1:fit:collection"
    cache.set.assert_awaited_once_with(key, folded, ttl=COLLECTION_CACHE_TTL)


@pytest.mark.asyncio
async def test_collection_cache_key_matches_the_recommend_invalidation_pattern(mock_redis: AsyncMock) -> None:
    """``invalidate_user`` scans ``recommend:explore:{user}:*``, which this key is under."""
    from api.cache import RecommendCache

    scanned: list[str] = []

    async def _scan(cursor: int = 0, match: str = "", count: int = 0) -> tuple[int, list[str]]:  # noqa: ARG001
        scanned.append(match)
        return 0, []

    mock_redis.scan = AsyncMock(side_effect=_scan)
    await RecommendCache(redis=mock_redis).invalidate_user("user-1")

    import fnmatch

    assert any(fnmatch.fnmatch(collection_cache_key("user-1"), pattern) for pattern in scanned)


@pytest.mark.asyncio
async def test_collection_ids_return_the_cached_fold_without_querying() -> None:
    """A cache hit is the whole point: no round trip, no re-fold."""
    cache = AsyncMock()
    cache.get = AsyncMock(return_value={"release_ids": ["cached"]})
    driver = _driver(records=_collection_rows())

    folded = await get_collection_ids(driver, "user-1", cache=cache)

    assert folded == {"release_ids": ["cached"]}
    assert driver.session.return_value.run.await_count == 0
    cache.set.assert_not_awaited()


# ──────────────────────────────────────────────────────────────────────────────
# get_release_context
# ──────────────────────────────────────────────────────────────────────────────


def _context_row() -> dict[str, Any]:
    return {
        "id": "555",
        "title": "Blue Train",
        "year": 1957,
        "artists": [{"id": "a-coltrane", "name": "John Coltrane"}],
        "labels": [{"id": "l-blue-note", "name": "Blue Note"}],
        "genres": ["Jazz"],
        "styles": ["Hard Bop"],
        "media_families": ["grooved"],
        "master_id": "m-blue-train",
        "master_title": "Blue Train",
        "siblings": [{"id": "1", "title": "Blue Train", "year": 1997, "media_families": ["digital"]}],
    }


@pytest.mark.asyncio
async def test_release_context_walks_the_master_hop_in_both_directions() -> None:
    """One round trip that reaches the master and comes back down to its siblings."""
    driver = _driver(single=_context_row())

    context = await get_release_context(driver, "555")

    assert driver.session.return_value.run.await_count == 1
    cypher = _cypher_of(driver)
    assert "OPTIONAL MATCH (r)-[:DERIVED_FROM]->(master:Master)" in cypher
    assert "OPTIONAL MATCH (master)<-[:DERIVED_FROM]-(sibling:Release)" in cypher
    assert "WHERE sibling.id <> r.id" in cypher
    assert context is not None
    assert context["master_id"] == "m-blue-train"


@pytest.mark.asyncio
async def test_release_context_returns_the_sibling_media_families_and_years() -> None:
    """Redundancy compares media families, so a sibling carries its own."""
    context = await get_release_context(_driver(single=_context_row()), "555")

    assert context is not None
    assert context["siblings"] == [{"id": "1", "title": "Blue Train", "year": 1997, "media_families": ["digital"]}]
    assert context["media_families"] == ["grooved"]


@pytest.mark.asyncio
async def test_release_context_reads_the_candidates_own_facets() -> None:
    """Artists and labels carry names, because the evidence strings name them."""
    context = await get_release_context(_driver(single=_context_row()), "555")

    assert context is not None
    assert context["artists"] == [{"id": "a-coltrane", "name": "John Coltrane"}]
    assert context["labels"] == [{"id": "l-blue-note", "name": "Blue Note"}]
    assert context["genres"] == ["Jazz"]
    assert context["styles"] == ["Hard Bop"]
    assert context["year"] == 1957


@pytest.mark.asyncio
async def test_release_context_of_an_unknown_release_is_none() -> None:
    """No release, no context — the endpoint turns this into a 404."""
    assert await get_release_context(_driver(single=None), "nope") is None


@pytest.mark.asyncio
async def test_release_context_without_a_master_carries_no_siblings() -> None:
    """A release the graph never linked to a master is contextual, not broken."""
    row = _context_row() | {"master_id": None, "master_title": None, "siblings": []}

    context = await get_release_context(_driver(single=row), "555")

    assert context is not None
    assert context["master_id"] is None
    assert context["siblings"] == []


@pytest.mark.asyncio
async def test_release_context_tolerates_null_facet_lists() -> None:
    """A sparse release folds to empty lists rather than to nulls in the response."""
    row = {
        "id": "555",
        "title": None,
        "year": None,
        "artists": None,
        "labels": None,
        "genres": None,
        "styles": None,
        "media_families": [None],
        "master_id": None,
        "master_title": None,
        "siblings": None,
    }

    context = await get_release_context(_driver(single=row), "555")

    assert context is not None
    assert context["artists"] == []
    assert context["media_families"] == []
    assert context["siblings"] == []


# ──────────────────────────────────────────────────────────────────────────────
# get_release_rarity
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_release_rarity_reads_the_precomputed_row(mock_pool: MagicMock, mock_cur: MagicMock) -> None:
    """Two columns out of ``insights.release_rarity`` — nothing is scored on the request."""
    mock_cur.fetchone.return_value = {"rarity_score": 72.5, "tier": "scarce"}

    rarity = await get_release_rarity(mock_pool, "555")

    assert rarity == {"score": 72.5, "tier": "scarce"}
    sql = mock_cur.execute.await_args_list[0].args[0]
    assert "FROM insights.release_rarity" in sql
    assert mock_cur.execute.await_args_list[0].args[1] == (555,)


@pytest.mark.asyncio
async def test_release_rarity_of_an_unscored_release_is_none(mock_pool: MagicMock, mock_cur: MagicMock) -> None:
    """The insights pipeline runs on its own schedule; an unscored release is normal."""
    mock_cur.fetchone.return_value = None

    assert await get_release_rarity(mock_pool, "555") is None


@pytest.mark.asyncio
async def test_release_rarity_tolerates_a_null_score(mock_pool: MagicMock, mock_cur: MagicMock) -> None:
    """A row with a tier and no score degrades the field rather than the request."""
    mock_cur.fetchone.return_value = {"rarity_score": None, "tier": None}

    assert await get_release_rarity(mock_pool, "555") == {"score": None, "tier": None}


@pytest.mark.asyncio
async def test_release_rarity_without_a_pool_is_none() -> None:
    """Rarity is context beside the answer; an unwired pool leaves the field empty."""
    assert await get_release_rarity(None, "555") is None


@pytest.mark.asyncio
async def test_release_rarity_of_a_non_numeric_id_is_none(mock_pool: MagicMock) -> None:
    """``insights.release_rarity`` keys on the integer Discogs id and nothing else."""
    assert await get_release_rarity(mock_pool, "not-a-number") is None


@pytest.mark.asyncio
async def test_release_rarity_never_raises_into_the_request(mock_pool: MagicMock, mock_cur: MagicMock) -> None:
    """An unreadable insights schema degrades the profile, not the fit answer."""
    mock_cur.execute.side_effect = RuntimeError("insights schema missing")

    assert await get_release_rarity(mock_pool, "555") is None
