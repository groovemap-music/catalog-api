"""gm-catalog-api-tsmu.1: endpoint-shaped latency, old candidate path vs new.

Review round 1 pointed out that the bare-SQL mean measured against the small (120-release)
parity fixture does not answer the acceptance criteria's actual question: the old candidate
generator's caps (top 5 genres, 500 artists per genre, a 100k-release-per-genre scan, 200
overall, 50 profiled) exist precisely because a broad genre blows those numbers up, and a
120-release fixture is too thin for any genre to ever be broad. This module measures against
a synthetic, catalog-shaped fixture instead (``scripts/generate_latency_fixture.py``): a
handful of mega genres/styles/labels, a long tail of niche ones, and a prolific artist at the
high end of the release-count distribution (release_count ~1285, comfortably past p99 for
this fixture's ~2500 artists).

"Endpoint-shaped" means the same call sequence ``api/routers/recommend.py``'s
``similar_artists`` makes: ``get_artist_identity``, then ``get_artist_profile`` and
``get_candidate_artists`` concurrently, then ``compute_similar_artists`` -- not just the bare
candidate SQL. The FastAPI/HTTP layer itself is not exercised (no ASGI transport, no
JSON-response serialization); that overhead is the same for both paths and is not what this
bead changed.

Manual/diagnostic, not part of `just check` or the default `just test-integration[-pg19]`
target: generating and seeding several thousand releases, then profiling a candidate pool that
can run into the thousands for the mega-genre target, takes real wall-clock time that does not
belong in the deterministic gate. Run explicitly, e.g. on the PG19 tier the acceptance
criteria names:

    POSTGRES_INTEGRATION_IMAGE="postgres:19beta3-alpine@sha256:b1692e50613a21e61c424859f943b9e193ae73e5a8c68abd5382dfb235bf15fc" \\
    SCHEMA_PROPERTY_GRAPH=enabled \\
    INTEGRATION_TEST_TARGET="tests/test_recommend_candidate_latency.py" \\
    PYTEST_ADDOPTS="-s" \\
    bash scripts/test-integration.sh

No pass/fail latency gate is asserted here on purpose. Per review: if the new path regresses
by more than 20% against the old one, that is documented and stopped for review, not silently
fixed by this test adding a cap back in.
"""

from __future__ import annotations

import asyncio
import json
import statistics
from collections.abc import Awaitable, Callable
from time import perf_counter
from typing import Any

import pytest

from api.queries import recommend_pg_queries
from api.queries.recommend_queries import compute_similar_artists
from scripts.generate_latency_fixture import LatencyFixture, build_fixture, percentile
from tests.graph_fixture import (
    _BOOTSTRAP_FILL,
    _SEED_ARTIST,
    _SEED_LABEL,
    _SEED_RELEASE,
    _TRUNCATE_ENTITIES,
    open_postgres_pool,
)
from tests.legacy_recommend_sql import LEGACY_CANDIDATE_ARTISTS_SQL


pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_REPS = 15
_MIN_RELEASES = 3


async def _seed_latency_fixture(pool: Any, fixture: LatencyFixture) -> None:
    """Seed the synthetic catalog as documents, then let the schema producer project it.

    Same two-step ``seed_postgres`` uses (write documents, then ``graph.bootstrap_fill()``),
    just with ``executemany`` batches sized for thousands of rows instead of a per-row loop
    sized for a couple hundred.
    """
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


async def _legacy_get_candidate_artists(pool: Any, artist_id: str) -> list[dict[str, Any]]:
    """The candidate generator gm-catalog-api-tsmu.1 replaced, as a drop-in for comparison.

    Mirrors the pre-tsmu.1 ``recommend_pg_queries.get_candidate_artists`` exactly: the capped
    query, then only the first 50 rows profiled.
    """
    rows = await recommend_pg_queries._rows(pool, LEGACY_CANDIDATE_ARTISTS_SQL, {"artist_id": artist_id, "min_releases": _MIN_RELEASES})
    if not rows:
        return []
    profile_candidates = rows[:50]
    profiles = await recommend_pg_queries._batch_artist_profiles(pool, [row[0] for row in profile_candidates])
    return [{"artist_id": aid, "artist_name": name, "release_count": count, **profiles[aid]} for aid, name, count in profile_candidates]


async def _capped_get_candidate_artists(pool: Any, artist_id: str, cap: int) -> list[dict[str, Any]]:
    """Experimental only, not shipped: the new (uncapped, all-signal) candidate SQL, but with
    an overall post-hoc cap applied to the *profiled* set -- the top ``cap`` candidates by the
    query's own ``release_count DESC`` order, which already reflects genuine multi-signal
    overlap, not just genre. Measures whether the cost is in the candidate SQL itself or in
    profiling/scoring every match; see the module docstring and the bead report for why this
    is a measurement, not a proposal being implemented here.
    """
    rows = await recommend_pg_queries._rows(pool, recommend_pg_queries.CANDIDATE_ARTISTS_SQL, {"artist_id": artist_id, "min_releases": _MIN_RELEASES})
    if not rows:
        return []
    profile_candidates = rows[:cap]
    profiles = await recommend_pg_queries._batch_artist_profiles(pool, [row[0] for row in profile_candidates])
    return [{"artist_id": aid, "artist_name": name, "release_count": count, **profiles[aid]} for aid, name, count in profile_candidates]


