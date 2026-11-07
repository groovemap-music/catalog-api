"""Tests for the insights compute router endpoints."""

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import psycopg
import pytest
import yaml
from fastapi.testclient import TestClient
from neo4j.exceptions import Neo4jError, TransientError


def _neo4j_error(code: str, message: str = "boom") -> Neo4jError:
    """Build a real Neo4jError with a server code.

    ``Neo4jError.code`` is a read-only property, so the driver's own hydration
    factory is the only way to construct one faithfully — and it dispatches to
    the right subclass (ClientError / TransientError) from the code itself.
    """
    return Neo4jError._hydrate_neo4j(code=code, message=message)


def test_community_enrichment_contract_covers_producer_worst_case() -> None:
    """Keep the published processing budget synchronized with the bounded producer."""
    from api.routers.insights_compute import _ENRICHMENT_DELAY_SECONDS, MAX_ENRICHMENT_RELEASES

    contract = yaml.safe_load((Path(__file__).parents[1] / "api/contracts/internal-insights/v2/openapi.yaml").read_text(encoding="utf-8"))
    operation = contract["paths"]["/api/internal/insights/community-enrichment"]["get"]
    assert operation["x-groovemap-max-processing-seconds"] == MAX_ENRICHMENT_RELEASES * _ENRICHMENT_DELAY_SECONDS


class TestArtistCentralityEndpoint:
    """Tests for GET /api/internal/insights/artist-centrality."""

    def test_success(self, test_client: TestClient) -> None:
        """Returns 200 with artist centrality results."""
        items = [{"artist_id": "1", "artist_name": "Miles Davis", "edge_count": 500}]
        with patch("api.routers.insights_compute.query_artist_centrality", new_callable=AsyncMock, return_value=items):
            response = test_client.get("/api/internal/insights/artist-centrality")
        assert response.status_code == 200
        assert response.json() == {"items": items}

    def test_not_ready(self, test_client: TestClient) -> None:
        """Returns 503 when Neo4j is not configured."""
        import api.routers.insights_compute as mod

        original = mod._neo4j
        mod._neo4j = None
        try:
            response = test_client.get("/api/internal/insights/artist-centrality")
            assert response.status_code == 503
        finally:
            mod._neo4j = original


class TestGenreTrendsEndpoint:
    """Tests for GET /api/internal/insights/genre-trends."""

    def test_success(self, test_client: TestClient) -> None:
        """Returns 200 with genre trends results."""
        items = [{"genre": "Jazz", "decade": 1960, "release_count": 50}]
        with patch("api.routers.insights_compute.query_genre_trends", new_callable=AsyncMock, return_value=items):
            response = test_client.get("/api/internal/insights/genre-trends")
        assert response.status_code == 200
        assert response.json() == {"items": items}

    def test_not_ready(self, test_client: TestClient) -> None:
        """Returns 503 when Neo4j is not configured."""
        import api.routers.insights_compute as mod

        original = mod._neo4j
        mod._neo4j = None
        try:
            response = test_client.get("/api/internal/insights/genre-trends")
            assert response.status_code == 503
        finally:
            mod._neo4j = original


_CONTRACT = Path(__file__).parents[1] / "api/contracts/internal-insights/v2/openapi.yaml"


class TestGenreTrendsTimeout:
    """A real timeout on the genre-trends read is a retryable failure, never empty data."""

    def test_503_on_transaction_timeout(self, test_client: TestClient) -> None:
        err = _neo4j_error("Neo.ClientError.Transaction.TransactionTimedOutClientConfiguration", "transaction timed out")
        with patch("api.routers.insights_compute.query_genre_trends", new=AsyncMock(side_effect=err)):
            response = test_client.get("/api/internal/insights/genre-trends")
        assert response.status_code == 503
        assert response.json()["code"] == "Neo.ClientError.Transaction.TransactionTimedOutClientConfiguration"
        assert "items" not in response.json()

    def test_non_retryable_error_is_not_masked_as_503(self, test_client: TestClient) -> None:
        err = _neo4j_error("Neo.ClientError.Statement.SyntaxError", "invalid syntax")
        with patch("api.routers.insights_compute.query_genre_trends", new=AsyncMock(side_effect=err)):
            response = test_client.get("/api/internal/insights/genre-trends")
        assert response.status_code == 500


