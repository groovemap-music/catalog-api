"""The gm-catalog-api-tsmu.1 set-based, all-signal candidate query -- evaluation-only.

The maintainer's decision (round 5, option (b)): the production similar-artist path stays on
the legacy per-genre-capped candidate generator (``api.queries.recommend_pg_queries.
CANDIDATE_ARTISTS_SQL`` and ``api.queries.recommend_queries.get_candidate_artists``'s Cypher).
The serving-side improvement moves to gm-catalog-api-2zsq (kNN retrieval) instead. What stays
from this bead is the *evaluation* side -- ``api.evaluation``'s registered
``similar-artist-all-signals-2026-09`` baseline, which mirrors this query in pure Python via
``GoldenGraph.candidate_artists_all_signals`` -- and this module, which keeps the real SQL
reproducible for the latency benchmark (``tests/test_recommend_candidate_latency.py``) without
it being production code.

A candidate qualifies by sharing at least one genre, style, or label with any of the target's
releases, or by appearing on the same release as the target (collaborator), and by clearing a
minimum shared-release-count floor. There is no per-genre cap during expansion: the query
finds and ranks every qualifying artist by shared release count (descending, ties broken by
artist id ascending) before an overall ``LIMIT`` caps how many are returned for profiling.
This is what was measured, across four rounds, to cost more in profiling and scoring latency
than the 20% budget allowed at every swept cap -- see
``docs/query-performance-optimizations.md``.
"""

from __future__ import annotations

import asyncio
from typing import Any, Final

from api.queries import recommend_pg_queries


ALL_SIGNAL_CANDIDATE_ARTISTS_SQL: Final[str] = """
WITH target_releases AS (
    SELECT release_id FROM graph.by_artist WHERE artist_id = %(artist_id)s
), target_genres AS (
    SELECT DISTINCT genre_name FROM graph.in_genre
    WHERE release_id IN (SELECT release_id FROM target_releases)
), target_styles AS (
    SELECT DISTINCT style_name FROM graph.in_style
    WHERE release_id IN (SELECT release_id FROM target_releases)
), target_labels AS (
    SELECT DISTINCT label_id FROM graph.on_label
    WHERE release_id IN (SELECT release_id FROM target_releases)
), signal_hits AS (
    SELECT candidate.artist_id, candidate.release_id
    FROM graph.by_artist candidate
    JOIN graph.in_genre genre USING (release_id)
    WHERE candidate.artist_id <> %(artist_id)s AND genre.genre_name IN (SELECT genre_name FROM target_genres)
    UNION
    SELECT candidate.artist_id, candidate.release_id
    FROM graph.by_artist candidate
    JOIN graph.in_style style USING (release_id)
    WHERE candidate.artist_id <> %(artist_id)s AND style.style_name IN (SELECT style_name FROM target_styles)
    UNION
    SELECT candidate.artist_id, candidate.release_id
    FROM graph.by_artist candidate
    JOIN graph.on_label edge USING (release_id)
    WHERE candidate.artist_id <> %(artist_id)s AND edge.label_id IN (SELECT label_id FROM target_labels)
    UNION
    SELECT candidate.artist_id, candidate.release_id
    FROM graph.by_artist candidate
    WHERE candidate.artist_id <> %(artist_id)s AND candidate.release_id IN (SELECT release_id FROM target_releases)
), ranked AS (
    SELECT artist_id, count(DISTINCT release_id)::bigint AS release_count
    FROM signal_hits
    GROUP BY artist_id
    HAVING count(DISTINCT release_id) >= %(min_releases)s
)
SELECT ranked.artist_id, artist.name, ranked.release_count
FROM ranked JOIN graph.artist artist USING (artist_id)
WHERE artist.name IS NOT NULL
ORDER BY ranked.release_count DESC, ranked.artist_id
LIMIT %(limit)s
"""


async def batch_artist_profiles_concurrent(pool: Any, candidate_ids: list[str]) -> dict[str, dict[str, Any]]:
    """The round-3 profile-batch shape: the four dimension queries run concurrently.

    Not shipped in production (per the round-5 decision, concurrent profiling is a
    recommended follow-up, not a change made here) -- kept as a reproducible comparator so
    its measured gain (docs/query-performance-optimizations.md) stays checkable against
    ``recommend_pg_queries._batch_artist_profiles``'s sequential shape.
    """
    profiles: dict[str, dict[str, Any]] = {artist_id: {"genres": [], "styles": [], "labels": [], "collaborators": []} for artist_id in candidate_ids}
    dimensions = list(recommend_pg_queries._BATCH_PROFILE_SQL)
    results = await asyncio.gather(
        *(
            recommend_pg_queries._rows(pool, recommend_pg_queries._BATCH_PROFILE_SQL[dimension], {"artist_ids": candidate_ids})
            for dimension in dimensions
        )
    )
    for dimension, dimension_rows in zip(dimensions, results, strict=True):
        for artist_id, name, count in dimension_rows:
            profiles[artist_id][dimension].append({"name": name, "count": count})
    return profiles


async def get_candidate_artists(
    pool: Any, artist_id: str, *, min_releases: int, limit: int, concurrent_profiles: bool = True
) -> list[dict[str, Any]]:
    """The all-signal query as a drop-in comparator for the benchmark. Not called from production.

    Args:
        min_releases: The shared-release-count floor (production's ``MIN_ARTIST_RELEASES``).
        limit: The overall profile/score cap swept across benchmark runs.
        concurrent_profiles: Whether to fetch the four profile dimensions concurrently
            (the round-3 shape) or sequentially (matching production's current, reverted
            shape). Default ``True`` to demonstrate the recommended follow-up.
    """
    rows = await recommend_pg_queries._rows(
        pool, ALL_SIGNAL_CANDIDATE_ARTISTS_SQL, {"artist_id": artist_id, "min_releases": min_releases, "limit": limit}
    )
    if not rows:
        return []
    candidate_ids = [row[0] for row in rows]
    profiles = (
        await batch_artist_profiles_concurrent(pool, candidate_ids)
        if concurrent_profiles
        else await recommend_pg_queries._batch_artist_profiles(pool, candidate_ids)
    )
    return [{"artist_id": aid, "artist_name": name, "release_count": count, **profiles[aid]} for aid, name, count in rows]
