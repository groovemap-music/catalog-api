"""Engine-backed proof of the automatic identity-maintenance watcher (api/reattach_trigger.py).

Runs against the real schema and a real Neo4j through the fixtures `tests/test_real_databases.py`
provides, so the advisory lock, the `users` foreign key on `admin_audit_log`, and the latch
query are the server's own. Nothing here touches a shared database: `just test-integration`
starts throwaway containers.

`public.loader_extraction_latch` is declared by `database-schema` at a revision newer than the
one this repository pins for its test fixture, so the fixture below declares it with the
producer's DDL (`groovemap_schema/postgres.py`, "loader_extraction_latch table" plus its
`generation` column). That is test scaffolding for a relation this service only reads; the
production relation always comes from `database-schema`.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import patch

import pytest
import pytest_asyncio
from common import AsyncPostgreSQLPool, AsyncResilientNeo4jDriver
from common.config import parse_postgres_host_port

from api import reattach_trigger
from api.reattach_trigger import AUDIT_ACTION, SYSTEM_ACTOR_EMAIL, SYSTEM_ACTOR_ID, run_pending
from tests.test_real_databases import _consume, neo4j_driver, postgres_pool
from tests.test_reattach_integration import _current, _execute, _split_release


__all__ = ["neo4j_driver", "postgres_pool"]

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_LATCH_DDL = """
    CREATE TABLE IF NOT EXISTS loader_extraction_latch (
        loader       TEXT NOT NULL,
        version      TEXT NOT NULL,
        signals      TEXT[] NOT NULL DEFAULT '{}',
        created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        refreshed_at TIMESTAMPTZ,
        generation   BIGINT,
        CONSTRAINT loader_extraction_latch_pkey PRIMARY KEY (loader, version)
    )
