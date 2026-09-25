"""Engine-backed proof of the catalog re-attachment job (api/reattach.py) — ADR 0014 section 8.

Runs against the real schema `create_postgres_schema` applies, through the same
`postgres_pool` fixture `tests/test_real_databases.py` uses, so every statement is parsed and
planned by PostgreSQL and every lock, index, and constraint is the real one. Nothing here
touches a shared database: `just test-integration` starts throwaway containers.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from common import AsyncPostgreSQLPool
from common.identity import AliasRef, attach_aliases

from api.reattach import KINDS, _reattach_one, reattach_item, run_census, run_reattachment
from tests.test_real_databases import TEST_USER_ID, postgres_pool


__all__ = ["postgres_pool"]

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

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
    return [*aliases, *rows, *items]


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
    assert release["guarded"] == 7
    assert release["eligible"] == 1
    # The observation hangs off an artifact, so its item counts under both tables.
    assert release["dependents"] == {"artifacts": 2, "owned_copies": 1, "observations": 1, "user_collections": 1, "user_wantlists": 1}
    assert release["guard_reasons"] == {"dependents": 5, "shared_native_id": 1, "non_catalog_alias": 1}
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


async def test_apply_reattaches_guards_and_is_idempotent(pool: AsyncPostgreSQLPool) -> None:
    seeded = await _seed_population(pool)
    mbid, split, discogs_native = seeded["split"]
    guarded_ids = [*seeded["guarded"].values(), seeded["shared"], seeded["non_catalog"]]
    guarded_before = await _execute(pool, "SELECT * FROM provider_aliases WHERE native_id = ANY(%s) ORDER BY id", (guarded_ids,))

    report = await run_reattachment(pool, apply=True, batch_size=2)

    assert report["outcomes"]["release"] == {
        "reattached": 1,
        "guarded": 7,
        "unchanged": 0,
        "failed": 0,
        "aliases_moved": 3,
        "identifier_collisions": 0,
        "guard_reasons": {"dependents": 5, "shared_native_id": 1, "non_catalog_alias": 1},
    }
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

    # Guarded items were never modified.
    assert await _execute(pool, "SELECT * FROM provider_aliases WHERE native_id = ANY(%s) ORDER BY id", (guarded_ids,)) == guarded_before

    # A second run finds only the guarded items and changes nothing.
    after_first = await _snapshot(pool)
    second = await run_reattachment(pool, apply=True)
    assert second["census"]["release"]["split"] == 7
    assert second["census"]["release"]["eligible"] == 0
    assert second["outcomes"]["release"]["reattached"] == 0
    assert second["outcomes"]["release"]["guarded"] == 7
    assert second["outcomes"]["artist"]["reattached"] == 0
    assert await _snapshot(pool) == after_first


async def test_contested_identifier_moves_without_violating_the_unique_index(pool: AsyncPostgreSQLPool) -> None:
    """The barcode the MusicBrainz side won is closed on the split item and held once, by the Discogs item."""
    mbid, split, discogs_native = await _split_release(pool, 4001)
    await _alias(pool, "barcode", "release", "4006381333931", split)

    outcome = await _reattach_one(pool, KINDS["release"], mbid)

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
    assert (await _reattach_one(pool, KINDS["release"], mbid)).status == "reattached"
    # The racing loader's late upsert and identifier attach, against the id it resolved earlier.
    await _execute(pool, "UPDATE musicbrainz.releases SET gm_item_id = %s WHERE mbid = %s", (split, mbid))
    await _alias(pool, "barcode", "release", "0724384260910", split)

    census = await run_census(pool)
    assert census["release"]["stale_gm_item_id"] == 1
    report = await run_reattachment(pool, apply=True)

    assert report["outcomes"]["release"]["reattached"] == 1
    assert await _execute(pool, "SELECT gm_item_id FROM musicbrainz.releases WHERE mbid = %s", (mbid,)) == [(discogs_native,)]
    assert await _current(pool, "barcode", "release", "0724384260910") == [(discogs_native, "catalog")]


async def test_waits_for_a_loader_holding_the_row_then_reverifies(pool: AsyncPostgreSQLPool) -> None:
    mbid, split, discogs_native = await _split_release(pool, 6001)

    async with pool.connection() as loader, loader.transaction(), loader.cursor() as cur:
        # The loader's per-message upsert of the same MusicBrainz row, not yet committed.
        await cur.execute("UPDATE musicbrainz.releases SET gm_item_id = %s WHERE mbid = %s", (split, mbid))
        job = asyncio.create_task(_reattach_one(pool, KINDS["release"], mbid))
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
        outcome = await reattach_item(cur, KINDS["release"], mbid)
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

    report = await reattach_module._run_once(apply=True, admin_id=str(TEST_USER_ID), batch_size=10, job_id="job-8001")

    assert report is not None
    entries = await _execute(pool, "SELECT admin_id, action, target, details FROM admin_audit_log")
    assert len(entries) == 1
    admin_id, action, target, details = entries[0]
    assert (admin_id, action, target) == (TEST_USER_ID, "identity.reattach.apply", "job-8001")
    assert details["outcomes"]["release"]["reattached"] == 1
