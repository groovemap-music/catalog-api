"""Tests for GET /api/search endpoint."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from psycopg import sql


def _transaction() -> MagicMock:
    """An async context manager standing in for ``conn.transaction()`` (search statements run inside one)."""
    transaction = MagicMock()
    transaction.__aenter__ = AsyncMock(return_value=None)
    transaction.__aexit__ = AsyncMock(return_value=False)
    return transaction


class TestSearchEndpointBasic:
    """Basic search endpoint behaviour."""

    def test_search_returns_200_with_results(self, test_client: TestClient) -> None:
        expected_response = {
            "query": "blue",
            "total": 1,
            "facets": {"type": {"artist": 1}, "genre": {}, "decade": {}},
            "results": [
                {
                    "type": "artist",
                    "id": "123",
                    "name": "Blue Note",
                    "highlight": "<em>Blue</em> Note",
                    "relevance": 0.95,
                    "metadata": {},
                }
            ],
            "pagination": {"limit": 20, "offset": 0, "has_more": False},
        }
        with patch("api.routers.search.execute_search", return_value=expected_response):
            response = test_client.get("/api/search?q=blue")

        assert response.status_code == 200
        data = response.json()
        assert data["query"] == "blue"
        assert data["total"] == 1
        assert len(data["results"]) == 1
        assert data["results"][0]["name"] == "Blue Note"
        assert "facets" in data
        assert "pagination" in data

    def test_search_503_when_pool_not_ready(self, test_client: TestClient) -> None:
        import api.routers.search as search_router

        original_pool = search_router._pool
        try:
            search_router._pool = None
            response = test_client.get("/api/search?q=blue")
        finally:
            search_router._pool = original_pool

        assert response.status_code == 503

    def test_search_422_when_query_too_short(self, test_client: TestClient) -> None:
        response = test_client.get("/api/search?q=ab")
        assert response.status_code == 422  # FastAPI validation rejects min_length=3

    def test_search_400_with_invalid_type(self, test_client: TestClient) -> None:
        with patch("api.routers.search.execute_search", new_callable=AsyncMock):
            response = test_client.get("/api/search?q=blue&types=invalid")
        assert response.status_code == 400
        assert "invalid" in response.json()["error"].lower()

    def test_search_default_params(self, test_client: TestClient) -> None:
        """Defaults: all 4 types, limit=20, offset=0, no genres, no year filters."""
        captured: dict[str, Any] = {}

        async def _capture(**kwargs: Any) -> dict[str, Any]:
            captured.update(kwargs)
            return {
                "query": "blue",
                "total": 0,
                "facets": {"type": {}, "genre": {}, "decade": {}},
                "results": [],
                "pagination": {"limit": 20, "offset": 0, "has_more": False},
            }

        with patch("api.routers.search.execute_search", side_effect=_capture):
            test_client.get("/api/search?q=blue")

        assert set(captured["types"]) == {"artist", "label", "master", "release"}
        assert captured["limit"] == 20
        assert captured["offset"] == 0
        assert captured["genres"] == []
        assert captured["year_min"] is None
        assert captured["year_max"] is None


class TestSearchFiltering:
    """Filter parameters forwarded correctly to execute_search."""

    def test_types_filter_forwarded(self, test_client: TestClient) -> None:
        captured: dict[str, Any] = {}

        async def _capture(**kwargs: Any) -> dict[str, Any]:
            captured.update(kwargs)
            return {
                "query": "blue",
                "total": 0,
                "facets": {"type": {}, "genre": {}, "decade": {}},
                "results": [],
                "pagination": {"limit": 20, "offset": 0, "has_more": False},
            }

        with patch("api.routers.search.execute_search", side_effect=_capture):
            test_client.get("/api/search?q=blue&types=artist,label")

        assert set(captured["types"]) == {"artist", "label"}

    def test_year_range_forwarded(self, test_client: TestClient) -> None:
        captured: dict[str, Any] = {}

        async def _capture(**kwargs: Any) -> dict[str, Any]:
            captured.update(kwargs)
            return {
                "query": "blue",
                "total": 0,
                "facets": {"type": {}, "genre": {}, "decade": {}},
                "results": [],
                "pagination": {"limit": 20, "offset": 0, "has_more": False},
            }

        with patch("api.routers.search.execute_search", side_effect=_capture):
            test_client.get("/api/search?q=blue&year_min=1960&year_max=1980")

        assert captured["year_min"] == 1960
        assert captured["year_max"] == 1980

    def test_genres_filter_forwarded(self, test_client: TestClient) -> None:
        captured: dict[str, Any] = {}

        async def _capture(**kwargs: Any) -> dict[str, Any]:
            captured.update(kwargs)
            return {
                "query": "blue",
                "total": 0,
                "facets": {"type": {}, "genre": {}, "decade": {}},
                "results": [],
                "pagination": {"limit": 20, "offset": 0, "has_more": False},
            }

        with patch("api.routers.search.execute_search", side_effect=_capture):
            test_client.get("/api/search?q=blue&genres=Jazz,Rock")

        assert set(captured["genres"]) == {"Jazz", "Rock"}

    def test_pagination_forwarded(self, test_client: TestClient) -> None:
        captured: dict[str, Any] = {}

        async def _capture(**kwargs: Any) -> dict[str, Any]:
            captured.update(kwargs)
            return {
                "query": "blue",
                "total": 0,
                "facets": {"type": {}, "genre": {}, "decade": {}},
                "results": [],
                "pagination": {"limit": 10, "offset": 20, "has_more": False},
            }

        with patch("api.routers.search.execute_search", side_effect=_capture):
            test_client.get("/api/search?q=blue&limit=10&offset=20")

        assert captured["limit"] == 10
        assert captured["offset"] == 20


class TestSearchResponseShape:
    """Response structure validation."""

    def test_response_shape_complete(self, test_client: TestClient) -> None:
        """Response must contain query, total, facets.{type,genre,decade}, results, pagination."""
        expected = {
            "query": "blue",
            "total": 0,
            "facets": {"type": {}, "genre": {}, "decade": {}},
            "results": [],
            "pagination": {"limit": 20, "offset": 0, "has_more": False},
        }
        with patch("api.routers.search.execute_search", return_value=expected):
            response = test_client.get("/api/search?q=blue")

        data = response.json()
        assert "query" in data
        assert "total" in data
        assert "facets" in data
        assert "type" in data["facets"]
        assert "genre" in data["facets"]
        assert "decade" in data["facets"]
        assert "results" in data
        assert "pagination" in data
        assert "limit" in data["pagination"]
        assert "offset" in data["pagination"]
        assert "has_more" in data["pagination"]


class TestSearchQueryModuleHelpers:
    """Unit tests for search_queries helper functions (no router needed)."""

    def test_cache_key_is_stable(self) -> None:
        from api.queries.search_queries import cache_key

        k1 = cache_key("blue", ["artist", "label"], [], None, None, 20, 0)
        k2 = cache_key("blue", ["label", "artist"], [], None, None, 20, 0)
        assert k1 == k2, "Order of types should not affect cache key"

    def test_cache_key_differs_on_query(self) -> None:
        from api.queries.search_queries import cache_key

        k1 = cache_key("blue", ["artist"], [], None, None, 20, 0)
        k2 = cache_key("red", ["artist"], [], None, None, 20, 0)
        assert k1 != k2

    def test_cache_key_differs_on_offset(self) -> None:
        from api.queries.search_queries import cache_key

        k1 = cache_key("blue", ["artist"], [], None, None, 20, 0)
        k2 = cache_key("blue", ["artist"], [], None, None, 20, 20)
        assert k1 != k2

    def test_format_result_artist(self) -> None:
        from api.queries.search_queries import _format_result

        row = {"type": "artist", "id": "1", "name": "Blue Note", "rank": 0.9, "highlight": "<em>Blue</em> Note", "year": None, "genres": None}
        result = _format_result(row)
        assert result["type"] == "artist"
        assert result["id"] == "1"
        assert result["name"] == "Blue Note"
        assert result["relevance"] == 0.9
        assert result["metadata"] == {}

    def test_entity_select_without_per_table_limit(self) -> None:
        from api.queries.search_queries import _entity_select

        frag, params = _entity_select("artist", "name", has_year=False, has_genres=False)
        rendered = frag.as_string(None)
        assert "LIMIT" not in rendered
        assert params == []

    def test_entity_select_with_per_table_limit(self) -> None:
        from api.queries.search_queries import _entity_select

        frag, params = _entity_select("artist", "name", has_year=False, has_genres=False, per_table_limit=50)
        rendered = frag.as_string(None)
        assert "ORDER BY rank DESC, data_id LIMIT 50" in rendered
        assert params == []

    def test_unfiltered_release_select_ranks_the_stored_title_vector(self) -> None:
        """Unfiltered releases rank public.release_search.title_tsv, so no document is read per match."""
        from api.queries.search_queries import _entity_select

        frag, params = _entity_select("release", "title", has_year=True, has_genres=True, per_table_limit=40)
        rendered = frag.as_string(None)
        assert "FROM public.release_search" in rendered
        assert "ts_rank(title_tsv, q.tsq)" in rendered
        assert "to_tsvector" not in rendered
        assert "ORDER BY rank DESC, data_id LIMIT 40" in rendered
        assert params == []

    def test_filtered_release_select_filters_the_stored_attributes(self) -> None:
        """A filtered releases branch ranks and filters public.release_search; the document is read only after the cap."""
        from api.queries.search_queries import _entity_select

        frag, params = _entity_select(
            "release",
            "title",
            has_year=True,
            has_genres=True,
            has_media=True,
            has_country=True,
            per_table_limit=40,
            year_min=1960,
            genres=["Rock"],
            media_families=["vinyl"],
            media_mediums=["CD"],
            countries=["UK"],
        )
        rendered = frag.as_string(None)
        ranked = rendered[rendered.index("FROM q, (") : rendered.index("ORDER BY rank DESC, data_id LIMIT 40")]
        assert "FROM public.release_search" in ranked
        assert "to_tsvector" not in rendered
        assert "title_tsv @@ q.tsq AND (year IS NULL OR year::int >= %s)" in ranked
        assert "(genres IS NULL OR genres ?| %s::text[])" in ranked
        assert "media_mediums IS NULL OR (media_families ?| %s::text[] OR media_mediums ? %s::text)" in ranked
        assert "(country IS NULL OR country = ANY(%s::text[]))" in ranked
        assert "data->" not in ranked
        assert "jsonb_path_exists" not in ranked
        assert params == [1960, ["Rock"], ["vinyl"], "CD", ["UK"]]

    def test_other_entity_selects_never_use_release_search(self) -> None:
        from api.queries.search_queries import _entity_select

        for entity, field in (("artist", "name"), ("label", "name")):
            frag, _ = _entity_select(entity, field, has_year=False, has_genres=False, per_table_limit=40)
            assert "release_search" not in frag.as_string(None)

    def test_master_select_ranks_the_stored_title_vector(self) -> None:
        from api.queries.search_queries import _entity_select

        frag, params = _entity_select("master", "title", has_year=True, has_genres=False, per_table_limit=40)
        rendered = frag.as_string(None)
        assert "FROM public.master_search" in rendered
        assert "ts_rank(title_tsv, q.tsq)" in rendered
        assert "to_tsvector" not in rendered
        assert params == []

    def test_filtered_master_select_filters_on_the_year_column(self) -> None:
        """The year filter reads master_search.year before the cap; genre/media/country params are dropped (TRUE for masters)."""
        from api.queries.search_queries import _entity_select

        frag, params = _entity_select(
            "master", "title", has_year=True, has_genres=False, per_table_limit=40, year_min=1960, year_max=1969, genres=["Rock"]
        )
        rendered = frag.as_string(None)
        ranked = rendered[: rendered.index("ORDER BY rank DESC")]
        assert "public.master_search" in ranked
        assert "(year IS NULL OR year::int >= %s)" in ranked
        assert "to_tsvector" not in rendered
        assert params == [1960, 1969]

    def test_master_count_union_and_type_counts_use_master_search(self) -> None:
        from api.queries.search_queries import _build_count_union

        plain, p0 = _build_count_union(["master"])
        assert "public.master_search" in plain.as_string(None)
        assert "to_tsvector" not in plain.as_string(None)
        assert p0 == []
        filtered, p1 = _build_count_union(["master"], year_min=1990, genres=["Rock"])
        assert "year::int >= %s" in filtered.as_string(None)
        assert p1 == [1990]

    def test_count_union_uses_release_search_for_every_releases_branch(self) -> None:
        from api.queries.search_queries import _build_count_union

        plain, _ = _build_count_union(["release"])
        assert "public.release_search" in plain.as_string(None)
        assert "to_tsvector" not in plain.as_string(None)
        filtered, _ = _build_count_union(["release"], year_min=1960, media_mediums=["CD"])
        rendered = filtered.as_string(None)
        assert "public.release_search" in rendered
        assert "to_tsvector" not in rendered
        assert "data->" not in rendered
        assert "media_mediums ?" in rendered
        artists, _ = _build_count_union(["artist"])
        assert "release_search" not in artists.as_string(None)

    def test_entity_select_pushes_year_filter_before_limit(self) -> None:
        """Year filter must land in the subquery WHERE,
        BEFORE its ORDER BY rank LIMIT — not only in an outer WHERE applied
        after the per-table cap.
        """
        from api.queries.search_queries import _entity_select

        frag, params = _entity_select("release", "title", has_year=True, has_genres=True, per_table_limit=40, year_min=1960, year_max=1969)
        rendered = frag.as_string(None)
        where_idx = rendered.index("WHERE")
        limit_idx = rendered.index("ORDER BY rank DESC, data_id LIMIT")
        assert where_idx < limit_idx
        # the year predicate text must appear before the LIMIT, not after
        assert "year" in rendered[where_idx:limit_idx].lower()
        assert params == [1960, 1969]

    def test_entity_select_pushes_genre_filter_before_limit(self) -> None:
        from api.queries.search_queries import _entity_select

        frag, params = _entity_select("release", "title", has_year=True, has_genres=True, per_table_limit=40, genres=["Rock", "Jazz"])
        rendered = frag.as_string(None)
        where_idx = rendered.index("WHERE")
        limit_idx = rendered.index("ORDER BY rank DESC, data_id LIMIT")
        assert "?|" in rendered[where_idx:limit_idx]
        assert params == [["Rock", "Jazz"]]

    def test_entity_select_no_filter_clause_without_filters(self) -> None:
        from api.queries.search_queries import _entity_select

        frag, params = _entity_select("artist", "name", has_year=False, has_genres=False, per_table_limit=40)
        rendered = frag.as_string(None)
        # No filters given -> WHERE clause should only be the tsvector match,
        # immediately followed by ORDER BY (no " AND (...)" filter tacked on).
        assert "@@ q.tsq order by rank desc, data_id limit" in rendered.lower()
        assert params == []

    def test_build_union_passes_per_table_limit(self) -> None:
        from api.queries.search_queries import _build_union

        frag, params = _build_union(["artist"], per_table_limit=30)
        rendered = frag.as_string(None)
        assert "LIMIT 30" in rendered
        assert params == []

    def test_build_union_no_limit_by_default(self) -> None:
        from api.queries.search_queries import _build_union

        frag, params = _build_union(["artist"])
        rendered = frag.as_string(None)
        assert "LIMIT" not in rendered
        assert params == []

    def test_build_union_collects_filter_params_per_type(self) -> None:
        """Filter params must appear once per unioned subquery, in emission order,
        since each subquery has its own WHERE with its own %s placeholders.
        """
        from api.queries.search_queries import _build_union

        frag, params = _build_union(["artist", "release"], per_table_limit=40, year_min=1960)
        rendered = frag.as_string(None)
        # artist has no year column, so its clause is trivially satisfied but the
        # param is still consumed there too (NULL::text IS NULL OR ... >= %s)
        assert params == [1960, 1960]
        assert rendered.count("UNION ALL") == 1

    def test_format_result_release_with_metadata(self) -> None:
        from api.queries.search_queries import _format_result

        row = {
            "type": "release",
            "id": "42",
            "name": "Kind of Blue",
            "rank": 0.8,
            "highlight": "Kind of <em>Blue</em>",
            "year": "1959",
            "genres": ["Jazz"],
        }
        result = _format_result(row)
        assert result["metadata"]["year"] == 1959
        assert result["metadata"]["genres"] == ["Jazz"]

    def test_all_types_constant(self) -> None:
        from api.queries.search_queries import ALL_TYPES

        assert set(ALL_TYPES) == {"artist", "label", "master", "release"}

    @pytest.mark.asyncio
    async def test_run_results_uses_per_table_limit(self) -> None:
        """_run_results passes per_table_limit = limit + offset to _build_union."""
        from api.queries.search_queries import _run_results

        mock_cursor = AsyncMock()
        mock_cursor.fetchall = AsyncMock(return_value=[])
        mock_cursor.__aenter__ = AsyncMock(return_value=mock_cursor)
        mock_cursor.__aexit__ = AsyncMock(return_value=False)

        mock_conn = AsyncMock()
        mock_conn.cursor = MagicMock(return_value=mock_cursor)
        mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_conn.transaction = MagicMock(return_value=_transaction())
        mock_conn.__aexit__ = AsyncMock(return_value=False)

        mock_pool = MagicMock()
        mock_pool.connection = MagicMock(return_value=mock_conn)

        with patch("api.queries.search_queries.execute_sql", new_callable=AsyncMock) as mock_exec:
            result = await _run_results(mock_pool, "Rock", ["artist"], [], None, None, 20, 0)

        assert result == []
        # Verify the SQL was built with per_table_limit = (limit + offset) * 2
        # For limit=20, offset=0: (20 + 0) * 2 = 40
        executed_sql = mock_exec.call_args[0][1]
        rendered = executed_sql.as_string(None)
        assert "LIMIT 40" in rendered

    @pytest.mark.asyncio
    async def test_run_results_applies_year_genre_filter_before_per_table_limit(self) -> None:
        """Regression: filtered search must not apply
        year/genre WHERE after the per-table rank LIMIT — that silently
        drops/empties results for high-cardinality terms whose top-ranked
        rows fall outside the filter. Each per-table subquery's WHERE (before
        its own LIMIT) must carry the filter, and every %s placeholder must
        have a matching param (no param-count mismatch).
        """
        from api.queries.search_queries import _run_results

        mock_cursor = AsyncMock()
        mock_cursor.fetchall = AsyncMock(return_value=[])
        mock_cursor.__aenter__ = AsyncMock(return_value=mock_cursor)
        mock_cursor.__aexit__ = AsyncMock(return_value=False)

        mock_conn = AsyncMock()
        mock_conn.cursor = MagicMock(return_value=mock_cursor)
        mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_conn.transaction = MagicMock(return_value=_transaction())
        mock_conn.__aexit__ = AsyncMock(return_value=False)

        mock_pool = MagicMock()
        mock_pool.connection = MagicMock(return_value=mock_conn)

        with patch("api.queries.search_queries.execute_sql", new_callable=AsyncMock) as mock_exec:
            await _run_results(mock_pool, "love", ["release"], ["Rock"], 1960, 1969, 10, 0)

        executed_sql = mock_exec.call_args[0][1]
        executed_params = mock_exec.call_args[0][2]
        rendered = executed_sql.as_string(None)

        # The per-table subquery's own WHERE (with the filter) must precede its
        # ORDER BY rank LIMIT — not a trailing outer WHERE applied after the cap.
        subquery_where = rendered.index("WHERE")
        subquery_limit = rendered.index("ORDER BY rank DESC, data_id LIMIT")
        assert subquery_where < subquery_limit
        assert "year" in rendered[subquery_where:subquery_limit].lower()
        assert "?|" in rendered[subquery_where:subquery_limit]

        # There must be no outer "WHERE ... AND ..." applied after the results CTE.
        outer_select_idx = rendered.rindex("SELECT type, id, name, rank, highlight, year, genres")
        outer_tail = rendered[outer_select_idx:]
        assert "WHERE" not in outer_tail

        # %s placeholder count must match param count exactly (q + year_min +
        # year_max + genres + limit + offset).
        assert rendered.count("%s") == len(executed_params)

    def test_build_union_raises_on_empty_types(self) -> None:
        from api.queries.search_queries import _build_union

        with pytest.raises(ValueError, match="types must not be empty"):
            _build_union([])

    def test_build_union_multiple_types(self) -> None:
        from api.queries.search_queries import _build_union

        frag, _params = _build_union(["artist", "release"])
        rendered = frag.as_string(None)
        assert "UNION ALL" in rendered

    def test_year_filter_clause_no_filters(self) -> None:
        from api.queries.search_queries import _year_filter_clause

        clause, params = _year_filter_clause(None, None)
        assert clause.as_string(None) == "TRUE"
        assert params == []

    def test_year_filter_clause_min_only(self) -> None:
        from api.queries.search_queries import _year_filter_clause

        clause, params = _year_filter_clause(1960, None)
        rendered = clause.as_string(None)
        assert "year::int >= %s" in rendered
        assert params == [1960]

    def test_year_filter_clause_max_only(self) -> None:
        from api.queries.search_queries import _year_filter_clause

        clause, params = _year_filter_clause(None, 1980)
        rendered = clause.as_string(None)
        assert "year::int <= %s" in rendered
        assert params == [1980]

    def test_year_filter_clause_both(self) -> None:
        from api.queries.search_queries import _year_filter_clause

        clause, params = _year_filter_clause(1960, 1980)
        rendered = clause.as_string(None)
        assert ">=" in rendered
        assert "<=" in rendered
        assert params == [1960, 1980]

    def test_genre_filter_clause_empty(self) -> None:
        from api.queries.search_queries import _genre_filter_clause

        clause, params = _genre_filter_clause([])
        assert clause.as_string(None) == "TRUE"
        assert params == []

    def test_genre_filter_clause_with_genres(self) -> None:
        from api.queries.search_queries import _genre_filter_clause

        clause, params = _genre_filter_clause(["Rock", "Jazz"])
        rendered = clause.as_string(None)
        assert "?|" in rendered
        assert params == [["Rock", "Jazz"]]

    def test_format_result_no_rank(self) -> None:
        from api.queries.search_queries import _format_result

        row = {"type": "artist", "id": "1", "name": "X", "rank": None, "highlight": None, "year": None, "genres": None}
        result = _format_result(row)
        assert result["relevance"] == 0.0
        assert result["highlight"] == "X"

    def test_format_result_no_name(self) -> None:
        from api.queries.search_queries import _format_result

        row = {"type": "artist", "id": "1", "name": None, "rank": 0.5, "highlight": None, "year": None, "genres": None}
        result = _format_result(row)
        assert result["name"] == ""
        assert result["highlight"] == ""

    def test_format_result_invalid_year_ignored(self) -> None:
        from api.queries.search_queries import _format_result

        row = {"type": "release", "id": "1", "name": "X", "rank": 0.5, "highlight": "X", "year": "unknown", "genres": None}
        result = _format_result(row)
        assert "year" not in result["metadata"]


class TestMediaFacetFilter:
    """Media (family/medium) filter -- ADR 0007 / gm-catalog-api-be1.5."""

    def test_split_media_filter_recognises_family_id(self) -> None:
        from api.queries.search_queries import split_media_filter

        families, mediums, unknown = split_media_filter(["vinyl"])
        assert families == ["vinyl"]
        assert mediums == []
        assert unknown == []

    def test_split_media_filter_recognises_medium_id(self) -> None:
        from api.queries.search_queries import split_media_filter

        families, mediums, unknown = split_media_filter(["vinyl_12"])
        assert families == []
        assert mediums == ["vinyl_12"]
        assert unknown == []

    def test_split_media_filter_mixed_family_and_medium(self) -> None:
        from api.queries.search_queries import split_media_filter

        families, mediums, unknown = split_media_filter(["vinyl", "optical_cd", "tape"])
        assert families == ["vinyl", "tape"]
        assert mediums == ["optical_cd"]
        assert unknown == []

    def test_split_media_filter_flags_unknown_ids(self) -> None:
        from api.queries.search_queries import split_media_filter

        families, mediums, unknown = split_media_filter(["vinyl", "not-a-real-id", "also-bogus"])
        assert families == ["vinyl"]
        assert mediums == []
        assert unknown == ["not-a-real-id", "also-bogus"]

    def test_split_media_filter_empty(self) -> None:
        from api.queries.search_queries import split_media_filter

        assert split_media_filter([]) == ([], [], [])

    def test_media_filter_clause_empty(self) -> None:
        from api.queries.search_queries import _media_filter_clause

        clause, params = _media_filter_clause([], [])
        assert clause.as_string(None) == "TRUE"
        assert params == []

    def test_media_filter_clause_family_only_uses_indexed_operator(self) -> None:
        from api.queries.search_queries import _media_filter_clause

        clause, params = _media_filter_clause(["vinyl", "tape"], [])
        rendered = clause.as_string(None)
        assert "?|" in rendered
        assert "families" in rendered
        assert "jsonb_path_exists" not in rendered
        assert params == [["vinyl", "tape"]]

    def test_media_filter_clause_medium_only_uses_jsonb_path_exists(self) -> None:
        from api.queries.search_queries import _media_filter_clause

        clause, params = _media_filter_clause([], ["vinyl_12"])
        rendered = clause.as_string(None)
        assert "jsonb_path_exists" in rendered
        assert "$.items[*]" in rendered
        assert params == ["vinyl_12"]

    def test_media_filter_clause_multiple_mediums_or_combined(self) -> None:
        from api.queries.search_queries import _media_filter_clause

        clause, params = _media_filter_clause([], ["vinyl_12", "optical_cd"])
        rendered = clause.as_string(None)
        assert rendered.count("jsonb_path_exists") == 2
        assert " OR " in rendered
        assert params == ["vinyl_12", "optical_cd"]

    def test_media_filter_clause_family_and_medium_or_combined(self) -> None:
        from api.queries.search_queries import _media_filter_clause

        clause, params = _media_filter_clause(["vinyl"], ["optical_cd"])
        rendered = clause.as_string(None)
        assert "?|" in rendered
        assert "jsonb_path_exists" in rendered
        assert " OR " in rendered
        assert params == [["vinyl"], "optical_cd"]

    def test_media_filter_clause_on_release_search_uses_the_hoisted_arrays(self) -> None:
        from api.queries.search_queries import _media_filter_clause

        clause, params = _media_filter_clause(["vinyl"], ["CD", "LP"], column=sql.SQL("media_families"), mediums_column=sql.SQL("media_mediums"))
        rendered = clause.as_string(None)
        assert rendered == "(media_mediums IS NULL OR (media_families ?| %s::text[] OR media_mediums ? %s::text OR media_mediums ? %s::text))"
        assert params == [["vinyl"], "CD", "LP"]

    def test_media_filter_clause_null_passthrough_for_non_release_types(self) -> None:
        """Entity types without a media column pass the filter (NULL IS NULL)."""
        from api.queries.search_queries import _media_filter_clause

        clause, _params = _media_filter_clause(["vinyl"], [], column=None)
        rendered = clause.as_string(None)
        assert "IS NULL OR" in rendered

    def test_entity_select_pushes_media_family_filter_before_limit(self) -> None:
        from api.queries.search_queries import _entity_select

        frag, params = _entity_select(
            "release", "title", has_year=False, has_genres=False, per_table_limit=40, has_media=True, media_families=["vinyl"]
        )
        rendered = frag.as_string(None)
        where_idx = rendered.index("WHERE")
        limit_idx = rendered.index("ORDER BY rank DESC, data_id LIMIT")
        assert where_idx < limit_idx
        assert "?|" in rendered[where_idx:limit_idx]
        assert params == [["vinyl"]]

    def test_entity_select_ignores_media_when_has_media_false(self) -> None:
        """Non-release entity types (e.g. artist) have no media column -- the
        filter must not be applied even if media ids are passed in.
        """
        from api.queries.search_queries import _entity_select

        frag, params = _entity_select("artist", "name", has_year=False, has_genres=False, per_table_limit=40, media_families=["vinyl"])
        rendered = frag.as_string(None)
        assert "NULL::jsonb" in rendered
        assert params == [["vinyl"]]  # param still consumed (NULL IS NULL OR ...)

    def test_build_union_combines_media_and_genre_filters(self) -> None:
        from api.queries.search_queries import _build_union

        frag, params = _build_union(["release"], per_table_limit=40, genres=["Rock"], media_families=["vinyl"])
        rendered = frag.as_string(None)
        assert "?| %s::text[]" in rendered
        assert params == [["Rock"], ["vinyl"]]

    @pytest.mark.asyncio
    async def test_run_results_applies_media_family_filter(self) -> None:
        from api.queries.search_queries import _run_results

        mock_pool = TestSearchQueryAsyncFunctions._db_mocks()
        with patch("api.queries.search_queries.execute_sql", new_callable=AsyncMock) as mock_exec:
            await _run_results(mock_pool, "love", ["release"], [], None, None, 10, 0, ["vinyl"], [])

        rendered = mock_exec.call_args[0][1].as_string(None)
        assert "families" in rendered
        assert "?|" in rendered

    @pytest.mark.asyncio
    async def test_run_results_applies_media_and_genre_filter_together(self) -> None:
        """Media and genre filters must combine (AND) on release results."""
        from api.queries.search_queries import _run_results

        mock_pool = TestSearchQueryAsyncFunctions._db_mocks()
        with patch("api.queries.search_queries.execute_sql", new_callable=AsyncMock) as mock_exec:
            await _run_results(mock_pool, "love", ["release"], ["Rock"], None, None, 10, 0, ["vinyl"], [])

        executed_params = mock_exec.call_args[0][2]
        rendered = mock_exec.call_args[0][1].as_string(None)
        where_idx = rendered.index("WHERE")
        limit_idx = rendered.index("ORDER BY rank DESC, data_id LIMIT")
        subquery = rendered[where_idx:limit_idx]
        assert "?| %s::text[]" in subquery  # genre
        assert "families" in subquery  # media family
        assert rendered.count("%s") == len(executed_params)

    @pytest.mark.asyncio
    async def test_execute_search_applies_media_filter_and_returns_media_facet(self) -> None:
        """A media filter is accepted end-to-end and the response carries a
        (possibly empty, since the DB is mocked) facets.media mapping.
        """
        from api.queries.search_queries import execute_search

        mock_pool = TestSearchQueryAsyncFunctions._db_mocks()

        with patch("api.queries.search_queries.execute_sql", new_callable=AsyncMock):
            result = await execute_search(mock_pool, None, "blue", ["release"], [], None, None, 20, 0, media_families=["vinyl"], media_mediums=[])

        assert "media" in result["facets"]
        assert result["facets"]["media"] == {}

    def test_cache_key_differs_on_media_filter(self) -> None:
        from api.queries.search_queries import cache_key

        k1 = cache_key("blue", ["release"], [], None, None, 20, 0, media=["vinyl"])
        k2 = cache_key("blue", ["release"], [], None, None, 20, 0, media=["optical_cd"])
        k3 = cache_key("blue", ["release"], [], None, None, 20, 0)
        assert len({k1, k2, k3}) == 3

    def test_cache_key_media_order_independent(self) -> None:
        from api.queries.search_queries import cache_key

        k1 = cache_key("blue", ["release"], [], None, None, 20, 0, media=["vinyl", "tape"])
        k2 = cache_key("blue", ["release"], [], None, None, 20, 0, media=["tape", "vinyl"])
        assert k1 == k2


class TestSearchMediaEndpoint:
    """Router-level behaviour for GET /api/search?media=..."""

    def test_media_filter_forwarded(self, test_client: TestClient) -> None:
        captured: dict[str, Any] = {}

        async def _capture(**kwargs: Any) -> dict[str, Any]:
            captured.update(kwargs)
            return {
                "query": "blue",
                "total": 0,
                "facets": {"type": {}, "genre": {}, "decade": {}, "media": {}},
                "results": [],
                "pagination": {"limit": 20, "offset": 0, "has_more": False},
            }

        with patch("api.routers.search.execute_search", side_effect=_capture):
            response = test_client.get("/api/search?q=blue&media=vinyl&media=optical_cd")

        assert response.status_code == 200
        assert captured["media_families"] == ["vinyl"]
        assert captured["media_mediums"] == ["optical_cd"]

    def test_media_and_genre_filters_combine(self, test_client: TestClient) -> None:
        captured: dict[str, Any] = {}

        async def _capture(**kwargs: Any) -> dict[str, Any]:
            captured.update(kwargs)
            return {
                "query": "blue",
                "total": 0,
                "facets": {"type": {}, "genre": {}, "decade": {}, "media": {}},
                "results": [],
                "pagination": {"limit": 20, "offset": 0, "has_more": False},
            }

        with patch("api.routers.search.execute_search", side_effect=_capture):
            response = test_client.get("/api/search?q=blue&media=vinyl&genres=Jazz")

        assert response.status_code == 200
        assert captured["media_families"] == ["vinyl"]
        assert captured["genres"] == ["Jazz"]

    def test_unknown_media_id_returns_400(self, test_client: TestClient) -> None:
        with patch("api.routers.search.execute_search", new_callable=AsyncMock):
            response = test_client.get("/api/search?q=blue&media=not-a-real-id")

        assert response.status_code == 400
        assert "not-a-real-id" in response.json()["error"]

    def test_unknown_media_id_among_valid_ones_lists_only_unknown(self, test_client: TestClient) -> None:
        with patch("api.routers.search.execute_search", new_callable=AsyncMock):
            response = test_client.get("/api/search?q=blue&media=vinyl&media=bogus")

        assert response.status_code == 400
        error = response.json()["error"]
        assert "bogus" in error
        assert "vinyl" not in error

    def test_no_media_filter_defaults_to_empty(self, test_client: TestClient) -> None:
        captured: dict[str, Any] = {}

        async def _capture(**kwargs: Any) -> dict[str, Any]:
            captured.update(kwargs)
            return {
                "query": "blue",
                "total": 0,
                "facets": {"type": {}, "genre": {}, "decade": {}, "media": {}},
                "results": [],
                "pagination": {"limit": 20, "offset": 0, "has_more": False},
            }

        with patch("api.routers.search.execute_search", side_effect=_capture):
            test_client.get("/api/search?q=blue")

        assert captured["media_families"] == []
        assert captured["media_mediums"] == []

    def test_response_includes_media_facet(self, test_client: TestClient) -> None:
        expected = {
            "query": "blue",
            "total": 1,
            "facets": {"type": {}, "genre": {}, "decade": {}, "media": {"vinyl": 1}},
            "results": [],
            "pagination": {"limit": 20, "offset": 0, "has_more": False},
        }
        with patch("api.routers.search.execute_search", return_value=expected):
            response = test_client.get("/api/search?q=blue")

        data = response.json()
        assert data["facets"]["media"] == {"vinyl": 1}


class TestSearchQueryAsyncFunctions:
    """Tests for async DB query functions in search_queries."""

    @pytest.mark.asyncio
    async def test_run_total(self) -> None:
        from api.queries.search_queries import _run_total

        mock_cursor = AsyncMock()
        mock_cursor.fetchone = AsyncMock(return_value={"total": 42})
        mock_cursor.__aenter__ = AsyncMock(return_value=mock_cursor)
        mock_cursor.__aexit__ = AsyncMock(return_value=False)

        mock_conn = AsyncMock()
        mock_conn.cursor = MagicMock(return_value=mock_cursor)
        mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_conn.transaction = MagicMock(return_value=_transaction())
        mock_conn.__aexit__ = AsyncMock(return_value=False)

        mock_pool = MagicMock()
        mock_pool.connection = MagicMock(return_value=mock_conn)

        with patch("api.queries.search_queries.execute_sql", new_callable=AsyncMock):
            result = await _run_total(mock_pool, "Rock", ["artist"], [], None, None)
        assert result == 42

    @pytest.mark.asyncio
    async def test_run_total_no_result(self) -> None:
        from api.queries.search_queries import _run_total

        mock_cursor = AsyncMock()
        mock_cursor.fetchone = AsyncMock(return_value=None)
        mock_cursor.__aenter__ = AsyncMock(return_value=mock_cursor)
        mock_cursor.__aexit__ = AsyncMock(return_value=False)

        mock_conn = AsyncMock()
        mock_conn.cursor = MagicMock(return_value=mock_cursor)
        mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_conn.transaction = MagicMock(return_value=_transaction())
        mock_conn.__aexit__ = AsyncMock(return_value=False)

        mock_pool = MagicMock()
        mock_pool.connection = MagicMock(return_value=mock_conn)

        with patch("api.queries.search_queries.execute_sql", new_callable=AsyncMock):
            result = await _run_total(mock_pool, "Rock", ["artist"], [], None, None)
        assert result == 0

    @pytest.mark.asyncio
    async def test_run_type_counts(self) -> None:
        from api.queries.search_queries import _run_type_counts

        mock_cursor = AsyncMock()
        mock_cursor.fetchall = AsyncMock(return_value=[{"type": "artist", "cnt": 10}, {"type": "label", "cnt": 5}])
        mock_cursor.__aenter__ = AsyncMock(return_value=mock_cursor)
        mock_cursor.__aexit__ = AsyncMock(return_value=False)

        mock_conn = AsyncMock()
        mock_conn.cursor = MagicMock(return_value=mock_cursor)
        mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_conn.transaction = MagicMock(return_value=_transaction())
        mock_conn.__aexit__ = AsyncMock(return_value=False)

        mock_pool = MagicMock()
        mock_pool.connection = MagicMock(return_value=mock_conn)

        with patch("api.queries.search_queries.execute_sql", new_callable=AsyncMock):
            result = await _run_type_counts(mock_pool, "Rock", ["artist", "label"])
        assert result == {"artist": 10, "label": 5}

    @pytest.mark.asyncio
    async def test_run_genre_facets(self) -> None:
        from api.queries.search_queries import _run_genre_facets

        mock_cursor = AsyncMock()
        mock_cursor.fetchall = AsyncMock(return_value=[{"genre": "Rock", "cnt": 100}, {"genre": "Jazz", "cnt": 50}])
        mock_cursor.__aenter__ = AsyncMock(return_value=mock_cursor)
        mock_cursor.__aexit__ = AsyncMock(return_value=False)

        mock_conn = AsyncMock()
        mock_conn.cursor = MagicMock(return_value=mock_cursor)
        mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_conn.transaction = MagicMock(return_value=_transaction())
        mock_conn.__aexit__ = AsyncMock(return_value=False)

        mock_pool = MagicMock()
        mock_pool.connection = MagicMock(return_value=mock_conn)

        with patch("api.queries.search_queries.execute_sql", new_callable=AsyncMock):
            result = await _run_genre_facets(mock_pool, "Hendrix")
        assert result == {"Rock": 100, "Jazz": 50}

    @pytest.mark.asyncio
    async def test_run_decade_facets(self) -> None:
        from api.queries.search_queries import _run_decade_facets

        mock_cursor = AsyncMock()
        mock_cursor.fetchall = AsyncMock(return_value=[{"decade": "1960s", "cnt": 30}, {"decade": "1970s", "cnt": 45}])
        mock_cursor.__aenter__ = AsyncMock(return_value=mock_cursor)
        mock_cursor.__aexit__ = AsyncMock(return_value=False)

        mock_conn = AsyncMock()
        mock_conn.cursor = MagicMock(return_value=mock_cursor)
        mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_conn.transaction = MagicMock(return_value=_transaction())
        mock_conn.__aexit__ = AsyncMock(return_value=False)

        mock_pool = MagicMock()
        mock_pool.connection = MagicMock(return_value=mock_conn)

        with patch("api.queries.search_queries.execute_sql", new_callable=AsyncMock):
            result = await _run_decade_facets(mock_pool, "Hendrix")
        assert result == {"1960s": 30, "1970s": 45}

    @pytest.mark.asyncio
    async def test_master_only_search_skips_release_facets(self) -> None:
        """A search that excludes releases runs no release-facet statement and no release decade half."""
        from api.queries import search_queries as sq

        with (
            patch.object(sq, "_run_results", new=AsyncMock(return_value=[])),
            patch.object(sq, "_run_total", new=AsyncMock(return_value=0)),
            patch.object(sq, "_run_type_counts", new=AsyncMock(return_value={"master": 0})),
            patch.object(sq, "_run_genre_facets", new=AsyncMock(return_value={"x": 1})) as genre,
            patch.object(sq, "_run_media_facets", new=AsyncMock(return_value={"x": 1})) as media,
            patch.object(sq, "_run_decade_facets", new=AsyncMock(return_value={})) as decade,
            patch.object(sq, "native_ids_for_pairs", new=AsyncMock(return_value={})),
        ):
            result = await sq.execute_search(MagicMock(), None, "love", ["master"], [], None, None, 20, 0)
        genre.assert_not_called()
        media.assert_not_called()
        assert decade.await_args.args[2] == ["master"]
        assert result["facets"]["genre"] == {}
        assert result["facets"]["media"] == {}

    @pytest.mark.asyncio
    async def test_execute_search_raises_on_empty_types(self) -> None:
        from api.queries.search_queries import execute_search

        mock_pool = MagicMock()
        with pytest.raises(ValueError, match="types must not be empty"):
            await execute_search(mock_pool, None, "blue", [], [], None, None, 20, 0)

    @pytest.mark.asyncio
    async def test_execute_search_returns_cached(self) -> None:
        import json

        from api.queries.search_queries import execute_search

        cached_response = {"query": "blue", "total": 1, "facets": {}, "results": [], "pagination": {}}
        mock_valkey = AsyncMock()
        mock_valkey.get = AsyncMock(return_value=json.dumps(cached_response))
        mock_pool = MagicMock()

        result = await execute_search(mock_pool, mock_valkey, "blue", ["artist"], [], None, None, 20, 0)
        assert result == cached_response

    @pytest.mark.asyncio
    async def test_execute_search_cache_miss(self) -> None:
        from api.queries.search_queries import execute_search

        mock_cursor = AsyncMock()
        mock_cursor.fetchall = AsyncMock(return_value=[])
        mock_cursor.fetchone = AsyncMock(return_value={"total": 0})
        mock_cursor.__aenter__ = AsyncMock(return_value=mock_cursor)
        mock_cursor.__aexit__ = AsyncMock(return_value=False)

        mock_conn = AsyncMock()
        mock_conn.cursor = MagicMock(return_value=mock_cursor)
        mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_conn.transaction = MagicMock(return_value=_transaction())
        mock_conn.__aexit__ = AsyncMock(return_value=False)

        mock_pool = MagicMock()
        mock_pool.connection = MagicMock(return_value=mock_conn)

        mock_valkey = AsyncMock()
        mock_valkey.get = AsyncMock(return_value=None)
        mock_valkey.setex = AsyncMock()

        with patch("api.queries.search_queries.execute_sql", new_callable=AsyncMock):
            result = await execute_search(mock_pool, mock_valkey, "blue", ["artist"], [], None, None, 20, 0)

        assert result["query"] == "blue"
        assert result["total"] == 0
        assert "facets" in result
        assert "results" in result
        assert "pagination" in result
        mock_valkey.setex.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_execute_search_no_valkey(self) -> None:
        from api.queries.search_queries import execute_search

        mock_cursor = AsyncMock()
        mock_cursor.fetchall = AsyncMock(return_value=[])
        mock_cursor.fetchone = AsyncMock(return_value={"total": 0})
        mock_cursor.__aenter__ = AsyncMock(return_value=mock_cursor)
        mock_cursor.__aexit__ = AsyncMock(return_value=False)

        mock_conn = AsyncMock()
        mock_conn.cursor = MagicMock(return_value=mock_cursor)
        mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_conn.transaction = MagicMock(return_value=_transaction())
        mock_conn.__aexit__ = AsyncMock(return_value=False)

        mock_pool = MagicMock()
        mock_pool.connection = MagicMock(return_value=mock_conn)

        with patch("api.queries.search_queries.execute_sql", new_callable=AsyncMock):
            result = await execute_search(mock_pool, None, "blue", ["artist"], [], None, None, 20, 0)

        assert result["query"] == "blue"

    @staticmethod
    def _db_mocks() -> MagicMock:
        """A pool whose cursor returns empty result sets for every _run_* query."""
        mock_cursor = AsyncMock()
        mock_cursor.fetchall = AsyncMock(return_value=[])
        mock_cursor.fetchone = AsyncMock(return_value={"total": 0})
        mock_cursor.__aenter__ = AsyncMock(return_value=mock_cursor)
        mock_cursor.__aexit__ = AsyncMock(return_value=False)
        mock_conn = AsyncMock()
        mock_conn.cursor = MagicMock(return_value=mock_cursor)
        mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_conn.transaction = MagicMock(return_value=_transaction())
        mock_conn.__aexit__ = AsyncMock(return_value=False)
        mock_pool = MagicMock()
        mock_pool.connection = MagicMock(return_value=mock_conn)
        return mock_pool

    @pytest.mark.asyncio
    async def test_execute_search_degrades_when_valkey_get_raises(self) -> None:
        """A Valkey read outage must fall through to the DB,
        not propagate a 500 — search is fully PostgreSQL-backed.
        """
        from valkey.exceptions import ConnectionError as ValkeyConnectionError

        from api.queries.search_queries import execute_search

        mock_pool = self._db_mocks()
        mock_valkey = AsyncMock()
        mock_valkey.get = AsyncMock(side_effect=ValkeyConnectionError("Connection refused"))
        mock_valkey.setex = AsyncMock()

        with patch("api.queries.search_queries.execute_sql", new_callable=AsyncMock):
            result = await execute_search(mock_pool, mock_valkey, "blue", ["artist"], [], None, None, 20, 0)

        # Answered from PostgreSQL despite the Valkey outage.
        assert result["query"] == "blue"
        assert result["total"] == 0
        mock_valkey.get.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_execute_search_returns_when_valkey_setex_raises(self) -> None:
        """A Valkey write outage must not fail an otherwise
        successful search response.
        """
        from valkey.exceptions import ConnectionError as ValkeyConnectionError

        from api.queries.search_queries import execute_search

        mock_pool = self._db_mocks()
        mock_valkey = AsyncMock()
        mock_valkey.get = AsyncMock(return_value=None)
        mock_valkey.setex = AsyncMock(side_effect=ValkeyConnectionError("Connection refused"))

        with patch("api.queries.search_queries.execute_sql", new_callable=AsyncMock):
            result = await execute_search(mock_pool, mock_valkey, "blue", ["artist"], [], None, None, 20, 0)

        assert result["query"] == "blue"
        mock_valkey.setex.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_execute_search_treats_corrupt_cache_entry_as_miss(self) -> None:
        """A corrupt (non-JSON) cache entry must be treated
        as a miss rather than raising.
        """
        from api.queries.search_queries import execute_search

        mock_pool = self._db_mocks()
        mock_valkey = AsyncMock()
        mock_valkey.get = AsyncMock(return_value="{not valid json")
        mock_valkey.setex = AsyncMock()

        with patch("api.queries.search_queries.execute_sql", new_callable=AsyncMock):
            result = await execute_search(mock_pool, mock_valkey, "blue", ["artist"], [], None, None, 20, 0)

        assert result["query"] == "blue"

    @pytest.mark.asyncio
    async def test_run_media_facets(self) -> None:
        from api.queries.search_queries import _run_media_facets

        mock_cursor = AsyncMock()
        mock_cursor.fetchall = AsyncMock(return_value=[{"family": "vinyl", "cnt": 40}, {"family": "optical", "cnt": 12}])
        mock_cursor.__aenter__ = AsyncMock(return_value=mock_cursor)
        mock_cursor.__aexit__ = AsyncMock(return_value=False)

        mock_conn = AsyncMock()
        mock_conn.cursor = MagicMock(return_value=mock_cursor)
        mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_conn.transaction = MagicMock(return_value=_transaction())
        mock_conn.__aexit__ = AsyncMock(return_value=False)

        mock_pool = MagicMock()
        mock_pool.connection = MagicMock(return_value=mock_conn)

        with patch("api.queries.search_queries.execute_sql", new_callable=AsyncMock):
            result = await _run_media_facets(mock_pool, "Hendrix")
        assert result == {"vinyl": 40, "optical": 12}

    @pytest.mark.asyncio
    async def test_run_results_final_order_has_unique_tiebreaker(self) -> None:
        """The outer paginated query must sort by a unique
        secondary key so OFFSET pagination is page-consistent across executions.
        """
        from api.queries.search_queries import _run_results

        mock_pool = self._db_mocks()
        with patch("api.queries.search_queries.execute_sql", new_callable=AsyncMock) as mock_exec:
            await _run_results(mock_pool, "Rock", ["artist"], [], None, None, 10, 10)

        rendered = mock_exec.call_args[0][1].as_string(None)
        # Final ordering carries the unique `id` tiebreaker; the per-table cap
        # carries the unique `data_id` tiebreaker.
        assert "ORDER BY rank DESC, id" in rendered
        assert "ORDER BY rank DESC, data_id LIMIT" in rendered


class TestReleaseFacetsReadReleaseSearch:
    """The genre, decade (releases half) and media facets count public.release_search, never releases.data."""

    @staticmethod
    async def _rendered(fn_name: str) -> str:
        from api.queries import search_queries

        mock_cursor = AsyncMock()
        mock_cursor.fetchall = AsyncMock(return_value=[])
        mock_cursor.__aenter__ = AsyncMock(return_value=mock_cursor)
        mock_cursor.__aexit__ = AsyncMock(return_value=False)
        mock_conn = AsyncMock()
        mock_conn.cursor = MagicMock(return_value=mock_cursor)
        mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_conn.transaction = MagicMock(return_value=_transaction())
        mock_conn.__aexit__ = AsyncMock(return_value=False)
        mock_pool = MagicMock()
        mock_pool.connection = MagicMock(return_value=mock_conn)
        with patch("api.queries.search_queries.execute_sql", new_callable=AsyncMock) as mock_exec:
            await getattr(search_queries, fn_name)(mock_pool, "love")
        return str(mock_exec.call_args[0][1].as_string(None))

    @pytest.mark.asyncio
    async def test_genre_facet(self) -> None:
        rendered = await self._rendered("_run_genre_facets")
        assert "FROM public.release_search" in rendered
        assert "LIMIT" not in rendered.replace("LIMIT 20", "")
        assert "jsonb_array_elements_text(genres)" in rendered
        assert "to_tsvector" not in rendered
        assert "releases" not in rendered.replace("release_search", "")

    @pytest.mark.asyncio
    async def test_media_facet(self) -> None:
        rendered = await self._rendered("_run_media_facets")
        assert "FROM public.release_search" in rendered
        assert "jsonb_array_elements_text(media_families)" in rendered
        assert "to_tsvector" not in rendered
        assert "data->" not in rendered

    @pytest.mark.asyncio
    async def test_decade_facet_reads_release_search_for_releases(self) -> None:
        rendered = await self._rendered("_run_decade_facets")
        assert "(SELECT year FROM public.release_search, q WHERE title_tsv @@ q.tsq AND year IS NOT NULL)" in rendered
        assert "FROM releases" not in rendered

    @pytest.mark.asyncio
    async def test_facets_count_every_match_by_default(self) -> None:
        for fn in ("_run_genre_facets", "_run_media_facets"):
            assert "LIMIT 10000" not in await self._rendered(fn)

    @pytest.mark.asyncio
    async def test_facet_cap_can_be_set_again(self) -> None:
        with patch("api.queries.search_queries._FACET_COUNT_CAP", 500):
            for fn in ("_run_genre_facets", "_run_media_facets"):
                assert "title_tsv @@ q.tsq LIMIT 500)" in await self._rendered(fn)
            decade = await self._rendered("_run_decade_facets")
            assert "FROM public.release_search, q WHERE title_tsv @@ q.tsq AND year IS NOT NULL LIMIT 500)" in decade


class TestSearchCountryFacet:
    """The ADR 0011 `country` facet on the releases branch of the union."""

    def test_country_filter_clause_empty(self) -> None:
        from api.queries.search_queries import _country_filter_clause

        clause, params = _country_filter_clause([])
        assert clause.as_string(None) == "TRUE"
        assert params == []

    def test_country_filter_clause_uses_any_over_the_indexed_expression(self) -> None:
        from api.queries.search_queries import _country_filter_clause

        clause, params = _country_filter_clause(["UK"])
        rendered = clause.as_string(None)
        assert "= ANY(%s::text[])" in rendered
        assert params == [["UK"]]

    def test_country_filter_clause_two_countries_are_one_any(self) -> None:
        """One facet's multiple selected values OR-combine, as everywhere else here."""
        from api.queries.search_queries import _country_filter_clause

        clause, params = _country_filter_clause(["UK", "Germany"])
        assert clause.as_string(None).count("ANY") == 1
        assert params == [["UK", "Germany"]]

    def test_country_filter_clause_null_passthrough_for_non_release_types(self) -> None:
        """Entity types without a country column pass the filter (NULL IS NULL)."""
        from api.queries.search_queries import _country_filter_clause

        clause, _params = _country_filter_clause(["UK"], column=None)
        assert "country IS NULL OR" in clause.as_string(None)

    def test_entity_select_pushes_country_filter_before_limit(self) -> None:
        from api.queries.search_queries import _entity_select

        frag, params = _entity_select("release", "title", has_year=False, has_genres=False, per_table_limit=40, has_country=True, countries=["UK"])
        rendered = frag.as_string(None)
        where_idx = rendered.index("WHERE")
        limit_idx = rendered.index("ORDER BY rank DESC, data_id LIMIT")
        assert where_idx < limit_idx
        assert "country = ANY" in rendered[where_idx:limit_idx]
        assert "data->>'country'" not in rendered[where_idx:limit_idx]
        assert params == [["UK"]]

    def test_entity_select_projects_null_country_when_has_country_false(self) -> None:
        from api.queries.search_queries import _entity_select

        frag, _params = _entity_select("artist", "name", has_year=False, has_genres=False, per_table_limit=40)
        rendered = frag.as_string(None)
        assert "NULL::text AS country" in rendered

    def test_build_union_combines_country_with_the_other_facets(self) -> None:
        from api.queries.search_queries import _build_union

        frag, params = _build_union(["release"], per_table_limit=40, genres=["Rock"], media_families=["vinyl"], countries=["UK"])
        rendered = frag.as_string(None)
        assert "data->>'country'" in rendered
        assert params == [["Rock"], ["vinyl"], ["UK"]]

    @pytest.mark.asyncio
    async def test_run_results_applies_one_country(self) -> None:
        from api.queries.search_queries import _run_results

        mock_pool = TestSearchQueryAsyncFunctions._db_mocks()
        with patch("api.queries.search_queries.execute_sql", new_callable=AsyncMock) as mock_exec:
            await _run_results(mock_pool, "love", ["release"], [], None, None, 10, 0, [], [], ["UK"])

        rendered = mock_exec.call_args[0][1].as_string(None)
        assert "data->>'country'" in rendered
        assert mock_exec.call_args[0][2][1] == ["UK"]

    @pytest.mark.asyncio
    async def test_run_results_applies_two_countries(self) -> None:
        from api.queries.search_queries import _run_results

        mock_pool = TestSearchQueryAsyncFunctions._db_mocks()
        with patch("api.queries.search_queries.execute_sql", new_callable=AsyncMock) as mock_exec:
            await _run_results(mock_pool, "love", ["release"], [], None, None, 10, 0, [], [], ["UK", "Germany"])

        executed_params = mock_exec.call_args[0][2]
        rendered = mock_exec.call_args[0][1].as_string(None)
        assert executed_params[1] == ["UK", "Germany"]
        assert rendered.count("%s") == len(executed_params)

    @pytest.mark.asyncio
    async def test_run_results_selects_country_on_the_outer_query(self) -> None:
        """The hit shape carries `country`, so the outer projection has to name it."""
        from api.queries.search_queries import _run_results

        mock_pool = TestSearchQueryAsyncFunctions._db_mocks()
        with patch("api.queries.search_queries.execute_sql", new_callable=AsyncMock) as mock_exec:
            await _run_results(mock_pool, "love", ["release"], [], None, None, 10, 0)

        assert "SELECT type, id, name, rank, highlight, year, genres, country" in mock_exec.call_args[0][1].as_string(None)

    @pytest.mark.asyncio
    async def test_run_total_applies_the_country_filter(self) -> None:
        from api.queries.search_queries import _run_total

        mock_pool = TestSearchQueryAsyncFunctions._db_mocks()
        with patch("api.queries.search_queries.execute_sql", new_callable=AsyncMock) as mock_exec:
            await _run_total(mock_pool, "love", ["release"], [], None, None, [], [], ["UK"])

        rendered = mock_exec.call_args[0][1].as_string(None)
        assert "country = ANY" in rendered
        assert "data->>'country'" not in rendered

    def test_format_result_carries_country(self) -> None:
        from api.queries.search_queries import _format_result

        hit = _format_result({"type": "release", "id": "10", "name": "A", "rank": 0.5, "highlight": "A", "country": "UK"})
        assert hit["country"] == "UK"

    def test_format_result_country_is_none_for_a_row_without_one(self) -> None:
        from api.queries.search_queries import _format_result

        hit = _format_result({"type": "artist", "id": "1", "name": "A", "rank": 0.5, "highlight": "A", "country": None})
        assert hit["country"] is None

    def test_cache_key_differs_on_country_filter(self) -> None:
        from api.queries.search_queries import cache_key

        k1 = cache_key("blue", ["release"], [], None, None, 20, 0, countries=["UK"])
        k2 = cache_key("blue", ["release"], [], None, None, 20, 0, countries=["Germany"])
        k3 = cache_key("blue", ["release"], [], None, None, 20, 0)
        assert len({k1, k2, k3}) == 3

    def test_cache_key_country_order_independent(self) -> None:
        from api.queries.search_queries import cache_key

        k1 = cache_key("blue", ["release"], [], None, None, 20, 0, countries=["UK", "Germany"])
        k2 = cache_key("blue", ["release"], [], None, None, 20, 0, countries=["Germany", "UK"])
        assert k1 == k2

    @pytest.mark.asyncio
    async def test_execute_search_accepts_countries_end_to_end(self) -> None:
        from api.queries.search_queries import execute_search

        mock_pool = TestSearchQueryAsyncFunctions._db_mocks()
        with patch("api.queries.search_queries.execute_sql", new_callable=AsyncMock):
            result = await execute_search(mock_pool, None, "blue", ["release"], [], None, None, 20, 0, countries=["UK"])

        assert result["query"] == "blue"
        assert result["results"] == []


