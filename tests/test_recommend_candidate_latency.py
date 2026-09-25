"""gm-catalog-api-tsmu.1 round 3: endpoint-shaped latency, cap sweep and concurrency gain.

Round 1 measured against the small (120-release) parity fixture and found nothing (no genre
is ever broad there). Round 2 built a catalog-shaped synthetic fixture and found the new
all-signal candidate path regresses p95 by 178-369% over the old one -- the candidate SQL
itself is cheap even uncapped, but profiling and scoring every matched candidate is not.
Round 3, per the maintainer's decision (keep the all-signal scope, improve performance):

1. Caps the *profiled/scored* set to the top N by shared release_count (ties broken by
   artist_id), pushed into the query itself (``LIMIT`` in both SQL and Cypher) so more than N
   rows are never fetched into Python. Swept here for N in 50/100/200/500 against both
   recall@10 (see ``tests/test_evaluation_similar_artist_candidates.py`` for the golden-set
   side of this sweep) and endpoint p95.
2. Runs the four profile-batch queries concurrently instead of sequentially
   (``api/queries/recommend_pg_queries.py::_batch_artist_profiles``), measured here in
   isolation from the cap (uncapped, sequential vs uncapped, concurrent).
3. Adds a larger synthetic tier (see ``test_..._scales_with_catalog_size`` below) to see how
   the candidate SQL and the endpoint scale between two sizes, since the round-2 fixture
   (2,500 artists) put 74% of all artists in the mega target's candidate pool -- a shape a
   real catalog would not have at that size, only at a much larger one.

"Endpoint-shaped" means the same call sequence ``api/routers/recommend.py``'s
``similar_artists`` makes: ``get_artist_identity``, then ``get_artist_profile`` and
``get_candidate_artists`` concurrently, then ``compute_similar_artists``. The FastAPI/HTTP
layer itself is not exercised.

Manual/diagnostic, not part of `just check` or the default `just test-integration[-pg19]`
target. Run explicitly, e.g. on the PG19 tier the acceptance criteria names:

    POSTGRES_INTEGRATION_IMAGE="postgres:19beta3-alpine@sha256:b1692e50613a21e61c424859f943b9e193ae73e5a8c68abd5382dfb235bf15fc" \\
    SCHEMA_PROPERTY_GRAPH=enabled \\
    INTEGRATION_TEST_TARGET="tests/test_recommend_candidate_latency.py" \\
    PYTEST_ADDOPTS="-s" \\
    bash scripts/test-integration.sh

No pass/fail latency gate is asserted here on purpose. Per review: a regression is documented
and stopped for review, not silently fixed by picking a budget in this test.
"""

from __future__ import annotations

import asyncio
import json
import statistics
from collections.abc import Awaitable, Callable
from time import perf_counter
from typing import Any

import pytest
from common import AsyncPostgreSQLPool, parse_postgres_host_port
from groovemap_schema.postgres import PROPERTY_GRAPH_MINIMUM_SERVER_VERSION, create_postgres_schema, property_graph_enabled

from api.queries import recommend_pg_queries
from api.queries.recommend_queries import compute_similar_artists
from scripts.generate_latency_fixture import LatencyFixture, build_fixture, percentile
from tests.graph_fixture import (
    _BOOTSTRAP_FILL,
    _SEED_ARTIST,
    _SEED_LABEL,
    _SEED_RELEASE,
    _TRUNCATE_ENTITIES,
    required_env,
)
from tests.legacy_recommend_sql import LEGACY_CANDIDATE_ARTISTS_SQL


pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_REPS = 12
_MIN_RELEASES = 3
_SERVER_VERSION_SQL = "SELECT current_setting('server_version_num')::int"


async def _open_pool(max_connections: int = 8) -> AsyncPostgreSQLPool:
    """Same as ``tests.graph_fixture.open_postgres_pool``, but with a production-sized pool.

    The shared fixture's pool caps at 2 connections, which would itself serialize the
    concurrent profile-batch queries this module measures the gain of and make the isolation
    meaningless. 8 matches ``ApiConfig.postgres_pool_max_size``'s default (``api/config.py``).
    """
    host, port = parse_postgres_host_port(required_env("POSTGRES_HOST"))
    pool = AsyncPostgreSQLPool(
        connection_params={
            "host": host,
            "port": port,
            "dbname": required_env("POSTGRES_DATABASE"),
            "user": required_env("POSTGRES_USERNAME"),
            "password": required_env("POSTGRES_PASSWORD"),
        },
        min_connections=1,
        max_connections=max_connections,
        max_retries=1,
        health_check_interval=3600,
    )
    await pool.initialize()
    if property_graph_enabled():
        async with pool.connection() as conn, conn.cursor() as cursor:
            await cursor.execute(_SERVER_VERSION_SQL)
            row = await cursor.fetchone()
        server_version_num = int(row[0]) if row else 0
        assert server_version_num >= PROPERTY_GRAPH_MINIMUM_SERVER_VERSION
    failures = await create_postgres_schema(pool)
    assert failures == 0, f"{failures} schema statements failed against the integration container"
    return pool