def test_genre_trends_contract_publishes_the_producer_read_budget() -> None:
    """The budget consumers pin is the server-side timeout the producer enforces, above the 30 s default."""
    from api.queries.helpers import DEFAULT_READ_TIMEOUT_SECONDS
    from api.queries.insights_neo4j_queries import GENRE_TRENDS_QUERY_TIMEOUT_SECONDS

    contract = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    operation = contract["paths"]["/api/internal/insights/genre-trends"]["get"]
    assert operation["x-groovemap-max-processing-seconds"] == GENRE_TRENDS_QUERY_TIMEOUT_SECONDS
    assert GENRE_TRENDS_QUERY_TIMEOUT_SECONDS > DEFAULT_READ_TIMEOUT_SECONDS


def test_style_year_counts_contract_publishes_the_producer_read_budget() -> None:
    """The processing budget consumers pin is the server-side timeout the producer enforces."""
    from api.queries.insights_neo4j_queries import STYLE_YEAR_QUERY_TIMEOUT_SECONDS

    contract = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    operation = contract["paths"]["/api/internal/insights/style-year-counts"]["get"]
    assert operation["x-groovemap-max-processing-seconds"] == STYLE_YEAR_QUERY_TIMEOUT_SECONDS
    assert contract["info"]["version"] == "2.0.0"


_STYLE_YEAR_RESULT = {
    "style_years": [{"style": "Bop", "year": 1959, "release_count": 4, "master_count": 3}],
    "style_parents": [{"style": "Bop", "genre": "Jazz", "release_count": 4, "style_release_count": 5, "share": 0.8}],
    "genre_years": [{"genre": "Jazz", "year": 1959, "release_count": 5}],
}


class TestStyleYearCountsEndpoint:
    """Tests for GET /api/internal/insights/style-year-counts."""

    def test_success_matches_the_published_schema_fields(self, test_client: TestClient) -> None:
        """Returns 200 and exactly the fields the contract requires, nothing extra."""
        with patch("api.routers.insights_compute.query_style_year_counts", new_callable=AsyncMock, return_value=_STYLE_YEAR_RESULT):
            response = test_client.get("/api/internal/insights/style-year-counts")
        assert response.status_code == 200
        body = response.json()
        assert body == _STYLE_YEAR_RESULT

        schemas = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))["components"]["schemas"]
        for key, schema in (("style_years", "StyleYearCount"), ("style_parents", "StyleParentShare"), ("genre_years", "GenreYearCount")):
            assert set(body[key][0]) == set(schemas[schema]["required"]) == set(schemas[schema]["properties"])

    def test_not_ready(self, test_client: TestClient) -> None:
        """Returns 503 when Neo4j is not configured."""
        import api.routers.insights_compute as mod

        original = mod._neo4j
        mod._neo4j = None
        try:
            response = test_client.get("/api/internal/insights/style-year-counts")
            assert response.status_code == 503
        finally:
            mod._neo4j = original

    def test_503_on_transaction_timeout(self, test_client: TestClient) -> None:
        """The server-side read budget expiring is a retryable 503, not a 500."""
        err = _neo4j_error("Neo.ClientError.Transaction.TransactionTimedOutClientConfiguration", "transaction timed out")
        with patch("api.routers.insights_compute.query_style_year_counts", new=AsyncMock(side_effect=err)):
            response = test_client.get("/api/internal/insights/style-year-counts")
        assert response.status_code == 503
        assert response.json()["code"] == "Neo.ClientError.Transaction.TransactionTimedOutClientConfiguration"

    def test_503_when_the_timeout_arrives_inside_a_task_group(self, test_client: TestClient) -> None:
        """The query runs its reads in a task group, which wraps failures in an ExceptionGroup."""
        err = _neo4j_error("Neo.ClientError.Transaction.TransactionTimedOut", "transaction timed out")
        group = ExceptionGroup("unhandled errors in a TaskGroup", [err])
        with patch("api.routers.insights_compute.query_style_year_counts", new=AsyncMock(side_effect=group)):
            response = test_client.get("/api/internal/insights/style-year-counts")
        assert response.status_code == 503

    def test_non_retryable_error_is_not_masked_as_503(self, test_client: TestClient) -> None:
        """A genuine query bug still surfaces as a server error."""
        err = _neo4j_error("Neo.ClientError.Statement.SyntaxError", "invalid syntax")
        with patch("api.routers.insights_compute.query_style_year_counts", new=AsyncMock(side_effect=err)):
            response = test_client.get("/api/internal/insights/style-year-counts")
        assert response.status_code == 500