class TestSearchCountryEndpoint:
    """Router-level behaviour for GET /api/search?country=..."""

    @staticmethod
    def _capture(captured: dict[str, Any]) -> Any:
        async def _run(**kwargs: Any) -> dict[str, Any]:
            captured.update(kwargs)
            return {
                "query": "blue",
                "total": 0,
                "facets": {"type": {}, "genre": {}, "decade": {}, "media": {}},
                "results": [],
                "pagination": {"limit": 20, "offset": 0, "has_more": False},
            }

        return _run

    def test_one_country_forwarded(self, test_client: TestClient) -> None:
        captured: dict[str, Any] = {}
        with patch("api.routers.search.execute_search", side_effect=self._capture(captured)):
            response = test_client.get("/api/search?q=blue&country=UK")

        assert response.status_code == 200
        assert captured["countries"] == ["UK"]

    def test_two_countries_forwarded_in_order(self, test_client: TestClient) -> None:
        captured: dict[str, Any] = {}
        with patch("api.routers.search.execute_search", side_effect=self._capture(captured)):
            response = test_client.get("/api/search?q=blue&country=UK&country=Germany")

        assert response.status_code == 200
        assert captured["countries"] == ["UK", "Germany"]

    def test_blank_country_values_are_dropped(self, test_client: TestClient) -> None:
        captured: dict[str, Any] = {}
        with patch("api.routers.search.execute_search", side_effect=self._capture(captured)):
            response = test_client.get("/api/search?q=blue&country=&country=%20UK%20")

        assert response.status_code == 200
        assert captured["countries"] == ["UK"]

    def test_no_country_is_an_empty_filter(self, test_client: TestClient) -> None:
        captured: dict[str, Any] = {}
        with patch("api.routers.search.execute_search", side_effect=self._capture(captured)):
            test_client.get("/api/search?q=blue")

        assert captured["countries"] == []

    def test_country_travels_in_the_recorded_filters(self) -> None:
        from api.routers.search import _search_filters

        assert _search_filters(["release"], [], [], None, None, ["UK", "Germany"]) == [
            "country:Germany",
            "country:UK",
            "type:release",
        ]


