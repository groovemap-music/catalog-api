"""Engine-backed proof of the catalog re-attachment job (api/reattach.py) — ADR 0014 section 8 —
and of the native-id merge steps it runs (api/catalog_merge.py) — ADR 0009's 2026-09-25 amendment.

Runs against the real schema `create_postgres_schema` applies, through the same
`postgres_pool` fixture `tests/test_real_databases.py` uses, so every statement is parsed and
planned by PostgreSQL and every lock, index, and constraint is the real one. Nothing here
touches a shared database: `just test-integration` starts throwaway containers.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from common import AsyncPostgreSQLPool
from common.identity import AliasRef, attach_aliases

from api.catalog_merge import MergeConflictError, current_supersession, lock_items, merge_items, revert_supersession
from api.reattach import KINDS, AuditEntryError, _reattach_one, apply_with_audit, reattach_item, run_census, run_reattachment
from api.routers.activity import _EXPORT_SECTIONS, _USER_OWNED_DELETES
from tests.test_real_databases import TEST_USER_ID, postgres_pool


__all__ = ["postgres_pool"]

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

# Every supersession an applying run opens names the run's audit entry; the tests use one run id.
RUN = uuid4()

_RESET = "TRUNCATE musicbrainz.releases, musicbrainz.release_groups, musicbrainz.artists, musicbrainz.labels, public.releases, admin_audit_log"


@pytest_asyncio.fixture
async def pool(postgres_pool: AsyncPostgreSQLPool) -> AsyncPostgreSQLPool:
    async with postgres_pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(_RESET)
    return postgres_pool


async def _execute(pool: AsyncPostgreSQLPool, sql: str, params: Any = None) -> list[tuple[Any, ...]]:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(sql, params)
        return await cur.fetchall() if cur.description else []


async def _item(pool: AsyncPostgreSQLPool, kind: str = "release") -> UUID:
    native_id = uuid4()
    await _execute(pool, "INSERT INTO catalog_items (id, kind) VALUES (%s, %s)", (native_id, kind))
    return native_id


async def _alias(pool: AsyncPostgreSQLPool, provider: str, entity_kind: str, external_id: str, native_id: UUID, source: str = "catalog") -> None:
    await _execute(
        pool,
        "INSERT INTO provider_aliases (provider, entity_kind, external_id, native_id, source) VALUES (%s, %s, %s, %s, %s)",
        (provider, entity_kind, external_id, native_id, source),
    )


async def _mb_release(pool: AsyncPostgreSQLPool, discogs_id: int | None, gm_item_id: UUID | None) -> str:
    mbid = str(uuid4())
    await _execute(
        pool,
        "INSERT INTO musicbrainz.releases (mbid, name, discogs_release_id, gm_item_id) VALUES (%s, 'r', %s, %s)",
        (mbid, discogs_id, gm_item_id),
    )
    return mbid


async def _split_release(pool: AsyncPostgreSQLPool, discogs_id: int) -> tuple[str, UUID, UUID]:
    """A MusicBrainz release loaded before its Discogs counterpart: two native items."""
    discogs_native = await _item(pool)
    await _alias(pool, "discogs", "release", str(discogs_id), discogs_native)
    split = await _item(pool)
    mbid = await _mb_release(pool, discogs_id, split)
    await _alias(pool, "musicbrainz", "release", mbid, split)
    return mbid, split, discogs_native


async def _current(pool: AsyncPostgreSQLPool, provider: str, entity_kind: str, external_id: str) -> list[tuple[Any, ...]]:
    return await _execute(
        pool,
        "SELECT native_id, source FROM provider_aliases WHERE provider = %s AND entity_kind = %s AND external_id = %s AND valid_to IS NULL",
        (provider, entity_kind, external_id),
    )


async def _snapshot(pool: AsyncPostgreSQLPool) -> list[tuple[Any, ...]]:
    aliases = await _execute(
        pool, "SELECT id, provider, entity_kind, external_id, native_id, valid_from, valid_to, source FROM provider_aliases ORDER BY id"
    )
    rows = await _execute(pool, "SELECT mbid, gm_item_id FROM musicbrainz.releases ORDER BY mbid")
    items = await _execute(pool, "SELECT id FROM catalog_items ORDER BY id")
    supersessions = await _execute(pool, "SELECT * FROM catalog_item_supersessions ORDER BY id")
    moves = await _execute(pool, "SELECT * FROM catalog_item_moves ORDER BY id")
    dependents = await _execute(
        pool,
        "SELECT 'artifact', id, item_id FROM artifacts UNION ALL SELECT 'copy', id, item_id FROM owned_copies "
        "UNION ALL SELECT 'collection', id, gm_item_id FROM user_collections UNION ALL SELECT 'wantlist', id, gm_item_id FROM user_wantlists "
        "ORDER BY 1, 2",
    )
    return [*aliases, *rows, *items, *supersessions, *moves, *dependents]


async def _survivor(pool: AsyncPostgreSQLPool, native_id: UUID) -> UUID:
    [(resolved,)] = await _execute(pool, "SELECT public.resolve_catalog_item(%s)", (native_id,))
    return resolved


async def _seed_population(pool: AsyncPostgreSQLPool) -> dict[str, Any]:
    """One row per case the census distinguishes."""
    seeded: dict[str, Any] = {}

    # Split, carrying a barcode the Discogs record also prints and a catno it does not.
    mbid, split, discogs_native = await _split_release(pool, 1001)
    await _alias(pool, "barcode", "release", "5012345678900", split)
    await _alias(pool, "catalog_number", "release", "MB-ONLY 1", split)
    document = {"identifiers": {"aliases": [{"provider": "barcode", "external_id": "5012345678900"}]}}
    await _execute(pool, "INSERT INTO public.releases (data_id, hash, data) VALUES ('1001', 'h', %s::jsonb)", (json.dumps(document),))
    seeded["split"] = (mbid, split, discogs_native)

    # Not split: attached to the Discogs item as the loader intends.
    attached = await _item(pool)
    await _alias(pool, "discogs", "release", "1002", attached)
    mbid = await _mb_release(pool, 1002, attached)
    await _alias(pool, "musicbrainz", "release", mbid, attached)
    seeded["attached"] = mbid

    # Unresolved: a Discogs id no Discogs row has claimed yet.
    unresolved = await _item(pool)
    mbid = await _mb_release(pool, 1003, unresolved)
    await _alias(pool, "musicbrainz", "release", mbid, unresolved)
    seeded["unresolved"] = mbid

    # One split per dependent table.
    guarded: dict[str, UUID] = {}
    for offset, table in enumerate(("artifacts", "owned_copies", "observations", "user_collections", "user_wantlists")):
        _mbid, split_id, _discogs = await _split_release(pool, 1100 + offset)
        guarded[table] = split_id
    await _execute(pool, "INSERT INTO artifacts (item_id) VALUES (%s)", (guarded["artifacts"],))
    await _execute(pool, "INSERT INTO owned_copies (user_id, item_id) VALUES (%s, %s)", (TEST_USER_ID, guarded["owned_copies"]))
    await _execute(
        pool,
        "WITH artifact AS (INSERT INTO artifacts (item_id) VALUES (%s) RETURNING id) "
        "INSERT INTO observations (user_id, artifact_id, kind, value, source) SELECT %s, id, 'matrix', 'A1', 'user' FROM artifact",
        (guarded["observations"], TEST_USER_ID),
    )
    await _execute(
        pool,
        "INSERT INTO user_collections (user_id, release_id, gm_item_id) VALUES (%s, 1103, %s)",
        (TEST_USER_ID, guarded["user_collections"]),
    )
    await _execute(
        pool, "INSERT INTO user_wantlists (user_id, release_id, gm_item_id) VALUES (%s, 1104, %s)", (TEST_USER_ID, guarded["user_wantlists"])
    )
    seeded["guarded"] = guarded

    # Shared: the "split" native id is itself another Discogs release's item.
    _mbid, shared, _discogs = await _split_release(pool, 1200)
    await _alias(pool, "discogs", "release", "1299", shared)
    seeded["shared"] = shared

    # Non-catalog: a person asserted an alias on the split item.
    _mbid, personal, _discogs = await _split_release(pool, 1300)
    await _alias(pool, "barcode", "release", "0000000000017", personal, source="user")
    seeded["non_catalog"] = personal

    # One split artist and one split release group (aliased as `master`).
    for table, column, entity_kind, discogs_id in (
        ("artists", "discogs_artist_id", "artist", 2001),
        ("release_groups", "discogs_master_id", "master", 3001),
    ):
        discogs_item = await _item(pool, entity_kind)
        await _alias(pool, "discogs", entity_kind, str(discogs_id), discogs_item)
        split_item = await _item(pool, entity_kind)
        other_mbid = str(uuid4())
        await _execute(
            pool,
            f"INSERT INTO musicbrainz.{table} (mbid, name, {column}, gm_item_id) VALUES (%s, 'n', %s, %s)",  # noqa: S608 — test constants
            (other_mbid, discogs_id, split_item),
        )
        await _alias(pool, "musicbrainz", entity_kind, other_mbid, split_item)
        seeded[entity_kind] = (table, other_mbid, discogs_item)
    return seeded


async def test_census_counts_every_case_against_the_real_schema(pool: AsyncPostgreSQLPool) -> None:
    await _seed_population(pool)
    before = await _snapshot(pool)

    census = await run_census(pool)

    assert await _snapshot(pool) == before
    release = census["release"]
    assert release["linked"] == 10
    assert release["unresolved"] == 1
    assert release["split"] == 8
    assert release["stale_gm_item_id"] == 0
    # Dependents no longer guard: only the two alias guards remain.
    assert release["guarded"] == 2
    assert release["eligible"] == 6
    # The observation hangs off an artifact, so its item counts under both tables.
    assert release["dependents"] == {"artifacts": 2, "owned_copies": 1, "observations": 1, "user_collections": 1, "user_wantlists": 1}
    assert release["guard_reasons"] == {"shared_native_id": 1, "non_catalog_alias": 1}
    # What the merge will move: the artifact item, the copy item, and the observed artifact's item.
    assert release["will_move"] == {"items": 3, "artifacts": 2, "owned_copies": 1}
    assert release["identifier_aliases"]["barcode"] == {"held": 2, "contested": 1}
    assert release["identifier_aliases"]["catalog_number"] == {"held": 1, "contested": 0}
    for kind in ("artist", "release_group"):
        assert census[kind]["split"] == 1
        assert census[kind]["eligible"] == 1
    assert census["label"]["linked"] == 0


async def test_census_runs_read_only(pool: AsyncPostgreSQLPool, monkeypatch: pytest.MonkeyPatch) -> None:
    """The server itself refuses a write inside the census transaction."""
    import api.reattach as reattach_module

    monkeypatch.setattr(reattach_module, "census_sql", lambda _kind: "INSERT INTO catalog_items (kind) VALUES ('release') RETURNING 0")

    with pytest.raises(Exception, match="read-only transaction"):
        await run_census(pool)


async def test_dry_run_writes_nothing(pool: AsyncPostgreSQLPool) -> None:
    await _seed_population(pool)
    before = await _snapshot(pool)

    report = await run_reattachment(pool)

    assert "outcomes" not in report
    assert await _snapshot(pool) == before


async def test_apply_reattaches_merges_guards_and_is_idempotent(pool: AsyncPostgreSQLPool) -> None:
    seeded = await _seed_population(pool)
    mbid, split, discogs_native = seeded["split"]
    guarded_ids = [seeded["shared"], seeded["non_catalog"]]
    guarded_before = await _execute(pool, "SELECT * FROM provider_aliases WHERE native_id = ANY(%s) ORDER BY id", (guarded_ids,))
    observations_before = await _execute(pool, "SELECT * FROM observations ORDER BY id")

    report = await run_reattachment(pool, apply=True, batch_size=2, decision_ref=RUN)

    outcome = report["outcomes"]["release"]
    assert {key: outcome[key] for key in ("reattached", "guarded", "unchanged", "failed", "aliases_moved", "identifier_collisions")} == {
        "reattached": 6,
        "guarded": 2,
        "unchanged": 0,
        "failed": 0,
        "aliases_moved": 8,
        "identifier_collisions": 0,
    }
    assert outcome["guard_reasons"] == {"shared_native_id": 1, "non_catalog_alias": 1}
    assert outcome["supersessions_opened"] == 6
    assert outcome["merged_with_dependents"] == 3
    assert outcome["moved"] == {"artifacts": 2, "owned_copies": 1}
    # The collection and wantlist rows cached on a split item follow its moved Discogs alias.
    assert outcome["caches_recomputed"]["musicbrainz.releases"] == 0  # step 3 already pointed each row at its survivor
    assert outcome["caches_recomputed"]["user_collections"] == 1
    assert outcome["caches_recomputed"]["user_wantlists"] == 1
    assert report["outcomes"]["artist"]["reattached"] == 1
    assert report["outcomes"]["release_group"]["reattached"] == 1

    # The MusicBrainz alias and both identifiers now name the Discogs item, as catalog rows.
    for provider, external_id in (("musicbrainz", mbid), ("barcode", "5012345678900"), ("catalog_number", "MB-ONLY 1")):
        assert await _current(pool, provider, "release", external_id) == [(discogs_native, "catalog")]
    # The split rows are closed, not deleted, and each closes exactly where its successor opens.
    history = await _execute(
        pool,
        "SELECT old.valid_to = new.valid_from FROM provider_aliases AS old "
        "JOIN provider_aliases AS new USING (provider, entity_kind, external_id) "
        "WHERE old.native_id = %s AND new.native_id = %s",
        (split, discogs_native),
    )
    assert history == [(True,), (True,), (True,)]
    assert await _execute(pool, "SELECT gm_item_id FROM musicbrainz.releases WHERE mbid = %s", (mbid,)) == [(discogs_native,)]
    assert await _execute(pool, "SELECT count(*) FROM catalog_items WHERE id = %s", (split,)) == [(1,)]
    for entity_kind in ("artist", "master"):
        table, other_mbid, discogs_item = seeded[entity_kind]
        assert await _current(pool, "musicbrainz", entity_kind, other_mbid) == [(discogs_item, "catalog")]
        assert await _execute(pool, f"SELECT gm_item_id FROM musicbrainz.{table} WHERE mbid = %s", (other_mbid,)) == [(discogs_item,)]  # noqa: S608

    # The split item is kept, superseded into the Discogs item by this run, and hidden from the graph.
    assert await _survivor(pool, split) == discogs_native
    assert await _execute(
        pool, "SELECT cause, decision_ref, via_id FROM catalog_item_supersessions WHERE superseded_id = %s AND valid_to IS NULL", (split,)
    ) == [("catalog_reattachment", RUN, None)]
    assert await _execute(pool, "SELECT count(*) FROM graph.catalog_item WHERE item_id = %s", (split,)) == [(0,)]
    assert await _execute(pool, "SELECT count(*) FROM graph.catalog_item WHERE item_id = %s", (discogs_native,)) == [(1,)]

    # Every formerly guarded dependent now points at its survivor, and each move is ledgered
    # with its owner; the collection and wantlist caches follow the moved alias.
    for table, former in seeded["guarded"].items():
        survivor = await _survivor(pool, former)
        assert survivor != former
        column = "gm_item_id" if table.startswith("user_") else "item_id"
        if table != "observations":
            assert await _execute(pool, f"SELECT count(*) FROM {table} WHERE {column} = %s", (former,)) == [(0,)]  # noqa: S608 — test constants
            assert await _execute(pool, f"SELECT count(*) FROM {table} WHERE {column} = %s", (survivor,)) == [(1,)]  # noqa: S608 — test constants
    ledger = await _execute(
        pool,
        "SELECT moves.table_name, moves.from_item_id = supersession.superseded_id, moves.to_item_id = supersession.survivor_id, moves.user_id "
        "FROM catalog_item_moves AS moves JOIN catalog_item_supersessions AS supersession ON supersession.id = moves.supersession_id "
        "ORDER BY moves.table_name, moves.user_id NULLS FIRST",
    )
    assert ledger == [("artifacts", True, True, None), ("artifacts", True, True, None), ("owned_copies", True, True, TEST_USER_ID)]
    # Observations are not touched: they keep naming their artifact, which kept its id.
    assert await _execute(pool, "SELECT * FROM observations ORDER BY id") == observations_before

    # Guarded items were never modified.
    assert await _execute(pool, "SELECT * FROM provider_aliases WHERE native_id = ANY(%s) ORDER BY id", (guarded_ids,)) == guarded_before
    assert await _execute(pool, "SELECT count(*) FROM catalog_item_supersessions WHERE superseded_id = ANY(%s)", (guarded_ids,)) == [(0,)]

    # A second run finds only the guarded items and changes nothing.
    after_first = await _snapshot(pool)
    second = await run_reattachment(pool, apply=True, decision_ref=uuid4())
    assert second["census"]["release"]["split"] == 2
    assert second["census"]["release"]["eligible"] == 0
    assert second["outcomes"]["release"]["reattached"] == 0
    assert second["outcomes"]["release"]["guarded"] == 2
    assert second["outcomes"]["artist"]["reattached"] == 0
    assert await _snapshot(pool) == after_first


async def test_contested_identifier_moves_without_violating_the_unique_index(pool: AsyncPostgreSQLPool) -> None:
    """The barcode the MusicBrainz side won is closed on the split item and held once, by the Discogs item."""
    mbid, split, discogs_native = await _split_release(pool, 4001)
    await _alias(pool, "barcode", "release", "4006381333931", split)

    outcome = await _reattach_one(pool, KINDS["release"], mbid, RUN)

    assert outcome.status == "reattached"
    rows = await _execute(
        pool, "SELECT native_id, valid_to IS NULL FROM provider_aliases WHERE provider = 'barcode' ORDER BY valid_from, valid_to NULLS LAST"
    )
    assert sorted(rows, key=lambda row: row[1]) == [(split, False), (discogs_native, True)]
    # The Discogs loader's own idempotent attach of the same value now converges on its item.
    async with pool.connection() as conn, conn.transaction():
        resolved = await attach_aliases(conn, {AliasRef("barcode", "release", "4006381333931"): discogs_native})
    assert resolved == {AliasRef("barcode", "release", "4006381333931"): discogs_native}
    assert len(await _current(pool, "barcode", "release", "4006381333931")) == 1


async def test_stale_cache_left_by_a_racing_loader_is_healed(pool: AsyncPostgreSQLPool) -> None:
    """A loader that resolved the split id before the re-attachment writes it back; the next run fixes it."""
    mbid, split, discogs_native = await _split_release(pool, 5001)
    assert (await _reattach_one(pool, KINDS["release"], mbid, RUN)).status == "reattached"
    # The racing loader's late upsert and identifier attach, against the id it resolved earlier.
    await _execute(pool, "UPDATE musicbrainz.releases SET gm_item_id = %s WHERE mbid = %s", (split, mbid))
    await _alias(pool, "barcode", "release", "0724384260910", split)

    census = await run_census(pool)
    assert census["release"]["stale_gm_item_id"] == 1
    report = await run_reattachment(pool, apply=True, decision_ref=uuid4())

    assert report["outcomes"]["release"]["reattached"] == 1
    # The split item was already superseded into the Discogs item: the re-run reuses that row.
    assert report["outcomes"]["release"]["supersessions_opened"] == 0
    assert await _execute(pool, "SELECT decision_ref FROM catalog_item_supersessions WHERE superseded_id = %s", (split,)) == [(RUN,)]
    assert await _execute(pool, "SELECT gm_item_id FROM musicbrainz.releases WHERE mbid = %s", (mbid,)) == [(discogs_native,)]
    assert await _current(pool, "barcode", "release", "0724384260910") == [(discogs_native, "catalog")]


async def test_waits_for_a_loader_holding_the_row_then_reverifies(pool: AsyncPostgreSQLPool) -> None:
    mbid, split, discogs_native = await _split_release(pool, 6001)

    async with pool.connection() as loader, loader.transaction(), loader.cursor() as cur:
        # The loader's per-message upsert of the same MusicBrainz row, not yet committed.
        await cur.execute("UPDATE musicbrainz.releases SET gm_item_id = %s WHERE mbid = %s", (split, mbid))
        job = asyncio.create_task(_reattach_one(pool, KINDS["release"], mbid, RUN))
        await asyncio.sleep(0.5)
        assert not job.done(), "the re-attachment must wait on the row lock"

    outcome = await asyncio.wait_for(job, timeout=10)
    assert outcome.status == "reattached"
    assert await _current(pool, "musicbrainz", "release", mbid) == [(discogs_native, "catalog")]


async def test_concurrent_loader_attach_converges_on_the_discogs_item(pool: AsyncPostgreSQLPool) -> None:
    """A loader attach racing an open re-attachment waits for it, then its ON CONFLICT DO NOTHING re-selects D."""
    mbid, split, discogs_native = await _split_release(pool, 7001)
    musicbrainz_ref = AliasRef("musicbrainz", "release", mbid)

    async def loader_attach() -> dict[AliasRef, UUID]:
        async with pool.connection() as conn, conn.transaction():
            # The loader's `_native_id` before this job ran would have attached to the split id.
            return await attach_aliases(conn, {musicbrainz_ref: split})

    async with pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
        outcome = await reattach_item(cur, KINDS["release"], mbid, decision_ref=RUN)
        assert outcome.status == "reattached"
        loader = asyncio.create_task(loader_attach())
        await asyncio.sleep(0.5)
        assert not loader.done(), "the loader's attach must wait on the uncommitted re-attachment"

    assert await asyncio.wait_for(loader, timeout=10) == {musicbrainz_ref: discogs_native}
    assert await _current(pool, "musicbrainz", "release", mbid) == [(discogs_native, "catalog")]


async def test_cli_apply_writes_one_audit_entry(pool: AsyncPostgreSQLPool) -> None:
    import api.reattach as reattach_module

    await _split_release(pool, 8001)
    await _execute(pool, "UPDATE users SET is_admin = TRUE WHERE id = %s", (TEST_USER_ID,))

    job_id = str(uuid4())

    report = await reattach_module._run_once(apply=True, admin_id=str(TEST_USER_ID), batch_size=10, job_id=job_id)

    assert report is not None
    entries = await _execute(pool, "SELECT id, admin_id, action, target, details FROM admin_audit_log")
    assert len(entries) == 1
    entry_id, admin_id, action, target, details = entries[0]
    assert (admin_id, action, target) == (TEST_USER_ID, "identity.reattach.apply", job_id)
    assert details["outcomes"]["release"]["reattached"] == 1
    # The supersession's decision_ref is exactly this audit entry.
    assert await _execute(pool, "SELECT decision_ref FROM catalog_item_supersessions") == [(entry_id,)]
    assert str(entry_id) == job_id
    # Per-table counts only: no user id or user-owned row id reaches the audit log.
    assert str(TEST_USER_ID) not in json.dumps(details)


# Supersessions whose decision_ref names no audit entry: the run's record would be missing.
_DANGLING = (
    "SELECT count(*) FROM catalog_item_supersessions s WHERE s.decision_ref IS NOT NULL "
    "AND NOT EXISTS (SELECT 1 FROM admin_audit_log a WHERE a.id = s.decision_ref)"
)


@pytest.mark.parametrize("ending", ["applied", "failed", "finalize_failed"])
async def test_every_decision_ref_resolves_however_the_run_ends(pool: AsyncPostgreSQLPool, ending: str) -> None:
    import api.reattach as reattach_module

    await _split_release(pool, 8101)
    await _split_release(pool, 8102)
    job_id = str(uuid4())
    real_one = reattach_module._reattach_one
    calls = 0

    async def second_item_fails(*args: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("connection lost")
        return await real_one(*args)

    async def update_fails(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("audit table unavailable")

    with (
        patch.object(reattach_module, "_reattach_one", second_item_fails if ending == "failed" else real_one),
        patch.object(reattach_module, "update_audit_entry", update_fails if ending == "finalize_failed" else reattach_module.update_audit_entry),
    ):
        if ending == "failed":
            with pytest.raises(RuntimeError, match="connection lost"):
                await apply_with_audit(pool, admin_id=str(TEST_USER_ID), job_id=job_id, batch_size=1)
        else:
            await apply_with_audit(pool, admin_id=str(TEST_USER_ID), job_id=job_id, batch_size=1)

    expected = {"applied": "identity.reattach.apply", "failed": "identity.reattach.failed", "finalize_failed": "identity.reattach.started"}
    [(entry_id, action)] = await _execute(pool, "SELECT id, action FROM admin_audit_log")
    assert (str(entry_id), action) == (job_id, expected[ending])
    decision_refs = await _execute(pool, "SELECT decision_ref FROM catalog_item_supersessions")
    # The failed run committed its first item before the error; the others committed both.
    assert decision_refs == [(entry_id,)] * (1 if ending == "failed" else 2)
    assert await _execute(pool, _DANGLING) == [(0,)]


async def test_a_run_whose_entry_cannot_be_written_changes_nothing(pool: AsyncPostgreSQLPool) -> None:
    import api.routers.admin as admin_mod

    await _split_release(pool, 8201)
    before = await _snapshot(pool)

    # No such user: the entry's foreign key refuses the insert, as any failed insert would.
    with pytest.raises(AuditEntryError):
        await apply_with_audit(pool, admin_id=str(uuid4()), job_id=str(uuid4()))
    assert await _snapshot(pool) == before

    # The route's background job: logged, never raised, and still nothing written.
    original_pool = admin_mod._pool
    admin_mod._pool = pool
    try:
        await admin_mod._run_reattach_job(str(uuid4()), str(uuid4()), True)
    finally:
        admin_mod._pool = original_pool
    assert await _snapshot(pool) == before
    assert await _execute(pool, "SELECT count(*) FROM admin_audit_log") == [(0,)]


async def test_route_job_apply_is_recorded_under_its_decision_ref(pool: AsyncPostgreSQLPool) -> None:
    import api.routers.admin as admin_mod

    await _split_release(pool, 8301)
    job_id = str(uuid4())
    original_pool = admin_mod._pool
    admin_mod._pool = pool
    try:
        await admin_mod._run_reattach_job(job_id, str(TEST_USER_ID), True)
    finally:
        admin_mod._pool = original_pool

    [(entry_id, admin_id, action, target, details)] = await _execute(pool, "SELECT id, admin_id, action, target, details FROM admin_audit_log")
    assert (str(entry_id), admin_id, action, target) == (job_id, TEST_USER_ID, "identity.reattach.apply", job_id)
    assert details["outcomes"]["release"]["reattached"] == 1
    assert await _execute(pool, "SELECT decision_ref FROM catalog_item_supersessions") == [(entry_id,)]


async def test_an_item_an_earlier_run_guarded_for_dependents_is_merged_by_the_next_run(pool: AsyncPostgreSQLPool) -> None:
    """The guard's removal releases what it skipped: a skip wrote nothing, so the item is still a candidate.

    The state below is exactly what a pre-merge run left behind for a guarded item — nothing,
    since a skip writes nothing — beside that run's skip report in the audit log.
    """
    mbid, split, discogs_native = await _split_release(pool, 9001)
    [(copy_id,)] = await _execute(
        pool, "INSERT INTO owned_copies (user_id, item_id, acquired_at) VALUES (%s, %s, '2020-01-02') RETURNING id", (TEST_USER_ID, split)
    )
    await _execute(
        pool,
        "INSERT INTO observations (user_id, owned_copy_id, kind, value, source) VALUES (%s, %s, 'grading', 'VG+', 'user')",
        (TEST_USER_ID, copy_id),
    )
    earlier_report = {"outcomes": {"release": {"guarded": 1, "guard_reasons": {"dependents": 1}}}}
    await _execute(
        pool,
        "INSERT INTO admin_audit_log (admin_id, action, target, details) VALUES (%s, 'identity.reattach.apply', 'earlier', %s::jsonb)",
        (TEST_USER_ID, json.dumps(earlier_report)),
    )
    copy_before = await _execute(pool, "SELECT id, user_id, artifact_id, collection_row_id, acquired_at, created_at, updated_at FROM owned_copies")

    census = await run_census(pool)
    assert census["release"]["guarded"] == 0
    assert census["release"]["will_move"] == {"items": 1, "artifacts": 0, "owned_copies": 1}

    report = await run_reattachment(pool, apply=True, decision_ref=RUN)

    # The operator's comparison: this run's merged-with-dependents against the earlier skips.
    assert report["outcomes"]["release"]["merged_with_dependents"] == earlier_report["outcomes"]["release"]["guard_reasons"]["dependents"]
    assert await _current(pool, "musicbrainz", "release", mbid) == [(discogs_native, "catalog")]
    assert await _survivor(pool, split) == discogs_native
    # Only the item reference changed on the user's copy: same id, owner, and every other value.
    assert await _execute(pool, "SELECT item_id FROM owned_copies WHERE id = %s", (copy_id,)) == [(discogs_native,)]
    assert (
        await _execute(pool, "SELECT id, user_id, artifact_id, collection_row_id, acquired_at, created_at, updated_at FROM owned_copies")
        == copy_before
    )
    assert await _execute(pool, "SELECT owned_copy_id FROM observations") == [(copy_id,)]
    assert await _execute(pool, "SELECT table_name, row_id, from_item_id, to_item_id, user_id FROM catalog_item_moves") == [
        ("owned_copies", copy_id, split, discogs_native, TEST_USER_ID)
    ]
    # A merge emits no event on the user's behalf.
    assert await _execute(pool, "SELECT count(*) FROM activity.events") == [(0,)]


async def _move_aliases(cur: Any, source: UUID, target: UUID) -> None:
    """ADR 0014's alias steps, reduced to what the merge steps read: every current alias moves."""
    await cur.execute(
        "WITH closed AS (UPDATE provider_aliases SET valid_to = now() WHERE native_id = %s AND valid_to IS NULL "
        "RETURNING provider, entity_kind, external_id, confidence) "
        "INSERT INTO provider_aliases (provider, entity_kind, external_id, native_id, source, confidence, valid_from) "
        "SELECT provider, entity_kind, external_id, %s, 'user', confidence, now() FROM closed",
        (source, target),
    )


