"""Tests for GET /api/lookup/{provider}/{value} and its query layer (ADR 0011)."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from api.queries.lookup_queries import releases_for_native_id, resolve_alias_native_id, resolve_alias_native_ids
from api.routers.lookup import _barcode_forms, lookup_providers, normalize_lookup_value


NATIVE_ID = UUID("018f3f7a-0000-7000-8000-000000000001")
OTHER_NATIVE_ID = UUID("018f3f7a-0000-7000-8000-000000000002")

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


class TestBarcodeForms:
    """ADR 0011's amendment: GTIN-12, GTIN-13, and GTIN-14 are one number space — a shorter
    GTIN is the same GTIN zero-padded to 14 digits. Every other shape is an exact match only."""

    def test_twelve_digits_also_probes_both_zero_padded_forms(self) -> None:
        assert _barcode_forms("036000291452") == ("036000291452", "0036000291452", "00036000291452")

    def test_thirteen_digits_with_leading_zero_also_probes_the_bare_and_double_padded_forms(self) -> None:
        assert _barcode_forms("0036000291452") == ("036000291452", "0036000291452", "00036000291452")

    def test_thirteen_digits_without_leading_zero_also_probes_its_fourteen_digit_padded_form(self) -> None:
        """A 13-digit EAN-13 that isn't a zero-prefixed UPC-A has no 12-digit equivalent, but
        it is still the same GTIN as its own single-zero-padded 14-digit form."""
        assert _barcode_forms("5012394144777") == ("5012394144777", "05012394144777")

    def test_fourteen_digits_with_two_leading_zeros_probes_all_three_shorter_lengths(self) -> None:
        assert _barcode_forms("00036000291452") == ("036000291452", "0036000291452", "00036000291452")

    def test_fourteen_digits_with_one_leading_zero_probes_only_the_thirteen_digit_form(self) -> None:
        """The second digit is nonzero, so stripping two leading zeros isn't possible."""
        assert _barcode_forms("05012394144777") == ("5012394144777", "05012394144777")

    def test_fourteen_digits_with_a_nonzero_indicator_is_exact_only(self) -> None:
        """GS1 reserves indicator digits 1-9 for a different trade item (e.g. a case), so this
        is not the same GTIN as anything shorter."""
        assert _barcode_forms("10360002914527") == ("10360002914527",)

    @pytest.mark.parametrize("value", ["", "1", "12345", "12345678"])
    def test_other_lengths_are_exact_only(self, value: str) -> None:
        assert _barcode_forms(value) == (value,)


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
    async def test_resolve_alias_ids_probes_every_form_in_one_query(self) -> None:
        rows = [{"external_id": "0036000291452", "native_id": NATIVE_ID}]
        pool = _make_pool(fetchall=rows)
        with patch("api.queries.lookup_queries.execute_sql", new_callable=AsyncMock) as mock_exec:
            resolved = await resolve_alias_native_ids(pool, "barcode", ("036000291452", "0036000291452"))

        assert resolved == {"0036000291452": NATIVE_ID}
        statement, params = mock_exec.call_args[0][1], mock_exec.call_args[0][2]
        assert "valid_to IS NULL" in statement
        assert "= ANY(" in statement
        assert params == ("barcode", "release", ["036000291452", "0036000291452"])

    @pytest.mark.asyncio
    async def test_resolve_alias_ids_omits_forms_with_no_row(self) -> None:
        pool = _make_pool(fetchall=[])
        with patch("api.queries.lookup_queries.execute_sql", new_callable=AsyncMock):
            resolved = await resolve_alias_native_ids(pool, "barcode", ("036000291452", "0036000291452"))

        assert resolved == {}

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
            # 8 digits (EAN-8/UPC-E) has no equivalent form under ADR 0011's amendment, so
            # this stays the plain single-value path shared with the other two providers.
            ("barcode", "1234-5678", "12345678"),
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
            response = test_client.get("/api/lookup/barcode/12345678")

        assert response.status_code == 200
        body = response.json()
        assert [release["source"] for release in body["releases"]] == ["discogs", "musicbrainz"]
        assert body["releases"][1]["artist"] is None

    def test_unknown_value_is_404(self, test_client: TestClient) -> None:
        resolve, fetch = _patched_lookup(None, [])
        with resolve, fetch:
            response = test_client.get("/api/lookup/barcode/00000000")

        assert response.status_code == 404
        assert "barcode" in response.json()["error"]

    def test_alias_with_no_loaded_release_is_404(self, test_client: TestClient) -> None:
        """A dangling alias is the same answer: nothing to show the caller."""
        resolve, fetch = _patched_lookup(NATIVE_ID, [])
        with resolve, fetch:
            response = test_client.get("/api/lookup/barcode/12345678")

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
            response = test_client.get("/api/lookup/barcode/12345678")

        assert response.status_code == 200
        assert "WWW-Authenticate" not in response.headers


