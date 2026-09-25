"""PostgreSQL reads for artist similarity and multi-signal recommendations.

Scoring and merging remain pure functions in ``recommend_queries``; only the
store-touching functions have backend-specific implementations here.
"""

from __future__ import annotations

from typing import Any, cast

from common.query_debug import execute_sql

from api.queries.recommend_queries import MIN_ARTIST_RELEASES


_YEAR = "CASE WHEN btrim(release.year) ~ '^[0-9]{1,9}$' THEN btrim(release.year)::integer END"

ARTIST_IDENTITY_SQL = """
SELECT artist.artist_id, artist.name, count(DISTINCT edge.release_id)::bigint
FROM graph.artist artist
LEFT JOIN graph.by_artist edge ON edge.artist_id = artist.artist_id
WHERE artist.artist_id = %(artist_id)s
GROUP BY artist.artist_id, artist.name
"""

# Every profile dimension counts distinct releases. The two BY edges in the
# collaborator dimension must bind the *same* release, never just the artist.
_PROFILE_SQL = {
    "genres": """SELECT genre.genre_name AS name, count(DISTINCT own.release_id)::bigint AS count
        FROM graph.by_artist own JOIN graph.in_genre genre USING (release_id)
        WHERE own.artist_id = %(artist_id)s GROUP BY genre.genre_name ORDER BY count DESC, name""",
    "styles": """SELECT style.style_name AS name, count(DISTINCT own.release_id)::bigint AS count
        FROM graph.by_artist own JOIN graph.in_style style USING (release_id)
        WHERE own.artist_id = %(artist_id)s GROUP BY style.style_name ORDER BY count DESC, name""",
    "labels": """SELECT label.name, count(DISTINCT own.release_id)::bigint AS count
        FROM graph.by_artist own JOIN graph.on_label edge USING (release_id)
        JOIN graph.label label USING (label_id)
        WHERE own.artist_id = %(artist_id)s GROUP BY label.name ORDER BY count DESC, name""",
    "collaborators": """SELECT other_artist.name, count(DISTINCT own.release_id)::bigint AS count
        FROM graph.by_artist own JOIN graph.by_artist other USING (release_id)
        JOIN graph.artist other_artist ON other_artist.artist_id = other.artist_id
        WHERE own.artist_id = %(artist_id)s AND other.artist_id <> own.artist_id
        GROUP BY other_artist.name ORDER BY count DESC, name""",
}

_BATCH_PROFILE_SQL = {
    dimension: sql.replace("SELECT ", "SELECT own.artist_id, ", 1)
    .replace("WHERE own.artist_id = %(artist_id)s", "WHERE own.artist_id = ANY(%(artist_ids)s::text[])")
    .replace("GROUP BY ", "GROUP BY own.artist_id, ", 1)
    .replace("ORDER BY count DESC, name", "ORDER BY own.artist_id, count DESC, name")
    for dimension, sql in _PROFILE_SQL.items()
}

# A deterministic release-id order makes the 100,000-per-genre cost control
# repeatable. It is deliberately applied *before* expanding BY edges, as in
# the Cypher query, and 500 candidates per genre / 200 overall / 50 profiled
# retain the original fan-out limits.
CANDIDATE_ARTISTS_SQL = """
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

COLLECTOR_COUNTS_SQL = """
SELECT requested.release_id, count(DISTINCT owned.user_id)::bigint
FROM unnest(%(release_ids)s::text[]) AS requested(release_id)
JOIN graph.release release ON release.release_id = requested.release_id
LEFT JOIN graph.collected owned ON owned.release_id::text = requested.release_id
GROUP BY requested.release_id
"""

LABEL_AFFINITY_SQL = f"""
WITH favorite AS (
    SELECT on_label.label_id, count(*)::bigint AS label_count
    FROM graph.collected collected JOIN graph.on_label on_label
      ON on_label.release_id = collected.release_id::text
    WHERE collected.user_id = %(user_id)s::uuid
    GROUP BY on_label.label_id ORDER BY label_count DESC, on_label.label_id LIMIT 10
)
SELECT release.release_id, release.title,
       (SELECT min(artist.name) FROM graph.by_artist by_artist
        JOIN graph.artist artist USING (artist_id)
        WHERE by_artist.release_id = release.release_id),
       label.name, {_YEAR} AS year,
       ARRAY(SELECT DISTINCT genre_name FROM graph.in_genre genre
             WHERE genre.release_id = release.release_id ORDER BY genre_name),
       favorite.label_count
FROM favorite JOIN graph.on_label edge USING (label_id)
JOIN graph.release release ON release.release_id = edge.release_id
JOIN graph.label label USING (label_id)
WHERE NOT EXISTS (SELECT 1 FROM graph.collected owned WHERE owned.user_id = %(user_id)s::uuid
                  AND owned.release_id::text = release.release_id)
  AND NOT EXISTS (SELECT 1 FROM graph.wants wanted WHERE wanted.user_id = %(user_id)s::uuid
                  AND wanted.release_id::text = release.release_id)
