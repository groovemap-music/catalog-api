"""GET /api/explore/strata (api/routers/explore.py) with the reads mocked."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from neo4j.exceptions import ClientError as Neo4jClientError
from psycopg.errors import QueryCanceled

from api.strata import MAX_ROOT_RELEASES, Catalog, Mark, Release, encode_catalog
from tests.conftest import TEST_USER_ID, make_test_jwt


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
        "get_strata_root": AsyncMock(return_value={"id": "1", "name": "Root", "releases": 4}),
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


def test_a_cancelled_postgres_read_is_a_504(test_client: TestClient, reads: dict[str, AsyncMock]) -> None:
    reads["get_strata_catalog"].side_effect = QueryCanceled("canceling statement due to statement timeout")

    response = test_client.get("/api/explore/strata?artist_id=1")

    assert response.status_code == 504


@pytest.fixture
def valkey_store(mock_valkey: AsyncMock) -> dict[str, str]:
    """Back the mocked Valkey's ``get`` and ``setex`` with a dict, so a write can be read back."""
    store: dict[str, str] = {}
    mock_valkey.get = AsyncMock(side_effect=store.get)
    mock_valkey.setex = AsyncMock(side_effect=lambda key, _ttl, value: store.__setitem__(key, value))
    return store


class TestCache:
    """The catalog side is cached like /api/explore; the caller's marks never are."""

    def test_a_second_read_is_served_from_the_cache(
        self, test_client: TestClient, reads: dict[str, AsyncMock], mock_valkey: AsyncMock, valkey_store: dict[str, str]
    ) -> None:
        first = test_client.get("/api/explore/strata?artist_id=1")
        second = test_client.get("/api/explore/strata?artist_id=1")

        assert first.status_code == second.status_code == 200
        assert second.json() == first.json()
        # Neither the root nor the ladder is read again.
        reads["get_strata_root"].assert_awaited_once()
        reads["get_strata_catalog"].assert_awaited_once()
        assert list(valkey_store) == ["explore:strata:1"]
        key, ttl, _value = mock_valkey.setex.await_args.args
        assert (key, ttl) == ("explore:strata:1", 86400)
        assert mock_valkey.setex.await_count == 1

    def test_a_cached_catalog_is_folded_for_every_page_and_budget(
        self,
        test_client: TestClient,
        reads: dict[str, AsyncMock],
        valkey_store: dict[str, str],  # noqa: ARG002
    ) -> None:
        test_client.get("/api/explore/strata?artist_id=1")
        focused = test_client.get("/api/explore/strata?artist_id=1&budget=6/12/16&focus=m:500&page=1")

        assert focused.status_code == 200
        assert focused.json()["focus"]["id"] == "ra:m:500"
        reads["get_strata_catalog"].assert_awaited_once()

    def test_marks_are_read_per_caller_and_never_cached(
        self, test_client: TestClient, reads: dict[str, AsyncMock], valkey_store: dict[str, str]
    ) -> None:
        other_user = "00000000-0000-0000-0000-000000000002"
        reads["get_strata_marks"].side_effect = lambda _driver, user_id, _artist: (
            {"10": Mark(owned=True, owned_at="2020-01-01T00:00:00Z")} if user_id == TEST_USER_ID else {"11": Mark(wanted=True)}
        )

        owner = test_client.get("/api/explore/strata?artist_id=1", headers={"Authorization": f"Bearer {make_test_jwt()}"})
        other = test_client.get("/api/explore/strata?artist_id=1", headers={"Authorization": f"Bearer {make_test_jwt(other_user)}"})
        anonymous = test_client.get("/api/explore/strata?artist_id=1")

        reads["get_strata_catalog"].assert_awaited_once()
        assert [call.args[1] for call in reads["get_strata_marks"].await_args_list] == [TEST_USER_ID, other_user]
        states = [{node["id"]: node.get("state") for node in response.json()["planes"][2]["nodes"]} for response in (owner, other, anonymous)]
        assert (states[0]["r:10"], states[0]["r:11"]) == ("owned", "none")
        assert (states[1]["r:10"], states[1]["r:11"]) == ("none", "wanted")
        assert set(states[2].values()) == {None}
        assert anonymous.json()["signed_in"] is False
        # The one cached entry is the catalog alone: no caller id, state, or recorded time.
        (cached,) = valkey_store.values()
        assert cached == encode_catalog(CATALOG)
        for marker in (TEST_USER_ID, other_user, "owned", "wanted", "2020-01-01"):
            assert marker not in cached

    def test_without_valkey_every_read_goes_to_the_graph(self, test_client: TestClient, reads: dict[str, AsyncMock]) -> None:
        import api.routers.explore as explore_module

        original = explore_module._valkey
        explore_module._valkey = None
        try:
            assert test_client.get("/api/explore/strata?artist_id=1").status_code == 200
            assert test_client.get("/api/explore/strata?artist_id=1").status_code == 200
        finally:
            explore_module._valkey = original
        assert reads["get_strata_catalog"].await_count == 2

    def test_a_valkey_outage_degrades_to_the_graph_read(self, test_client: TestClient, reads: dict[str, AsyncMock], mock_valkey: AsyncMock) -> None:
        mock_valkey.get = AsyncMock(side_effect=ConnectionError("valkey down"))
        mock_valkey.setex = AsyncMock(side_effect=ConnectionError("valkey down"))

        with patch(f"{ROUTER}.record_cache_write_failure") as write_failure:
            response = test_client.get("/api/explore/strata?artist_id=1")

        assert response.status_code == 200
        assert response.json()["root"]["name"] == "Root"
        reads["get_strata_catalog"].assert_awaited_once()
        assert write_failure.call_args.args[0] == "strata"

    def test_an_entry_this_build_cannot_read_is_a_miss_and_is_rewritten(
        self, test_client: TestClient, reads: dict[str, AsyncMock], valkey_store: dict[str, str]
    ) -> None:
        valkey_store["explore:strata:1"] = '{"format": 0}'

        response = test_client.get("/api/explore/strata?artist_id=1")

        assert response.status_code == 200
        reads["get_strata_catalog"].assert_awaited_once()
        assert valkey_store["explore:strata:1"] == encode_catalog(CATALOG)