async def _merge(pool: AsyncPostgreSQLPool, superseded: UUID, survivor: UUID, cause: str = "edition_promotion") -> UUID:
    """One promotion-shaped transaction: the alias steps, then merge steps 4-7."""
    async with pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
        kind = await lock_items(cur, superseded, survivor)
        existing = await current_supersession(cur, superseded, survivor)
        await _move_aliases(cur, superseded, survivor)
        outcome = await merge_items(cur, superseded, survivor, kind=kind, cause=cause, decision_ref=uuid4(), existing=existing)
    return outcome.supersession_id


async def _revert(pool: AsyncPostgreSQLPool, supersession_id: UUID) -> Any:
    """One revert-shaped transaction: the aliases move back first, then the merge is reverted."""
    [(superseded, survivor)] = await _execute(
        pool, "SELECT superseded_id, survivor_id FROM catalog_item_supersessions WHERE id = %s", (supersession_id,)
    )
    async with pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
        await cur.execute(
            "WITH closed AS (UPDATE provider_aliases SET valid_to = now() WHERE native_id = %s AND valid_to IS NULL AND provider = 'musicbrainz' "
            "RETURNING provider, entity_kind, external_id, confidence) "
            "INSERT INTO provider_aliases (provider, entity_kind, external_id, native_id, source, confidence, valid_from) "
            "SELECT provider, entity_kind, external_id, %s, 'catalog', confidence, now() FROM closed",
            (survivor, superseded),
        )
        return await revert_supersession(cur, supersession_id)