async def _seed_latency_fixture(pool: Any, fixture: LatencyFixture) -> None:
    """Seed the synthetic catalog as documents, then let the schema producer project it."""
    artist_rows = [(artist_id, "latency-fixture", json.dumps({"name": name})) for artist_id, name in fixture.artists.items()]
    label_rows = [(label_id, "latency-fixture", json.dumps({"name": name})) for label_id, name in fixture.labels.items()]
    release_rows = [
        (
            release.id,
            "latency-fixture",
            json.dumps(
                {
                    "title": f"Release {release.id}",
                    "year": release.year,
                    "artists": [{"id": int(artist_id)} for artist_id in release.artist_ids],
                    "labels": [{"id": int(release.label_id)}],
                    "genres": [release.genre],
                    "styles": [release.style],
                }
            ),
        )
        for release in fixture.releases
    ]
    async with pool.connection() as conn, conn.cursor() as cursor:
        await cursor.execute(_TRUNCATE_ENTITIES)
        await cursor.executemany(_SEED_ARTIST, artist_rows)
        await cursor.executemany(_SEED_LABEL, label_rows)
        await cursor.executemany(_SEED_RELEASE, release_rows)
        await cursor.execute(_BOOTSTRAP_FILL)
        await cursor.fetchall()


async def _sequential_batch_artist_profiles(pool: Any, candidate_ids: list[str]) -> dict[str, dict[str, Any]]:
    """The pre-round-3 sequential profile fetch, kept here only to isolate the concurrency gain.

    Production's ``recommend_pg_queries._batch_artist_profiles`` is now always concurrent
    (round 3); this is a frozen copy of what it replaced, so ``legacy`` and the
    ``*-sequential`` variants below stay comparable across rounds instead of silently
    inheriting the round-3 speedup.
    """
    profiles: dict[str, dict[str, Any]] = {artist_id: {"genres": [], "styles": [], "labels": [], "collaborators": []} for artist_id in candidate_ids}
    for dimension, sql in recommend_pg_queries._BATCH_PROFILE_SQL.items():
        for artist_id, name, count in await recommend_pg_queries._rows(pool, sql, {"artist_ids": candidate_ids}):
            profiles[artist_id][dimension].append({"name": name, "count": count})
    return profiles


async def _legacy_get_candidate_artists(pool: Any, artist_id: str) -> list[dict[str, Any]]:
    """The candidate generator gm-catalog-api-tsmu.1 replaced, as a drop-in for comparison.

    Mirrors the pre-tsmu.1 ``recommend_pg_queries.get_candidate_artists`` exactly: the capped
    query, then only the first 50 rows profiled, sequentially.
    """
    rows = await recommend_pg_queries._rows(pool, LEGACY_CANDIDATE_ARTISTS_SQL, {"artist_id": artist_id, "min_releases": _MIN_RELEASES})
    if not rows:
        return []
    profile_candidates = rows[:50]
    profiles = await _sequential_batch_artist_profiles(pool, [row[0] for row in profile_candidates])
    return [{"artist_id": aid, "artist_name": name, "release_count": count, **profiles[aid]} for aid, name, count in profile_candidates]


async def _new_uncapped_sequential_get_candidate_artists(pool: Any, artist_id: str) -> list[dict[str, Any]]:
    """The round-2 shape: new all-signal query, uncapped, profiled sequentially.

    Kept only to isolate the concurrency gain (item 2): compared against
    ``recommend_pg_queries.get_candidate_artists(pool, artist_id, limit=<a very large number>)``,
    which is the same query and cap but profiled concurrently, the only variable is
    concurrency.
    """
    rows = await recommend_pg_queries._rows(
        pool, recommend_pg_queries.CANDIDATE_ARTISTS_SQL, {"artist_id": artist_id, "min_releases": _MIN_RELEASES, "limit": 1_000_000}
    )
    if not rows:
        return []
    profiles = await _sequential_batch_artist_profiles(pool, [row[0] for row in rows])
    return [{"artist_id": aid, "artist_name": name, "release_count": count, **profiles[aid]} for aid, name, count in rows]


def _new_capped_concurrent(limit: int) -> Callable[[Any, str], Awaitable[list[dict[str, Any]]]]:
    """The real shipped path (round 3): production's query and concurrent profiling, at ``limit``."""

    def _fn(pool: Any, artist_id: str) -> Awaitable[list[dict[str, Any]]]:
        return recommend_pg_queries.get_candidate_artists(pool, artist_id, limit=limit)

    return _fn