def test_style_landmarks_contract_publishes_the_producer_read_budget() -> None:
    """The landmark operation publishes the server-side timeout the producer enforces."""
    from api.queries.insights_neo4j_queries import STYLE_LANDMARK_QUERY_TIMEOUT_SECONDS

    contract = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    operation = contract["paths"]["/api/internal/insights/style-landmarks"]["get"]
    assert operation["x-groovemap-max-processing-seconds"] == STYLE_LANDMARK_QUERY_TIMEOUT_SECONDS


_STYLE_LANDMARK_RESULT = {
    "landmarks": [
        {"style": "Bop", "master_id": "m1", "title": "Blue Train", "year": 1958, "release_count": 12},
        {"style": "Bop", "master_id": "m2", "title": "Undated", "year": None, "release_count": 3},
    ]
}


class TestStyleLandmarksEndpoint:
    """Tests for GET /api/internal/insights/style-landmarks."""

    def test_success_matches_the_published_schema_fields(self, test_client: TestClient) -> None:
        """Returns 200 and exactly the fields the contract requires, nothing extra."""
        with patch("api.routers.insights_compute.query_style_landmarks", new_callable=AsyncMock, return_value=_STYLE_LANDMARK_RESULT):
            response = test_client.get("/api/internal/insights/style-landmarks")
        assert response.status_code == 200
        body = response.json()
        assert body == _STYLE_LANDMARK_RESULT

        schema = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))["components"]["schemas"]["StyleLandmark"]
        assert set(body["landmarks"][0]) == set(schema["required"]) == set(schema["properties"])

    def test_not_ready(self, test_client: TestClient) -> None:
        """Returns 503 when Neo4j is not configured."""
        import api.routers.insights_compute as mod

        original = mod._neo4j
        mod._neo4j = None
        try:
            response = test_client.get("/api/internal/insights/style-landmarks")
            assert response.status_code == 503
        finally:
            mod._neo4j = original

    def test_503_on_transaction_timeout(self, test_client: TestClient) -> None:
        """The server-side read budget expiring is a retryable 503, not a 500."""
        err = _neo4j_error("Neo.ClientError.Transaction.TransactionTimedOutClientConfiguration", "transaction timed out")
        with patch("api.routers.insights_compute.query_style_landmarks", new=AsyncMock(side_effect=err)):
            response = test_client.get("/api/internal/insights/style-landmarks")
        assert response.status_code == 503
        assert response.json()["code"] == "Neo.ClientError.Transaction.TransactionTimedOutClientConfiguration"

    def test_503_on_transient_error(self, test_client: TestClient) -> None:
        """A transient database error is a retryable 503."""
        err = _neo4j_error("Neo.TransientError.General.MemoryPoolOutOfMemoryError", "out of memory")
        with patch("api.routers.insights_compute.query_style_landmarks", new=AsyncMock(side_effect=err)):
            response = test_client.get("/api/internal/insights/style-landmarks")
        assert response.status_code == 503

    def test_non_retryable_error_is_not_masked_as_503(self, test_client: TestClient) -> None:
        """A genuine query bug still surfaces as a server error."""
        err = _neo4j_error("Neo.ClientError.Statement.SyntaxError", "invalid syntax")
        with patch("api.routers.insights_compute.query_style_landmarks", new=AsyncMock(side_effect=err)):
            response = test_client.get("/api/internal/insights/style-landmarks")
        assert response.status_code == 500


def test_style_cotags_contract_publishes_the_producer_read_budget() -> None:
    """The co-tag operation publishes the server-side timeout the producer enforces."""
    from api.queries.insights_neo4j_queries import STYLE_COTAG_QUERY_TIMEOUT_SECONDS

    contract = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    operation = contract["paths"]["/api/internal/insights/style-cotags"]["get"]
    assert operation["x-groovemap-max-processing-seconds"] == STYLE_COTAG_QUERY_TIMEOUT_SECONDS


_STYLE_COTAG_RESULT = {
    "cotags": [
        {"style": "Bop", "other": "Cool", "release_count": 12},
        {"style": "Bop", "other": "Hard Bop", "release_count": 3},
    ]
}