async def test_revert_moves_back_exactly_the_ledgered_rows_still_on_the_survivor(pool: AsyncPostgreSQLPool) -> None:
    mbid, split, discogs_native = await _split_release(pool, 9101)
    moved_copy, moved_elsewhere = [
        (await _execute(pool, "INSERT INTO owned_copies (user_id, item_id) VALUES (%s, %s) RETURNING id", (TEST_USER_ID, split)))[0][0]
        for _ in range(2)
    ]
    [(artifact,)] = await _execute(pool, "INSERT INTO artifacts (item_id, created_by) VALUES (%s, %s) RETURNING id", (split, TEST_USER_ID))
    assert (await _reattach_one(pool, KINDS["release"], mbid, RUN)).status == "reattached"
    [(supersession_id,)] = await _execute(pool, "SELECT id FROM catalog_item_supersessions WHERE superseded_id = %s", (split,))

    # After the merge: a copy created against the survivor, and a moved copy the user re-pointed.
    [(created_after,)] = await _execute(
        pool, "INSERT INTO owned_copies (user_id, item_id) VALUES (%s, %s) RETURNING id", (TEST_USER_ID, discogs_native)
    )
    elsewhere = await _item(pool)
    await _execute(pool, "UPDATE owned_copies SET item_id = %s WHERE id = %s", (elsewhere, moved_elsewhere))

    outcome = await _revert(pool, supersession_id)

    assert outcome.moved_back == {"artifacts": 1, "owned_copies": 1}
    assert outcome.chains_reopened == 0
    items = dict(await _execute(pool, "SELECT id, item_id FROM owned_copies UNION ALL SELECT id, item_id FROM artifacts"))
    assert items == {moved_copy: split, artifact: split, created_after: discogs_native, moved_elsewhere: elsewhere}
    # The former id resolves to itself again, reappears in the graph, and the cache followed the alias.
    assert await _survivor(pool, split) == split
    assert await _execute(pool, "SELECT count(*) FROM graph.catalog_item WHERE item_id = %s", (split,)) == [(1,)]
    assert await _execute(pool, "SELECT gm_item_id FROM musicbrainz.releases WHERE mbid = %s", (mbid,)) == [(split,)]
    assert outcome.caches_recomputed["musicbrainz.releases"] == 1
    # History is kept: the row is closed and the ledger stays.
    assert await _execute(pool, "SELECT valid_to IS NOT NULL FROM catalog_item_supersessions WHERE id = %s", (supersession_id,)) == [(True,)]
    assert await _execute(pool, "SELECT count(*) FROM catalog_item_moves WHERE supersession_id = %s", (supersession_id,)) == [(3,)]

    # A second revert of the same row is refused.
    with pytest.raises(MergeConflictError, match="not current"):
        await _revert(pool, supersession_id)


