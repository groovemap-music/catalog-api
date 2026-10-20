"""The precomputed-list similar-artist path, replayed offline on the golden set.

``GET /api/recommend/similar/artist/{id}`` can serve each artist's monthly exact top-K list
from ``artist_similar_artists`` (see :mod:`api.queries.similar_precomputed_queries`), pure or
fused with the heuristic. This module replays that path as a candidate method so it is scored
on the identical split and metric as the heuristic baselines.

Everything here is synthetic. The golden set carries no embeddings, so a deterministic sparse
random projection of each artist's observed genre/style/label/collaborator profile stands in
for the FastRP vectors, and the "published list" is the exact top-K cosine over those vectors,
which is what the real batch computes over the real ones. Because the stand-in vectors are
built from the very signals the heuristic scores, the numbers prove the wiring and give the
comparison a fixed shape; they say nothing about how real embeddings rank. That measurement
is gm-analytics-engine-8ts.

Only the observed (pre-cut) graph feeds the vectors, so nothing held out leaks into the lists.
"""

from __future__ import annotations

import math
import random
from dataclasses import replace
from typing import Any, Final

from api.evaluation.baseline import SIMILAR_ARTIST_LIMIT, BaselineRun
from api.evaluation.graph import GoldenGraph
from api.queries.recommend_queries import compute_similar_artists
from api.queries.similar_precomputed_queries import cosine, fuse


PRECOMPUTED_VERSION: Final[str] = "similar-artist-precomputed-2026-10"
PRECOMPUTED_FUSED_VERSION: Final[str] = "similar-artist-precomputed-fused-2026-10"

#: Synthetic embedding width and seed. Frozen: the committed snapshot is a function of both.
EMBEDDING_DIMS: Final[int] = 64
EMBEDDING_SEED: Final[str] = "similar-artist-precomputed-2026-10"

#: Published list length. Larger than the fixture's artist count, so the list is the full ranking.
LIST_K: Final[int] = 50

_DIMENSIONS: Final = ("genre", "style", "label", "collaborator")


def synthetic_embeddings(graph: GoldenGraph, *, dims: int = EMBEDDING_DIMS, seed: str = EMBEDDING_SEED) -> dict[str, list[float]]:
    """One vector per artist: a seeded Gaussian projection of its sqrt-count profile tokens."""
    token_vectors: dict[str, list[float]] = {}

    def _vector(token: str) -> list[float]:
        if token not in token_vectors:
            rng = random.Random(f"{seed}:{token}")  # noqa: S311 -- deterministic fixture data, not security
            token_vectors[token] = [rng.gauss(0.0, 1.0) for _ in range(dims)]
        return token_vectors[token]

    embeddings: dict[str, list[float]] = {}
    for artist_id in sorted(graph.golden.artists):
        profile = graph.artist_profile(artist_id)
        total = [0.0] * dims
        for dimension in _DIMENSIONS:
            for item in profile.get(f"{dimension}s", []):
                weight = math.sqrt(item["count"])
                for index, value in enumerate(_vector(f"{dimension}:{item['name']}")):
                    total[index] += weight * value
        embeddings[artist_id] = total
    return embeddings


def synthetic_lists(embeddings: dict[str, list[float]], *, k: int = LIST_K) -> dict[str, tuple[tuple[str, float], ...]]:
    """Exact top-K cosine neighbours per artist, ties by artist id -- the monthly batch's contract."""
    lists: dict[str, tuple[tuple[str, float], ...]] = {}
    for artist_id, vector in embeddings.items():
        scored = [(other, cosine(vector, embeddings[other])) for other in embeddings if other != artist_id]
        scored.sort(key=lambda item: (-item[1], item[0]))
        lists[artist_id] = tuple(scored[:k])
    return lists


def _serve(
    graph: GoldenGraph,
    artist_id: str,
    lists: dict[str, tuple[tuple[str, float], ...]],
    embeddings: dict[str, list[float]],
    alpha: float,
) -> tuple[dict[str, Any], ...]:
    """What the endpoint would return for one artist from the published lists."""
    neighbours = dict(lists.get(artist_id, ()))
    if not neighbours:
        return ()
    names = {other: graph.golden.artists[other].name for other in neighbours}
    if alpha >= 1.0:
        return tuple({"artist_id": other, "artist_name": names[other], "similarity": round(score, 4)} for other, score in neighbours.items())[
            :SIMILAR_ARTIST_LIMIT
        ]

    target = graph.artist_profile(artist_id)
    heuristic = {row["artist_id"]: row for row in compute_similar_artists(target, graph.candidate_artists_all_signals(artist_id), limit=50)}
    extra = [
        {"artist_id": other, "artist_name": names[other], "release_count": 0, **graph.artist_profile(other)}
        for other in neighbours
        if other not in heuristic
    ]
    for row in compute_similar_artists(target, extra, limit=len(extra)):
        heuristic[row["artist_id"]] = row
    cosines = {other: cosine(embeddings[artist_id], embeddings[other]) for other in heuristic if other not in neighbours}
    fused = fuse(neighbours, heuristic, cosines, alpha=alpha)
    return tuple(
        {"artist_id": other, "artist_name": graph.golden.artists[other].name, "similarity": round(score, 4)}
        for other, score, _cos, _heur in fused[:SIMILAR_ARTIST_LIMIT]
    )


def run_precomputed(graph: GoldenGraph, baseline: BaselineRun, *, alpha: float = 1.0) -> BaselineRun:
    """Replace ``baseline``'s similar-artist rankings with the precomputed-list path's.

    Seeded exactly as ``api.evaluation.baseline._similar_artists`` seeds it (each collector's
    most-collected pre-cut artist), so the metric compares like with like. ``alpha=1.0`` is the
    pure list; anything lower fuses with the all-signals heuristic.

    Args:
        graph: The observed (pre-cut) graph.
        baseline: A baseline run over the same graph; its other outputs are carried through.
        alpha: Weight on the embedding cosine; defaults to pure. The endpoint default is
            :data:`api.config.DEFAULT_FUSION_ALPHA`.
    """
    embeddings = synthetic_embeddings(graph)
    lists = synthetic_lists(embeddings)
    similar: dict[str, tuple[dict[str, Any], ...]] = {}
    for collector_id in sorted(graph.holdings):
        top = graph._top_collected_artists(collector_id, 1)
        similar[collector_id] = _serve(graph, top[0][0], lists, embeddings, alpha) if top else ()
    version = PRECOMPUTED_VERSION if alpha >= 1.0 else PRECOMPUTED_FUSED_VERSION
    return replace(baseline, version=version, similar_artists=similar)


__all__ = ["PRECOMPUTED_FUSED_VERSION", "PRECOMPUTED_VERSION", "run_precomputed", "synthetic_embeddings", "synthetic_lists"]