"""

_RESET = (
    "TRUNCATE musicbrainz.releases, musicbrainz.release_groups, musicbrainz.artists, musicbrainz.labels, public.releases, loader_extraction_latch"
)

_ALL = ["artists", "labels", "masters", "releases"]


@pytest_asyncio.fixture
async def pool(postgres_pool: AsyncPostgreSQLPool) -> AsyncPostgreSQLPool:
    await _execute(postgres_pool, _LATCH_DDL)
    await _execute(postgres_pool, _RESET)
    return postgres_pool


@pytest_asyncio.fixture
async def second_pool(pool: AsyncPostgreSQLPool) -> AsyncIterator[AsyncPostgreSQLPool]:  # noqa: ARG001 - after `pool` has declared the latch
    """A second replica: its own pool, so its own PostgreSQL sessions and advisory locks."""
    host, port = parse_postgres_host_port(os.environ["POSTGRES_HOST"])
    replica = AsyncPostgreSQLPool(
        connection_params={
            "host": host,
            "port": port,
            "dbname": os.environ["POSTGRES_DATABASE"],
            "user": os.environ["POSTGRES_USERNAME"],
            "password": os.environ["POSTGRES_PASSWORD"],
        },
        min_connections=1,
        max_connections=2,
        max_retries=1,
        health_check_interval=3600,
    )
    await replica.initialize()
    try:
        yield replica
    finally:
        await replica.close()


async def _latch(pool: AsyncPostgreSQLPool, version: str, signals: list[str], *, loader: str = "discogs", age: str = "0 seconds") -> None:
    await _execute(
        pool,
        "INSERT INTO loader_extraction_latch (loader, version, signals, created_at) VALUES (%s, %s, %s::text[], NOW() - %s::interval) "
        "ON CONFLICT (loader, version) DO UPDATE SET signals = EXCLUDED.signals",
        (loader, version, signals, age),
    )


async def _markers(pool: AsyncPostgreSQLPool) -> list[tuple[Any, ...]]:
    return await _execute(pool, "SELECT admin_id, target, details FROM admin_audit_log WHERE action = %s ORDER BY created_at", (AUDIT_ACTION,))


async def test_complete_extraction_runs_both_jobs_once_and_survives_a_restart(
    pool: AsyncPostgreSQLPool, second_pool: AsyncPostgreSQLPool, neo4j_driver: AsyncResilientNeo4jDriver
) -> None:
    mbid, split, discogs_native = await _split_release(pool, 7001)
    await _consume(neo4j_driver, "CREATE (:Release {id: '7001'})")
    await _latch(pool, "20260901", _ALL)

    assert await run_pending(pool, neo4j_driver) == "20260901"

    assert await _current(pool, "musicbrainz", "release", mbid) == [(discogs_native, "catalog")]
    async with neo4j_driver.session(database="neo4j") as session:
        record = await (await session.run("MATCH (n:Release {id: '7001'}) RETURN n.gm_id AS gm_id")).single()
    assert record is not None and record["gm_id"] == str(discogs_native)

    [(admin_id, target, details)] = await _markers(pool)
    assert (admin_id, target) == (SYSTEM_ACTOR_ID, "discogs:20260901")
    assert details["extraction"] == "20260901"
    assert details["outcomes"]["release"]["reattached"] == 1
    assert details["projection"]["Release"] >= 1
    actor = await _execute(pool, "SELECT email, is_active, is_admin FROM users WHERE id = %s", (SYSTEM_ACTOR_ID,))
    assert actor == [(SYSTEM_ACTOR_EMAIL, False, False)]
    assert split != discogs_native

    # A restarted process — or any other replica — reads the durable marker and does nothing.
    assert await run_pending(second_pool, neo4j_driver) is None
    assert await run_pending(pool, neo4j_driver) is None
    assert len(await _markers(pool)) == 1


async def test_incomplete_or_foreign_latch_rows_do_not_trigger(pool: AsyncPostgreSQLPool, neo4j_driver: AsyncResilientNeo4jDriver) -> None:
    await _latch(pool, "20260901", ["artists", "labels", "masters"])
    await _latch(pool, "20260902", _ALL, loader="musicbrainz")
    assert await run_pending(pool, neo4j_driver) is None
    assert await _markers(pool) == []


async def test_missing_latch_relation_is_not_an_error(pool: AsyncPostgreSQLPool, neo4j_driver: AsyncResilientNeo4jDriver) -> None:
    await _execute(pool, "DROP TABLE loader_extraction_latch")
    try:
        assert await run_pending(pool, neo4j_driver) is None
    finally:
        await _execute(pool, _LATCH_DDL)


async def test_a_newer_extraction_runs_again_and_an_older_straggler_does_not(
    pool: AsyncPostgreSQLPool, neo4j_driver: AsyncResilientNeo4jDriver
) -> None:
    await _latch(pool, "20260801", _ALL, age="40 days")
    assert await run_pending(pool, neo4j_driver) == "20260801"

    # A newer extraction starts; an older one's last signal lands after it (a straggler).
    await _latch(pool, "20260901", ["artists"], age="1 day")
    await _latch(pool, "20260715", _ALL, age="80 days")
    assert await run_pending(pool, neo4j_driver) is None

    await _latch(pool, "20260901", _ALL)
    assert await run_pending(pool, neo4j_driver) == "20260901"
    assert [target for _admin, target, _details in await _markers(pool)] == ["discogs:20260801", "discogs:20260901"]


async def test_two_replicas_racing_run_it_exactly_once(
    pool: AsyncPostgreSQLPool, second_pool: AsyncPostgreSQLPool, neo4j_driver: AsyncResilientNeo4jDriver
) -> None:
    await _split_release(pool, 7002)
    await _latch(pool, "20260901", _ALL)
    real = reattach_trigger.run_reattachment
    runs = 0

    async def slow_reattachment(target: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal runs
        runs += 1
        await asyncio.sleep(0.5)  # keep the lock held while the other replica polls
        return await real(target, **kwargs)

    with patch.object(reattach_trigger, "run_reattachment", slow_reattachment):
        results = await asyncio.gather(run_pending(pool, neo4j_driver), run_pending(second_pool, neo4j_driver))

    assert sorted(results, key=str) == ["20260901", None]
    assert runs == 1
    assert len(await _markers(pool)) == 1


async def test_a_failed_run_leaves_no_marker_releases_the_lock_and_is_retried(
    pool: AsyncPostgreSQLPool, second_pool: AsyncPostgreSQLPool, neo4j_driver: AsyncResilientNeo4jDriver
) -> None:
    await _latch(pool, "20260901", _ALL)
    with patch.object(reattach_trigger, "run_gm_id_projection", side_effect=RuntimeError("neo4j down")), pytest.raises(RuntimeError):
        await run_pending(pool, neo4j_driver)
    assert await _markers(pool) == []

    # The lock was released, so the other replica's next poll takes it and completes the run.
    assert await run_pending(second_pool, neo4j_driver) == "20260901"
    assert len(await _markers(pool)) == 1