async def test_chain_compression_keeps_resolution_one_hop_and_reverts_in_reverse_order(pool: AsyncPostgreSQLPool) -> None:
    first, middle, last = [await _item(pool) for _ in range(3)]
    first_mbid = str(uuid4())
    await _alias(pool, "musicbrainz", "release", first_mbid, first)
    [(copy_id,)] = await _execute(pool, "INSERT INTO owned_copies (user_id, item_id) VALUES (%s, %s) RETURNING id", (TEST_USER_ID, first))

    first_into_middle = await _merge(pool, first, middle)
    assert await _survivor(pool, first) == middle
    middle_into_last = await _merge(pool, middle, last)

    # first → middle was closed and first → last opened, naming the merge that compressed it.
    rows = await _execute(
        pool, "SELECT id, superseded_id, survivor_id, via_id, valid_to IS NULL FROM catalog_item_supersessions ORDER BY valid_from, id"
    )
    open_rows = {(superseded, survivor, via) for _id, superseded, survivor, via, is_open in rows if is_open}
    assert open_rows == {(first, last, middle_into_last), (middle, last, None)}
    assert await _survivor(pool, first) == last
    assert await _execute(pool, "SELECT item_id FROM owned_copies WHERE id = %s", (copy_id,)) == [(last,)]

    # The compressed row cannot be reverted directly; the later merge goes first.
    [(compressed,)] = await _execute(pool, "SELECT id FROM catalog_item_supersessions WHERE via_id = %s", (middle_into_last,))
    with pytest.raises(MergeConflictError, match="revert that merge first"):
        await _revert(pool, compressed)
    with pytest.raises(MergeConflictError, match="not current"):
        await _revert(pool, first_into_middle)

    outcome = await _revert(pool, middle_into_last)
    assert outcome.chains_reopened == 1
    assert await _survivor(pool, first) == middle
    assert await _survivor(pool, middle) == middle
    assert await _execute(pool, "SELECT item_id FROM owned_copies WHERE id = %s", (copy_id,)) == [(middle,)]
    # The original row is open again, the very row whose ledger records the first move.
    assert await _execute(pool, "SELECT valid_to IS NULL FROM catalog_item_supersessions WHERE id = %s", (first_into_middle,)) == [(True,)]

    await _revert(pool, first_into_middle)
    assert await _survivor(pool, first) == first
    assert await _current(pool, "musicbrainz", "release", first_mbid) == [(first, "catalog")]
    assert await _execute(pool, "SELECT item_id FROM owned_copies WHERE id = %s", (copy_id,)) == [(first,)]
    assert await _execute(pool, "SELECT count(*) FROM catalog_item_supersessions WHERE valid_to IS NULL") == [(0,)]


