"""Native identity, owned copies, snapshots, and change events in the sync.

ADR 0009 makes the sync the point where a Discogs release id becomes a native
catalog item and a collection row becomes an owned copy; ADR 0010 makes the
sync diff the origin of the collection and wantlist change events. Both are
exercised here against a scripted cursor rather than the blanket mock in
``test_syncer.py``: these tests care which statement produced which rows, and a
cursor that answers every query with ``[]`` cannot express a re-sync.

Payloads are asserted whole rather than key by key: the published payload
schemas are additionalProperties: false, so an extra key is a contract break
and a test that only checked the keys it expected would not catch one.

Offline, like the rest of the suite — no PostgreSQL, no Neo4j, no Discogs.
"""

import hashlib
from collections.abc import Generator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
from common.identity import AliasRef

import api.syncer as syncer
from api.syncer import sync_collection, sync_wantlist


TEST_USER_UUID = UUID("00000000-0000-0000-0000-000000000001")
TEST_DISCOGS_USERNAME = "test_dj"
TEST_CREDENTIALS = ("consumer_key", "consumer_secret", "access_token", "token_secret")
TEST_USER_AGENT = "TestApp/1.0"

ITEM_1 = UUID("11111111-1111-7111-8111-111111111111")
ITEM_2 = UUID("22222222-2222-7222-8222-222222222222")
ROW_1 = UUID("aaaaaaaa-0000-4000-8000-000000000001")
ROW_2 = UUID("aaaaaaaa-0000-4000-8000-000000000002")
COPY_1 = UUID("cccccccc-0000-7000-8000-000000000001")
COPY_2 = UUID("cccccccc-0000-7000-8000-000000000002")

DATE_ADDED = datetime(2025, 1, 1, tzinfo=UTC)

# The statement fragments a scripted response is keyed by. Each names exactly
# one of the statements a sync page issues, so a script reads as a sequence of
# database answers rather than a sequence of positions.
PAGE_ROWS = "SELECT id, release_id"
WANT_ROWS = "SELECT release_id"
MINT_COPIES = "INSERT INTO owned_copies"
LINK_COPIES = "UPDATE user_collections"
SWEEP_COLLECTION = "DELETE FROM user_collections"
SWEEP_WANTLIST = "DELETE FROM user_wantlists"
SNAPSHOT_COPIES = "SELECT id FROM owned_copies"
SNAPSHOT_INSERT = "INSERT INTO collection_snapshots"


class ScriptedCursor:
    """An async cursor whose result set is chosen by the statement it is given.

    Each key of ``script`` is a fragment identifying one statement, and its
    value is the queue of result sets that statement returns, in the order the
    sync issues it — which is what lets one script express a page's before-state
    and after-state with the same ``SELECT``.
    """

    def __init__(self, script: dict[str, list[list[Any]]] | None = None) -> None:
        self._script = {fragment: list(results) for fragment, results in (script or {}).items()}
        self.executed: list[tuple[str, Any]] = []
        self.executed_many: list[tuple[str, list[Any]]] = []
        self._rows: list[Any] = []

    async def execute(self, query: str, params: Any = None) -> None:
        self.executed.append((query, params))
        self._rows = self._answer(query)

    async def executemany(self, query: str, params_seq: list[Any]) -> None:
        self.executed_many.append((query, list(params_seq)))
        self._rows = []

    async def fetchall(self) -> list[Any]:
        return self._rows

    async def fetchone(self) -> Any:
        return self._rows[0] if self._rows else None

    def _answer(self, query: str) -> list[Any]:
        for fragment, results in self._script.items():
            if fragment in query:
                return results.pop(0) if results else []
        return []

    def statements(self, fragment: str) -> list[tuple[str, Any]]:
        """Every executed statement containing `fragment`, in order."""
        return [entry for entry in self.executed if fragment in entry[0]]

    def only(self, fragment: str) -> tuple[str, Any]:
        """The single executed statement containing `fragment`."""
        matches = self.statements(fragment)
        assert len(matches) == 1, f"expected one {fragment!r} statement, got {len(matches)}"
        return matches[0]


