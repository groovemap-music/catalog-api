"""Behavioral tests for the points that emit first-party activity.

Each test drives the real endpoint through the test client with the recorder patched, and
asserts on the exact event types, policy ids, and payload keys that reached it. That is
the contract these emission points owe: a payload is a published schema, not a free-form
dict, and a policy id on a stored impression is historical data nobody can rewrite later.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from common.events import event_types, payload_schema_for
from fastapi.testclient import TestClient

import api.activity as activity


ITEM_ID = "22222222-2222-2222-2222-222222222222"
OTHER_ITEM_ID = "33333333-3333-3333-3333-333333333333"
IMPRESSION_ID = "44444444-4444-4444-4444-444444444444"


def search_payload(results: list[dict[str, Any]]) -> dict[str, Any]:
    """A search response body in the shape `execute_search` returns."""
    return {
        "query": "miles",
        "total": len(results),
        "facets": {"type": {}, "genre": [], "decade": [], "media": []},
        "results": results,
        "pagination": {"limit": 20, "offset": 0, "has_more": False},
    }


def payload_for(recorder: AsyncMock, event_type: str) -> dict[str, Any]:
    """Return the payload of the first recorded event of ``event_type``."""
    for call in recorder.await_args_list:
        if call.args[1] == event_type:
            return dict(call.args[2])
    raise AssertionError(f"no {event_type} event was recorded")


class TestSearchEmission:
    """GET /api/search records the query and one impression per hit shown."""

    @pytest.fixture
    def hits(self) -> list[dict[str, Any]]:
        return [
            {"type": "artist", "id": "1", "name": "Miles Davis", "gm_id": ITEM_ID},
            {"type": "release", "id": "2", "name": "Kind of Blue", "gm_id": OTHER_ITEM_ID},
        ]

    def test_the_query_event_carries_exactly_the_schema_keys(
        self, test_client: TestClient, auth_headers: dict[str, str], hits: list[dict[str, Any]]
    ) -> None:
        with (
            patch("api.routers.search.execute_search", AsyncMock(return_value=search_payload(hits))),
            patch("api.activity.record_event", AsyncMock()) as record_event,
            patch("api.activity.record_events", AsyncMock()),
        ):
            response = test_client.get("/api/search?q=miles&types=artist,release&genres=Jazz&year_min=1959", headers=auth_headers)

        assert response.status_code == 200
        payload = payload_for(record_event, "search.query")
        assert set(payload) == set(payload_schema_for("search.query")["properties"])
        assert payload["query"] == "miles"
        assert payload["result_count"] == 2
        assert UUID(payload["request_id"])
        assert payload["filters"] == ["genre:Jazz", "type:artist", "type:release", "year_min:1959"]

    def test_one_result_impression_per_hit_in_position_order(
        self, test_client: TestClient, auth_headers: dict[str, str], hits: list[dict[str, Any]]
    ) -> None:
        with (
            patch("api.routers.search.execute_search", AsyncMock(return_value=search_payload(hits))),
            patch("api.activity.record_event", AsyncMock()),
            patch("api.activity.record_events", AsyncMock()) as record_events,
        ):
            response = test_client.get("/api/search?q=miles", headers=auth_headers)

        assert response.status_code == 200
        batch = record_events.await_args.args[1]
        assert [event_type for event_type, _payload, _key in batch] == ["search.result_impression"] * 2
        assert [payload["position"] for _type, payload, _key in batch] == [1, 2], "positions are one-based"
        assert [payload["item_id"] for _type, payload, _key in batch] == [ITEM_ID, OTHER_ITEM_ID]
        for _type, payload, _key in batch:
            assert set(payload) == set(payload_schema_for("search.result_impression")["properties"])

    def test_every_hit_of_one_page_shares_one_request_id(
        self, test_client: TestClient, auth_headers: dict[str, str], hits: list[dict[str, Any]]
    ) -> None:
        with (
            patch("api.routers.search.execute_search", AsyncMock(return_value=search_payload(hits))),
            patch("api.activity.record_event", AsyncMock()) as record_event,
            patch("api.activity.record_events", AsyncMock()) as record_events,
        ):
            test_client.get("/api/search?q=miles", headers=auth_headers)

        query_request_id = payload_for(record_event, "search.query")["request_id"]
        batch = record_events.await_args.args[1]
        assert {payload["request_id"] for _type, payload, _key in batch} == {query_request_id}

    def test_each_hit_is_returned_with_its_impression_id(
        self, test_client: TestClient, auth_headers: dict[str, str], hits: list[dict[str, Any]]
    ) -> None:
        with (
            patch("api.routers.search.execute_search", AsyncMock(return_value=search_payload(hits))),
            patch("api.activity.record_event", AsyncMock()),
            patch("api.activity.record_events", AsyncMock()) as record_events,
        ):
            response = test_client.get("/api/search?q=miles", headers=auth_headers)

        returned = [UUID(hit["impression_id"]) for hit in response.json()["results"]]
        batch = record_events.await_args.args[1]
        assert returned == [UUID(payload["impression_id"]) for _type, payload, _key in batch]

    def test_a_hit_without_a_native_id_is_counted_and_not_logged(self, test_client: TestClient, auth_headers: dict[str, str]) -> None:
        hits = [{"type": "artist", "id": "1", "name": "Unknown", "gm_id": None}]
        with (
            patch("api.routers.search.execute_search", AsyncMock(return_value=search_payload(hits))),
            patch("api.activity.record_event", AsyncMock()),
            patch("api.activity.record_events", AsyncMock()) as record_events,
            patch("api.activity.count_unidentified_candidate") as counted,
        ):
            response = test_client.get("/api/search?q=miles", headers=auth_headers)

        assert record_events.await_args.args[1] == []
        assert counted.call_count == 1
        assert response.json()["results"][0]["impression_id"] is None

    def test_an_anonymous_search_records_nothing(self, test_client: TestClient, hits: list[dict[str, Any]]) -> None:
        with (
            patch("api.routers.search.execute_search", AsyncMock(return_value=search_payload(hits))),
            patch("api.activity.record_event", AsyncMock()) as record_event,
            patch("api.activity.record_events", AsyncMock()) as record_events,
        ):
            response = test_client.get("/api/search?q=miles")

        assert response.status_code == 200
        assert record_event.await_count == 0
        assert record_events.await_count == 0

    def test_an_empty_type_list_falls_back_to_every_type_in_the_filters(self, test_client: TestClient, auth_headers: dict[str, str]) -> None:
        from api.queries.search_queries import ALL_TYPES

        with (
            patch("api.routers.search.execute_search", AsyncMock(return_value=search_payload([]))),
            patch("api.activity.record_event", AsyncMock()) as record_event,
            patch("api.activity.record_events", AsyncMock()),
        ):
            response = test_client.get("/api/search?q=miles&types=,&year_max=1999", headers=auth_headers)

        assert response.status_code == 200
        filters = payload_for(record_event, "search.query")["filters"]
        assert "year_max:1999" in filters
        assert {f"type:{entity_type}" for entity_type in ALL_TYPES} <= set(filters)

    def test_an_invalid_request_records_nothing(self, test_client: TestClient, auth_headers: dict[str, str]) -> None:
        with patch("api.activity.record_event", AsyncMock()) as record_event:
            response = test_client.get("/api/search?q=miles&types=nonsense", headers=auth_headers)

        assert response.status_code == 400
        assert record_event.await_count == 0


class TestRecommendationImpressions:
    """The three recommendation surfaces, each with its own ranking policy."""

    @pytest.fixture
    def stamped(self) -> Any:
        """Patch the stamping helper and give each item a deterministic impression id."""

        async def stamp(user_id: str, policy_id: str, items: list[dict[str, Any]], **_kwargs: Any) -> None:  # noqa: ARG001
            for item in items:
                item["impression_id"] = IMPRESSION_ID

        with patch("api.activity.stamp_recommendation_impressions", AsyncMock(side_effect=stamp)) as helper:
            yield helper

    def test_similar_artists_uses_its_policy_and_its_own_score_key(self, test_client: TestClient, auth_headers: dict[str, str], stamped: Any) -> None:
        ranked = [
            {"artist_id": "1", "artist_name": "A", "similarity": 0.9, "breakdown": {}, "release_count": 5, "shared_genres": [], "shared_labels": []}
        ]
        with (
            patch("api.routers.recommend.get_artist_identity", AsyncMock(return_value={"artist_id": "1", "artist_name": "A", "release_count": 12})),
            patch("api.routers.recommend.get_artist_profile", AsyncMock(return_value={})),
            patch("api.routers.recommend.get_candidate_artists", AsyncMock(return_value=[])),
            patch("api.routers.recommend.compute_similar_artists", MagicMock(return_value=ranked)),
            patch("api.routers.recommend.native_ids_for", AsyncMock(return_value={"1": ITEM_ID})),
        ):
            response = test_client.get("/api/recommend/similar/artist/1", headers=auth_headers)

        assert response.status_code == 200
        assert stamped.await_args.args[1] == activity.POLICY_SIMILAR_ARTIST
        assert stamped.await_args.kwargs["score_key"] == "similarity"
        assert response.json()["similar"][0]["impression_id"] == IMPRESSION_ID

    def test_user_recommendations_distinguish_the_two_strategies(self, test_client: TestClient, auth_headers: dict[str, str], stamped: Any) -> None:
        results = [{"id": "1", "title": "T", "score": 3}]
        with (
            patch("api.routers.user.get_user_recommendations", AsyncMock(return_value=list(results))),
            patch("api.routers.user.native_ids_for", AsyncMock(return_value={"1": ITEM_ID})),
        ):
            artist = test_client.get("/api/user/recommendations?strategy=artist", headers=auth_headers)
        assert artist.status_code == 200
        assert stamped.await_args.args[1] == activity.POLICY_USER_RECOMMENDATIONS_ARTIST
        assert artist.json()["recommendations"][0]["impression_id"] == IMPRESSION_ID

        with (
            patch("api.routers.user.get_user_recommendations", AsyncMock(return_value=list(results))),
            patch("api.routers.user.get_label_affinity_candidates", AsyncMock(return_value=[])),
            patch("api.routers.user.get_blindspot_candidates", AsyncMock(return_value=[])),
            patch("api.routers.user.get_collector_counts", AsyncMock(return_value={})),
            patch("api.routers.user.merge_recommendation_candidates", MagicMock(return_value=[{"id": "1", "score": 0.5}])),
            patch("api.routers.user.native_ids_for", AsyncMock(return_value={"1": ITEM_ID})),
        ):
            multi = test_client.get("/api/user/recommendations?strategy=multi", headers=auth_headers)
        assert multi.status_code == 200
        assert stamped.await_args.args[1] == activity.POLICY_USER_RECOMMENDATIONS_MULTI

    def test_explore_uses_its_policy(self, test_client: TestClient, auth_headers: dict[str, str], stamped: Any) -> None:
        scored = [{"id": "1", "name": "N", "type": "artist", "score": 0.7, "path": [], "reason": "r"}]
        with (
            patch("api.routers.recommend.get_explore_traversal", AsyncMock(return_value=[])),
            patch("api.routers.recommend.get_taste_heatmap", AsyncMock(return_value=([], 0))),
            patch("api.routers.recommend.get_blind_spots", AsyncMock(return_value=[])),
            patch("api.routers.recommend.score_discoveries", MagicMock(return_value=scored)),
            patch("api.routers.recommend.native_ids_for_pairs", AsyncMock(return_value={("artist", "1"): ITEM_ID})),
        ):
            response = test_client.get("/api/recommend/explore/artist/1", headers=auth_headers)

        assert response.status_code == 200
        assert stamped.await_args.args[1] == activity.POLICY_EXPLORE
        assert response.json()["discoveries"][0]["impression_id"] == IMPRESSION_ID

    def test_a_cached_explore_body_still_records_the_showing(self, test_client: TestClient, auth_headers: dict[str, str], stamped: Any) -> None:
        cached = {
            "from": {"id": "1", "name": "N", "type": "artist", "gm_id": None},
            "discoveries": [{"id": "1", "type": "artist", "score": 0.7, "gm_id": ITEM_ID}],
        }
        with patch("api.routers.recommend._cache") as cache:
            cache.get = AsyncMock(return_value=cached)
            response = test_client.get("/api/recommend/explore/artist/1", headers=auth_headers)

        assert response.status_code == 200
        assert stamped.await_count == 1, "impressions are recorded per request served, not per cache fill"
        assert stamped.await_args.args[1] == activity.POLICY_EXPLORE

    def test_a_cached_similarity_body_still_records_the_showing(self, test_client: TestClient, auth_headers: dict[str, str], stamped: Any) -> None:
        cached = {"artist_id": "1", "artist_name": "A", "similar": [{"artist_id": "2", "similarity": 0.5, "gm_id": ITEM_ID}]}
        with patch("api.routers.recommend._cache") as cache:
            cache.get = AsyncMock(return_value=cached)
            response = test_client.get("/api/recommend/similar/artist/1", headers=auth_headers)

        assert response.status_code == 200
        assert stamped.await_count == 1
        assert stamped.await_args.args[1] == activity.POLICY_SIMILAR_ARTIST

    def test_an_anonymous_similarity_request_records_no_impression(self, test_client: TestClient, stamped: Any) -> None:
        cached = {"artist_id": "1", "artist_name": "A", "similar": [{"artist_id": "2", "similarity": 0.5, "gm_id": ITEM_ID}]}
        with patch("api.routers.recommend._cache") as cache:
            cache.get = AsyncMock(return_value=cached)
            response = test_client.get("/api/recommend/similar/artist/1")

        assert response.status_code == 200
        assert stamped.await_args.args[0] == "", "an anonymous showing has no subject to record against"


class TestStampRecommendationImpressions:
    """The helper the three surfaces share, exercised against the recorder itself."""

    @pytest.mark.asyncio
    async def test_each_item_is_stamped_with_the_id_its_impression_was_written_under(self) -> None:
        items = [{"gm_id": ITEM_ID, "score": 0.9}, {"gm_id": OTHER_ITEM_ID, "score": 0.4}]
        with patch("api.activity.record_impressions", AsyncMock(return_value=["a", "b"])) as record:
            await activity.stamp_recommendation_impressions("user", "policy_v1", items)

        assert [item["impression_id"] for item in items] == ["a", "b"]
        entries = record.await_args.args[4]
        assert [position for position, _item, _score, _propensity in entries] == [1, 2]
        assert [score for _position, _item, score, _propensity in entries] == [0.9, 0.4]
        assert record.await_args.args[1] == activity.SURFACE_RECOMMENDATION

    @pytest.mark.asyncio
    async def test_a_candidate_without_a_native_id_is_skipped_counted_and_stamped_none(self) -> None:
        items = [{"gm_id": None, "score": 0.9}, {"gm_id": ITEM_ID, "score": 0.4}]
        with (
            patch("api.activity.record_impressions", AsyncMock(return_value=["b"])) as record,
            patch("api.activity.count_unidentified_candidate") as counted,
        ):
            await activity.stamp_recommendation_impressions("user", "policy_v1", items)

        assert [item["impression_id"] for item in items] == [None, "b"]
        assert counted.call_count == 1
        assert [position for position, _i, _s, _p in record.await_args.args[4]] == [2], "position is the rank in the list shown"

    @pytest.mark.asyncio
    async def test_a_failed_write_leaves_every_impression_id_null(self) -> None:
        items = [{"gm_id": ITEM_ID, "score": 0.9}]
        with patch("api.activity.record_impressions", AsyncMock(return_value=[None])):
            await activity.stamp_recommendation_impressions("user", "policy_v1", items)

        assert items[0]["impression_id"] is None

    @pytest.mark.asyncio
    async def test_an_anonymous_caller_records_nothing_but_still_stamps_the_key(self) -> None:
        items = [{"gm_id": ITEM_ID, "score": 0.9}]
        with patch("api.activity.record_impressions", AsyncMock()) as record:
            await activity.stamp_recommendation_impressions("", "policy_v1", items)

        assert record.await_count == 0
        assert items[0]["impression_id"] is None

    @pytest.mark.asyncio
    async def test_no_candidate_has_a_native_id_so_nothing_is_written(self) -> None:
        items = [{"gm_id": None, "score": 0.9}]
        with patch("api.activity.record_impressions", AsyncMock()) as record:
            await activity.stamp_recommendation_impressions("user", "policy_v1", items)

        assert record.await_count == 0

    @pytest.mark.asyncio
    async def test_an_empty_list_records_nothing(self) -> None:
        with patch("api.activity.record_impressions", AsyncMock()) as record:
            await activity.stamp_recommendation_impressions("user", "policy_v1", [])
        assert record.await_count == 0


class TestOutcomeEndpoint:
    """POST /api/activity/events — the one client-writable event path."""

    @pytest.mark.parametrize(
        "event_type",
        ["recommendation.opened", "recommendation.saved", "recommendation.dismissed", "recommendation.hidden"],
    )
    def test_each_outcome_is_accepted_and_recorded(self, test_client: TestClient, auth_headers: dict[str, str], event_type: str) -> None:
        with patch("api.activity.record_event", AsyncMock()) as record_event:
            response = test_client.post(
                "/api/activity/events",
                json={"event_type": event_type, "impression_id": IMPRESSION_ID, "item_id": ITEM_ID},
                headers=auth_headers,
            )

        assert response.status_code == 202
        assert record_event.await_args.args[1] == event_type
        assert record_event.await_args.args[2] == {"impression_id": IMPRESSION_ID, "item_id": ITEM_ID}
        assert record_event.await_args.kwargs["idempotency_key"] == f"{event_type}:{IMPRESSION_ID}"

    @pytest.mark.parametrize(
        "body",
        [
            {"event_type": "search.query", "impression_id": IMPRESSION_ID, "item_id": ITEM_ID},
            {"event_type": "consent.granted", "impression_id": IMPRESSION_ID, "item_id": ITEM_ID},
            {"event_type": "recommendation.shown", "impression_id": IMPRESSION_ID, "item_id": ITEM_ID},
            {"event_type": "recommendation.invented", "impression_id": IMPRESSION_ID, "item_id": ITEM_ID},
            {"event_type": "recommendation.opened", "item_id": ITEM_ID},
            {"event_type": "recommendation.opened", "impression_id": "not-a-uuid", "item_id": ITEM_ID},
            {"event_type": "recommendation.opened", "impression_id": IMPRESSION_ID},
        ],
    )
    def test_anything_but_the_four_outcomes_is_rejected(self, test_client: TestClient, auth_headers: dict[str, str], body: dict[str, Any]) -> None:
        with patch("api.activity.record_event", AsyncMock()) as record_event:
            response = test_client.post("/api/activity/events", json=body, headers=auth_headers)

        assert response.status_code == 422
        assert record_event.await_count == 0

    def test_the_endpoint_requires_a_user(self, test_client: TestClient) -> None:
        response = test_client.post(
            "/api/activity/events",
            json={"event_type": "recommendation.opened", "impression_id": IMPRESSION_ID, "item_id": ITEM_ID},
        )
        assert response.status_code == 401

    def test_the_four_outcomes_are_the_vocabulary_types_a_client_may_write(self) -> None:
        from api.models import RECOMMENDATION_OUTCOMES

        assert set(RECOMMENDATION_OUTCOMES) < set(event_types())
        assert all(payload_schema_for(outcome)["required"] == ["impression_id", "item_id"] for outcome in RECOMMENDATION_OUTCOMES)


class TestBatchWriter:
    """record_events — one round trip for a page of events."""

    @pytest.mark.asyncio
    async def test_a_batch_is_written_with_one_executemany(self, mock_pool: MagicMock, mock_cur: MagicMock) -> None:
        mock_cur.fetchone.return_value = (uuid4(),)
        mock_cur.fetchall.return_value = []
        activity.configure(mock_pool, None)

        await activity.record_events(
            "user",
            [
                ("wantlist.item_added", {"item_id": ITEM_ID}, "a"),
                ("wantlist.item_removed", {"item_id": OTHER_ITEM_ID}, "b"),
            ],
        )

        assert mock_cur.executemany.await_count == 1
        sql, rows = mock_cur.executemany.await_args.args
        assert "INSERT INTO activity.events" in str(sql)
        assert [row[1] for row in rows] == ["wantlist.item_added", "wantlist.item_removed"]
        assert [row[11] for row in rows] == ["a", "b"]
        assert len({row[3] for row in rows}) == 1, "one subject resolution for the whole batch"

    @pytest.mark.asyncio
    async def test_one_invalid_event_does_not_lose_the_rest_of_the_page(self, mock_pool: MagicMock, mock_cur: MagicMock) -> None:
        mock_cur.fetchone.return_value = (uuid4(),)
        mock_cur.fetchall.return_value = []
        activity.configure(mock_pool, None)

        await activity.record_events(
            "user",
            [("wantlist.item_added", {"nope": 1}, None), ("wantlist.item_added", {"item_id": ITEM_ID}, None)],
        )

        _sql, rows = mock_cur.executemany.await_args.args
        assert len(rows) == 1

    @pytest.mark.asyncio
    async def test_a_batch_of_only_invalid_events_writes_nothing(self, mock_pool: MagicMock, mock_cur: MagicMock) -> None:
        mock_cur.fetchone.return_value = (uuid4(),)
        mock_cur.fetchall.return_value = []
        activity.configure(mock_pool, None)

        await activity.record_events("user", [("wantlist.item_added", {"nope": 1}, None)])

        assert mock_cur.executemany.await_count == 0

    @pytest.mark.asyncio
    async def test_an_empty_batch_and_an_unconfigured_recorder_are_both_no_ops(self, mock_pool: MagicMock, mock_cur: MagicMock) -> None:
        activity.configure(None, None)
        await activity.record_events("user", [("wantlist.item_added", {"item_id": ITEM_ID}, None)])

        activity.configure(mock_pool, None)
        await activity.record_events("user", [])
        assert mock_cur.executemany.await_count == 0

    @pytest.mark.asyncio
    async def test_an_unresolvable_subject_drops_the_batch(self, mock_pool: MagicMock, mock_cur: MagicMock) -> None:
        mock_cur.fetchone.return_value = None
        activity.configure(mock_pool, None)

        await activity.record_events("user", [("wantlist.item_added", {"item_id": ITEM_ID}, None)])

        assert mock_cur.executemany.await_count == 0

    @pytest.mark.asyncio
    async def test_a_failing_batch_write_does_not_raise(self, mock_pool: MagicMock, mock_cur: MagicMock) -> None:
        mock_cur.fetchone.return_value = (uuid4(),)
        mock_cur.fetchall.return_value = []
        mock_cur.executemany.side_effect = RuntimeError("events unavailable")
        activity.configure(mock_pool, None)

        await activity.record_events("user", [("wantlist.item_added", {"item_id": ITEM_ID}, None)])