class TestStyleCotagsEndpoint:
    """Tests for GET /api/internal/insights/style-cotags."""

    def test_success_matches_the_published_schema_fields(self, test_client: TestClient) -> None:
        """Returns 200 and exactly the fields the contract requires, nothing extra."""
        with patch("api.routers.insights_compute.query_style_cotags", new_callable=AsyncMock, return_value=_STYLE_COTAG_RESULT):
            response = test_client.get("/api/internal/insights/style-cotags")
        assert response.status_code == 200
        body = response.json()
        assert body == _STYLE_COTAG_RESULT

        schema = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))["components"]["schemas"]["StyleCotag"]
        assert set(body["cotags"][0]) == set(schema["required"]) == set(schema["properties"])

    def test_not_ready(self, test_client: TestClient) -> None:
        """Returns 503 when Neo4j is not configured."""
        import api.routers.insights_compute as mod

        original = mod._neo4j
        mod._neo4j = None
        try:
            response = test_client.get("/api/internal/insights/style-cotags")
            assert response.status_code == 503
        finally:
            mod._neo4j = original

    def test_503_on_transaction_timeout(self, test_client: TestClient) -> None:
        """The server-side read budget expiring is a retryable 503, not a 500."""
        err = _neo4j_error("Neo.ClientError.Transaction.TransactionTimedOutClientConfiguration", "transaction timed out")
        with patch("api.routers.insights_compute.query_style_cotags", new=AsyncMock(side_effect=err)):
            response = test_client.get("/api/internal/insights/style-cotags")
        assert response.status_code == 503
        assert response.json()["code"] == "Neo.ClientError.Transaction.TransactionTimedOutClientConfiguration"

    def test_503_on_transient_error(self, test_client: TestClient) -> None:
        """A transient database error is a retryable 503."""
        err = _neo4j_error("Neo.TransientError.General.MemoryPoolOutOfMemoryError", "out of memory")
        with patch("api.routers.insights_compute.query_style_cotags", new=AsyncMock(side_effect=err)):
            response = test_client.get("/api/internal/insights/style-cotags")
        assert response.status_code == 503

    def test_non_retryable_error_is_not_masked_as_503(self, test_client: TestClient) -> None:
        """A genuine query bug still surfaces as a server error."""
        err = _neo4j_error("Neo.ClientError.Statement.SyntaxError", "invalid syntax")
        with patch("api.routers.insights_compute.query_style_cotags", new=AsyncMock(side_effect=err)):
            response = test_client.get("/api/internal/insights/style-cotags")
        assert response.status_code == 500


class TestLabelLongevityEndpoint:
    """Tests for GET /api/internal/insights/label-longevity."""

    def test_success(self, test_client: TestClient) -> None:
        """Returns 200 with label longevity results."""
        items = [{"label_id": "1", "label_name": "Blue Note", "years_active": 80}]
        with patch("api.routers.insights_compute.query_label_longevity", new_callable=AsyncMock, return_value=items):
            response = test_client.get("/api/internal/insights/label-longevity")
        assert response.status_code == 200
        assert response.json() == {"items": items}

    def test_placeholders_stay_out_unless_asked_for(self, test_client: TestClient) -> None:
        """A caller that names no choice, such as an older analytics-engine, gets the default."""
        read = AsyncMock(return_value=[])
        with patch("api.routers.insights_compute.query_label_longevity", read):
            test_client.get("/api/internal/insights/label-longevity?limit=7")
            test_client.get("/api/internal/insights/label-longevity?limit=7&include_placeholders=true")

        assert [call.kwargs["include_placeholders"] for call in read.await_args_list] == [False, True]
        assert {call.kwargs["limit"] for call in read.await_args_list} == {7}

    def test_not_ready(self, test_client: TestClient) -> None:
        """Returns 503 when Neo4j is not configured."""
        import api.routers.insights_compute as mod

        original = mod._neo4j
        mod._neo4j = None
        try:
            response = test_client.get("/api/internal/insights/label-longevity")
            assert response.status_code == 503
        finally:
            mod._neo4j = original

    def test_503_on_transaction_timeout(self, test_client: TestClient) -> None:
        """A real timeout is a retryable failure, never empty data."""
        err = _neo4j_error("Neo.ClientError.Transaction.TransactionTimedOutClientConfiguration", "transaction timed out")
        with patch("api.routers.insights_compute.query_label_longevity", new=AsyncMock(side_effect=err)):
            response = test_client.get("/api/internal/insights/label-longevity")
        assert response.status_code == 503
        assert response.json()["code"] == "Neo.ClientError.Transaction.TransactionTimedOutClientConfiguration"
        assert "items" not in response.json()

    def test_503_on_memory_pool_exhaustion(self, test_client: TestClient) -> None:
        """The transaction pool is shared, so running out of it is a retryable failure, never empty data."""
        code = "Neo.TransientError.General.MemoryPoolOutOfMemoryError"
        with patch(
            "api.routers.insights_compute.query_label_longevity",
            new=AsyncMock(side_effect=TransientError._hydrate_neo4j(code=code, message="pool full")),
        ):
            response = test_client.get("/api/internal/insights/label-longevity")
        assert response.status_code == 503
        assert response.json()["code"] == code
        assert "items" not in response.json()

    def test_non_retryable_error_is_not_masked_as_503(self, test_client: TestClient) -> None:
        err = _neo4j_error("Neo.ClientError.Statement.SyntaxError", "invalid syntax")
        with patch("api.routers.insights_compute.query_label_longevity", new=AsyncMock(side_effect=err)):
            response = test_client.get("/api/internal/insights/label-longevity")
        assert response.status_code == 500


