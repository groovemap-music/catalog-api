"""Engine-backed checks for the precomputed similar-artist reads (gm-catalog-api-2zsq.1).

Runs on the integration tier (`just test-integration`), whose PostgreSQL image is whatever
`scripts/test-integration.sh` pins; the server version is printed with the timings. That
image carries no pgvector, so `public.artist_embeddings` does not exist here: the vector
lookup is covered by unit tests, and its absence is itself the "unreadable vectors" case.

The release and list tables are declared unconditionally by the schema, so the real
primary-key read is exercised against real rows at a realistic list width (K=50).
"""

from __future__ import annotations

import os
import statistics
from collections.abc import AsyncIterator
from time import perf_counter

import pytest
import pytest_asyncio
from common import AsyncPostgreSQLPool, AsyncResilientNeo4jDriver, parse_postgres_host_port
from groovemap_schema.postgres import create_postgres_schema

import api.routers.recommend as recommend_router
from api.queries.similar_precomputed_queries import get_current_list, get_embedding_cosines


pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

ARTISTS = 100_000
K = 50
SAMPLES = 2_000
MODEL_VERSION = "integration-fastrp-v1"


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.fail(f"{name} must be supplied by `just test-integration`")
    return value


@pytest_asyncio.fixture
async def pool() -> AsyncIterator[AsyncPostgreSQLPool]:
    host, port = parse_postgres_host_port(_required_env("POSTGRES_HOST"))
    pg_pool = AsyncPostgreSQLPool(
        connection_params={
            "host": host,
            "port": port,
            "dbname": _required_env("POSTGRES_DATABASE"),
            "user": _required_env("POSTGRES_USERNAME"),
            "password": _required_env("POSTGRES_PASSWORD"),
        },
        min_connections=1,
        max_connections=2,
        max_retries=1,
        health_check_interval=3600,
    )
    await pg_pool.initialize()
    assert await create_postgres_schema(pg_pool) == 0
    async with pg_pool.connection() as conn, conn.cursor() as cursor:
        await cursor.execute("DELETE FROM public.artist_embedding_releases WHERE model_version = %s", (MODEL_VERSION,))
    try:
        yield pg_pool
    finally:
        async with pg_pool.connection() as conn, conn.cursor() as cursor:
            await cursor.execute("DELETE FROM public.artist_embedding_releases WHERE model_version = %s", (MODEL_VERSION,))
        await pg_pool.close()


async def _publish(pool: AsyncPostgreSQLPool, *, artists: int, current: bool) -> int:
    async with pool.connection() as conn, conn.cursor() as cursor:
        await cursor.execute(
            "INSERT INTO public.artist_embedding_releases (model_version, source_dump_id, source_dump_date, k, artists, is_current) "
            "VALUES (%s, 'integration', DATE '2026-10-01', %s, %s, FALSE) RETURNING release_id",
            (MODEL_VERSION, K, artists),
        )
        row = await cursor.fetchone()
        release_id = int(row[0])
        await cursor.execute(
            """
            INSERT INTO public.artist_similar_artists (release_id, artist_id, similar_artist_ids, scores)
            SELECT %s, a::text,
                   ARRAY(SELECT ((a + n) %% %s)::text FROM generate_series(1, %s) AS n),
                   ARRAY(SELECT (1.0 - n / 100.0)::real FROM generate_series(1, %s) AS n)
            FROM generate_series(0, %s - 1) AS a
            """,
            (release_id, artists, K, K, artists),
        )
        await cursor.execute("ANALYZE public.artist_similar_artists")
        if current:
            await cursor.execute("UPDATE public.artist_embedding_releases SET is_current = TRUE WHERE release_id = %s", (release_id,))
    return release_id


async def test_current_release_list_is_read_by_primary_key(pool: AsyncPostgreSQLPool) -> None:
    release_id = await _publish(pool, artists=1_000, current=True)
    result = await get_current_list(pool, "10")
    assert result is not None
    assert (result.release_id, result.model_version) == (release_id, MODEL_VERSION)
    assert len(result.neighbours) == K
    assert result.neighbours[0] == ("11", pytest.approx(0.99, abs=1e-6))
    assert await get_current_list(pool, "no-such-artist") is None


async def test_a_release_that_is_not_current_is_not_served(pool: AsyncPostgreSQLPool) -> None:
    await _publish(pool, artists=10, current=False)
    assert await get_current_list(pool, "1") is None