def scripted_pool(cursor: ScriptedCursor) -> MagicMock:
    """A pool whose every connection and cursor is the scripted one."""
    cur_ctx = AsyncMock()
    cur_ctx.__aenter__ = AsyncMock(return_value=cursor)
    cur_ctx.__aexit__ = AsyncMock(return_value=False)

    tx_ctx = AsyncMock()
    tx_ctx.__aenter__ = AsyncMock(return_value=tx_ctx)
    tx_ctx.__aexit__ = AsyncMock(return_value=False)

    conn = AsyncMock()
    conn.cursor = MagicMock(return_value=cur_ctx)
    conn.transaction = MagicMock(return_value=tx_ctx)

    conn_ctx = AsyncMock()
    conn_ctx.__aenter__ = AsyncMock(return_value=conn)
    conn_ctx.__aexit__ = AsyncMock(return_value=False)

    pool = MagicMock()
    pool.connection = MagicMock(return_value=conn_ctx)
    return pool


@pytest.fixture
def mock_neo4j() -> MagicMock:
    """Mock AsyncResilientNeo4jDriver."""
    driver = MagicMock()
    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.run = AsyncMock()
    driver.session = MagicMock(return_value=session)
    driver._mock_session = session
    return driver


@pytest.fixture
def recorder() -> Generator[AsyncMock]:
    """Wire a recording event recorder, and put the module back afterwards."""
    recording = AsyncMock(return_value=None)
    syncer.configure(recording)
    yield recording
    syncer.configure(None)


def events_of(recorder: AsyncMock) -> list[tuple[str, dict[str, Any]]]:
    """The (event_type, payload) pairs the recorder was handed, in order."""
    return [(call.args[1], call.args[2]) for call in recorder.await_args_list]


def collection_row(
    release_id: int,
    row_id: UUID,
    item_id: UUID | None,
    *,
    owned_copy_id: UUID | None = None,
    rating: int = 4,
    folder_id: int = 1,
    date_added: datetime = DATE_ADDED,
) -> dict[str, Any]:
    """One `user_collections` row as the page SELECT returns it."""
    return {
        "id": row_id,
        "release_id": release_id,
        "instance_id": 1000 + release_id,
        "gm_item_id": item_id,
        "owned_copy_id": owned_copy_id,
        "folder_id": folder_id,
        "rating": rating,
        "date_added": date_added,
    }


def release_item(release_id: int, **overrides: Any) -> dict[str, Any]:
    """One Discogs collection item."""
    item: dict[str, Any] = {
        "instance_id": 1000 + release_id,
        "folder_id": 1,
        "rating": 4,
        "date_added": "2025-01-01T00:00:00Z",
        "basic_information": {
            "id": release_id,
            "title": f"Album {release_id}",
            "year": 2020,
            "artists": [{"name": f"Artist {release_id}"}],
            "labels": [{"name": f"Label {release_id}"}],
            "formats": [{"name": "Vinyl"}],
        },
    }
    item.update(overrides)
    return item


def want_item(release_id: int) -> dict[str, Any]:
    """One Discogs wantlist item (release id at the top level, not nested)."""
    return {
        "id": release_id,
        "rating": 3,
        "notes": "Want this!",
        "date_added": "2025-02-01T00:00:00Z",
        "basic_information": {
            "title": f"Want {release_id}",
            "year": 2021,
            "artists": [{"name": f"Artist {release_id}"}],
            "formats": [{"name": "CD"}],
        },
    }


def http_pages(payloads: list[dict[str, Any]]) -> MagicMock:
    """An httpx client class whose GET walks `payloads` page by page."""
    responses = []
    for payload in payloads:
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = payload
        responses.append(response)

    client = AsyncMock()
    client.get = AsyncMock(side_effect=responses)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client_cls = MagicMock(return_value=client)
    return client_cls


