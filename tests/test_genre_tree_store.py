"""The stored genre tree: serving, change detection, locking, and failure handling (audit H3)."""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from api import genre_tree_store as store


class FakeValkey:
    """Just enough of valkey.asyncio for the store: get, set (nx/ex), delete."""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self.fail = False

    async def get(self, key: str) -> str | None:
        if self.fail:
            raise ConnectionError("down")
        return self.data.get(key)

    async def set(self, key: str, value: str, nx: bool = False, ex: int | None = None) -> bool | None:  # noqa: ARG002
        if self.fail:
            raise ConnectionError("down")
        if nx and key in self.data:
            return None
        self.data[key] = value
        return True

    async def delete(self, key: str) -> None:
        self.data.pop(key, None)


TREE = [{"name": "Rock", "release_count": 9, "styles": [{"name": "Punk", "release_count": 4}]}]


@pytest.fixture
def valkey() -> FakeValkey:
    fake = FakeValkey()
    store.configure(fake)
    return fake


def _queries(signature: str = "1:9:1", tree: list[dict[str, Any]] | None = None) -> Any:
    return (
        patch("api.queries.genre_tree_queries.get_genre_signature", AsyncMock(return_value=signature)),
        patch("api.queries.genre_tree_queries.build_genre_tree", AsyncMock(return_value=TREE if tree is None else tree)),
    )


@pytest.mark.asyncio
async def test_cold_start_is_not_ready(valkey: FakeValkey) -> None:  # noqa: ARG001
    with pytest.raises(store.GenreTreeNotReadyError):
        await store.get_genre_tree()


@pytest.mark.asyncio
async def test_first_refresh_builds_stores_and_serves_without_neo4j(valkey: FakeValkey) -> None:
    sig, build = _queries()
    with sig, build as built:
        assert await store.refresh_once(object()) is True
        assert await store.get_genre_tree() == TREE
        assert built.await_count == 1
    assert store.SNAPSHOT_KEY in valkey.data
    assert store.LOCK_KEY not in valkey.data


@pytest.mark.asyncio
async def test_unchanged_counts_do_not_rebuild(valkey: FakeValkey) -> None:  # noqa: ARG001
    sig, build = _queries()
    with sig, build as built:
        await store.refresh_once(object())
        assert await store.refresh_once(object()) is False
        assert built.await_count == 1


@pytest.mark.asyncio
async def test_changed_counts_rebuild_and_old_tree_serves_until_then(valkey: FakeValkey) -> None:  # noqa: ARG001
    sig, build = _queries("1:9:1")
    with sig, build:
        await store.refresh_once(object())
    newer = [{"name": "Rock", "release_count": 10, "styles": []}]
    sig, build = _queries("1:10:1", newer)
    with sig, build:
        assert await store.get_genre_tree() == TREE
        assert await store.refresh_once(object()) is True
        assert await store.get_genre_tree() == newer


@pytest.mark.asyncio
async def test_stale_tree_rebuilds_after_max_age(valkey: FakeValkey) -> None:  # noqa: ARG001
    sig, build = _queries()
    with sig, build as built:
        await store.refresh_once(object())
        with patch("api.genre_tree_store.time.time", return_value=time.time() + 100):
            assert await store.refresh_once(object(), max_age=50) is True
        assert built.await_count == 2


@pytest.mark.asyncio
async def test_failed_rebuild_keeps_the_previous_tree(valkey: FakeValkey) -> None:  # noqa: ARG001
    sig, build = _queries()
    with sig, build:
        await store.refresh_once(object())
    with (
        patch("api.queries.genre_tree_queries.get_genre_signature", AsyncMock(return_value="1:99:1")),
        patch("api.queries.genre_tree_queries.build_genre_tree", AsyncMock(side_effect=RuntimeError("boom"))),
    ):
        assert await store.refresh_once(object()) is False
    assert await store.get_genre_tree() == TREE


@pytest.mark.asyncio
async def test_restart_serves_the_valkey_copy(valkey: FakeValkey) -> None:
    sig, build = _queries()
    with sig, build:
        await store.refresh_once(object())
    store.configure(valkey)  # a new process: memory empty, Valkey kept
    assert await store.get_genre_tree() == TREE


@pytest.mark.asyncio
async def test_a_second_replica_does_not_build_while_the_lock_is_held(valkey: FakeValkey) -> None:
    valkey.data[store.LOCK_KEY] = "1"
    sig, build = _queries()
    with sig, build as built:
        assert await store.refresh_once(object()) is False
        assert built.await_count == 0


@pytest.mark.asyncio
async def test_valkey_down_still_builds_locally() -> None:
    fake = FakeValkey()
    fake.fail = True
    store.configure(fake)
    sig, build = _queries()
    with sig, build:
        assert await store.refresh_once(object()) is True
        assert await store.get_genre_tree() == TREE


@pytest.mark.asyncio
async def test_corrupt_valkey_value_is_ignored(valkey: FakeValkey) -> None:
    valkey.data[store.SNAPSHOT_KEY] = "{not json"
    with pytest.raises(store.GenreTreeNotReadyError):
        await store.get_genre_tree()