async def _similar_artists_endpoint(pool: Any, artist_id: str, candidate_fn: Callable[[Any, str], Awaitable[list[dict[str, Any]]]]) -> int:
    """Replay ``api/routers/recommend.py::similar_artists``'s call sequence; return candidate count."""
    identity = await recommend_pg_queries.get_artist_identity(pool, artist_id)
    assert identity is not None
    profile, candidates = await asyncio.gather(
        recommend_pg_queries.get_artist_profile(pool, artist_id),
        candidate_fn(pool, artist_id),
    )
    compute_similar_artists(profile, candidates, limit=50)
    return len(candidates)


async def _measure(
    pool: Any, artist_id: str, candidate_fn: Callable[[Any, str], Awaitable[list[dict[str, Any]]]], reps: int = _REPS
) -> tuple[list[float], int]:
    """Run the endpoint chain ``reps`` times; return (latencies in ms, candidate count)."""
    latencies: list[float] = []
    candidate_count = 0
    for _ in range(reps):
        before = perf_counter()
        candidate_count = await _similar_artists_endpoint(pool, artist_id, candidate_fn)
        latencies.append((perf_counter() - before) * 1000)
    return latencies, candidate_count


def _p95(values: list[float]) -> float:
    if len(values) == 1:
        return values[0]
    return statistics.quantiles(values, n=100, method="inclusive")[94]


def _print_row(target: str, variant: str, candidates: int, latencies: list[float]) -> None:
    print(f"{target:<45}{variant:<28}{candidates:>11}{statistics.median(latencies):>10.2f}{_p95(latencies):>10.2f}")


async def test_similar_artist_endpoint_latency_cap_and_concurrency_sweep() -> None:
    """Cap sweep (N=50/100/200/500) and the concurrency gain, isolated from the cap."""
    fixture = build_fixture()
    pool = await _open_pool()
    try:
        await _seed_latency_fixture(pool, fixture)

        counts = fixture.release_counts()
        mid_target = min(
            (artist_id for artist_id, count in counts.items() if count >= _MIN_RELEASES and artist_id != fixture.mega_artist_id),
            key=lambda artist_id: abs(counts[artist_id] - percentile(list(counts.values()), 50)),
        )
        targets = {
            "mega (p99 release count, all-mega facets)": fixture.mega_artist_id,
            "mid (median release count)": mid_target,
            "niche (all-niche facets, few releases)": fixture.niche_artist_id,
        }

        print(f"\nfixture: {len(fixture.artists)} artists, {len(fixture.releases)} releases, {len(fixture.labels)} labels")
        header = f"{'target':<45}{'variant':<28}{'candidates':>11}{'p50 ms':>10}{'p95 ms':>10}"
        print(header)
        print("-" * len(header))

        sweep_limits = (50, 100, 200, 500)
        variants: list[tuple[str, Callable[[Any, str], Awaitable[list[dict[str, Any]]]]]] = [
            ("legacy", _legacy_get_candidate_artists),
            ("new-uncapped-sequential", _new_uncapped_sequential_get_candidate_artists),
            ("new-uncapped-concurrent", _new_capped_concurrent(1_000_000)),
            *((f"new-capped-{n}-concurrent", _new_capped_concurrent(n)) for n in sweep_limits),
        ]

        results: dict[str, dict[str, tuple[list[float], int]]] = {}
        for label, artist_id in targets.items():
            assert counts[artist_id] >= _MIN_RELEASES, f"{label} target has too few releases to query"
            results[label] = {}
            for variant_name, candidate_fn in variants:
                latencies, candidate_count = await _measure(pool, artist_id, candidate_fn)
                results[label][variant_name] = (latencies, candidate_count)
                _print_row(label, variant_name, candidate_count, latencies)

        print("\n--- deltas vs legacy p95 ---")
        for label in targets:
            legacy_p95 = _p95(results[label]["legacy"][0])
            for variant_name, _fn in variants[1:]:
                variant_p95 = _p95(results[label][variant_name][0])
                delta = (variant_p95 - legacy_p95) / legacy_p95 * 100 if legacy_p95 else float("inf")
                print(f"{label} / {variant_name}: {delta:+.1f}% ({legacy_p95:.2f}ms -> {variant_p95:.2f}ms)")

        print("\n--- concurrency gain in isolation (uncapped query, sequential vs concurrent profiling) ---")
        for label in targets:
            sequential_p95 = _p95(results[label]["new-uncapped-sequential"][0])
            concurrent_p95 = _p95(results[label]["new-uncapped-concurrent"][0])
            gain = (sequential_p95 - concurrent_p95) / sequential_p95 * 100 if sequential_p95 else 0.0
            print(f"{label}: sequential {sequential_p95:.2f}ms -> concurrent {concurrent_p95:.2f}ms (-{gain:.1f}%)")

        print("\n--- cap gain in isolation (concurrent profiling throughout, uncapped vs each N) ---")
        for label in targets:
            uncapped_p95 = _p95(results[label]["new-uncapped-concurrent"][0])
            for n in sweep_limits:
                capped_p95 = _p95(results[label][f"new-capped-{n}-concurrent"][0])
                gain = (uncapped_p95 - capped_p95) / uncapped_p95 * 100 if uncapped_p95 else 0.0
                print(f"{label} / N={n}: uncapped {uncapped_p95:.2f}ms -> capped {capped_p95:.2f}ms (-{gain:.1f}%)")

        # Sanity, not a latency gate.
        for label in ("mega (p99 release count, all-mega facets)", "mid (median release count)"):
            assert results[label]["legacy"][1] > 0
            assert results[label]["new-uncapped-concurrent"][1] > 0
            assert results[label][f"new-capped-{sweep_limits[0]}-concurrent"][1] <= results[label]["new-uncapped-concurrent"][1]

        print("\n--- EXPLAIN (ANALYZE, BUFFERS) of the new candidate query, niche target ---")
        async with pool.connection() as conn, conn.cursor() as cursor:
            await cursor.execute(
                f"EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT) {recommend_pg_queries.CANDIDATE_ARTISTS_SQL}",
                {"artist_id": fixture.niche_artist_id, "min_releases": _MIN_RELEASES, "limit": 500},
            )
            plan_rows = await cursor.fetchall()
        print("\n".join(row[0] for row in plan_rows))
    finally:
        await pool.close()