def collection_page(items: list[dict[str, Any]], page: int = 1, pages: int = 1) -> dict[str, Any]:
    return {"releases": items, "pagination": {"page": page, "pages": pages}}


def wantlist_page(items: list[dict[str, Any]], page: int = 1, pages: int = 1) -> dict[str, Any]:
    return {"wants": items, "pagination": {"page": page, "pages": pages}}


async def run_collection(pool: MagicMock, neo4j: MagicMock, payloads: list[dict[str, Any]], resolved: dict[AliasRef, UUID]) -> int:
    """Run a collection sync over `payloads` with `resolved` as the resolve result."""
    with (
        patch("api.syncer.httpx.AsyncClient", http_pages(payloads)),
        patch("api.syncer.asyncio.sleep", new_callable=AsyncMock),
        patch("api.syncer.resolve_aliases", new_callable=AsyncMock) as resolver,
    ):
        resolver.return_value = resolved
        total = await sync_collection(TEST_USER_UUID, TEST_DISCOGS_USERNAME, *TEST_CREDENTIALS, TEST_USER_AGENT, pool, neo4j)
        run_collection.resolver = resolver  # type: ignore[attr-defined]
    return total


async def run_wantlist(pool: MagicMock, neo4j: MagicMock, payloads: list[dict[str, Any]], resolved: dict[AliasRef, UUID]) -> int:
    """Run a wantlist sync over `payloads` with `resolved` as the resolve result."""
    with (
        patch("api.syncer.httpx.AsyncClient", http_pages(payloads)),
        patch("api.syncer.asyncio.sleep", new_callable=AsyncMock),
        patch("api.syncer.resolve_aliases", new_callable=AsyncMock) as resolver,
    ):
        resolver.return_value = resolved
        total = await sync_wantlist(TEST_USER_UUID, TEST_DISCOGS_USERNAME, *TEST_CREDENTIALS, TEST_USER_AGENT, pool, neo4j)
        run_wantlist.resolver = resolver  # type: ignore[attr-defined]
    return total


