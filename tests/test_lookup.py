"""Tests for GET /api/lookup/{provider}/{value} and its query layer (ADR 0011)."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from api.queries.lookup_queries import releases_for_native_id, resolve_alias_native_id
from api.routers.lookup import lookup_providers, normalize_lookup_value


NATIVE_ID = UUID("018f3f7a-0000-7000-8000-000000000001")

DISCOGS_ROW = {
    "id": "249504",
    "source": "discogs",
    "title": "Never Gonna Give You Up",
    "artist": "Rick Astley",
    "year": 1987,
    "media_families": ["vinyl"],
}
MUSICBRAINZ_ROW = {
    "id": "f4b7b1a0-0000-4000-8000-00000000000a",
    "source": "musicbrainz",
    "title": "Never Gonna Give You Up",
    "artist": None,
    "year": 1987,
    "media_families": ["vinyl"],
}


def _make_pool(*, fetchone: Any = None, fetchall: list[Any] | None = None) -> MagicMock:
    """A pool whose single cursor answers with the given rows."""
    cur = AsyncMock()
    cur.execute = AsyncMock()
    cur.fetchone = AsyncMock(return_value=fetchone)
    cur.fetchall = AsyncMock(return_value=fetchall or [])

    cur_ctx = AsyncMock()
    cur_ctx.__aenter__ = AsyncMock(return_value=cur)
    cur_ctx.__aexit__ = AsyncMock(return_value=False)

    conn = AsyncMock()
    conn.cursor = MagicMock(return_value=cur_ctx)

    conn_ctx = AsyncMock()
    conn_ctx.__aenter__ = AsyncMock(return_value=conn)
    conn_ctx.__aexit__ = AsyncMock(return_value=False)

    pool = MagicMock()
    pool.connection = MagicMock(return_value=conn_ctx)
    return pool


class TestLookupVocabulary:
    """The addressable providers come from the vendored vocabulary, not from this module."""

    def test_providers_are_the_three_alias_namespaces(self) -> None:
        assert lookup_providers() == ("barcode", "catalog_number", "matrix")

    def test_every_provider_is_derived_from_an_alias_bearing_type(self) -> None:
        """Each namespace the vocabulary declares is addressable, and nothing else is."""
        from common.identifiers import alias_identifier_types

        assert len(lookup_providers()) == len(alias_identifier_types())


class TestNormalization:
    """Each namespace's declared normalization, applied through the shared helper."""

    @pytest.mark.parametrize(
        ("provider", "value", "expected"),
        [
            ("barcode", "5 012394 144777", "5012394144777"),
            ("barcode", "5-012394-144777", "5012394144777"),
            ("barcode", "  5012394144777  ", "5012394144777"),
            ("catalog_number", "pb 41447", "PB 41447"),
            ("catalog_number", "  pb   41447 ", "PB 41447"),
            ("matrix", "  PB 41447-A2   UTOPIA MS  ", "PB 41447-A2 UTOPIA MS"),
        ],
    )
    def test_normalizes_per_namespace(self, provider: str, value: str, expected: str) -> None:
        assert normalize_lookup_value(provider, value) == expected

    def test_matrix_keeps_its_case(self) -> None:
        """The characters stamped into the disc are the evidence, so case survives."""
        assert normalize_lookup_value("matrix", "pb 41447-a2") == "pb 41447-a2"

    def test_value_that_normalizes_away_is_none(self) -> None:
        assert normalize_lookup_value("barcode", "---") is None

    def test_unknown_provider_raises(self) -> None:
        with pytest.raises(KeyError):
            normalize_lookup_value("rights_society", "BIEM")