async def test_similar_artist_endpoint_latency_scales_with_catalog_size() -> None:
    """Compare the round-2 fixture (2,500 artists) against a ~12x larger one, same shape.

    The round-2 fixture put 74% of all artists in the mega target's candidate pool -- not a
    shape a real catalog has at any size, only an artifact of a fixture three orders of
    magnitude below production. This tier does not close that gap either (still far short of
    production), but it shows whether cost scales roughly with candidate-pool size (which the
    round-2 EXPLAIN breakdown predicts) or worse, from two points instead of one.

    The uncapped path is measured only as a single bare-SQL count at the large size, not
    profiled and scored repeatedly: an early run of this test timed out inside
    ``compute_similar_artists`` doing exactly that, which is itself a finding -- profiling and
    scoring every match against a mega-genre target is not just slower at this scale, it is
    impractical, and the capped path is the only one worth measuring end to end here.
    """
    small = build_fixture()
    large = build_fixture(seed=20260925, n_artists=30_000, n_releases=100_000)
    pool = await _open_pool()
    try:
        print(f"\n{'fixture':<10}{'artists':>10}{'releases':>10}{'mega candidates (uncapped, bare SQL)':>38}")
        rows: dict[str, dict[str, tuple[list[float], int]]] = {}
        for fixture_name, fixture in (("small", small), ("large", large)):
            await _seed_latency_fixture(pool, fixture)
            mega_id = fixture.mega_artist_id

            uncapped_rows = await recommend_pg_queries._rows(
                pool, recommend_pg_queries.CANDIDATE_ARTISTS_SQL, {"artist_id": mega_id, "min_releases": _MIN_RELEASES, "limit": 1_000_000}
            )
            print(f"{fixture_name:<10}{len(fixture.artists):>10}{len(fixture.releases):>10}{len(uncapped_rows):>38}")

            rows[fixture_name] = {}
            for variant_name, candidate_fn in (
                ("legacy", _legacy_get_candidate_artists),
                ("new-capped-200-concurrent", _new_capped_concurrent(200)),
            ):
                latencies, candidate_count = await _measure(pool, mega_id, candidate_fn, reps=8)
                rows[fixture_name][variant_name] = (latencies, candidate_count)

        print(f"\n{'variant':<28}{'small p95':>12}{'large p95':>12}{'scale factor':>14}")
        for variant_name in ("legacy", "new-capped-200-concurrent"):
            small_p95 = _p95(rows["small"][variant_name][0])
            large_p95 = _p95(rows["large"][variant_name][0])
            factor = large_p95 / small_p95 if small_p95 else float("inf")
            print(f"{variant_name:<28}{small_p95:>12.2f}{large_p95:>12.2f}{factor:>14.2f}x")

        assert rows["large"]["new-capped-200-concurrent"][1] > 0
    finally:
        await pool.close()