async def test_a_superseded_survivor_or_a_kind_mismatch_is_refused_by_the_database_rows(pool: AsyncPostgreSQLPool) -> None:
    first, middle, last = [await _item(pool) for _ in range(3)]
    await _merge(pool, middle, last)
    with pytest.raises(MergeConflictError, match="itself superseded"):
        await _merge(pool, first, middle)
    artist = await _item(pool, "artist")
    with pytest.raises(MergeConflictError, match="kinds differ"):
        await _merge(pool, first, artist)
    # Both refusals rolled back whole.
    assert await _execute(pool, "SELECT count(*) FROM catalog_item_supersessions") == [(1,)]


async def test_a_copy_created_against_the_split_item_waits_for_the_merge(pool: AsyncPostgreSQLPool) -> None:
    """The split item's FOR UPDATE blocks a foreign-key insert, so no dependent is created behind the move."""
    mbid, split, discogs_native = await _split_release(pool, 9201)

    async def insert_copy(item: UUID) -> None:
        await _execute(pool, "INSERT INTO owned_copies (user_id, item_id) VALUES (%s, %s)", (TEST_USER_ID, item))

    async with pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
        outcome = await reattach_item(cur, KINDS["release"], mbid, decision_ref=RUN)
        assert outcome.status == "reattached"
        # The survivor is locked FOR NO KEY UPDATE only: a copy created against it goes straight through.
        await asyncio.wait_for(insert_copy(discogs_native), timeout=5)
        on_split = asyncio.create_task(insert_copy(split))
        await asyncio.sleep(0.5)
        assert not on_split.done(), "an insert against the split item must wait on the merge"

    await asyncio.wait_for(on_split, timeout=10)