class TestFirstCollectionSync:
    """A collection the service has never seen: every row is new, so every row
    resolves to a native item, mints a copy, and earns an added event."""

    @pytest.fixture
    def cursor(self) -> ScriptedCursor:
        return ScriptedCursor(
            {
                PAGE_ROWS: [
                    [],  # before: nothing held
                    [collection_row(1, ROW_1, ITEM_1), collection_row(2, ROW_2, ITEM_2)],
                ],
                LINK_COPIES: [[{"id": ROW_1, "owned_copy_id": COPY_1}, {"id": ROW_2, "owned_copy_id": COPY_2}]],
                SNAPSHOT_COPIES: [[(COPY_2,), (COPY_1,)]],
            }
        )

    @pytest.mark.asyncio
    async def test_resolves_the_whole_page_in_one_call(self, cursor: ScriptedCursor, mock_neo4j: MagicMock) -> None:
        pool = scripted_pool(cursor)

        await run_collection(pool, mock_neo4j, [collection_page([release_item(1), release_item(2)])], {})

        resolver = run_collection.resolver  # type: ignore[attr-defined]
        resolver.assert_awaited_once()
        assert list(resolver.await_args.args[1]) == [
            AliasRef("discogs", "release", "1"),
            AliasRef("discogs", "release", "2"),
        ]

    @pytest.mark.asyncio
    async def test_one_resolve_call_per_page(self, mock_neo4j: MagicMock) -> None:
        cursor = ScriptedCursor()
        pool = scripted_pool(cursor)
        payloads = [
            collection_page([release_item(1)], page=1, pages=2),
            collection_page([release_item(2)], page=2, pages=2),
        ]

        await run_collection(pool, mock_neo4j, payloads, {})

        assert run_collection.resolver.await_count == 2  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_upsert_carries_gm_item_id_without_moving_updated_at(self, cursor: ScriptedCursor, mock_neo4j: MagicMock) -> None:
        pool = scripted_pool(cursor)
        resolved = {AliasRef("discogs", "release", "1"): ITEM_1, AliasRef("discogs", "release", "2"): ITEM_2}

        await run_collection(pool, mock_neo4j, [collection_page([release_item(1), release_item(2)])], resolved)

        upsert_sql, batch = cursor.executed_many[0]
        assert "gm_item_id" in upsert_sql
        assert "gm_item_id = COALESCE(EXCLUDED.gm_item_id, user_collections.gm_item_id)" in upsert_sql
        assert [params[13] for params in batch] == [ITEM_1, ITEM_2]
        # updated_at stays last: the reconciliation cutoff is read from there.
        assert all(isinstance(params[-1], datetime) for params in batch)

    @pytest.mark.asyncio
    async def test_mints_one_copy_per_row_and_links_it_back(self, cursor: ScriptedCursor, mock_neo4j: MagicMock) -> None:
        pool = scripted_pool(cursor)

        await run_collection(pool, mock_neo4j, [collection_page([release_item(1), release_item(2)])], {})

        mint_sql, mint_params = cursor.only(MINT_COPIES)
        assert "ON CONFLICT (collection_row_id) WHERE collection_row_id IS NOT NULL" in mint_sql
        assert mint_params == (str(TEST_USER_UUID), [ITEM_1, ITEM_2], [ROW_1, ROW_2], [DATE_ADDED, DATE_ADDED])

        link_sql, link_params = cursor.only(LINK_COPIES)
        assert "SET owned_copy_id = oc.id" in link_sql
        assert link_params == ([ROW_1, ROW_2],)

    @pytest.mark.asyncio
    async def test_writes_a_snapshot_of_sorted_copy_ids(self, cursor: ScriptedCursor, mock_neo4j: MagicMock) -> None:
        pool = scripted_pool(cursor)

        await run_collection(pool, mock_neo4j, [collection_page([release_item(1), release_item(2)])], {})

        _, params = cursor.only(SNAPSHOT_INSERT)
        user_id, taken_at, item_count, copy_ids, content_hash = params
        assert user_id == str(TEST_USER_UUID)
        assert isinstance(taken_at, datetime)
        assert item_count == 2
        # The select returned them out of order; the row records them sorted.
        assert copy_ids == [COPY_1, COPY_2]
        assert content_hash == hashlib.sha256(f"{COPY_1}\n{COPY_2}".encode()).digest()

    @pytest.mark.asyncio
    async def test_emits_one_added_event_per_row(self, cursor: ScriptedCursor, mock_neo4j: MagicMock, recorder: AsyncMock) -> None:
        pool = scripted_pool(cursor)

        await run_collection(pool, mock_neo4j, [collection_page([release_item(1), release_item(2)])], {})

        assert events_of(recorder) == [
            ("collection.item_added", {"item_id": str(ITEM_1), "artifact_id": None, "owned_copy_id": str(COPY_1)}),
            ("collection.item_added", {"item_id": str(ITEM_2), "artifact_id": None, "owned_copy_id": str(COPY_2)}),
        ]
        assert all(call.args[0] == str(TEST_USER_UUID) for call in recorder.await_args_list)


