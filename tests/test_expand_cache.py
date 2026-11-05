"""/api/expand caches one page and its total, and orders by index when before_year is set (audit H7)."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from api import telemetry
from api.queries import neo4j_queries as q
from api.routers import explore


class FakeValkey:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self.gets = 0

    async def get(self, key: str) -> str | None:
        self.gets += 1
        return self.data.get(key)

    async def setex(self, key: str, _ttl: int, value: str) -> None:
        self.data[key] = value


@pytest.fixture
def valkey() -> Any:
    fake = FakeValkey()
    with patch.object(explore, "_valkey", fake):
        yield fake


def _dispatch(rows: list[dict[str, Any]], total: int = 5000) -> tuple[AsyncMock, AsyncMock, Any, Any]:
    query, count = AsyncMock(return_value=rows), AsyncMock(return_value=total)
    return (
        query,
        count,
        patch.dict(explore.EXPAND_DISPATCH, {"genre": {"releases": query}}),
        patch.dict(explore.COUNT_DISPATCH, {"genre": {"releases": count}}),
    )


URL = "/api/expand?node_id=Rock&type=genre&category=releases"


def test_second_request_is_served_from_the_cache_with_an_identical_body(test_client: TestClient, valkey: FakeValkey) -> None:  # noqa: ARG001
    query, count, a, b = _dispatch([{"id": "r1", "name": "x"}], 5000)
    with a, b:
        first = test_client.get(URL)
        second = test_client.get(URL)
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert first.json() == {"children": [{"id": "r1", "name": "x"}], "total": 5000, "offset": 0, "limit": 50, "has_more": True}
    assert query.await_count == count.await_count == 1


@pytest.mark.parametrize("suffix", ["&offset=50", "&before_year=1970", "&node_id=Pop"])
def test_every_parameter_that_changes_the_page_changes_the_key(test_client: TestClient, valkey: FakeValkey, suffix: str) -> None:
    query, _count, a, b = _dispatch([{"id": "r1"}], 5000)
    with a, b:
        test_client.get(URL)
        test_client.get(URL + suffix if "node_id" not in suffix else URL.replace("node_id=Rock", "node_id=Pop"))
    assert query.await_count == 2
    assert len(valkey.data) == 2


def test_an_empty_page_is_not_cached(test_client: TestClient, valkey: FakeValkey) -> None:
    query, _count, a, b = _dispatch([], 0)
    with a, b:
        test_client.get(URL)
        test_client.get(URL)
    assert query.await_count == 2
    assert valkey.data == {}


def test_a_failing_cache_read_or_write_does_not_fail_the_request(test_client: TestClient) -> None:
    broken = MagicMock()
    broken.get = AsyncMock(side_effect=ConnectionError("down"))
    broken.setex = AsyncMock(side_effect=ConnectionError("down"))
    _query, _count, a, b = _dispatch([{"id": "r1"}], 5000)
    with patch.object(explore, "_valkey", broken), a, b:
        response = test_client.get(URL)
    assert response.status_code == 200
    assert response.json()["total"] == 5000


def test_hits_and_misses_are_counted_under_the_expand_cache(test_client: TestClient, valkey: FakeValkey) -> None:  # noqa: ARG001
    _query, _count, a, b = _dispatch([{"id": "r1"}], 5000)
    with a, b, patch.object(telemetry, "record_cache") as record:
        test_client.get(URL)
        test_client.get(URL)
    outcomes = [(c.args[0], c.kwargs["hit"]) for c in record.call_args_list]
    assert outcomes == [(telemetry.CACHE_EXPAND, False), (telemetry.CACHE_EXPAND, True)]


def test_cache_key_is_stable_and_cannot_collide_on_a_colon() -> None:
    a = explore._expand_cache_key("label", "a:b", "releases", 50, 0, None)
    assert a == explore._expand_cache_key("label", "a:b", "releases", 50, 0, None)
    assert a != explore._expand_cache_key("label", "a", "b:releases", 50, 0, None)
    assert json.dumps(a)  # a plain string


@pytest.mark.asyncio
async def test_before_year_orders_by_the_properties_and_otherwise_keeps_the_null_safe_order() -> None:
    sent: list[str] = []

    async def capture(_driver: Any, cypher: str, **_params: Any) -> list[dict[str, Any]]:
        sent.append(cypher)
        return []

    with patch("api.queries.neo4j_queries.run_query", AsyncMock(side_effect=capture)):
        await q.expand_genre_releases(object(), "Rock", before_year=1970)
        await q.expand_genre_releases(object(), "Rock")
    assert "ORDER BY r.year DESC, r.id" in sent[0]
    assert "year IS NULL" not in sent[0]
    assert "ORDER BY year IS NULL ASC, year DESC, id" in sent[1]


@pytest.mark.parametrize(
    "path",
    [
        "&limit=10",  # not the default page size
        "&offset=25",  # not on a page boundary
        "&offset=1000",  # past the first 20 pages
        "&offset=50&before_year=1970",  # a year filter only caches the first page
    ],
)
def test_pages_outside_the_bounded_set_are_served_but_never_stored(test_client: TestClient, valkey: FakeValkey, path: str) -> None:
    query, _count, a, b = _dispatch([{"id": "r1"}], 5000)
    with a, b:
        first = test_client.get(URL + path)
        second = test_client.get(URL + path)
    assert first.status_code == 200
    assert first.json() == second.json()
    assert query.await_count == 2
    assert valkey.data == {}


def test_a_small_parent_is_served_but_not_stored(test_client: TestClient, valkey: FakeValkey) -> None:
    query, _count, a, b = _dispatch([{"id": "r1"}], 12)
    with a, b:
        test_client.get(URL)
        test_client.get(URL)
    assert query.await_count == 2
    assert valkey.data == {}


def test_the_default_page_with_a_year_filter_is_stored_at_offset_zero(test_client: TestClient, valkey: FakeValkey) -> None:
    _query, _count, a, b = _dispatch([{"id": "r1"}], 5000)
    with a, b:
        test_client.get(URL + "&before_year=1970")
    assert len(valkey.data) == 1