class TestLookupQueries:
    """The two statements behind the endpoint."""

    @pytest.mark.asyncio
    async def test_resolve_alias_reads_only_the_valid_row(self) -> None:
        pool = _make_pool(fetchone={"native_id": NATIVE_ID})
        with patch("api.queries.lookup_queries.execute_sql", new_callable=AsyncMock) as mock_exec:
            resolved = await resolve_alias_native_id(pool, "barcode", "5012394144777")

        assert resolved == NATIVE_ID
        statement, params = mock_exec.call_args[0][1], mock_exec.call_args[0][2]
        assert "valid_to IS NULL" in statement
        assert params == ("barcode", "release", "5012394144777")

    @pytest.mark.asyncio
    async def test_resolve_alias_returns_none_when_no_row(self) -> None:
        pool = _make_pool(fetchone=None)
        with patch("api.queries.lookup_queries.execute_sql", new_callable=AsyncMock):
            assert await resolve_alias_native_id(pool, "barcode", "0000000000000") is None

    @pytest.mark.asyncio
    async def test_releases_union_covers_both_catalogs(self) -> None:
        rows = [
            {"id": "249504", "source": "discogs", "title": "A", "artist": "B", "year": "1987", "media_families": ["vinyl"]},
            {
                "id": "f4b7b1a0-0000-4000-8000-00000000000a",
                "source": "musicbrainz",
                "title": "A",
                "artist": None,
                "year": "1987-07-27",
                "media_families": ["vinyl"],
            },
        ]
        pool = _make_pool(fetchall=rows)
        with patch("api.queries.lookup_queries.execute_sql", new_callable=AsyncMock) as mock_exec:
            found = await releases_for_native_id(pool, NATIVE_ID)

        statement = mock_exec.call_args[0][1]
        assert "FROM releases" in statement
        assert "FROM musicbrainz.releases" in statement
        assert mock_exec.call_args[0][2] == (NATIVE_ID, NATIVE_ID)
        assert [row["source"] for row in found] == ["discogs", "musicbrainz"]
        assert [row["year"] for row in found] == [1987, 1987]

    @pytest.mark.asyncio
    async def test_release_without_media_block_carries_an_empty_family_list(self) -> None:
        rows = [{"id": "1", "source": "discogs", "title": "A", "artist": None, "year": None, "media_families": None}]
        pool = _make_pool(fetchall=rows)
        with patch("api.queries.lookup_queries.execute_sql", new_callable=AsyncMock):
            found = await releases_for_native_id(pool, NATIVE_ID)

        assert found[0]["media_families"] == []
        assert found[0]["year"] is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("raw", ["0", "", "n/a", None])
    async def test_unusable_year_degrades_to_none(self, raw: Any) -> None:
        rows = [{"id": "1", "source": "discogs", "title": "A", "artist": None, "year": raw, "media_families": []}]
        pool = _make_pool(fetchall=rows)
        with patch("api.queries.lookup_queries.execute_sql", new_callable=AsyncMock):
            found = await releases_for_native_id(pool, NATIVE_ID)

        assert found[0]["year"] is None


def _patched_lookup(native_id: UUID | None, releases: list[dict[str, Any]]) -> Any:
    """Patch both query functions with the given answer."""
    return (
        patch("api.routers.lookup.resolve_alias_native_id", AsyncMock(return_value=native_id)),
        patch("api.routers.lookup.releases_for_native_id", AsyncMock(return_value=releases)),
    )