async def test_missing_embeddings_table_is_unreadable_not_an_error(pool: AsyncPostgreSQLPool) -> None:
    async with pool.connection() as conn, conn.cursor() as cursor:
        await cursor.execute("SELECT to_regclass('public.artist_embeddings') IS NOT NULL")
        row = await cursor.fetchone()
    if row[0]:
        pytest.skip("this PostgreSQL carries pgvector; the absent-table case is not reachable here")
    assert await get_embedding_cosines(pool, MODEL_VERSION, "1", ["2"]) is None


async def test_primary_key_read_latency(pool: AsyncPostgreSQLPool, capsys: pytest.CaptureFixture[str]) -> None:
    """Record the per-request read cost at 100k artists x K=50. Printed, with the server version."""
    await _publish(pool, artists=ARTISTS, current=True)
    async with pool.connection() as conn, conn.cursor() as cursor:
        await cursor.execute("SHOW server_version")
        version = (await cursor.fetchone())[0]
    step = ARTISTS // SAMPLES
    ids = [str((i * step) % ARTISTS) for i in range(SAMPLES)]
    for artist_id in ids[:50]:  # warm the connection and cache
        await get_current_list(pool, artist_id)
    timings: list[float] = []
    for artist_id in ids:
        started = perf_counter()
        result = await get_current_list(pool, artist_id)
        timings.append((perf_counter() - started) * 1000.0)
        assert result is not None
    timings.sort()
    p50, p95, p99 = (timings[int(len(timings) * q) - 1] for q in (0.50, 0.95, 0.99))
    with capsys.disabled():
        print(
            f"\nPRECOMPUTED_READ_LATENCY server={version} artists={ARTISTS} k={K} samples={SAMPLES} "
            f"p50={p50:.2f}ms p95={p95:.2f}ms p99={p99:.2f}ms mean={statistics.fmean(timings):.2f}ms"
        )
    assert p95 < 250.0, "a primary-key read should not approach this; investigate rather than raise the bound"


async def test_pure_path_latency_with_graph_names(pool: AsyncPostgreSQLPool, capsys: pytest.CaptureFixture[str]) -> None:
    """The endpoint's work minus HTTP, auth, rate limiting, and the identity check.

    At alpha=1.0 that is the primary-key list read plus one Neo4j lookup for the K names.
    """
    driver = AsyncResilientNeo4jDriver(
        uri=_required_env("NEO4J_HOST"),
        auth=(_required_env("NEO4J_USERNAME"), _required_env("NEO4J_PASSWORD")),
        max_retries=1,
    )
    saved = (recommend_router._neo4j_driver, recommend_router._pg_pool, recommend_router._fusion_alpha)
    try:
        async with driver.session(database="neo4j") as session:
            await (await session.run("MATCH (n) DETACH DELETE n")).consume()
            await (
                await session.run(
                    "UNWIND range(0, $n - 1) AS i CREATE (a:Artist {id: toString(i), name: 'Artist ' + toString(i)}) "
                    "WITH a, i CREATE (r:Release {id: 'r' + toString(i)})-[:BY]->(a)",
                    {"n": 1_000},
                )
            ).consume()
        await _publish(pool, artists=1_000, current=True)
        recommend_router._neo4j_driver, recommend_router._pg_pool, recommend_router._fusion_alpha = driver, pool, 1.0
        timings: list[float] = []
        for i in range(550):
            started = perf_counter()
            plist = await get_current_list(pool, str(i))
            assert plist is not None
            ranked = await recommend_router._rank_precomputed(str(i), plist)
            timings.append((perf_counter() - started) * 1000.0)
            assert ranked is not None
            assert len(ranked) == K
            if i == 0:
                assert ranked[0]["artist_name"] == "Artist 1"
        timings = sorted(timings[50:])
        p50, p95 = (timings[int(len(timings) * q) - 1] for q in (0.50, 0.95))
        with capsys.disabled():
            print(f"\nPRECOMPUTED_PURE_PATH_LATENCY samples={len(timings)} k={K} p50={p50:.2f}ms p95={p95:.2f}ms")
    finally:
        recommend_router._neo4j_driver, recommend_router._pg_pool, recommend_router._fusion_alpha = saved
        async with driver.session(database="neo4j") as session:
            await (await session.run("MATCH (n) DETACH DELETE n")).consume()
        await driver.close()
