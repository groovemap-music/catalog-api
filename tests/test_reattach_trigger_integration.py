"""Engine-backed proof of the automatic identity-maintenance watcher (api/reattach_trigger.py).

Runs against the real schema and a real Neo4j through the fixtures `tests/test_real_databases.py`
provides, so the advisory lock, the `users` foreign key on `admin_audit_log`, and the latch
query are the server's own. Nothing here touches a shared database: `just test-integration`
starts throwaway containers.

`public.loader_extraction_latch` comes from the pinned `database-schema` DDL the
`postgres_pool` fixture applies, exactly as in production; this service only reads it.
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
from api.reattach_trigger import AUDIT_ACTION, FAILED_ACTION, STARTED_ACTION, SYSTEM_ACTOR_EMAIL, SYSTEM_ACTOR_ID, run_pending
from tests.test_real_databases import _consume, neo4j_driver, postgres_pool
from tests.test_reattach_integration import _DANGLING, _current, _execute, _split_release


__all__ = ["neo4j_driver", "postgres_pool"]

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

# The loader's refresh-job table references the latch, so it is emptied with it.
_RESET = (
    "TRUNCATE musicbrainz.releases, musicbrainz.release_groups, musicbrainz.artists, musicbrainz.labels, public.releases, "
    "loader_extraction_latch, loader_derived_refresh_job"
)

_ALL = ["artists", "labels", "masters", "releases"]


@pytest_asyncio.fixture
async def pool(postgres_pool: AsyncPostgreSQLPool) -> AsyncPostgreSQLPool:
    await _execute(postgres_pool, _RESET)
    return postgres_pool


@pytest_asyncio.fixture
async def second_pool(pool: AsyncPostgreSQLPool) -> AsyncIterator[AsyncPostgreSQLPool]:  # noqa: ARG001 - after `pool` has reset the tables
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
    # Hidden by a rename rather than dropped: the loader's refresh-job table holds a foreign key
    # to it, and a rename keeps that constraint for the rename back.
    await _execute(pool, "ALTER TABLE loader_extraction_latch RENAME TO loader_extraction_latch_hidden")
    try:
        assert await run_pending(pool, neo4j_driver) is None
    finally:
        await _execute(pool, "ALTER TABLE loader_extraction_latch_hidden RENAME TO loader_extraction_latch")


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
    # Recorded under the failed action, which is not the marker.
    [(failed_target,)] = await _execute(pool, "SELECT target FROM admin_audit_log WHERE action = %s", (FAILED_ACTION,))
    assert failed_target == "discogs:20260901"

    # The lock was released, so the other replica's next poll takes it and completes the run.
    assert await run_pending(second_pool, neo4j_driver) == "20260901"
    assert len(await _markers(pool)) == 1


async def _raise_audit_unavailable(*_args: Any, **_kwargs: Any) -> None:
    raise RuntimeError("audit table unavailable")


@pytest.mark.parametrize("stop", ["marker_write_fails", "cancelled_after_the_writes"])
async def test_a_run_stopped_after_its_started_row_is_retried_and_every_decision_ref_resolves(
    pool: AsyncPostgreSQLPool, second_pool: AsyncPostgreSQLPool, neo4j_driver: AsyncResilientNeo4jDriver, stop: str
) -> None:
    await _split_release(pool, 7003)
    await _latch(pool, "20260901", _ALL)
    real = reattach_trigger.run_reattachment

    async def cancelled_after_the_writes(target: Any, **kwargs: Any) -> dict[str, Any]:
        await real(target, **kwargs)
        raise asyncio.CancelledError  # a shutdown mid-run: not an Exception, so no failed update

    if stop == "marker_write_fails":
        stopped = patch.object(reattach_trigger, "update_audit_entry", _raise_audit_unavailable)
        expected: type[BaseException] = RuntimeError
    else:
        stopped = patch.object(reattach_trigger, "run_reattachment", cancelled_after_the_writes)
        expected = asyncio.CancelledError
    with stopped, pytest.raises(expected):
        await run_pending(pool, neo4j_driver)

    # The identity writes committed under a row that exists but is not the handled marker.
    assert await _markers(pool) == []
    [(first_id, first_target)] = await _execute(pool, "SELECT id, target FROM admin_audit_log WHERE action = %s", (STARTED_ACTION,))
    assert first_target == "discogs:20260901"
    assert await _execute(pool, "SELECT decision_ref FROM catalog_item_supersessions") == [(first_id,)]
    assert await _execute(pool, _DANGLING) == [(0,)]

    # Not handled, and the lock was released: the next poll retries under its own job id.
    assert await run_pending(second_pool, neo4j_driver) == "20260901"
    [marker_id] = [row[0] for row in await _execute(pool, "SELECT id FROM admin_audit_log WHERE action = %s", (AUDIT_ACTION,))]
    assert marker_id != first_id
    assert await _execute(pool, "SELECT id FROM admin_audit_log WHERE action = %s", (STARTED_ACTION,)) == [(first_id,)]
    assert await _execute(pool, _DANGLING) == [(0,)]
    assert await run_pending(pool, neo4j_driver) is None


async def test_a_started_row_that_cannot_be_written_stops_the_run(pool: AsyncPostgreSQLPool, neo4j_driver: AsyncResilientNeo4jDriver) -> None:
    mbid, split, _discogs_native = await _split_release(pool, 7004)
    await _latch(pool, "20260901", _ALL)
    with patch.object(reattach_trigger, "insert_audit_entry", _raise_audit_unavailable), pytest.raises(RuntimeError):
        await run_pending(pool, neo4j_driver)

    assert await _current(pool, "musicbrainz", "release", mbid) == [(split, "catalog")]
    assert await _execute(pool, "SELECT count(*) FROM catalog_item_supersessions") == [(0,)]
    assert await _execute(pool, "SELECT count(*) FROM admin_audit_log") == [(0,)]
    # The lock was released: the next poll runs it.
    assert await run_pending(pool, neo4j_driver) == "20260901"