class TestCollectionResync:
    """The same collection synced twice: the second run must change nothing and
    say nothing, and the run after a real change must say exactly what moved."""

    @pytest.mark.asyncio
    async def test_unchanged_resync_emits_nothing_and_relinks_nothing(self, mock_neo4j: MagicMock, recorder: AsyncMock) -> None:
        held = collection_row(1, ROW_1, ITEM_1, owned_copy_id=COPY_1)
        cursor = ScriptedCursor(
            {
                PAGE_ROWS: [[held], [held]],  # before and after are the same row
                LINK_COPIES: [[]],  # the link was already correct
                SNAPSHOT_COPIES: [[(COPY_1,)]],
            }
        )
        pool = scripted_pool(cursor)

        await run_collection(pool, mock_neo4j, [collection_page([release_item(1)])], {})

        assert events_of(recorder) == []
        # The copy statements still run — they are what makes the re-sync
        # idempotent — but neither writes a row the database did not already have.
        assert len(cursor.statements(MINT_COPIES)) == 1
        assert len(cursor.statements(LINK_COPIES)) == 1

    @pytest.mark.asyncio
    async def test_snapshot_is_written_on_every_successful_sync(self, mock_neo4j: MagicMock) -> None:
        held = collection_row(1, ROW_1, ITEM_1, owned_copy_id=COPY_1)
        cursor = ScriptedCursor({PAGE_ROWS: [[held], [held]], SNAPSHOT_COPIES: [[(COPY_1,)]]})
        pool = scripted_pool(cursor)

        await run_collection(pool, mock_neo4j, [collection_page([release_item(1)])], {})

        _, params = cursor.only(SNAPSHOT_INSERT)
        assert params[2] == 1
        assert params[3] == [COPY_1]

    @pytest.mark.asyncio
    async def test_instance_change_emits_item_updated_with_the_changed_fields(self, mock_neo4j: MagicMock, recorder: AsyncMock) -> None:
        before = collection_row(1, ROW_1, ITEM_1, owned_copy_id=COPY_1, rating=3, folder_id=1)
        after = collection_row(1, ROW_1, ITEM_1, owned_copy_id=COPY_1, rating=5, folder_id=2)
        cursor = ScriptedCursor({PAGE_ROWS: [[before], [after]], SNAPSHOT_COPIES: [[(COPY_1,)]]})
        pool = scripted_pool(cursor)

        await run_collection(pool, mock_neo4j, [collection_page([release_item(1)])], {})

        assert events_of(recorder) == [
            (
                "collection.item_updated",
                {
                    "item_id": str(ITEM_1),
                    "artifact_id": None,
                    "owned_copy_id": str(COPY_1),
                    "changed_fields": ["folder_id", "rating"],
                },
            )
        ]

    @pytest.mark.asyncio
    async def test_provider_metadata_alone_is_not_an_update(self, mock_neo4j: MagicMock, recorder: AsyncMock) -> None:
        """A corrected title is the provider changing its mind, not the user
        changing what they record, so it earns no collection.item_updated."""
        held = collection_row(1, ROW_1, ITEM_1, owned_copy_id=COPY_1)
        cursor = ScriptedCursor({PAGE_ROWS: [[held], [dict(held)]], SNAPSHOT_COPIES: [[(COPY_1,)]]})
        pool = scripted_pool(cursor)

        await run_collection(pool, mock_neo4j, [collection_page([release_item(1, basic_information={"id": 1, "title": "Corrected"})])], {})

        assert events_of(recorder) == []

    @pytest.mark.asyncio
    async def test_addition_alongside_an_unchanged_row(self, mock_neo4j: MagicMock, recorder: AsyncMock) -> None:
        held = collection_row(1, ROW_1, ITEM_1, owned_copy_id=COPY_1)
        added = collection_row(2, ROW_2, ITEM_2)
        cursor = ScriptedCursor(
            {
                PAGE_ROWS: [[held], [held, added]],
                LINK_COPIES: [[{"id": ROW_2, "owned_copy_id": COPY_2}]],
                SNAPSHOT_COPIES: [[(COPY_1,), (COPY_2,)]],
            }
        )
        pool = scripted_pool(cursor)

        await run_collection(pool, mock_neo4j, [collection_page([release_item(1), release_item(2)])], {})

        assert events_of(recorder) == [("collection.item_added", {"item_id": str(ITEM_2), "artifact_id": None, "owned_copy_id": str(COPY_2)})]