def test_label_longevity_contract_publishes_the_placeholder_choice() -> None:
    """Off by default, so a consumer that predates the parameter keeps real labels only."""
    contract = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    operation = contract["paths"]["/api/internal/insights/label-longevity"]["get"]
    names = [parameter["$ref"].rsplit("/", 1)[-1] for parameter in operation["parameters"]]
    parameter = contract["components"]["parameters"]["LabelLongevityIncludePlaceholders"]

    assert "LabelLongevityIncludePlaceholders" in names
    assert parameter["name"] == "include_placeholders"
    assert parameter["required"] is False
    assert parameter["schema"] == {"type": "boolean", "default": False}
    assert "is_placeholder" in operation["description"]


def test_label_longevity_contract_publishes_the_producer_read_budget() -> None:
    """The budget consumers pin is the server-side timeout the producer enforces, above the 30 s default."""
    from api.queries.helpers import DEFAULT_READ_TIMEOUT_SECONDS
    from api.queries.insights_neo4j_queries import LABEL_LONGEVITY_QUERY_TIMEOUT_SECONDS

    contract = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    operation = contract["paths"]["/api/internal/insights/label-longevity"]["get"]
    assert operation["x-groovemap-max-processing-seconds"] == LABEL_LONGEVITY_QUERY_TIMEOUT_SECONDS
    assert LABEL_LONGEVITY_QUERY_TIMEOUT_SECONDS > DEFAULT_READ_TIMEOUT_SECONDS
    assert "503" in operation["responses"]


class TestAnniversariesEndpoint:
    """Tests for GET /api/internal/insights/anniversaries."""

    def test_success(self, test_client: TestClient) -> None:
        """Returns 200 with anniversary results."""
        items = [{"master_id": "1", "title": "Kind of Blue", "release_year": 1959}]
        with patch("api.routers.insights_compute.query_monthly_anniversaries", new_callable=AsyncMock, return_value=items):
            response = test_client.get("/api/internal/insights/anniversaries?year=2026&month=3")
        assert response.status_code == 200
        assert response.json() == {"items": items}

    def test_not_ready(self, test_client: TestClient) -> None:
        """Returns 503 when Neo4j is not configured."""
        import api.routers.insights_compute as mod

        original = mod._neo4j
        mod._neo4j = None
        try:
            response = test_client.get("/api/internal/insights/anniversaries?year=2026&month=3")
            assert response.status_code == 503
        finally:
            mod._neo4j = original


