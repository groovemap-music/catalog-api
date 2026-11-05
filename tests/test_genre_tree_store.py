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


@pytest.mark.asyncio
async def test_loop_retries_on_a_short_backoff_until_a_tree_exists_then_uses_the_interval(valkey: FakeValkey) -> None:  # noqa: ARG001
    import asyncio

    delays: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        delays.append(seconds)
        if len(delays) == 5:
            raise asyncio.CancelledError

    outcomes = [RuntimeError("neo4j restarting"), RuntimeError("neo4j restarting"), TREE]
    sig = patch("api.queries.genre_tree_queries.get_genre_signature", AsyncMock(return_value="1:9:1"))
    build = patch("api.queries.genre_tree_queries.build_genre_tree", AsyncMock(side_effect=outcomes))
    with sig, build, patch("api.genre_tree_store.asyncio.sleep", fake_sleep), pytest.raises(asyncio.CancelledError):
        await store.run_refresh_loop(object(), interval=3600, retry_initial=30, retry_max=300)

    # Two failures back off 30 s then 60 s; the third attempt stores a tree; then the hourly interval.
    assert delays[:2] == [30, 60]
    assert delays[2:] == [3600, 3600, 3600]
    assert await store.get_genre_tree() == TREE


@pytest.mark.asyncio
async def test_loop_backoff_is_capped_and_never_exceeds_the_interval(valkey: FakeValkey) -> None:  # noqa: ARG001
    import asyncio

    delays: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        delays.append(seconds)
        if len(delays) == 7:
            raise asyncio.CancelledError

    sig = patch("api.queries.genre_tree_queries.get_genre_signature", AsyncMock(return_value="1:9:1"))
    build = patch("api.queries.genre_tree_queries.build_genre_tree", AsyncMock(side_effect=RuntimeError("down")))
    with sig, build, patch("api.genre_tree_store.asyncio.sleep", fake_sleep), pytest.raises(asyncio.CancelledError):
        await store.run_refresh_loop(object(), interval=3600, retry_initial=30, retry_max=300)
    assert delays == [30, 60, 120, 240, 300, 300, 300]


@pytest.mark.asyncio
async def test_loop_retries_when_another_replica_holds_the_lock_and_nothing_is_stored(valkey: FakeValkey) -> None:
    import asyncio

    valkey.data[store.LOCK_KEY] = "1"
    delays: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        delays.append(seconds)
        valkey.data.pop(store.LOCK_KEY, None)  # the other replica gave up without storing
        if len(delays) == 2:
            raise asyncio.CancelledError

    sig, build = _queries()
    with sig, build as built, patch("api.genre_tree_store.asyncio.sleep", fake_sleep), pytest.raises(asyncio.CancelledError):
        await store.run_refresh_loop(object(), interval=3600, retry_initial=30, retry_max=300)
    assert delays[0] == 30
    assert built.await_count == 1
    assert await store.get_genre_tree() == TREE