class TestCollectionRemoval:
    """A row the reconciliation sweep deletes keeps its copy: the FK nulls the
    back-link rather than cascading, which is what makes a removal reversible."""

    @pytest.mark.asyncio
    async def test_removed_row_emits_item_removed_naming_its_copy(self, mock_neo4j: MagicMock, recorder: AsyncMock) -> None:
        cursor = ScriptedCursor(
            {
                SWEEP_COLLECTION: [[{"gm_item_id": ITEM_2, "owned_copy_id": COPY_2}]],
                SNAPSHOT_COPIES: [[]],
            }
        )
        pool = scripted_pool(cursor)

        await run_collection(pool, mock_neo4j, [collection_page([])], {})

        sweep_sql, _ = cursor.only(SWEEP_COLLECTION)
        assert "RETURNING gm_item_id, owned_copy_id" in sweep_sql
        assert events_of(recorder) == [("collection.item_removed", {"item_id": str(ITEM_2), "artifact_id": None, "owned_copy_id": str(COPY_2)})]

    @pytest.mark.asyncio
    async def test_the_sweep_never_deletes_owned_copies(self, mock_neo4j: MagicMock) -> None:
        cursor = ScriptedCursor({SWEEP_COLLECTION: [[{"gm_item_id": ITEM_2, "owned_copy_id": COPY_2}]]})
        pool = scripted_pool(cursor)

        await run_collection(pool, mock_neo4j, [collection_page([])], {})

        assert [sql for sql, _ in cursor.executed if "DELETE FROM owned_copies" in sql] == []

    @pytest.mark.asyncio
    async def test_snapshot_excludes_the_copy_of_a_removed_row(self, mock_neo4j: MagicMock) -> None:
        """The snapshot runs after the sweep, so an unlinked copy is already out
        of `SELECT ... WHERE collection_row_id IS NOT NULL`."""
        cursor = ScriptedCursor(
            {
                SWEEP_COLLECTION: [[{"gm_item_id": ITEM_2, "owned_copy_id": COPY_2}]],
                SNAPSHOT_COPIES: [[(COPY_1,)]],
            }
        )
        pool = scripted_pool(cursor)

        await run_collection(pool, mock_neo4j, [collection_page([])], {})

        sweep_position = next(index for index, (sql, _) in enumerate(cursor.executed) if SWEEP_COLLECTION in sql)
        snapshot_position = next(index for index, (sql, _) in enumerate(cursor.executed) if SNAPSHOT_INSERT in sql)
        assert sweep_position < snapshot_position

        _, params = cursor.only(SNAPSHOT_INSERT)
        assert params[3] == [COPY_1]


class TestWantlistIdentity:
    """A wantlist entry is an intention, so it gains a native item and no copy."""

    @pytest.mark.asyncio
    async def test_first_sync_writes_gm_item_id_and_emits_added(self, mock_neo4j: MagicMock, recorder: AsyncMock) -> None:
        cursor = ScriptedCursor({WANT_ROWS: [[]]})
        pool = scripted_pool(cursor)
        resolved = {AliasRef("discogs", "release", "456"): ITEM_1}

        total = await run_wantlist(pool, mock_neo4j, [wantlist_page([want_item(456)])], resolved)

        assert total == 1
        run_wantlist.resolver.assert_awaited_once()  # type: ignore[attr-defined]
        upsert_sql, batch = cursor.executed_many[0]
        assert "gm_item_id" in upsert_sql
        assert batch[0][-2] == ITEM_1
        assert events_of(recorder) == [("wantlist.item_added", {"item_id": str(ITEM_1)})]

    @pytest.mark.asyncio
    async def test_resync_of_a_held_want_emits_nothing(self, mock_neo4j: MagicMock, recorder: AsyncMock) -> None:
        cursor = ScriptedCursor({WANT_ROWS: [[{"release_id": 456}]]})
        pool = scripted_pool(cursor)
        resolved = {AliasRef("discogs", "release", "456"): ITEM_1}

        await run_wantlist(pool, mock_neo4j, [wantlist_page([want_item(456)])], resolved)

        assert events_of(recorder) == []

    @pytest.mark.asyncio
    async def test_removed_want_emits_item_removed(self, mock_neo4j: MagicMock, recorder: AsyncMock) -> None:
        cursor = ScriptedCursor({SWEEP_WANTLIST: [[{"gm_item_id": ITEM_1}]]})
        pool = scripted_pool(cursor)

        await run_wantlist(pool, mock_neo4j, [wantlist_page([])], {})

        sweep_sql, _ = cursor.only(SWEEP_WANTLIST)
        assert "RETURNING gm_item_id" in sweep_sql
        assert events_of(recorder) == [("wantlist.item_removed", {"item_id": str(ITEM_1)})]

    @pytest.mark.asyncio
    async def test_wantlist_mints_no_owned_copy_and_no_snapshot(self, mock_neo4j: MagicMock) -> None:
        cursor = ScriptedCursor({WANT_ROWS: [[]]})
        pool = scripted_pool(cursor)

        await run_wantlist(pool, mock_neo4j, [wantlist_page([want_item(456)])], {})

        assert cursor.statements(MINT_COPIES) == []
        assert cursor.statements(SNAPSHOT_INSERT) == []