class TestDataCompletenessCache:
    """Tests for Valkey caching on /api/internal/insights/data-completeness."""

    def test_cache_hit_returns_cached(self, test_client: TestClient, mock_valkey: AsyncMock) -> None:
        """Cache hit returns cached data without querying PostgreSQL."""
        cached = {"items": [{"entity_type": "artists", "total_count": 100}]}
        mock_valkey.get = AsyncMock(return_value=json.dumps(cached))

        with patch("api.routers.insights_compute.query_data_completeness") as mock_query:
            response = test_client.get("/api/internal/insights/data-completeness")

        assert response.status_code == 200
        assert response.json() == cached
        mock_query.assert_not_called()

    def test_cache_miss_queries_and_caches(self, test_client: TestClient, mock_valkey: AsyncMock) -> None:
        """Cache miss queries PostgreSQL and stores result in Valkey."""
        mock_valkey.get = AsyncMock(return_value=None)
        items = [{"entity_type": "artists", "total_count": 500, "completeness_pct": 85.0}]

        with patch("api.routers.insights_compute.query_data_completeness", new_callable=AsyncMock, return_value=items):
            response = test_client.get("/api/internal/insights/data-completeness")

        assert response.status_code == 200
        assert response.json() == {"items": items}
        mock_valkey.setex.assert_called_once()
        call_args = mock_valkey.setex.call_args[0]
        assert call_args[0] == "insights:data-completeness"
        assert call_args[1] == 21600  # 6h TTL

    def test_cache_get_failure_falls_through(self, test_client: TestClient, mock_valkey: AsyncMock) -> None:
        """Valkey get failure falls through to PostgreSQL query."""
        mock_valkey.get = AsyncMock(side_effect=Exception("connection lost"))
        items = [{"entity_type": "labels", "total_count": 200}]

        with patch("api.routers.insights_compute.query_data_completeness", new_callable=AsyncMock, return_value=items):
            response = test_client.get("/api/internal/insights/data-completeness")

        assert response.status_code == 200
        assert response.json() == {"items": items}

    def test_cache_set_failure_still_returns(self, test_client: TestClient, mock_valkey: AsyncMock) -> None:
        """Valkey set failure does not prevent response."""
        mock_valkey.get = AsyncMock(return_value=None)
        mock_valkey.setex = AsyncMock(side_effect=Exception("write failed"))
        items = [{"entity_type": "masters", "total_count": 300}]

        with patch("api.routers.insights_compute.query_data_completeness", new_callable=AsyncMock, return_value=items):
            response = test_client.get("/api/internal/insights/data-completeness")

        assert response.status_code == 200
        assert response.json() == {"items": items}


class TestDataCompletenessNoValkey:
    """Tests for data-completeness when Valkey is not configured."""

    def test_no_valkey_queries_directly(self, test_client: TestClient) -> None:
        """When Valkey is None, queries PostgreSQL directly."""
        import api.routers.insights_compute as mod

        original_valkey = mod._valkey
        mod._valkey = None
        try:
            items = [{"entity_type": "releases", "total_count": 1000}]
            with patch("api.routers.insights_compute.query_data_completeness", new_callable=AsyncMock, return_value=items):
                response = test_client.get("/api/internal/insights/data-completeness")
            assert response.status_code == 200
            assert response.json() == {"items": items}
        finally:
            mod._valkey = original_valkey


class TestDataCompletenessNotReady:
    """Test 503 when pool is not configured."""

    def test_pool_not_ready(self, test_client: TestClient) -> None:
        """Returns 503 when _pool is None."""
        import api.routers.insights_compute as mod

        original = mod._pool
        mod._pool = None
        try:
            response = test_client.get("/api/internal/insights/data-completeness")
            assert response.status_code == 503
        finally:
            mod._pool = original


class TestConfigureWithValkey:
    """Test configure() accepts valkey parameter."""

    def test_configure_sets_valkey(self) -> None:
        """configure() stores valkey reference."""
        import api.routers.insights_compute as mod

        original = mod._valkey
        mock = AsyncMock()
        try:
            mod.configure(AsyncMock(), AsyncMock(), valkey=mock)
            assert mod._valkey is mock
        finally:
            mod._valkey = original

    def test_configure_without_valkey(self) -> None:
        """configure() without valkey sets _valkey to None."""
        import api.routers.insights_compute as mod

        original = mod._valkey
        try:
            mod.configure(AsyncMock(), AsyncMock())
            assert mod._valkey is None
        finally:
            mod._valkey = original


_DISTRIBUTIONS = {"artist": [[1, 2], [5, 1]], "label": [], "genre": [[1000, 3]]}
_QUALITY_PATH = "/api/internal/insights/rarity-scores/quality-counts"
_PAGE_PATH = "/api/internal/insights/rarity-scores/page"


class TestRarityQualityCounts:
    """Tests for GET /api/internal/insights/rarity-scores/quality-counts."""

    def test_passes_the_cursor_and_returns_the_page(self, test_client: TestClient) -> None:
        page = {**_DISTRIBUTIONS, "releases": 2, "release_total": 9, "next_after": "b"}
        with patch("api.routers.insights_compute.fetch_rarity_quality_counts", new=AsyncMock(return_value=page)) as fetch:
            response = test_client.get(_QUALITY_PATH, params={"after": "a", "limit": 2})
        assert response.status_code == 200
        assert response.json() == page
        assert fetch.await_args.kwargs == {"after": "a", "limit": 2}

    @pytest.mark.parametrize("limit", [0, 20_001])
    def test_rejects_a_limit_outside_the_contract(self, test_client: TestClient, limit: int) -> None:
        assert test_client.get(_QUALITY_PATH, params={"limit": limit}).status_code == 422

    def test_503_when_neo4j_is_not_configured(self, test_client: TestClient) -> None:
        with patch("api.routers.insights_compute._neo4j", None):
            assert test_client.get(_QUALITY_PATH).status_code == 503

    def test_503_on_a_transient_neo4j_error(self, test_client: TestClient) -> None:
        with patch("api.routers.insights_compute.fetch_rarity_quality_counts", new=AsyncMock(side_effect=TransientError("oom"))):
            assert test_client.get(_QUALITY_PATH).status_code == 503


