"""GET /api/explore/strata (api/routers/explore.py) with the reads mocked."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from neo4j.exceptions import ClientError as Neo4jClientError

from api.strata import Catalog, Mark, Release
from tests.conftest import TEST_USER_ID


ROUTER = "api.routers.explore"

CATALOG = Catalog(
    "1",
    "Root",
    (
        Release("10", "500", 1971, ("1",)),
        Release("11", "500", 1975, ("1",)),
        Release("12", "501", None, ("1", "2")),
        Release("13", None, None, ("1",)),
        Release("mb-1", "500", 1999, (), "musicbrainz", "MB reissue"),
    ),
    {"500": 1971, "501": None},
)
LABELS = ({"10": "First", "11": "Second", "12": "Third", "13": "Fourth"}, {"500": "Master A", "501": "Master B"}, {"1": "Root", "2": "Guest"})
EDGES = [{"source": "500", "target": "501", "categories": ["Production"], "people": 3}]


@pytest.fixture
def reads() -> Iterator[dict[str, AsyncMock]]:
    mocks = {
        "get_strata_root": AsyncMock(return_value={"id": "1", "name": "Root"}),
        "get_strata_catalog": AsyncMock(return_value=CATALOG),
        "get_strata_marks": AsyncMock(return_value={"10": Mark(owned=True, owned_at="2020-01-01T00:00:00Z")}),
        "get_strata_labels": AsyncMock(return_value=LABELS),
        "get_shared_personnel": AsyncMock(return_value=EDGES),
    }
    with patch.multiple(ROUTER, **mocks):
        yield mocks


def _states(payload: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(payload, dict):
        found |= {"state", "states", "recorded_at"} & set(payload)
        for value in payload.values():
            found |= _states(value)
    elif isinstance(payload, list):
        for value in payload:
            found |= _states(value)
    return found


def test_anonymous_callers_get_the_three_planes_and_no_collector_state(test_client: TestClient, reads: dict[str, AsyncMock]) -> None:
    response = test_client.get("/api/explore/strata?artist_id=1")

    assert response.status_code == 200
    payload = response.json()
    assert payload["root"] == {"type": "artist", "id": "1", "name": "Root"}
    assert payload["budget"] == {"credits": 3, "masters": 12, "releases": 16}
    assert [plane["kind"] for plane in payload["planes"]] == ["credit", "master", "release"]
    assert payload["signed_in"] is False
    assert _states(payload) == set()
    reads["get_strata_marks"].assert_not_awaited()

    credits, masters, releases = payload["planes"]
    assert credits["nodes"][1]["artists"] == [{"id": "1", "name": "Root"}, {"id": "2", "name": "Guest"}]
    assert {node["master_id"]: node["title"] for node in masters["nodes"]} == {"500": "Master A", "501": "Master B"}
    assert [agg["kind"] for agg in masters["aggregates"]] == ["no_master"]
    assert masters["edges"] == [{"source": "m:500", "target": "m:501", "kind": "shared_personnel", "categories": ["Production"], "people": 3}]
    by_id = {node["id"]: node for node in releases["nodes"]}
    assert by_id["mb:mb-1"]["provider"] == "musicbrainz"
    assert by_id["mb:mb-1"]["title"] == "MB reissue"
    assert by_id["r:13"]["undated"] is True

    catalog_call = reads["get_strata_catalog"].await_args
    assert catalog_call.args[2:] == ("1", "Root")
    labels_call = reads["get_strata_labels"].await_args
    assert sorted(labels_call.args[1]) == ["10", "11", "12", "13"]
    assert reads["get_shared_personnel"].await_args.kwargs == {"limit": 8}


def test_signed_in_callers_get_their_state_and_recorded_time(
    test_client: TestClient, reads: dict[str, AsyncMock], auth_headers: dict[str, str]
) -> None:
    response = test_client.get("/api/explore/strata?artist_id=1", headers=auth_headers)

    assert response.status_code == 200
    payload = response.json()
    assert payload["signed_in"] is True
    reads["get_strata_marks"].assert_awaited_once()
    assert reads["get_strata_marks"].await_args.args[1:] == (TEST_USER_ID, "1")
    nodes = {node["id"]: node for node in payload["planes"][2]["nodes"]}
    assert nodes["r:10"]["state"] == "owned"
    assert nodes["r:10"]["recorded_at"] == {"owned": "2020-01-01T00:00:00Z", "wanted": None}
    assert nodes["r:11"]["state"] == "none"
    assert nodes["mb:mb-1"]["state"] is None


def test_an_invalid_token_is_served_as_anonymous(test_client: TestClient, reads: dict[str, AsyncMock]) -> None:
    response = test_client.get("/api/explore/strata?artist_id=1", headers={"Authorization": "Bearer not-a-token"})

    assert response.status_code == 200
    assert response.json()["signed_in"] is False
    reads["get_strata_marks"].assert_not_awaited()


def test_budget_and_focus_reach_the_rule(test_client: TestClient, reads: dict[str, AsyncMock]) -> None:  # noqa: ARG001
    response = test_client.get("/api/explore/strata?artist_id=1&budget=6/12/16&focus=m:500&page=1")

    assert response.status_code == 200
    payload = response.json()
    assert payload["budget"] == {"credits": 6, "masters": 12, "releases": 16}
    assert payload["focus"] == {"id": "ra:m:500", "plane": "release", "page": 0, "pages": 1, "page_size": 3, "members": 3}


@pytest.mark.parametrize("budget", ["3/12", "2/12/16", "x/y/z", "3/5/16"])
def test_bad_budgets_are_a_400(test_client: TestClient, reads: dict[str, AsyncMock], budget: str) -> None:
    response = test_client.get(f"/api/explore/strata?artist_id=1&budget={budget}")

    assert response.status_code == 400
    assert "error" in response.json()
    reads["get_strata_root"].assert_not_awaited()


def test_unknown_artist_is_a_404(test_client: TestClient, reads: dict[str, AsyncMock]) -> None:
    reads["get_strata_root"].return_value = None

    response = test_client.get("/api/explore/strata?artist_id=404")

    assert response.status_code == 404
    assert response.json() == {"error": "Artist '404' not found"}
    reads["get_strata_catalog"].assert_not_awaited()


def test_unknown_focus_is_a_404(test_client: TestClient, reads: dict[str, AsyncMock]) -> None:
    response = test_client.get("/api/explore/strata?artist_id=1&focus=m:404")

    assert response.status_code == 404
    assert "m:404" in response.json()["error"]
    reads["get_strata_labels"].assert_not_awaited()


@pytest.mark.parametrize("query", ["", "artist_id=", "artist_id=1&page=-1", "artist_id=1&focus="])
def test_invalid_parameters_are_a_422(test_client: TestClient, reads: dict[str, AsyncMock], query: str) -> None:  # noqa: ARG001
    assert test_client.get(f"/api/explore/strata?{query}").status_code == 422


def test_no_driver_is_a_503(test_client: TestClient) -> None:
    import api.routers.explore as explore_module

    original = explore_module._neo4j_driver
    explore_module._neo4j_driver = None
    try:
        assert test_client.get("/api/explore/strata?artist_id=1").status_code == 503
    finally:
        explore_module._neo4j_driver = original


def test_a_timed_out_read_is_a_504(test_client: TestClient, reads: dict[str, AsyncMock]) -> None:
    reads["get_strata_catalog"].side_effect = Neo4jClientError("Neo.ClientError.Transaction.TransactionTimedOut")

    response = test_client.get("/api/explore/strata?artist_id=1")

    assert response.status_code == 504
    assert "timed out" in response.json()["error"]


def test_other_graph_errors_are_not_swallowed(test_client: TestClient, reads: dict[str, AsyncMock]) -> None:
    reads["get_strata_catalog"].side_effect = Neo4jClientError("Neo.ClientError.Statement.SyntaxError")

    assert test_client.get("/api/explore/strata?artist_id=1").status_code == 500