class TestSearchBackpressureResponses:
    """A shed or timed-out search is a defined error, never an empty success, and is never recorded."""

    def test_a_busy_search_is_503_with_retry_after(self, test_client: TestClient) -> None:
        from api.queries.search_queries import SearchBusy

        with patch("api.routers.search.execute_search", AsyncMock(side_effect=SearchBusy())):
            response = test_client.get("/api/search?q=love")
        assert response.status_code == 503
        # The limiter's header injection may overwrite the value, but a retry hint is always present.
        assert "retry-after" in response.headers
        assert "results" not in response.json()

    def test_a_timed_out_search_is_504(self, test_client: TestClient) -> None:
        from api.queries.search_queries import SearchTimedOut

        with patch("api.routers.search.execute_search", AsyncMock(side_effect=SearchTimedOut())):
            response = test_client.get("/api/search?q=love")
        assert response.status_code == 504
        assert "results" not in response.json()

    @pytest.mark.asyncio
    async def test_a_failed_statement_is_not_cached_and_surfaces(self) -> None:
        from api.queries.search_queries import SearchTimedOut, execute_search

        valkey = AsyncMock()
        valkey.get = AsyncMock(return_value=None)
        pool = TestSearchQueryAsyncFunctions._db_mocks()
        with (
            patch("api.queries.search_queries.execute_sql", AsyncMock(side_effect=SearchTimedOut())),
            pytest.raises(SearchTimedOut),
        ):
            await execute_search(pool, valkey, "love", ["release"], [], None, None, 20, 0)
        valkey.setex.assert_not_called()