async def test_concurrent_merges_into_one_survivor_serialize(pool: AsyncPostgreSQLPool) -> None:
    """Both items' locks: a second merge of an overlapping pair waits for the first to commit."""
    first, second, survivor = [await _item(pool) for _ in range(3)]

    async with pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
        await lock_items(cur, first, survivor)
        await merge_items(cur, first, survivor, kind="release", cause="edition_promotion", decision_ref=uuid4())
        other = asyncio.create_task(_merge(pool, second, survivor))
        await asyncio.sleep(0.5)
        assert not other.done(), "a merge into the same survivor must wait on its lock"

    await asyncio.wait_for(other, timeout=10)
    assert {await _survivor(pool, first), await _survivor(pool, second)} == {survivor}


async def test_a_revert_racing_a_merge_of_the_same_item_serializes(pool: AsyncPostgreSQLPool) -> None:
    first, middle, last = [await _item(pool) for _ in range(3)]
    first_into_middle = await _merge(pool, first, middle)

    async with pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
        await lock_items(cur, middle, last)
        await merge_items(cur, middle, last, kind="release", cause="edition_promotion", decision_ref=uuid4())
        revert = asyncio.create_task(_revert(pool, first_into_middle))
        await asyncio.sleep(0.5)
        assert not revert.done(), "the revert must wait for the merge that holds its items"

    # The merge compressed the row the revert was aimed at, so the revert, re-reading under
    # lock, refuses it instead of reverting a row that is no longer current.
    with pytest.raises(MergeConflictError, match="not current"):
        await asyncio.wait_for(revert, timeout=10)
    assert await _survivor(pool, first) == last