ORDER BY favorite.label_count DESC, release.release_id, label.label_id
LIMIT %(limit)s
"""  # noqa: S608 -- interpolates only the static guarded year expression

BLINDSPOT_CANDIDATES_SQL = f"""
WITH favorite AS (
    SELECT by_artist.artist_id, count(*)::bigint AS artist_releases
    FROM graph.collected collected JOIN graph.by_artist by_artist
      ON by_artist.release_id = collected.release_id::text
    WHERE collected.user_id = %(user_id)s::uuid
    GROUP BY by_artist.artist_id ORDER BY artist_releases DESC, by_artist.artist_id LIMIT 20
), candidate_edges AS (
    SELECT genre.genre_name, by_artist.release_id, favorite.artist_id
    FROM favorite JOIN graph.by_artist by_artist USING (artist_id)
    JOIN graph.in_genre genre USING (release_id)
    WHERE NOT EXISTS (SELECT 1 FROM graph.collected owned
                      WHERE owned.user_id = %(user_id)s::uuid
                        AND owned.release_id::text = by_artist.release_id)
      AND NOT EXISTS (
          SELECT 1 FROM graph.collected owned
          JOIN graph.in_genre owned_genre ON owned_genre.release_id = owned.release_id::text
          WHERE owned.user_id = %(user_id)s::uuid AND owned_genre.genre_name = genre.genre_name
      )
), genre_overlap AS (
    SELECT genre_name, count(DISTINCT artist_id)::bigint AS artist_overlap
    FROM candidate_edges GROUP BY genre_name
), candidates AS (
    SELECT DISTINCT genre_name, release_id FROM candidate_edges
), sampled AS (
    SELECT candidates.*, genre_overlap.artist_overlap,
           row_number() OVER (PARTITION BY candidates.genre_name ORDER BY candidates.release_id) AS sample_rank
    FROM candidates
    JOIN genre_overlap USING (genre_name)
)
SELECT release.release_id, release.title,
       (SELECT min(artist.name) FROM graph.by_artist edge JOIN graph.artist artist USING (artist_id)
        WHERE edge.release_id = release.release_id),
       (SELECT min(label.name) FROM graph.on_label edge JOIN graph.label label USING (label_id)
        WHERE edge.release_id = release.release_id),
       {_YEAR} AS year, sampled.genre_name, sampled.artist_overlap
FROM sampled JOIN graph.release release USING (release_id)
WHERE sampled.sample_rank <= 5
ORDER BY sampled.artist_overlap DESC, release.release_id, sampled.genre_name
LIMIT %(limit)s
"""  # noqa: S608 -- interpolates only the static guarded year expression


async def _rows(pool: Any, sql: str, params: dict[str, Any]) -> list[tuple[Any, ...]]:
    async with pool.connection() as conn, conn.cursor() as cursor_cm:
        cursor = cast("Any", cursor_cm)
        await execute_sql(cursor, sql, params)
        return cast("list[tuple[Any, ...]]", await cursor.fetchall())


async def get_artist_identity(pool: Any, artist_id: str) -> dict[str, Any] | None:
    rows = await _rows(pool, ARTIST_IDENTITY_SQL, {"artist_id": artist_id})
    if not rows:
        return None
    aid, name, count = rows[0]
    return {"artist_id": aid, "artist_name": name, "release_count": count}


async def get_artist_profile(pool: Any, artist_id: str) -> dict[str, Any]:
    profile: dict[str, Any] = {}
    for dimension, sql in _PROFILE_SQL.items():
        profile[dimension] = [{"name": name, "count": count} for name, count in await _rows(pool, sql, {"artist_id": artist_id})]
    return profile


async def _batch_artist_profiles(pool: Any, candidate_ids: list[str]) -> dict[str, dict[str, Any]]:
    # Four batch queries regardless of candidate count, matching the Neo4j cost
    # shape and avoiding a 50 x 4 N+1 fan-out on the ranked candidate page.
    profiles: dict[str, dict[str, Any]] = {artist_id: {"genres": [], "styles": [], "labels": [], "collaborators": []} for artist_id in candidate_ids}
    for dimension, sql in _BATCH_PROFILE_SQL.items():
        for artist_id, name, count in await _rows(pool, sql, {"artist_ids": candidate_ids}):
            profiles[artist_id][dimension].append({"name": name, "count": count})
    return profiles


async def get_candidate_artists(pool: Any, artist_id: str) -> list[dict[str, Any]]:
    rows = await _rows(pool, CANDIDATE_ARTISTS_SQL, {"artist_id": artist_id, "min_releases": MIN_ARTIST_RELEASES})
    if not rows:
        return []
    profiles = await _batch_artist_profiles(pool, [row[0] for row in rows])
    return [{"artist_id": aid, "artist_name": name, "release_count": count, **profiles[aid]} for aid, name, count in rows]


async def get_collector_counts(pool: Any, release_ids: list[str]) -> dict[str, int]:
    if not release_ids:
        return {}
    return dict(await _rows(pool, COLLECTOR_COUNTS_SQL, {"release_ids": release_ids}))


async def get_label_affinity_candidates(pool: Any, user_id: str, limit: int = 50) -> list[dict[str, Any]]:
    rows = await _rows(pool, LABEL_AFFINITY_SQL, {"user_id": user_id, "limit": limit})
    return [
        {
            "id": rid,
            "title": title,
            "artist": artist,
            "label": label,
            "year": year,
            "genres": genres,
            "score": score,
            "source": f"label: {label} (top label)",
        }
        for rid, title, artist, label, year, genres, score in rows
    ]


async def get_blindspot_candidates(pool: Any, user_id: str, limit: int = 50) -> list[dict[str, Any]]:
    rows = await _rows(pool, BLINDSPOT_CANDIDATES_SQL, {"user_id": user_id, "limit": limit})
    return [
        {
            "id": rid,
            "title": title,
            "artist": artist,
            "label": label,
            "year": year,
            "genres": [genre],
            "score": score,
            "source": f"blind_spot: {genre}",
        }
        for rid, title, artist, label, year, genre, score in rows
    ]