class TestUnresolvedItems:
    """A row that resolved to no native id has no identity, so there is nothing
    to mint a copy against and nothing an event could name."""

    @pytest.mark.asyncio
    async def test_no_copy_and_no_event_without_a_native_id(self, mock_neo4j: MagicMock, recorder: AsyncMock) -> None:
        cursor = ScriptedCursor({PAGE_ROWS: [[], [collection_row(1, ROW_1, None)]]})
        pool = scripted_pool(cursor)

        await run_collection(pool, mock_neo4j, [collection_page([release_item(1)])], {})

        assert cursor.statements(MINT_COPIES) == []
        assert cursor.statements(LINK_COPIES) == []
        assert events_of(recorder) == []

    @pytest.mark.asyncio
    async def test_unresolved_removal_emits_no_event(self, mock_neo4j: MagicMock, recorder: AsyncMock) -> None:
        cursor = ScriptedCursor({SWEEP_COLLECTION: [[{"gm_item_id": None, "owned_copy_id": None}]]})
        pool = scripted_pool(cursor)

        await run_collection(pool, mock_neo4j, [collection_page([])], {})

        assert events_of(recorder) == []


class TestRecorderWiring:
    """The recorder is injected, optional, and never able to fail a sync."""

    @pytest.mark.asyncio
    async def test_default_recorder_drops_events(self, mock_neo4j: MagicMock) -> None:
        cursor = ScriptedCursor({PAGE_ROWS: [[], [collection_row(1, ROW_1, ITEM_1)]]})
        pool = scripted_pool(cursor)

        total = await run_collection(pool, mock_neo4j, [collection_page([release_item(1)])], {})

        assert total == 1

    @pytest.mark.asyncio
    async def test_configure_none_restores_the_no_op(self, mock_neo4j: MagicMock) -> None:
        recording = AsyncMock()
        syncer.configure(recording)
        syncer.configure(None)

        cursor = ScriptedCursor({PAGE_ROWS: [[], [collection_row(1, ROW_1, ITEM_1)]]})
        await run_collection(scripted_pool(cursor), mock_neo4j, [collection_page([release_item(1)])], {})

        recording.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_failing_recorder_does_not_fail_the_sync(self, mock_neo4j: MagicMock, recorder: AsyncMock) -> None:
        recorder.side_effect = RuntimeError("activity store unreachable")
        cursor = ScriptedCursor(
            {
                PAGE_ROWS: [[], [collection_row(1, ROW_1, ITEM_1)]],
                LINK_COPIES: [[{"id": ROW_1, "owned_copy_id": COPY_1}]],
                SNAPSHOT_COPIES: [[(COPY_1,)]],
            }
        )

        total = await run_collection(scripted_pool(cursor), mock_neo4j, [collection_page([release_item(1)])], {})

        assert total == 1
        recorder.assert_awaited_once()