async def test_erasure_deletes_and_export_returns_the_users_ledger_rows(pool: AsyncPostgreSQLPool) -> None:
    """The statements the erasure and export procedures run, against a real ledger."""
    mbid, split, _discogs_native = await _split_release(pool, 9301)
    await _execute(pool, "INSERT INTO owned_copies (user_id, item_id) VALUES (%s, %s)", (TEST_USER_ID, split))
    await _execute(pool, "INSERT INTO artifacts (item_id) VALUES (%s)", (split,))
    assert (await _reattach_one(pool, KINDS["release"], mbid, RUN)).status == "reattached"

    [export] = [statement for kind, statement, _by_subject in _EXPORT_SECTIONS if kind == "catalog_item_move"]
    exported = await _execute(pool, export, (str(TEST_USER_ID),))
    assert [(row[2], row[4]) for row in exported] == [("owned_copies", split)]

    [erase] = [statement for statement in _USER_OWNED_DELETES if "catalog_item_moves" in statement]
    await _execute(pool, erase, (str(TEST_USER_ID),))
    assert await _execute(pool, export, (str(TEST_USER_ID),)) == []
    # The artifact nobody created keeps its ledger row: it is not the user's.
    assert await _execute(pool, "SELECT table_name, user_id FROM catalog_item_moves") == [("artifacts", None)]
    # The whole erasure closure still runs cleanly in its order against the merged rows.
    for statement in _USER_OWNED_DELETES:
        await _execute(pool, statement, (str(TEST_USER_ID),))