class TestLookupEndpoint:
    """Router behaviour for GET /api/lookup/{provider}/{value}."""

    @pytest.mark.parametrize(
        ("provider", "value", "normalized"),
        [
            ("barcode", "5 012394 144777", "5012394144777"),
            ("catalog_number", "pb 41447", "PB 41447"),
            ("matrix", "PB 41447-A2 UTOPIA MS", "PB 41447-A2 UTOPIA MS"),
        ],
    )
    def test_each_provider_resolves(self, test_client: TestClient, provider: str, value: str, normalized: str) -> None:
        resolve, fetch = _patched_lookup(NATIVE_ID, [DISCOGS_ROW])
        with resolve as mock_resolve, fetch:
            response = test_client.get(f"/api/lookup/{provider}/{value}")

        assert response.status_code == 200
        body = response.json()
        assert body["provider"] == provider
        assert body["value"] == value
        assert body["normalized"] == normalized
        assert body["gm_id"] == str(NATIVE_ID)
        assert body["releases"] == [DISCOGS_ROW]
        assert mock_resolve.await_args[0][1:] == (provider, normalized)

    def test_one_value_can_name_releases_in_both_catalogs(self, test_client: TestClient) -> None:
        """A barcode is printed on the object, and both catalogs describe that object."""
        resolve, fetch = _patched_lookup(NATIVE_ID, [DISCOGS_ROW, MUSICBRAINZ_ROW])
        with resolve, fetch:
            response = test_client.get("/api/lookup/barcode/5012394144777")

        assert response.status_code == 200
        body = response.json()
        assert [release["source"] for release in body["releases"]] == ["discogs", "musicbrainz"]
        assert body["releases"][1]["artist"] is None

    def test_unknown_value_is_404(self, test_client: TestClient) -> None:
        resolve, fetch = _patched_lookup(None, [])
        with resolve, fetch:
            response = test_client.get("/api/lookup/barcode/0000000000000")

        assert response.status_code == 404
        assert "barcode" in response.json()["error"]

    def test_alias_with_no_loaded_release_is_404(self, test_client: TestClient) -> None:
        """A dangling alias is the same answer: nothing to show the caller."""
        resolve, fetch = _patched_lookup(NATIVE_ID, [])
        with resolve, fetch:
            response = test_client.get("/api/lookup/barcode/5012394144777")

        assert response.status_code == 404

    def test_value_that_normalizes_away_is_404_without_a_query(self, test_client: TestClient) -> None:
        resolve, fetch = _patched_lookup(NATIVE_ID, [DISCOGS_ROW])
        with resolve as mock_resolve, fetch:
            response = test_client.get("/api/lookup/barcode/---")

        assert response.status_code == 404
        mock_resolve.assert_not_awaited()

    def test_unminted_namespace_is_400(self, test_client: TestClient) -> None:
        response = test_client.get("/api/lookup/rights_society/BIEM")
        assert response.status_code == 400
        assert "barcode" in response.json()["error"]

    def test_503_when_pool_not_ready(self, test_client: TestClient) -> None:
        import api.routers.lookup as lookup_router

        original = lookup_router._pool
        try:
            lookup_router._pool = None
            response = test_client.get("/api/lookup/barcode/5012394144777")
        finally:
            lookup_router._pool = original

        assert response.status_code == 503

    def test_lookup_is_public(self, test_client: TestClient) -> None:
        """No credentials, and no challenge: a person in a shop cannot sign in."""
        resolve, fetch = _patched_lookup(NATIVE_ID, [DISCOGS_ROW])
        with resolve, fetch:
            response = test_client.get("/api/lookup/barcode/5012394144777")

        assert response.status_code == 200
        assert "WWW-Authenticate" not in response.headers


class TestLookupActivity:
    """ADR 0010: a lookup is recorded as the search it is, for a caller with a subject."""

    def test_anonymous_lookup_records_nothing(self, test_client: TestClient) -> None:
        resolve, fetch = _patched_lookup(NATIVE_ID, [DISCOGS_ROW])
        with resolve, fetch, patch("api.activity.record_event", new_callable=AsyncMock) as mock_record:
            test_client.get("/api/lookup/barcode/5012394144777")

        mock_record.assert_not_awaited()

    def test_signed_in_lookup_records_a_search_query(self, test_client: TestClient, auth_headers: dict[str, str]) -> None:
        resolve, fetch = _patched_lookup(NATIVE_ID, [DISCOGS_ROW])
        with resolve, fetch, patch("api.activity.record_event", new_callable=AsyncMock) as mock_record:
            response = test_client.get("/api/lookup/barcode/5 012394 144777", headers=auth_headers)

        assert response.status_code == 200
        mock_record.assert_awaited_once()
        _user_id, event_type, payload = mock_record.await_args[0]
        assert event_type == "search.query"
        assert payload["filters"] == ["lookup:barcode"]
        assert payload["query"] == "5 012394 144777"
        assert payload["result_count"] == 1

    def test_a_miss_is_recorded_too(self, test_client: TestClient, auth_headers: dict[str, str]) -> None:
        """A barcode the catalog cannot resolve is the most useful thing this surface learns."""
        resolve, fetch = _patched_lookup(None, [])
        with resolve, fetch, patch("api.activity.record_event", new_callable=AsyncMock) as mock_record:
            response = test_client.get("/api/lookup/barcode/0000000000000", headers=auth_headers)

        assert response.status_code == 404
        mock_record.assert_awaited_once()
        assert mock_record.await_args[0][2]["result_count"] == 0
