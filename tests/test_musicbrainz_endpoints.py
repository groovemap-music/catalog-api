"""Tests for MusicBrainz enrichment API endpoints."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from api.queries.musicbrainz_pipeline import MusicBrainzHandles
from api.queries.musicbrainz_queries import (
    get_artist_external_links,
    get_artist_mb_relationships,
    get_artist_musicbrainz,
    get_enrichment_status,
)


# ---------------------------------------------------------------------------
# Helper: build a mock Neo4j driver with preconfigured session results
# ---------------------------------------------------------------------------


def _make_neo4j_driver(data_return: list | None = None) -> MagicMock:
    """Create a mock Neo4j driver compatible with run_single/run_query helpers."""
    rows = data_return if data_return is not None else []

    mock_result = AsyncMock()
    # run_single uses result.single() -> dict(record)
    mock_result.single = AsyncMock(return_value=rows[0] if rows else None)
    mock_result.consume = AsyncMock(return_value=MagicMock())

    # run_query uses `async for record in result` -> dict(record)
    async def _aiter_records() -> Any:
        for r in rows:
            yield r

    mock_result.__aiter__ = lambda _self: _aiter_records()

    mock_session = AsyncMock()
    mock_session.run = AsyncMock(return_value=mock_result)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    driver = MagicMock()
    driver.session = MagicMock(return_value=mock_session)
    return driver


def _handles(graph: Any, relational: Any | None = None) -> MusicBrainzHandles:
    return MusicBrainzHandles(graph=graph, relational=relational if relational is not None else MagicMock())


# ===========================================================================
# Endpoint tests (via TestClient)
# ===========================================================================


class TestArtistMusicbrainzEndpoint:
    """GET /api/artist/{id}/musicbrainz"""

    def test_get_artist_musicbrainz_found(self, test_client: TestClient) -> None:
        mb_data = {
            "discogs_id": 42,
            "mbid": "abc-123",
            "type": "Person",
            "gender": "Male",
            "begin_date": "1970-01-01",
            "end_date": None,
            "area": "United Kingdom",
            "begin_area": "London",
            "disambiguation": "singer",
        }
        backend = AsyncMock()
        backend.get_artist_musicbrainz.return_value = mb_data
        with patch("api.routers.musicbrainz.get_musicbrainz_backend", return_value=backend):
            resp = test_client.get("/api/artist/42/musicbrainz")
        assert resp.status_code == 200
        body = resp.json()
        assert body["mbid"] == "abc-123"
        assert body["discogs_id"] == 42
        assert body["type"] == "Person"

    def test_get_artist_musicbrainz_not_found(self, test_client: TestClient) -> None:
        backend = AsyncMock()
        backend.get_artist_musicbrainz.return_value = None
        with patch("api.routers.musicbrainz.get_musicbrainz_backend", return_value=backend):
            resp = test_client.get("/api/artist/999/musicbrainz")
        assert resp.status_code == 404
        assert "No MusicBrainz data" in resp.json()["detail"]

    def test_get_artist_musicbrainz_service_unavailable(self, test_client: TestClient) -> None:
        with patch("api.routers.musicbrainz._neo4j_driver", None):
            resp = test_client.get("/api/artist/1/musicbrainz")
        assert resp.status_code == 503

    def test_postgres_backend_does_not_require_neo4j(self, test_client: TestClient) -> None:
        import api.routers.musicbrainz as router

        backend = AsyncMock()
        backend.get_artist_musicbrainz.return_value = {
            "discogs_id": "1",
            "mbid": "99999999-0000-0000-0000-000000000001",
        }
        original_backend = router._graph_backend
        original_driver = router._neo4j_driver
        router._graph_backend = "postgres"
        router._neo4j_driver = None
        try:
            with patch("api.routers.musicbrainz.get_musicbrainz_backend", return_value=backend):
                resp = test_client.get("/api/artist/1/musicbrainz")
            assert resp.status_code == 200
            backend.get_artist_musicbrainz.assert_awaited_once()
        finally:
            router._graph_backend = original_backend
            router._neo4j_driver = original_driver


class TestArtistRelationshipsEndpoint:
    """GET /api/artist/{id}/relationships"""

    def test_get_artist_relationships_found(self, test_client: TestClient) -> None:
        rels = [
            {
                "type": "MEMBER_OF",
                "target_id": 100,
                "target_name": "Some Band",
                "direction": "outgoing",
                "begin_date": "1990",
                "end_date": "2000",
                "attributes": ["vocals"],
            }
        ]
        backend = AsyncMock()
        backend.get_artist_mb_relationships.return_value = rels
        with patch("api.routers.musicbrainz.get_musicbrainz_backend", return_value=backend):
            resp = test_client.get("/api/artist/42/relationships")
        assert resp.status_code == 200
        body = resp.json()
        assert body["discogs_id"] == 42
        assert len(body["relationships"]) == 1
        assert body["relationships"][0]["type"] == "MEMBER_OF"

    def test_get_artist_relationships_empty(self, test_client: TestClient) -> None:
        backend = AsyncMock()
        backend.get_artist_mb_relationships.return_value = []
        with patch("api.routers.musicbrainz.get_musicbrainz_backend", return_value=backend):
            resp = test_client.get("/api/artist/42/relationships")
        assert resp.status_code == 200
        assert resp.json()["relationships"] == []

    def test_get_artist_relationships_service_unavailable(self, test_client: TestClient) -> None:
        with patch("api.routers.musicbrainz._neo4j_driver", None):
            resp = test_client.get("/api/artist/1/relationships")
        assert resp.status_code == 503


class TestExternalLinksEndpoint:
    """GET /api/artist/{id}/external-links"""

    def test_get_external_links_found(self, test_client: TestClient) -> None:
        links = [
            {"service": "wikipedia", "url": "https://en.wikipedia.org/wiki/Artist"},
            {"service": "wikidata", "url": "https://www.wikidata.org/wiki/Q123"},
        ]
        backend = AsyncMock()
        backend.get_artist_external_links.return_value = links
        with patch("api.routers.musicbrainz.get_musicbrainz_backend", return_value=backend):
            resp = test_client.get("/api/artist/42/external-links")
        assert resp.status_code == 200
        body = resp.json()
        assert body["discogs_id"] == 42
        assert len(body["links"]) == 2
        assert body["links"][0]["service"] == "wikipedia"

    def test_get_external_links_empty(self, test_client: TestClient) -> None:
        backend = AsyncMock()
        backend.get_artist_external_links.return_value = []
        with patch("api.routers.musicbrainz.get_musicbrainz_backend", return_value=backend):
            resp = test_client.get("/api/artist/42/external-links")
        assert resp.status_code == 200
        assert resp.json()["links"] == []

    def test_get_external_links_service_unavailable(self, test_client: TestClient) -> None:
        with patch("api.routers.musicbrainz._pool", None):
            resp = test_client.get("/api/artist/1/external-links")
        assert resp.status_code == 503


class TestEnrichmentStatusEndpoint:
    """GET /api/enrichment/status"""

    def test_enrichment_status(self, test_client: TestClient) -> None:
        stats = {
            "musicbrainz": {
                "artists": {"total_mb": 100, "matched_to_discogs": 80, "enriched_in_neo4j": 75},
                "labels": {"total_mb": 50, "matched_to_discogs": 30, "enriched_in_neo4j": 25},
                "releases": {"total_mb": 200, "matched_to_discogs": 150, "enriched_in_neo4j": 140},
                "relationships": {"total_in_mb": 500, "created_in_neo4j": 450},
            }
        }
        backend = AsyncMock()
        backend.get_enrichment_status.return_value = stats
        with patch("api.routers.musicbrainz.get_musicbrainz_backend", return_value=backend):
            resp = test_client.get("/api/enrichment/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["musicbrainz"]["artists"]["total_mb"] == 100

    def test_enrichment_status_service_unavailable_no_pool(self, test_client: TestClient) -> None:
        with patch("api.routers.musicbrainz._pool", None):
            resp = test_client.get("/api/enrichment/status")
        assert resp.status_code == 503

    def test_enrichment_status_service_unavailable_no_driver(self, test_client: TestClient) -> None:
        with patch("api.routers.musicbrainz._neo4j_driver", None):
            resp = test_client.get("/api/enrichment/status")
        assert resp.status_code == 503


# ===========================================================================
# Query function unit tests
# ===========================================================================


class TestGetArtistMusicbrainzQuery:
    """Unit tests for get_artist_musicbrainz()."""

    @pytest.mark.anyio
    async def test_returns_data(self) -> None:
        row = {
            "mbid": "abc-123",
            "type": "Person",
            "gender": "Male",
            "begin_date": "1970-01-01",
            "end_date": None,
            "area": "UK",
            "begin_area": "London",
            "disambiguation": "",
        }
        driver = _make_neo4j_driver([row])
        result = await get_artist_musicbrainz(_handles(driver), 42)
        assert result is not None
        assert result["discogs_id"] == 42
        assert result["mbid"] == "abc-123"

    @pytest.mark.anyio
    async def test_returns_none(self) -> None:
        driver = _make_neo4j_driver([])
        result = await get_artist_musicbrainz(_handles(driver), 999)
        assert result is None


class TestGetArtistMbRelationshipsQuery:
    """Unit tests for get_artist_mb_relationships()."""

    @pytest.mark.anyio
    async def test_returns_relationships(self) -> None:
        rels = [
            {
                "type": "MEMBER_OF",
                "target_id": 100,
                "target_name": "Band",
                "direction": "outgoing",
                "begin_date": None,
                "end_date": None,
                "attributes": None,
            }
        ]
        driver = _make_neo4j_driver(rels)
        result = await get_artist_mb_relationships(_handles(driver), 42)
        assert len(result) == 1
        assert result[0]["type"] == "MEMBER_OF"

    @pytest.mark.anyio
    async def test_returns_empty(self) -> None:
        driver = _make_neo4j_driver([])
        result = await get_artist_mb_relationships(_handles(driver), 42)
        assert result == []


class TestGetArtistExternalLinksQuery:
    """Unit tests for get_artist_external_links()."""

    @pytest.mark.anyio
    async def test_returns_links(self) -> None:
        mock_cur = AsyncMock()
        mock_cur.fetchall = AsyncMock(return_value=[("wikipedia", "https://example.com")])
        mock_cur.execute = AsyncMock()
        cur_ctx = AsyncMock()
        cur_ctx.__aenter__ = AsyncMock(return_value=mock_cur)
        cur_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_conn = AsyncMock()
        mock_conn.cursor = MagicMock(return_value=cur_ctx)
        conn_ctx = AsyncMock()
        conn_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
        conn_ctx.__aexit__ = AsyncMock(return_value=False)
        pool = MagicMock()
        pool.connection = MagicMock(return_value=conn_ctx)

        result = await get_artist_external_links(_handles(object(), pool), 42)
        assert len(result) == 1
        assert result[0]["service"] == "wikipedia"

    @pytest.mark.anyio
    async def test_returns_empty_links(self) -> None:
        mock_cur = AsyncMock()
        mock_cur.fetchall = AsyncMock(return_value=[])
        mock_cur.execute = AsyncMock()
        cur_ctx = AsyncMock()
        cur_ctx.__aenter__ = AsyncMock(return_value=mock_cur)
        cur_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_conn = AsyncMock()
        mock_conn.cursor = MagicMock(return_value=cur_ctx)
        conn_ctx = AsyncMock()
        conn_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
        conn_ctx.__aexit__ = AsyncMock(return_value=False)
        pool = MagicMock()
        pool.connection = MagicMock(return_value=conn_ctx)

        result = await get_artist_external_links(_handles(object(), pool), 999)
        assert result == []


class TestGetEnrichmentStatusQuery:
    """Unit tests for get_enrichment_status()."""

    @pytest.mark.anyio
    async def test_returns_stats(self) -> None:
        # Mock PostgreSQL pool
        fetchone_values = [
            (100,),  # artists total
            (80,),  # artists matched
            (50,),  # labels total
            (30,),  # labels matched
            (200,),  # releases total
            (150,),  # releases matched
            (500,),  # relationships total
        ]
        mock_cur = AsyncMock()
        mock_cur.fetchone = AsyncMock(side_effect=fetchone_values)
        mock_cur.execute = AsyncMock()
        cur_ctx = AsyncMock()
        cur_ctx.__aenter__ = AsyncMock(return_value=mock_cur)
        cur_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_conn = AsyncMock()
        mock_conn.cursor = MagicMock(return_value=cur_ctx)
        conn_ctx = AsyncMock()
        conn_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
        conn_ctx.__aexit__ = AsyncMock(return_value=False)
        pool = MagicMock()
        pool.connection = MagicMock(return_value=conn_ctx)

        # Mock Neo4j driver - needs to return multiple results for run_single
        neo4j_single_results = [
            {"total": 75},  # artists enriched
            {"total": 25},  # labels enriched
            {"total": 140},  # releases enriched
            {"total": 450},  # relationships created
        ]
        mock_session = AsyncMock()
        call_idx = {"n": 0}

        async def mock_run(*_args: object, **_kwargs: object) -> AsyncMock:
            idx = call_idx["n"]
            call_idx["n"] += 1
            result = AsyncMock()
            result.single = AsyncMock(return_value=neo4j_single_results[idx])
            result.consume = AsyncMock(return_value=MagicMock())
            return result

        mock_session.run = mock_run
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        driver = MagicMock()
        driver.session = MagicMock(return_value=mock_session)

        stats = await get_enrichment_status(_handles(driver, pool))
        assert stats["musicbrainz"]["artists"]["total_mb"] == 100
        assert stats["musicbrainz"]["artists"]["matched_to_discogs"] == 80
        assert stats["musicbrainz"]["artists"]["enriched_in_neo4j"] == 75
        assert stats["musicbrainz"]["relationships"]["total_in_mb"] == 500
        assert stats["musicbrainz"]["relationships"]["created_in_neo4j"] == 450


# ===========================================================================
# Configure function test
# ===========================================================================


class TestConfigure:
    """Test configure() sets module-level state."""

    def test_configure_sets_pool_and_driver(self) -> None:
        import api.routers.musicbrainz as mb_router

        mock_pool = MagicMock()
        mock_driver = MagicMock()
        mb_router.configure(mock_pool, mock_driver, "postgres")
        assert mb_router._pool is mock_pool
        assert mb_router._neo4j_driver is mock_driver
        assert mb_router._graph_backend == "postgres"