class TestRarityPage:
    """Tests for POST /api/internal/insights/rarity-scores/page."""

    def test_passes_cursor_and_counts_and_returns_the_page(self, test_client: TestClient) -> None:
        page = {"items": [{"release_id": "1", "rarity_score": 85.0}], "next_after": None}
        with patch("api.routers.insights_compute.fetch_rarity_page", new=AsyncMock(return_value=page)) as fetch:
            response = test_client.post(_PAGE_PATH, params={"after": "a", "limit": 2}, json=_DISTRIBUTIONS)
        assert response.status_code == 200
        assert response.json() == page
        kwargs = fetch.await_args.kwargs
        assert (kwargs["after"], kwargs["limit"]) == ("a", 2)
        assert kwargs["distributions"] == {"artist": [(1.0, 2), (5.0, 1)], "label": [], "genre": [(1000.0, 3)]}

    @pytest.mark.parametrize("body", [{}, {**_DISTRIBUTIONS, "extra": []}, {**_DISTRIBUTIONS, "artist": [[1]]}])
    def test_rejects_a_body_that_is_not_the_distributions(self, test_client: TestClient, body: dict[str, Any]) -> None:
        assert test_client.post(_PAGE_PATH, json=body).status_code == 422

    @pytest.mark.parametrize("missing", ["_neo4j", "_pool"])
    def test_503_rather_than_scoring_without_a_database(self, test_client: TestClient, missing: str) -> None:
        """Without the pool every row would silently take the neutral community score."""
        with patch(f"api.routers.insights_compute.{missing}", None):
            assert test_client.post(_PAGE_PATH, json=_DISTRIBUTIONS).status_code == 503

    def test_503_when_community_counts_fail(self, test_client: TestClient) -> None:
        with patch("api.routers.insights_compute.fetch_rarity_page", new=AsyncMock(side_effect=psycopg.OperationalError("gone"))):
            assert test_client.post(_PAGE_PATH, json=_DISTRIBUTIONS).status_code == 503

    def test_503_on_a_page_that_overruns_its_budget(self, test_client: TestClient) -> None:
        async def _never(*_args: object, **_kwargs: object) -> dict[str, Any]:
            await asyncio.sleep(60)
            return {}

        with (
            patch("api.routers.insights_compute.fetch_rarity_page", new=_never),
            patch("api.routers.insights_compute.RARITY_PAGE_BUDGET_SECONDS", 0.01),
        ):
            assert test_client.post(_PAGE_PATH, json=_DISTRIBUTIONS).status_code == 503

    def test_a_whole_catalog_walk_is_not_rate_limited(self, test_client: TestClient) -> None:
        """Five requests a minute, the old route's limit, would stall a walk of 1,958 requests."""
        page = {"items": [], "next_after": None}
        with patch("api.routers.insights_compute.fetch_rarity_page", new=AsyncMock(return_value=page)):
            statuses = {test_client.post(_PAGE_PATH, json=_DISTRIBUTIONS).status_code for _ in range(30)}
        assert statuses == {200}

    def test_requires_the_internal_secret(self, test_client: TestClient) -> None:  # noqa: ARG002
        from api.api import app

        assert TestClient(app, raise_server_exceptions=False).post(_PAGE_PATH, json=_DISTRIBUTIONS).status_code == 401


def _disconnected_request() -> MagicMock:
    request = MagicMock()
    request.is_disconnected = AsyncMock(return_value=True)
    return request