class TestLargeRoots:
    """A root above MAX_ROOT_RELEASES is refused before its ladder is read."""

    def test_a_root_above_the_ceiling_is_refused_without_reading_its_ladder(
        self, test_client: TestClient, reads: dict[str, AsyncMock], valkey_store: dict[str, str], auth_headers: dict[str, str]
    ) -> None:
        # The size of GrooveMap's rank-1 root, the placeholder credit gm-design-1h2l sampled.
        reads["get_strata_root"].return_value = {"id": "9", "name": "Placeholder", "releases": 1_356_055}

        response = test_client.get("/api/explore/strata?artist_id=9", headers=auth_headers)

        assert response.status_code == 422
        body = response.json()
        assert body["releases"] == 1_356_055
        assert body["max_releases"] == MAX_ROOT_RELEASES == 134_051
        assert "134051" in body["error"]
        for read in ("get_strata_catalog", "get_strata_marks", "get_strata_labels", "get_shared_personnel"):
            reads[read].assert_not_awaited()
        assert valkey_store == {}

    def test_a_refused_root_is_checked_again_on_every_read(
        self,
        test_client: TestClient,
        reads: dict[str, AsyncMock],
        valkey_store: dict[str, str],  # noqa: ARG002
    ) -> None:
        reads["get_strata_root"].return_value = {"id": "1", "name": "Root", "releases": MAX_ROOT_RELEASES + 1}

        assert test_client.get("/api/explore/strata?artist_id=1").status_code == 422
        assert test_client.get("/api/explore/strata?artist_id=1").status_code == 422

        assert reads["get_strata_root"].await_count == 2
        reads["get_strata_catalog"].assert_not_awaited()

    def test_a_root_at_the_ceiling_is_served(self, test_client: TestClient, reads: dict[str, AsyncMock]) -> None:
        reads["get_strata_root"].return_value = {"id": "1", "name": "Root", "releases": MAX_ROOT_RELEASES}

        assert test_client.get("/api/explore/strata?artist_id=1").status_code == 200
        reads["get_strata_catalog"].assert_awaited_once()
