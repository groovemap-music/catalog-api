"""Engine-backed proof of the catalog re-attachment census (api/reattach.py) — ADR 0014 section 8.

Runs against the real schema `create_postgres_schema` applies, through the same
`postgres_pool` fixture `tests/test_real_databases.py` uses, so every statement is parsed and
planned by PostgreSQL and every lock, index, and constraint is the real one. Nothing here
touches a shared database: `just test-integration` starts throwaway containers.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from common import AsyncPostgreSQLPool

from api.reattach import run_census
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
