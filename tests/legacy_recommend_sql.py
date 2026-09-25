"""The candidate generator gm-catalog-api-tsmu.1 replaced.

Top 5 genres, 500 artists per genre, 200 overall, 50 profiled, a per-genre release scan
capped at 100k. Kept here rather than in ``api/queries/recommend_pg_queries.py`` purely so
tests can measure the new set-based, uncapped query (``CANDIDATE_ARTISTS_SQL``) against the
shape it replaced, on identical data, on both the small parity fixture
(``tests/test_real_databases.py``) and the larger synthetic latency fixture
(``tests/test_recommend_candidate_latency.py``).
"""

from __future__ import annotations

from typing import Final


LEGACY_CANDIDATE_ARTISTS_SQL: Final[str] = """
WITH target_genres AS (
    SELECT genre.genre_name, count(DISTINCT own.release_id)::bigint AS genre_count
    FROM graph.by_artist own JOIN graph.in_genre genre USING (release_id)
    WHERE own.artist_id = %(artist_id)s
    GROUP BY genre.genre_name ORDER BY genre_count DESC, genre.genre_name LIMIT 5
), genre_counts AS (
    SELECT target.genre_name, candidate.artist_id,
           count(DISTINCT sample.release_id)::bigint AS shared_in_genre
    FROM target_genres target
    CROSS JOIN LATERAL (
        SELECT release_id FROM graph.in_genre
        WHERE genre_name = target.genre_name ORDER BY release_id LIMIT 100000
    ) sample
    JOIN graph.by_artist candidate ON candidate.release_id = sample.release_id
    JOIN graph.artist artist ON artist.artist_id = candidate.artist_id
    WHERE candidate.artist_id <> %(artist_id)s AND artist.name IS NOT NULL
    GROUP BY target.genre_name, candidate.artist_id
), per_genre AS (
    SELECT *, row_number() OVER (
        PARTITION BY genre_name ORDER BY shared_in_genre DESC, artist_id
    ) AS rank_in_genre FROM genre_counts
), ranked AS (
    SELECT artist_id, sum(shared_in_genre)::bigint AS release_count
    FROM per_genre WHERE rank_in_genre <= 500 GROUP BY artist_id
    HAVING sum(shared_in_genre) >= %(min_releases)s
    ORDER BY release_count DESC, artist_id LIMIT 200
)
SELECT ranked.artist_id, artist.name, ranked.release_count
FROM ranked JOIN graph.artist artist USING (artist_id)
ORDER BY ranked.release_count DESC, ranked.artist_id LIMIT 50
"""