class TestClientDisconnect:
    """A read whose client has gone stops, rather than finishing for nobody."""

    async def _blocked_read(self, started: asyncio.Event, stopped: asyncio.Event) -> dict[str, Any]:
        started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            stopped.set()
            raise
        return {}

    @pytest.mark.asyncio
    async def test_cancels_the_read_and_says_so(self) -> None:
        from api.routers.insights_compute import _ClientLeft, _unless_client_left

        started, stopped = asyncio.Event(), asyncio.Event()
        with patch("api.routers.insights_compute._DISCONNECT_POLL_SECONDS", 0.01), pytest.raises(_ClientLeft):
            await _unless_client_left(_disconnected_request(), self._blocked_read(started, stopped))
        assert stopped.is_set()

    @pytest.mark.asyncio
    async def test_a_page_read_records_499(self) -> None:
        from api.routers.insights_compute import _rarity_page_response

        started, stopped = asyncio.Event(), asyncio.Event()
        with patch("api.routers.insights_compute._DISCONNECT_POLL_SECONDS", 0.01):
            response = await _rarity_page_response(_disconnected_request(), self._blocked_read(started, stopped), "Rarity page")
        assert response.status_code == 499
        assert stopped.is_set()


def test_rarity_page_contract_publishes_what_the_producer_enforces() -> None:
    from api.queries.rarity_queries import RARITY_PAGE_BUDGET_SECONDS, RARITY_PAGE_SIZE

    contract = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    quality = contract["paths"][_QUALITY_PATH]["get"]
    page = contract["paths"][_PAGE_PATH]["post"]
    assert quality["x-groovemap-max-processing-seconds"] == page["x-groovemap-max-processing-seconds"] == RARITY_PAGE_BUDGET_SECONDS
    assert contract["components"]["parameters"]["RarityPageLimit"]["schema"]["maximum"] == RARITY_PAGE_SIZE
    assert "/api/internal/insights/rarity-scores" not in contract["paths"]


class TestAnniversariesValidation:
    """Tests for milestones query parameter validation (lines 82-83)."""

    def test_anniversaries_invalid_milestones_returns_422(self, test_client: TestClient) -> None:
        """Returns 422 when milestones contains non-integer values."""
        response = test_client.get("/api/internal/insights/anniversaries?year=2025&month=3&milestones=25,abc,50")
        assert response.status_code == 422
        assert "milestones" in response.json()["error"].lower()


class TestInternalInsightsAuth:
    """The /api/internal/insights/* router must not be anonymously reachable."""

    _PROTECTED_PATHS = (
        "/api/internal/insights/artist-centrality",
        "/api/internal/insights/genre-trends",
        "/api/internal/insights/style-year-counts",
        "/api/internal/insights/style-landmarks",
        "/api/internal/insights/style-cotags",
        "/api/internal/insights/label-longevity",
        "/api/internal/insights/anniversaries?year=2025&month=3",
        "/api/internal/insights/data-completeness",
        "/api/internal/insights/community-enrichment",
    )

    @staticmethod
    def _bare_client() -> TestClient:
        """A client WITHOUT the default X-Internal-Secret header, sharing app state."""
        from api.api import app

        return TestClient(app, raise_server_exceptions=False)

    def test_all_endpoints_reject_missing_secret(self, test_client: TestClient) -> None:  # noqa: ARG002
        """Without the shared secret every endpoint returns 401 — no anonymous access."""
        bare = self._bare_client()
        for path in self._PROTECTED_PATHS:
            response = bare.get(path)
            assert response.status_code == 401, f"{path} was reachable without the internal secret"

    def test_endpoint_rejects_wrong_secret(self, test_client: TestClient) -> None:  # noqa: ARG002
        """A wrong shared secret is rejected with 401."""
        bare = self._bare_client()
        response = bare.get(
            "/api/internal/insights/artist-centrality",
            headers={"X-Internal-Secret": "not-the-secret"},
        )
        assert response.status_code == 401

    def test_endpoint_accepts_valid_secret(self, test_client: TestClient) -> None:
        """The configured secret grants access (test_client sends it by default)."""
        items = [{"artist_id": "1", "artist_name": "Miles Davis", "edge_count": 500}]
        with patch("api.routers.insights_compute.query_artist_centrality", new_callable=AsyncMock, return_value=items):
            response = test_client.get("/api/internal/insights/artist-centrality")
        assert response.status_code == 200
        assert response.json() == {"items": items}

    def test_fails_closed_when_secret_not_configured(self, test_client: TestClient) -> None:  # noqa: ARG002
        """When no secret is configured the router fails closed with 503 (never open)."""
        import api.routers.insights_compute as mod

        original = mod._config
        mod._config = None
        try:
            bare = self._bare_client()
            response = bare.get("/api/internal/insights/artist-centrality")
            assert response.status_code == 503
        finally:
            mod._config = original
