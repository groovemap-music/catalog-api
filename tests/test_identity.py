"""Tests for api/identity.py — the read-only native id lookup (ADR 0009)."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest
from common.identity import AliasRef


RELEASE_NATIVE = UUID("01890a5d-ac96-774b-bcce-b302099a8057")
ARTIST_NATIVE = UUID("01890a5d-ac96-774b-bcce-b302099a8058")


def _pool_returning(rows: list[Any]) -> MagicMock:
    """Build a pool whose single cursor returns `rows` from fetchall().

    The cursor is exposed as `pool.cursor` so a test can assert on the statement and the
    bound parameters without reaching through three context managers.
    """
    cursor = AsyncMock()
    cursor.__aenter__.return_value = cursor
    cursor.__aexit__.return_value = False
    cursor.fetchall.return_value = rows

    conn = AsyncMock()
    conn.__aenter__.return_value = conn
    conn.__aexit__.return_value = False
    conn.cursor = MagicMock(return_value=cursor)

    pool = MagicMock()
    pool.connection = MagicMock(return_value=conn)
    pool.cursor = cursor
    return pool


class TestCatalogRef:
    """`catalog_ref` filters the kinds the alias table can never key."""

    def test_builds_ref_for_catalog_kind(self) -> None:
        from api.identity import catalog_ref

        assert catalog_ref("artist", 123) == AliasRef("discogs", "artist", "123")

    @pytest.mark.parametrize("kind", ["genre", "style"])
    def test_none_for_name_keyed_kind(self, kind: str) -> None:
        from api.identity import catalog_ref

        assert catalog_ref(kind, "Rock") is None

    def test_none_for_missing_id(self) -> None:
        from api.identity import catalog_ref

        assert catalog_ref("release", None) is None
        assert catalog_ref("release", "") is None

    def test_none_for_a_provider_outside_the_vocabulary(self) -> None:
        """`AliasRef` validates the provider, and that rejection must not reach a response."""
        from api.identity import catalog_ref

        assert catalog_ref("release", "1", provider="spotify") is None


class TestLookupNativeIds:
    """One SELECT per batch, and no mint."""

    @pytest.mark.asyncio
    async def test_empty_refs_issues_no_statement(self, mock_conn: MagicMock, mock_cur: MagicMock) -> None:
        from api.identity import lookup_native_ids

        assert await lookup_native_ids(mock_conn, []) == {}
        mock_cur.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_maps_rows_onto_refs(self, mock_conn: MagicMock, mock_cur: MagicMock) -> None:
        from api.identity import lookup_native_ids

        mock_cur.fetchall.return_value = [("discogs", "release", "1", RELEASE_NATIVE)]
        resolved = await lookup_native_ids(
            mock_conn,
            [AliasRef("discogs", "release", "1"), AliasRef("discogs", "release", "2")],
        )

        assert resolved == {AliasRef("discogs", "release", "1"): RELEASE_NATIVE}
        # A ref with no valid alias is absent rather than minted.
        assert AliasRef("discogs", "release", "2") not in resolved

    @pytest.mark.asyncio
    async def test_batches_duplicates_into_one_statement(self, mock_conn: MagicMock, mock_cur: MagicMock) -> None:
        from api.identity import lookup_native_ids

        refs = [AliasRef("discogs", "release", "2"), AliasRef("discogs", "release", "1"), AliasRef("discogs", "release", "2")]
        await lookup_native_ids(mock_conn, refs)

        assert mock_cur.execute.call_count == 1
        params = mock_cur.execute.call_args.args[1]
        # Deduplicated and deterministically ordered, so two callers send the same arrays.
        assert params == (["discogs", "discogs"], ["release", "release"], ["1", "2"])


class TestResolveNativeIds:
    """The pool-acquiring entry point degrades instead of failing a response."""

    @pytest.mark.asyncio
    async def test_returns_empty_without_a_pool(self) -> None:
        from api.identity import configure, resolve_native_ids

        configure(None)
        assert await resolve_native_ids([AliasRef("discogs", "release", "1")]) == {}

    @pytest.mark.asyncio
    async def test_uses_the_configured_pool(self) -> None:
        from api.identity import configure, resolve_native_ids

        pool = _pool_returning([("discogs", "release", "1", RELEASE_NATIVE)])
        configure(pool)
        try:
            resolved = await resolve_native_ids([AliasRef("discogs", "release", "1")])
        finally:
            configure(None)

        assert resolved == {AliasRef("discogs", "release", "1"): RELEASE_NATIVE}

    @pytest.mark.asyncio
    async def test_empty_refs_never_reach_the_pool(self) -> None:
        from api.identity import resolve_native_ids

        pool = _pool_returning([])
        assert await resolve_native_ids([], pool=pool) == {}
        pool.connection.assert_not_called()

    @pytest.mark.asyncio
    async def test_swallows_a_database_failure(self) -> None:
        from api.identity import resolve_native_ids

        pool = MagicMock()
        pool.connection = MagicMock(side_effect=RuntimeError("alias table unreachable"))

        assert await resolve_native_ids([AliasRef("discogs", "release", "1")], pool=pool) == {}


class TestNativeIdsForHelpers:
    """The shapes the response paths actually consume."""

    @pytest.mark.asyncio
    async def test_native_ids_for_returns_string_ids(self) -> None:
        from api.identity import native_ids_for

        pool = _pool_returning([("discogs", "release", "1", RELEASE_NATIVE)])
        assert await native_ids_for("release", [1, 2], pool=pool) == {"1": str(RELEASE_NATIVE)}

    @pytest.mark.asyncio
    async def test_native_ids_for_pairs_keys_on_kind_and_id(self) -> None:
        from api.identity import native_ids_for_pairs

        pool = _pool_returning([("discogs", "artist", "7", ARTIST_NATIVE)])
        resolved = await native_ids_for_pairs([("artist", 7), ("genre", "Rock")], pool=pool)

        assert resolved == {("artist", "7"): str(ARTIST_NATIVE)}

    @pytest.mark.asyncio
    async def test_no_statement_when_every_kind_is_name_keyed(self) -> None:
        from api.identity import native_ids_for_pairs

        pool = _pool_returning([])
        assert await native_ids_for_pairs([("genre", "Rock"), ("style", "Punk")], pool=pool) == {}
        pool.connection.assert_not_called()


class TestNativeIdCache:
    """The per-request cache remembers both hits and misses."""

    @pytest.mark.asyncio
    async def test_second_resolve_issues_no_statement(self) -> None:
        from api.identity import NativeIdCache

        pool = _pool_returning([("discogs", "release", "1", RELEASE_NATIVE)])
        cache = NativeIdCache(pool)
        refs = [AliasRef("discogs", "release", "1")]

        assert await cache.resolve(refs) == {refs[0]: RELEASE_NATIVE}
        assert await cache.resolve(refs) == {refs[0]: RELEASE_NATIVE}
        assert pool.connection.call_count == 1

    @pytest.mark.asyncio
    async def test_a_miss_is_asked_for_once(self) -> None:
        from api.identity import NativeIdCache

        pool = _pool_returning([])
        cache = NativeIdCache(pool)
        refs = [AliasRef("discogs", "release", "9")]

        assert await cache.resolve(refs) == {}
        assert await cache.resolve(refs) == {}
        assert pool.connection.call_count == 1

    @pytest.mark.asyncio
    async def test_native_id_stringifies_a_resolved_ref(self) -> None:
        from api.identity import NativeIdCache

        pool = _pool_returning([("discogs", "release", "1", RELEASE_NATIVE)])
        cache = NativeIdCache(pool)
        ref = AliasRef("discogs", "release", "1")

        assert cache.native_id(ref) is None
        await cache.resolve([ref])
        assert cache.native_id(ref) == str(RELEASE_NATIVE)


class TestLookupOwnedCopyIds:
    """The owned copy is read from the collection row, owner-scoped."""

    @pytest.mark.asyncio
    async def test_maps_release_ids_to_copies(self) -> None:
        from api.identity import lookup_owned_copy_ids

        pool = _pool_returning([(1, RELEASE_NATIVE), (2, None)])
        resolved = await lookup_owned_copy_ids("user-1", ["1", "2"], pool=pool)

        assert resolved == {"1": str(RELEASE_NATIVE)}

    @pytest.mark.asyncio
    async def test_query_is_scoped_to_the_caller(self) -> None:
        from api.identity import lookup_owned_copy_ids

        pool = _pool_returning([])
        await lookup_owned_copy_ids("user-1", ["1", "2"], pool=pool)

        params = pool.cursor.execute.call_args.args[1]
        assert params == ("user-1", [1, 2])

    @pytest.mark.asyncio
    async def test_non_numeric_release_ids_are_dropped(self) -> None:
        from api.identity import lookup_owned_copy_ids

        pool = _pool_returning([])
        assert await lookup_owned_copy_ids("user-1", ["not-a-release"], pool=pool) == {}
        pool.connection.assert_not_called()

    @pytest.mark.asyncio
    async def test_returns_empty_without_a_pool_or_user(self) -> None:
        from api.identity import configure, lookup_owned_copy_ids

        configure(None)
        assert await lookup_owned_copy_ids("user-1", ["1"]) == {}
        assert await lookup_owned_copy_ids("", ["1"], pool=_pool_returning([])) == {}

    @pytest.mark.asyncio
    async def test_swallows_a_database_failure(self) -> None:
        from api.identity import lookup_owned_copy_ids

        pool = MagicMock()
        pool.connection = MagicMock(side_effect=RuntimeError("collection unreachable"))

        assert await lookup_owned_copy_ids("user-1", ["1"], pool=pool) == {}