def _patched_forms_lookup(resolved: dict[str, UUID], releases_by_native_id: dict[UUID, list[dict[str, Any]]]) -> Any:
    """Patch the batched multi-form resolver and the per-item releases fetch."""

    async def _fetch(_pool: Any, native_id: UUID) -> list[dict[str, Any]]:
        return releases_by_native_id.get(native_id, [])

    return (
        patch("api.routers.lookup.resolve_alias_native_ids", AsyncMock(return_value=resolved)),
        patch("api.routers.lookup.releases_for_native_id", AsyncMock(side_effect=_fetch)),
    )


class TestLookupBarcodeEquivalence:
    """ADR 0011's amendment: GTIN-12, GTIN-13, and GTIN-14 are one number space at lookup —
    resolved through the one batched query — without re-keying any stored alias."""

    def test_twelve_digit_form_finds_an_item_stored_under_the_thirteen_digit_form(self, test_client: TestClient) -> None:
        resolve, fetch = _patched_forms_lookup({"0036000291452": NATIVE_ID}, {NATIVE_ID: [DISCOGS_ROW]})
        with resolve as mock_resolve, fetch:
            response = test_client.get("/api/lookup/barcode/036000291452")

        assert response.status_code == 200
        body = response.json()
        assert body["gm_id"] == str(NATIVE_ID)
        assert body["releases"] == [DISCOGS_ROW]
        assert body["matches"] == []
        assert mock_resolve.await_args[0][1:] == ("barcode", ("036000291452", "0036000291452", "00036000291452"))

    def test_twelve_digit_form_finds_an_item_stored_under_the_fourteen_digit_form(self, test_client: TestClient) -> None:
        resolve, fetch = _patched_forms_lookup({"00036000291452": NATIVE_ID}, {NATIVE_ID: [DISCOGS_ROW]})
        with resolve, fetch:
            response = test_client.get("/api/lookup/barcode/036000291452")

        assert response.status_code == 200
        body = response.json()
        assert body["gm_id"] == str(NATIVE_ID)
        assert body["matches"] == []

    def test_thirteen_digit_form_finds_an_item_stored_under_the_twelve_digit_form(self, test_client: TestClient) -> None:
        resolve, fetch = _patched_forms_lookup({"036000291452": NATIVE_ID}, {NATIVE_ID: [DISCOGS_ROW]})
        with resolve as mock_resolve, fetch:
            response = test_client.get("/api/lookup/barcode/0036000291452")

        assert response.status_code == 200
        body = response.json()
        assert body["gm_id"] == str(NATIVE_ID)
        assert body["releases"] == [DISCOGS_ROW]
        assert body["matches"] == []
        assert mock_resolve.await_args[0][1:] == ("barcode", ("036000291452", "0036000291452", "00036000291452"))

    def test_thirteen_digit_nonzero_leading_form_finds_an_item_under_its_fourteen_digit_form(self, test_client: TestClient) -> None:
        """A 13-digit EAN-13 that isn't a zero-prefixed UPC-A still shares a GTIN with its own
        zero-padded 14-digit spelling."""
        resolve, fetch = _patched_forms_lookup({"05012394144777": NATIVE_ID}, {NATIVE_ID: [DISCOGS_ROW]})
        with resolve as mock_resolve, fetch:
            response = test_client.get("/api/lookup/barcode/5012394144777")

        assert response.status_code == 200
        body = response.json()
        assert body["gm_id"] == str(NATIVE_ID)
        assert body["matches"] == []
        assert mock_resolve.await_args[0][1:] == ("barcode", ("5012394144777", "05012394144777"))

    def test_fourteen_digit_double_padded_form_finds_items_under_both_shorter_forms(self, test_client: TestClient) -> None:
        resolve, fetch = _patched_forms_lookup({"036000291452": NATIVE_ID}, {NATIVE_ID: [DISCOGS_ROW]})
        with resolve as mock_resolve, fetch:
            response = test_client.get("/api/lookup/barcode/00036000291452")

        assert response.status_code == 200
        assert response.json()["gm_id"] == str(NATIVE_ID)
        assert mock_resolve.await_args[0][1:] == ("barcode", ("036000291452", "0036000291452", "00036000291452"))

    def test_fourteen_digit_single_padded_form_finds_the_thirteen_digit_form(self, test_client: TestClient) -> None:
        resolve, fetch = _patched_forms_lookup({"5012394144777": NATIVE_ID}, {NATIVE_ID: [DISCOGS_ROW]})
        with resolve as mock_resolve, fetch:
            response = test_client.get("/api/lookup/barcode/05012394144777")

        assert response.status_code == 200
        assert response.json()["gm_id"] == str(NATIVE_ID)
        assert mock_resolve.await_args[0][1:] == ("barcode", ("5012394144777", "05012394144777"))

    def test_exact_form_hit_is_unchanged_when_every_form_names_the_same_item(self, test_client: TestClient) -> None:
        """All resolved rows naming one native id is still the plain single-hit shape."""
        resolve, fetch = _patched_forms_lookup(
            {"036000291452": NATIVE_ID, "0036000291452": NATIVE_ID, "00036000291452": NATIVE_ID},
            {NATIVE_ID: [DISCOGS_ROW]},
        )
        with resolve, fetch:
            response = test_client.get("/api/lookup/barcode/036000291452")

        body = response.json()
        assert body["gm_id"] == str(NATIVE_ID)
        assert body["matches"] == []

    def test_two_items_stored_under_two_forms_of_one_gtin_are_both_returned(self, test_client: TestClient) -> None:
        """Two different native items minted under two forms of one GTIN: both come back, not
        merged and not treated as a split."""
        resolve, fetch = _patched_forms_lookup(
            {"036000291452": NATIVE_ID, "0036000291452": OTHER_NATIVE_ID},
            {NATIVE_ID: [DISCOGS_ROW], OTHER_NATIVE_ID: [MUSICBRAINZ_ROW]},
        )
        with resolve, fetch:
            response = test_client.get("/api/lookup/barcode/036000291452")

        assert response.status_code == 200
        body = response.json()
        # "036000291452" is the exact match for the typed value, so it sorts first regardless
        # of native id ordering.
        assert body["gm_id"] == str(NATIVE_ID)
        assert body["releases"] == [DISCOGS_ROW]
        assert [(match["gm_id"], match["external_id"]) for match in body["matches"]] == [
            (str(NATIVE_ID), "036000291452"),
            (str(OTHER_NATIVE_ID), "0036000291452"),
        ]
        assert body["matches"][0]["releases"] == [DISCOGS_ROW]
        assert body["matches"][1]["releases"] == [MUSICBRAINZ_ROW]

    def test_matches_are_ordered_exact_match_first_even_when_longer(self, test_client: TestClient) -> None:
        """Typing the 14-digit form: the exact match sorts first even though it is the
        longest stored value, ahead of the shorter, non-exact stored value."""
        resolve, fetch = _patched_forms_lookup(
            {"036000291452": OTHER_NATIVE_ID, "00036000291452": NATIVE_ID},
            {NATIVE_ID: [DISCOGS_ROW], OTHER_NATIVE_ID: [MUSICBRAINZ_ROW]},
        )
        with resolve, fetch:
            response = test_client.get("/api/lookup/barcode/00036000291452")

        assert response.status_code == 200
        body = response.json()
        assert [(m["gm_id"], m["external_id"]) for m in body["matches"]] == [
            (str(NATIVE_ID), "00036000291452"),
            (str(OTHER_NATIVE_ID), "036000291452"),
        ]
        assert body["gm_id"] == str(NATIVE_ID)

    def test_matches_fall_back_to_shortest_stored_value_when_no_row_is_exact(self, test_client: TestClient) -> None:
        """Typing the 12-digit form, with rows only under the 13- and 14-digit forms: neither
        is an exact match, so the shorter stored value sorts first."""
        resolve, fetch = _patched_forms_lookup(
            {"00036000291452": OTHER_NATIVE_ID, "0036000291452": NATIVE_ID},
            {NATIVE_ID: [DISCOGS_ROW], OTHER_NATIVE_ID: [MUSICBRAINZ_ROW]},
        )
        with resolve, fetch:
            response = test_client.get("/api/lookup/barcode/036000291452")

        assert response.status_code == 200
        body = response.json()
        assert [(m["gm_id"], m["external_id"]) for m in body["matches"]] == [
            (str(NATIVE_ID), "0036000291452"),
            (str(OTHER_NATIVE_ID), "00036000291452"),
        ]
        assert body["gm_id"] == str(NATIVE_ID)

    def test_a_native_id_reached_through_two_forms_is_not_merged_into_one_entry(self, test_client: TestClient) -> None:
        """A second, genuinely different item is also present, so the multi-entry shape
        applies — and the one item reached through two forms gets two entries, not one."""
        resolve, fetch = _patched_forms_lookup(
            {"036000291452": NATIVE_ID, "0036000291452": NATIVE_ID, "00036000291452": OTHER_NATIVE_ID},
            {NATIVE_ID: [DISCOGS_ROW], OTHER_NATIVE_ID: [MUSICBRAINZ_ROW]},
        )
        with resolve, fetch:
            response = test_client.get("/api/lookup/barcode/036000291452")

        assert response.status_code == 200
        body = response.json()
        assert [(m["gm_id"], m["external_id"]) for m in body["matches"]] == [
            (str(NATIVE_ID), "036000291452"),
            (str(NATIVE_ID), "0036000291452"),
            (str(OTHER_NATIVE_ID), "00036000291452"),
        ]

    def test_result_count_recorded_sums_releases_across_distinct_items_only(self, test_client: TestClient, auth_headers: dict[str, str]) -> None:
        """One item reached through two forms contributes its releases once, not twice."""
        resolve, fetch = _patched_forms_lookup(
            {"036000291452": NATIVE_ID, "0036000291452": NATIVE_ID, "00036000291452": OTHER_NATIVE_ID},
            {NATIVE_ID: [DISCOGS_ROW], OTHER_NATIVE_ID: [MUSICBRAINZ_ROW]},
        )
        with resolve, fetch, patch("api.activity.record_event", new_callable=AsyncMock) as mock_record:
            response = test_client.get("/api/lookup/barcode/036000291452", headers=auth_headers)

        assert response.status_code == 200
        assert mock_record.await_args[0][2]["result_count"] == 2

    def test_dangling_alternate_form_alone_is_still_404(self, test_client: TestClient) -> None:
        """The alternate form resolves, but names no loaded release row: nothing to show."""
        resolve, fetch = _patched_forms_lookup({"0036000291452": NATIVE_ID}, {NATIVE_ID: []})
        with resolve, fetch:
            response = test_client.get("/api/lookup/barcode/036000291452")

        assert response.status_code == 404

    def test_dangling_form_is_dropped_leaving_a_single_hit(self, test_client: TestClient) -> None:
        """One of two resolved rows has no loaded release: it drops out, leaving one item and
        the plain single-hit shape rather than a one-entry `matches` list."""
        resolve, fetch = _patched_forms_lookup(
            {"036000291452": NATIVE_ID, "0036000291452": OTHER_NATIVE_ID},
            {NATIVE_ID: [DISCOGS_ROW], OTHER_NATIVE_ID: []},
        )
        with resolve, fetch:
            response = test_client.get("/api/lookup/barcode/036000291452")

        assert response.status_code == 200
        body = response.json()
        assert body["gm_id"] == str(NATIVE_ID)
        assert body["matches"] == []

    def test_fourteen_digit_nonzero_indicator_never_expands(self, test_client: TestClient) -> None:
        """GS1's indicator digit marks a different trade item, so the plain single-form
        resolver is used, not the batch."""
        resolve, fetch = _patched_lookup(NATIVE_ID, [DISCOGS_ROW])
        with resolve as mock_resolve, fetch, patch("api.routers.lookup.resolve_alias_native_ids", AsyncMock()) as mock_resolve_many:
            response = test_client.get("/api/lookup/barcode/10360002914527")

        assert response.status_code == 200
        mock_resolve.assert_awaited_once()
        mock_resolve_many.assert_not_awaited()

    def test_eight_digit_barcode_never_expands(self, test_client: TestClient) -> None:
        """EAN-8/UPC-E has no equivalent form under this amendment."""
        resolve, fetch = _patched_lookup(NATIVE_ID, [DISCOGS_ROW])
        with resolve as mock_resolve, fetch, patch("api.routers.lookup.resolve_alias_native_ids", AsyncMock()) as mock_resolve_many:
            response = test_client.get("/api/lookup/barcode/12345678")

        assert response.status_code == 200
        mock_resolve.assert_awaited_once()
        mock_resolve_many.assert_not_awaited()

    def test_catalog_number_is_never_expanded(self, test_client: TestClient) -> None:
        """A 12-character catalogue number is not a barcode; equivalence is barcode-only."""
        resolve, fetch = _patched_lookup(NATIVE_ID, [DISCOGS_ROW])
        with resolve as mock_resolve, fetch, patch("api.routers.lookup.resolve_alias_native_ids", AsyncMock()) as mock_resolve_many:
            response = test_client.get("/api/lookup/catalog_number/ABCDEFGHIJKL")

        assert response.status_code == 200
        mock_resolve.assert_awaited_once()
        mock_resolve_many.assert_not_awaited()