async def _similar_artists_endpoint(pool: Any, artist_id: str, candidate_fn: Callable[[Any, str], Awaitable[list[dict[str, Any]]]]) -> int:
    """Replay ``api/routers/recommend.py::similar_artists``'s call sequence; return candidate count.

    Identity first (as the router does, to 404/422 before doing any real work), then profile
    and candidates concurrently, then the same ranking call the router makes. Timed as a whole
    by the caller -- this function's return value is only for the report table.
    """
    identity = await recommend_pg_queries.get_artist_identity(pool, artist_id)
    assert identity is not None
    profile, candidates = await asyncio.gather(
        recommend_pg_queries.get_artist_profile(pool, artist_id),
        candidate_fn(pool, artist_id),
    )
    compute_similar_artists(profile, candidates, limit=50)
    return len(candidates)


async def _measure(pool: Any, artist_id: str, candidate_fn: Callable[[Any, str], Awaitable[list[dict[str, Any]]]]) -> tuple[list[float], int]:
    """Run the endpoint chain ``_REPS`` times; return (latencies in ms, candidate count)."""
    latencies: list[float] = []
    candidate_count = 0
    for _ in range(_REPS):
        before = perf_counter()
        candidate_count = await _similar_artists_endpoint(pool, artist_id, candidate_fn)
        latencies.append((perf_counter() - before) * 1000)
    return latencies, candidate_count


def _p95(values: list[float]) -> float:
    if len(values) == 1:
        return values[0]
    return statistics.quantiles(values, n=100, method="inclusive")[94]


async def test_similar_artist_endpoint_latency_old_vs_new_on_a_realistic_fixture() -> None:
    """Print p50/p95 for the legacy and new candidate paths, per target-artist tier."""
    fixture = build_fixture()
    pool = await open_postgres_pool()
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
        header = f"{'target':<45}{'path':<10}{'candidates':>11}{'p50 ms':>10}{'p95 ms':>10}"
        print(header)
        print("-" * len(header))
        experimental_cap = 200

        def _capped_fn(pool: Any, artist_id: str) -> Awaitable[list[dict[str, Any]]]:
            return _capped_get_candidate_artists(pool, artist_id, experimental_cap)

        rows: dict[str, dict[str, tuple[list[float], int]]] = {}
        for label, artist_id in targets.items():
            assert counts[artist_id] >= _MIN_RELEASES, f"{label} target has too few releases to query"
            rows[label] = {}
            for path_name, candidate_fn in (
                ("legacy", _legacy_get_candidate_artists),
                ("new", recommend_pg_queries.get_candidate_artists),
                (f"new-capped-{experimental_cap}", _capped_fn),
            ):
                latencies, candidate_count = await _measure(pool, artist_id, candidate_fn)
                rows[label][path_name] = (latencies, candidate_count)
                p50 = statistics.median(latencies)
                p95 = _p95(latencies)
                print(f"{label:<45}{path_name:<10}{candidate_count:>11}{p50:>10.2f}{p95:>10.2f}")

        print()
        for label in targets:
            legacy_p95 = _p95(rows[label]["legacy"][0])
            new_p95 = _p95(rows[label]["new"][0])
            capped_p95 = _p95(rows[label][f"new-capped-{experimental_cap}"][0])
            new_delta = (new_p95 - legacy_p95) / legacy_p95 * 100 if legacy_p95 else float("inf")
            capped_delta = (capped_p95 - legacy_p95) / legacy_p95 * 100 if legacy_p95 else float("inf")
            print(
                f"{label}: new p95 delta = {new_delta:+.1f}% (legacy {legacy_p95:.2f}ms -> new {new_p95:.2f}ms); "
                f"new-capped-{experimental_cap} p95 delta = {capped_delta:+.1f}% ({capped_p95:.2f}ms)"
            )

        # Sanity, not a latency gate: both paths must actually return candidates for the mega
        # and mid targets (a query that silently returns nothing would make the timing above
        # meaningless), and the new path's candidate pool must be at least as large as the
        # legacy path's capped one -- that is the whole point of the change.
        for label in ("mega (p99 release count, all-mega facets)", "mid (median release count)"):
            assert rows[label]["legacy"][1] > 0
            assert rows[label]["new"][1] > 0
            assert rows[label]["new"][1] >= rows[label]["legacy"][1]
    finally:
        await pool.close()
