"""GET /api/user/taste-projection: the fold (api/taste_projection.py), its read, and its route."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from neo4j.exceptions import ClientError as Neo4jClientError

from api.queries import taste_queries
from api.taste_projection import HeldItem, build_taste_projection, instant
from tests.conftest import TEST_USER_ID, make_test_jwt


OTHER_USER_ID = "00000000-0000-0000-0000-000000000002"
ROUTER = "api.routers.taste"


def _item(release_id: str, **fields: Any) -> HeldItem:
    return HeldItem(release_id, **{"owned_edges": 1, "wanted_edges": 0, **fields})


ITEMS = [
    _item(
        "10",
        owned_edges=2,
        wanted_edges=1,
        owned_at=["2021-01-01T00:00:00-08:00", "2019-05-01T12:00:00-07:00"],
        wanted_at=["2018-02-03T04:05:06-08:00"],
        master_id="500",
        master_year=1971,
        genres=["Rock"],
        styles=["Prog Rock", "Psychedelic Rock"],
    ),
    _item("11", owned_at=["2020-01-01T00:00:00Z"], master_id="500", master_year=1971, genres=["Rock"], styles=["Prog Rock"]),
    _item("12", owned_edges=0, wanted_edges=1, wanted_at=["2020-06-07T08:09:10-07:00"], master_id="9", genres=["Jazz", "Rock"], styles=["Fusion"]),
    _item("13", genres=["Electronic"]),
    _item("14", owned_edges=0, wanted_edges=1),
]


def test_items_land_on_every_genre_style_and_master_they_carry() -> None:
    projection = build_taste_projection(ITEMS)

    assert projection["totals"] == {
        "items": 5,
        "owned": 3,
        "wanted": 3,
        "wanted_owned": 1,
        "masterless_items": 2,
        "genre_only_items": 1,
        "unplaced_items": 1,
    }
    assert [(g["id"], g["items"], g["owned"], g["wanted"], g["state"]) for g in projection["genres"]] == [
        ("Rock", 3, 2, 2, "wanted_owned"),
        ("Electronic", 1, 1, 0, "owned"),
        ("Jazz", 1, 0, 1, "wanted"),
    ]
    assert [(s["id"], s["items"], s["state"]) for s in projection["styles"]] == [
        ("Prog Rock", 2, "wanted_owned"),
        ("Fusion", 1, "wanted"),
        ("Psychedelic Rock", 1, "wanted_owned"),
    ]


def test_masters_carry_the_inputs_for_their_position_and_masterless_items_get_none() -> None:
    masters = build_taste_projection(ITEMS)["masters"]

    assert [m["id"] for m in masters] == ["9", "500"]
    nine, five_hundred = masters
    assert (nine["year"], nine["genres"], nine["styles"], nine["state"]) == (None, ["Jazz", "Rock"], ["Fusion"], "wanted")
    assert (five_hundred["year"], five_hundred["items"], five_hundred["owned"], five_hundred["wanted"]) == (1971, 2, 2, 1)
    assert (five_hundred["genres"], five_hundred["styles"]) == (["Rock"], ["Prog Rock", "Psychedelic Rock"])


def test_the_recorded_span_compares_instants_not_strings_and_counts_undated_edges() -> None:
    projection = build_taste_projection(ITEMS)

    assert projection["recorded_at"]["owned"] == {"first": "2019-05-01T12:00:00-07:00", "last": "2021-01-01T00:00:00-08:00", "undated": 1}
    assert projection["recorded_at"]["wanted"] == {"first": "2018-02-03T04:05:06-08:00", "last": "2020-06-07T08:09:10-07:00", "undated": 1}
    rock = next(g for g in projection["genres"] if g["id"] == "Rock")
    assert rock["recorded_at"]["wanted"] == {"first": "2018-02-03T04:05:06-08:00", "last": "2020-06-07T08:09:10-07:00", "undated": 0}


def test_a_span_orders_by_instant_across_offsets() -> None:
    later_by_clock_earlier_by_instant = ["2020-01-01T01:00:00+05:00", "2020-01-01T00:00:00Z"]
    projection = build_taste_projection([_item("1", owned_at=later_by_clock_earlier_by_instant, genres=["Rock"])])

    assert projection["recorded_at"]["owned"] == {"first": "2020-01-01T01:00:00+05:00", "last": "2020-01-01T00:00:00Z", "undated": 0}


def test_an_unreadable_time_is_counted_as_undated_and_a_naive_time_is_utc() -> None:
    projection = build_taste_projection([_item("1", owned_edges=2, owned_at=["not a time", "2020-01-01T00:00:00"], genres=["Rock"])])

    assert projection["recorded_at"]["owned"] == {"first": "2020-01-01T00:00:00", "last": "2020-01-01T00:00:00", "undated": 1}
    assert instant("2020-01-01T00:00:00") == instant("2020-01-01T00:00:00Z")
    assert instant("") is None


def test_an_empty_collection_projects_to_empty_aggregates() -> None:
    projection = build_taste_projection([])

    assert projection["totals"]["items"] == 0
    assert projection["recorded_at"] == {"owned": None, "wanted": None}
    assert (projection["genres"], projection["styles"], projection["masters"]) == ([], [], [])


def test_the_order_is_deterministic() -> None:
    assert build_taste_projection(ITEMS) == build_taste_projection(reversed(ITEMS))


# --- the read ----------------------------------------------------------------------------------


def _row(kind: str, release_id: str = "10", **fields: Any) -> dict[str, Any]:
    return {
        "id": release_id,
        "kind": kind,
        "edges": 1,
        "recorded": [],
        "master_id": None,
        "master_year": None,
        "genres": [],
        "styles": [],
        **fields,
    }


class _Temporal:
    def iso_format(self) -> str:
        return "2020-01-01T00:00:00.000000000+00:00"


@pytest.mark.asyncio
async def test_the_read_joins_a_release_in_both_lists_into_one_item() -> None:
    rows = [
        _row(
            "COLLECTED",
            edges=2,
            recorded=["2021-01-01T00:00:00Z", _Temporal()],
            master_id=500,
            master_year=1971,
            genres=["Rock"],
            styles=["Prog Rock"],
        ),
        _row("WANTS", recorded=["2018-02-03T04:05:06Z"], master_id=500, master_year=1971, genres=["Rock"], styles=["Prog Rock"]),
        _row("WANTS", release_id="11", genres=["Jazz"]),
    ]
    with patch.object(taste_queries, "run_query", AsyncMock(return_value=rows)) as run_query:
        projection = await taste_queries.get_taste_projection(object(), "user-1")

    assert run_query.await_args.kwargs["user_id"] == "user-1"
    assert run_query.await_args.kwargs["timeout"] == taste_queries.TASTE_PROJECTION_TIMEOUT_SECONDS
    assert projection["totals"] == {
        "items": 2,
        "owned": 1,
        "wanted": 2,
        "wanted_owned": 1,
        "masterless_items": 1,
        "genre_only_items": 1,
        "unplaced_items": 0,
    }
    (master,) = projection["masters"]
    assert (master["id"], master["year"], master["state"], master["owned"], master["wanted"]) == ("500", 1971, "wanted_owned", 1, 1)
    assert master["recorded_at"]["owned"] == {"first": "2020-01-01T00:00:00.000000000+00:00", "last": "2021-01-01T00:00:00Z", "undated": 0}


def test_the_read_is_scoped_to_the_callers_own_edges() -> None:
    assert "(:User {id: $user_id})-[edge:COLLECTED|WANTS]->(r:Release)" in taste_queries._TASTE_PROJECTION
    assert taste_queries._TASTE_PROJECTION.count("MATCH (:User") == 1


# --- the route ---------------------------------------------------------------------------------


def _headers(user_id: str = TEST_USER_ID) -> dict[str, str]:
    return {"Authorization": f"Bearer {make_test_jwt(user_id)}"}


def _projection_for(user_id: str) -> dict[str, Any]:
    return build_taste_projection([_item(f"item-of-{user_id[-1]}", genres=["Rock"], master_id=user_id[-1])])


def test_the_route_returns_the_callers_projection_uncached(test_client: TestClient) -> None:
    with patch(f"{ROUTER}.get_taste_projection", AsyncMock(return_value=build_taste_projection(ITEMS))) as read:
        response = test_client.get("/api/user/taste-projection", headers=_headers())

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["vary"] == "Authorization"
    assert read.await_args.args[1] == TEST_USER_ID
    body = response.json()
    assert set(body) == {"totals", "recorded_at", "genres", "styles", "masters"}
    assert body["totals"]["items"] == 5
    assert [m["id"] for m in body["masters"]] == ["9", "500"]


def test_an_anonymous_caller_is_a_401_and_nothing_is_read(test_client: TestClient) -> None:
    with patch(f"{ROUTER}.get_taste_projection", AsyncMock()) as read:
        anonymous = test_client.get("/api/user/taste-projection")
        invalid = test_client.get("/api/user/taste-projection", headers={"Authorization": "Bearer not-a-token"})

    assert anonymous.status_code == 401
    assert invalid.status_code == 401
    read.assert_not_awaited()


def test_two_callers_never_see_each_others_projection(test_client: TestClient) -> None:
    async def read(_driver: Any, user_id: str) -> dict[str, Any]:
        return _projection_for(user_id)

    with patch(f"{ROUTER}.get_taste_projection", AsyncMock(side_effect=read)) as reads:
        first = test_client.get("/api/user/taste-projection", headers=_headers(TEST_USER_ID))
        second = test_client.get("/api/user/taste-projection", headers=_headers(OTHER_USER_ID))
        first_again = test_client.get("/api/user/taste-projection", headers=_headers(TEST_USER_ID))

    assert [call.args[1] for call in reads.await_args_list] == [TEST_USER_ID, OTHER_USER_ID, TEST_USER_ID]
    assert first.json()["masters"][0]["id"] == TEST_USER_ID[-1]
    assert second.json()["masters"][0]["id"] == OTHER_USER_ID[-1]
    assert first.json() == first_again.json()
    assert first.json() != second.json()
    assert all(r.headers["cache-control"] == "no-store" for r in (first, second, first_again))


def test_the_projection_is_never_written_to_a_shared_cache(test_client: TestClient, mock_valkey: AsyncMock) -> None:
    mock_valkey.reset_mock()
    with patch(f"{ROUTER}.get_taste_projection", AsyncMock(return_value=build_taste_projection(ITEMS))):
        test_client.get("/api/user/taste-projection", headers=_headers())

    written = [call for call in mock_valkey.method_calls if call[0] in {"set", "setex", "hset", "mset", "psetex"}]
    assert written == []


def test_a_timed_out_read_is_a_504_and_other_errors_are_not_swallowed(test_client: TestClient) -> None:
    timeout = Neo4jClientError("TransactionTimedOut: query timed out")
    with patch(f"{ROUTER}.get_taste_projection", AsyncMock(side_effect=timeout)):
        response = test_client.get("/api/user/taste-projection", headers=_headers())
    assert response.status_code == 504
    assert "timed out" in response.json()["error"]
    assert response.headers["cache-control"] == "no-store"

    with patch(f"{ROUTER}.get_taste_projection", AsyncMock(side_effect=Neo4jClientError("SomethingElse: boom"))):
        assert test_client.get("/api/user/taste-projection", headers=_headers()).status_code == 500


def test_the_route_is_503_before_the_graph_is_wired(test_client: TestClient) -> None:
    import api.routers.taste as taste_router

    original = taste_router._neo4j_driver
    taste_router._neo4j_driver = None
    try:
        assert test_client.get("/api/user/taste-projection", headers=_headers()).status_code == 503
    finally:
        taste_router._neo4j_driver = original


def test_a_user_with_no_items_gets_an_empty_projection_not_a_422(test_client: TestClient) -> None:
    with patch(f"{ROUTER}.get_taste_projection", AsyncMock(return_value=build_taste_projection([]))):
        response = test_client.get("/api/user/taste-projection", headers=_headers())

    assert response.status_code == 200
    assert response.json()["totals"]["items"] == 0


@pytest.mark.parametrize("method", ["post", "put", "delete"])
def test_the_route_is_read_only(test_client: TestClient, method: str) -> None:
    assert getattr(test_client, method)("/api/user/taste-projection", headers=_headers()).status_code == 405