class TestLookupActivity:
    """ADR 0010: a lookup is recorded as the search it is, for a caller with a subject."""

    def test_anonymous_lookup_records_nothing(self, test_client: TestClient) -> None:
        resolve, fetch = _patched_lookup(NATIVE_ID, [DISCOGS_ROW])
        with resolve, fetch, patch("api.activity.record_event", new_callable=AsyncMock) as mock_record:
            test_client.get("/api/lookup/barcode/12345678")

        mock_record.assert_not_awaited()

    def test_signed_in_lookup_records_a_search_query(self, test_client: TestClient, auth_headers: dict[str, str]) -> None:
        resolve, fetch = _patched_lookup(NATIVE_ID, [DISCOGS_ROW])
        with resolve, fetch, patch("api.activity.record_event", new_callable=AsyncMock) as mock_record:
            response = test_client.get("/api/lookup/barcode/1234 5678", headers=auth_headers)

        assert response.status_code == 200
        mock_record.assert_awaited_once()
        _user_id, event_type, payload = mock_record.await_args[0]
        assert event_type == "search.query"
        assert payload["filters"] == ["lookup:barcode"]
        assert payload["query"] == "1234 5678"
        assert payload["result_count"] == 1

    def test_a_miss_is_recorded_too(self, test_client: TestClient, auth_headers: dict[str, str]) -> None:
        """A barcode the catalog cannot resolve is the most useful thing this surface learns."""
        resolve, fetch = _patched_lookup(None, [])
        with resolve, fetch, patch("api.activity.record_event", new_callable=AsyncMock) as mock_record:
            response = test_client.get("/api/lookup/barcode/00000000", headers=auth_headers)

        assert response.status_code == 404
        mock_record.assert_awaited_once()
        assert mock_record.await_args[0][2]["result_count"] == 0
