"""Precomputed similar-artist lists: the PostgreSQL reads and the score fusion.

The monthly exact top-K batch (analytics-engine) publishes one row per artist into
``artist_similar_artists`` under a release recorded in ``artist_embedding_releases``; exactly
one release is ``is_current``. Serving is a read of that release pointer and one primary-key
lookup -- no ANN index and no ``ef_search`` (ADR 0013, gm-analytics-engine-8ts).

Every read here answers "nothing usable" with ``None`` or an empty mapping instead of raising,
so the caller's one fallback (the heuristic path) covers all of: no current release, the
artist absent from the release, and an instance whose tables do not exist.

Fusion follows the gm-design-chw.2 / 8ts harness: ``alpha * embedding_cosine + (1 - alpha) *
heuristic``, so ``alpha`` weights the *embedding* and 1.0 is the pure precomputed ranking.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Final

import structlog
from common.query_debug import execute_sql
from psycopg import Error as PsycopgError


logger = structlog.get_logger(__name__)

# One round trip distinguishes "no current release" (no row) from "artist absent from the
# release" (a row with NULL arrays). The list side is the (release_id, artist_id) primary key.
_CURRENT_LIST_SQL: Final = """
SELECT r.release_id, r.model_version, s.similar_artist_ids, s.scores
FROM public.artist_embedding_releases r
LEFT JOIN public.artist_similar_artists s
       ON s.release_id = r.release_id AND s.artist_id = %s
WHERE r.is_current
"""

# Point lookups on the (artist_id, model_version) primary key. The vector is read as its text
# form (`[0.1,0.2,...]`), which every pgvector version emits, and the cosine is taken here so
# the fusion is the same arithmetic the tests and the offline harness exercise.
_EMBEDDINGS_SQL: Final = """
SELECT artist_id, embedding::text
FROM public.artist_embeddings
WHERE model_version = %s AND artist_id = ANY(%s)
"""


@dataclass(frozen=True)
class PrecomputedList:
    """One artist's published neighbours, rank order preserved."""

    release_id: int
    model_version: str
    neighbours: tuple[tuple[str, float], ...]


async def get_current_list(pg_pool: Any, artist_id: str) -> PrecomputedList | None:
    """Return the artist's list from the current release, or ``None`` when there is none.

    ``None`` covers a missing pool, no current release, the artist absent from the release,
    and a database without the tables (or otherwise unreadable): in every case the caller
    serves the heuristic result.
    """
    if pg_pool is None:
        return None
    try:
        async with pg_pool.connection() as conn, conn.cursor() as cur:
            await execute_sql(cur, _CURRENT_LIST_SQL, (artist_id,))
            row = await cur.fetchone()
    except PsycopgError:
        logger.warning("⚠️ Precomputed similar-artist lookup failed; using the heuristic path", exc_info=True)
        return None
    if row is None:
        return None
    release_id, model_version, ids, scores = row
    if ids is None or scores is None:
        return None
    return PrecomputedList(
        release_id=int(release_id),
        model_version=str(model_version),
        neighbours=tuple((str(i), float(s)) for i, s in zip(ids, scores, strict=True)),
    )


def parse_vector(text: str) -> list[float]:
    """Parse pgvector's text form (``[1,2,3]``) into floats."""
    body = text.strip().removeprefix("[").removesuffix("]")
    return [float(part) for part in body.split(",")] if body else []


def cosine(first: list[float], second: list[float]) -> float:
    """Cosine similarity; 0.0 for empty, zero-norm, or mismatched-length vectors."""
    if not first or len(first) != len(second):
        return 0.0
    norm = math.sqrt(sum(x * x for x in first)) * math.sqrt(sum(y * y for y in second))
    if norm == 0.0:
        return 0.0
    return sum(x * y for x, y in zip(first, second, strict=True)) / norm


async def get_embedding_cosines(pg_pool: Any, model_version: str, artist_id: str, candidate_ids: list[str]) -> dict[str, float] | None:
    """Cosine of ``artist_id``'s stored vector to each candidate's, for one model version.

    Candidates without a stored vector are absent from the result (the caller scores them 0.0,
    as the harness does for artists with no shipped vector). ``None`` means the vectors could
    not be read at all -- ``artist_embeddings`` needs pgvector and may not exist -- and the
    caller must not fuse on a half-read.
    """
    if pg_pool is None:
        return None
    if not candidate_ids:
        return {}
    try:
        async with pg_pool.connection() as conn, conn.cursor() as cur:
            await execute_sql(cur, _EMBEDDINGS_SQL, (model_version, [artist_id, *candidate_ids]))
            rows = await cur.fetchall()
    except PsycopgError:
        logger.warning("⚠️ Artist embedding lookup failed; using the heuristic path", exc_info=True)
        return None
    vectors = {str(row[0]): parse_vector(row[1]) for row in rows}
    target = vectors.get(artist_id)
    if target is None:
        return {}
    return {candidate: cosine(target, vectors[candidate]) for candidate in candidate_ids if candidate in vectors}


def fuse(
    neighbours: dict[str, float],
    heuristic: dict[str, dict[str, Any]],
    cosines: dict[str, float],
    *,
    alpha: float,
) -> list[tuple[str, float, float, float]]:
    """Fuse the precomputed list with the heuristic candidates and rank the union.

    Args:
        neighbours: Precomputed ``artist_id -> cosine`` (the batch's stored score).
        heuristic: ``artist_id -> heuristic row`` (a ``similarity`` key) for the candidates
            the heuristic scored; absent means a heuristic score of 0.0.
        cosines: Stored-vector cosines for candidates not on the precomputed list.
        alpha: Weight on the embedding cosine; ``1.0`` ranks by the cosine alone.

    Returns:
        ``(artist_id, fused, cosine, heuristic)`` best first, ties broken by artist id.
    """
    ranked = []
    for artist_id in neighbours.keys() | heuristic.keys():
        cos = neighbours[artist_id] if artist_id in neighbours else cosines.get(artist_id, 0.0)
        heur = float(heuristic[artist_id]["similarity"]) if artist_id in heuristic else 0.0
        ranked.append((artist_id, alpha * cos + (1.0 - alpha) * heur, cos, heur))
    ranked.sort(key=lambda entry: (-entry[1], entry[0]))
    return ranked
