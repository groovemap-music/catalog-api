"""The frozen heuristic baseline: versioned weight tables and a reproducible run.

Today's recommendation and rarity weights are module-level dict literals. They are easy to
change and nothing records what they were, so "the new model beats the heuristics" has no
fixed thing to beat. This module freezes them.

The frozen copies below are the baseline. A test asserts the live constants still equal them,
and fails with instructions rather than a diff: changing a weight is allowed, changing it
silently is not, because every number in ``tests/fixtures/golden/expected-metrics.json`` moves
with it. Bump :data:`BASELINE_VERSION`, refresh the snapshot, and update the frozen copy in
one commit.

Determinism is the other half. :func:`run_baseline` takes the current year from
:data:`BASELINE_CURRENT_YEAR` rather than the clock, because ``temporal_scarcity`` is computed
against it and a baseline that drifts by 1.5 points every January is not a baseline. Bumping
the harness to a later reference year is a deliberate act with the same version-and-snapshot
consequences as changing a weight.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from api.evaluation.fixtures import GoldenSet
from api.evaluation.graph import GoldenGraph
from api.queries import recommend_queries
from api.queries.rarity_queries import _percentile_rank
from api.queries.recommend_queries import (
    compute_similar_artists,
    merge_recommendation_candidates,
    score_discoveries,
)
from api.rarity import (
    ReleaseContext,
    compute_collection_prevalence_score,
    compute_format_rarity_score,
    compute_graph_isolation_score,
    compute_label_catalog_score,
    compute_medium_rarity_score,
    compute_temporal_scarcity_score,
    resolve_media,
    score_release,
)
from api.rarity import core as rarity_core
from api.rarity.families import module_weights
from api.rarity.families.grooved import PRESSING_FACT


BASELINE_VERSION: Final[str] = "heuristics-2026-09"

# gm-catalog-api-tsmu.1: same heuristics-2026-09 weights, but the similar-artist endpoint's
# candidate scope is every artist sharing >=1 genre/style/label/collaborator signal (see
# GoldenGraph.candidate_artists_all_signals), not the frozen top-500-per-genre generator.
# Nothing else about the run changes -- recommendations, discoveries, and rarity are
# identical to heuristics-2026-09 -- so this version is registered alongside it rather than
# replacing it, and heuristics-2026-09's own committed expected-metrics.json stays untouched.
SIMILAR_ARTIST_CANDIDATES_VERSION: Final[str] = "similar-artist-all-signals-2026-09"

# The reference year temporal_scarcity is measured against. Frozen, not `datetime.now().year`.
BASELINE_CURRENT_YEAR: Final[int] = 2026

# Run shape, matching what the multi-signal endpoint asks the queries for.
RECOMMENDATION_LIMIT: Final[int] = 25
CANDIDATE_LIMIT: Final[int] = 50
SIMILAR_ARTIST_LIMIT: Final[int] = 20
DISCOVERY_LIMIT: Final[int] = 10
EXPLORE_HOPS: Final[int] = 2


# ── Frozen weight tables ────────────────────────────────────────────

#: Frozen copy of ``api.queries.recommend_queries._WEIGHTS``.
BASELINE_SIMILARITY_WEIGHTS: Final[dict[str, float]] = {
    "genre": 0.35,
    "style": 0.25,
    "label": 0.25,
    "collaborator": 0.15,
}

#: Frozen copy of ``api.queries.recommend_queries._SIGNAL_WEIGHTS``.
BASELINE_SIGNAL_WEIGHTS: Final[dict[str, float]] = {
    "artist": 0.35,
    "label": 0.25,
    "blindspot": 0.25,
    "obscurity": 0.15,
}

#: Frozen copy of ``api.rarity.core.CORE_SIGNAL_WEIGHTS``. Sums to 0.75; the grooved module
#: contributes the remaining 0.25 when it applies, and ``compose`` renormalises when it does not.
BASELINE_CORE_SIGNAL_WEIGHTS: Final[dict[str, float]] = {
    "label_catalog": 0.10,
    "medium_rarity": 0.10,
    "temporal_scarcity": 0.20,
    "graph_isolation": 0.15,
    "collection_prevalence": 0.20,
}

#: Frozen copy of ``api.rarity.families.grooved.GroovedSignals.weights``.
BASELINE_FAMILY_WEIGHTS: Final[dict[str, dict[str, float]]] = {"grooved": {"pressing_scarcity": 0.25}}

#: Frozen copy of ``api.rarity.core.MEDIUM_RARITY_SCORES``.
BASELINE_MEDIUM_RARITY_SCORES: Final[dict[str, float]] = {
    "vinyl_7": 45.0,
    "vinyl_10": 65.0,
    "vinyl_12": 40.0,
    "vinyl_unspecified": 40.0,
    "shellac_7": 90.0,
    "shellac_10": 90.0,
    "shellac_12": 90.0,
    "shellac_unspecified": 90.0,
    "grooved_acetate": 96.0,
    "grooved_lathe_cut": 98.0,
    "grooved_flexi_disc": 95.0,
    "grooved_cylinder": 97.0,
    "grooved_edison_disc": 96.0,
    "grooved_pathe_disc": 96.0,
    "grooved_piano_roll": 92.0,
    "grooved_other_unspecified": 90.0,
    "tape_cassette": 35.0,
    "tape_microcassette": 75.0,
    "tape_reel_to_reel": 70.0,
    "tape_8_track": 60.0,
    "tape_4_track": 80.0,
    "tape_playtape": 85.0,
    "tape_dat": 70.0,
    "tape_dcc": 80.0,
    "tape_elcaset": 90.0,
    "tape_unspecified": 50.0,
    "optical_cd": 10.0,
    "optical_cdr": 50.0,
    "optical_sacd": 45.0,
    "optical_dvd_audio": 50.0,
    "optical_minidisc": 65.0,
    "optical_dualdisc": 60.0,
    "optical_umd": 75.0,
    "optical_unspecified": 20.0,
    "digital_file": 5.0,
    "digital_usb": 45.0,
    "digital_memory_card": 55.0,
    "digital_download_card": 20.0,
    "digital_floppy_disk": 75.0,
    "digital_unspecified": 5.0,
    "video_dvd": 20.0,
    "video_dvdr": 45.0,
    "video_blu_ray": 25.0,
    "video_hd_dvd": 70.0,
    "video_laserdisc": 55.0,
    "video_cdv": 70.0,
    "video_vcd": 55.0,
    "video_svcd": 65.0,
    "video_vhs": 40.0,
    "video_betamax": 75.0,
    "video_vhd": 85.0,
    "video_ced": 80.0,
    "video_film_reel": 85.0,
    "video_unspecified": 45.0,
    "other_unspecified": 50.0,
}

WEIGHT_DRIFT_MESSAGE: Final[str] = (
    "{name} no longer matches its frozen copy in api/evaluation/baseline.py. "
    "Every number in tests/fixtures/golden/expected-metrics.json is computed from these "
    "weights, so changing one is a baseline change: bump BASELINE_VERSION, update the frozen "
    "copy, regenerate the snapshot with `just evaluate`, and commit all three together."
)


def frozen_weight_tables() -> dict[str, Mapping[str, Any]]:
    """Return every frozen weight table, keyed by the live constant it copies."""
    return {
        "api.queries.recommend_queries._WEIGHTS": BASELINE_SIMILARITY_WEIGHTS,
        "api.queries.recommend_queries._SIGNAL_WEIGHTS": BASELINE_SIGNAL_WEIGHTS,
        "api.rarity.core.CORE_SIGNAL_WEIGHTS": BASELINE_CORE_SIGNAL_WEIGHTS,
        "api.rarity.core.MEDIUM_RARITY_SCORES": BASELINE_MEDIUM_RARITY_SCORES,
        "api.rarity.families.module_weights()": BASELINE_FAMILY_WEIGHTS,
    }


def live_weight_tables() -> dict[str, Mapping[str, Any]]:
    """Return the live weight tables the frozen copies are checked against."""
    return {
        "api.queries.recommend_queries._WEIGHTS": recommend_queries._WEIGHTS,
        "api.queries.recommend_queries._SIGNAL_WEIGHTS": recommend_queries._SIGNAL_WEIGHTS,
        "api.rarity.core.CORE_SIGNAL_WEIGHTS": rarity_core.CORE_SIGNAL_WEIGHTS,
        "api.rarity.core.MEDIUM_RARITY_SCORES": rarity_core.MEDIUM_RARITY_SCORES,
        "api.rarity.families.module_weights()": module_weights(),
    }


# ── Run results ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class RarityResult:
    """One release's baseline rarity score, in the shape the precomputed row carries."""

    release_id: str
    families: tuple[str, ...]
    score: float
    tier: str
    hidden_gem_score: float
    signals: Mapping[str, float]
    family_signals: Mapping[str, Mapping[str, float]]
    format_rarity: float


@dataclass(frozen=True)
class BaselineRun:
    """Everything one baseline pass produced, keyed for the metrics to consume."""

    version: str
    current_year: int
    recommendations: Mapping[str, tuple[dict[str, Any], ...]]
    discoveries: Mapping[str, tuple[dict[str, Any], ...]]
    rarity: Mapping[str, RarityResult]
    similar_artists: Mapping[str, tuple[dict[str, Any], ...]] = field(default_factory=dict)

    def ranked_ids(self, collector_id: str) -> tuple[str, ...]:
        """The ranked release ids recommended to one collector, best first."""
        return tuple(row["id"] for row in self.recommendations.get(collector_id, ()))


# ── The run ─────────────────────────────────────────────────────────


def _recommend(graph: GoldenGraph, collector_id: str, limit: int) -> tuple[dict[str, Any], ...]:
    """Replay the multi-signal recommendation pipeline for one collector.

    Mirrors ``GET /api/user/recommendations?strategy=multi``: three candidate signals, merged
    by release id under the frozen signal weights, with an obscurity bonus from the collector
    counts of every candidate.
    """
    artist = graph.artist_affinity_candidates(collector_id, limit=CANDIDATE_LIMIT)
    label = graph.label_affinity_candidates(collector_id, limit=CANDIDATE_LIMIT)
    blindspot = graph.blindspot_candidates(collector_id, limit=CANDIDATE_LIMIT)
    all_ids = sorted({row["id"] for rows in (artist, label, blindspot) for row in rows})
    counts = graph.collector_counts(all_ids) if all_ids else {}
    return tuple(merge_recommendation_candidates(artist, label, blindspot, collector_counts=counts, limit=limit))


def _discover(graph: GoldenGraph, collector_id: str) -> tuple[dict[str, Any], ...]:
    """Replay Explore From Here, seeded from the collector's most-collected artist."""
    top = graph._top_collected_artists(collector_id, 1)
    if not top:
        return ()
    traversal = graph.explore_traversal("artist", top[0][0], hops=EXPLORE_HOPS)
    return tuple(score_discoveries(traversal, graph.taste_genre_vector(collector_id), graph.blind_spot_genres(collector_id), limit=DISCOVERY_LIMIT))


def _similar_artists(graph: GoldenGraph, collector_id: str, *, all_signal_candidates: bool = False) -> tuple[dict[str, Any], ...]:
    """Rank artists similar to the collector's most-collected artist.

    Args:
        all_signal_candidates: Use the gm-catalog-api-tsmu.1 candidate scope
            (:meth:`~api.evaluation.graph.GoldenGraph.candidate_artists_all_signals`) instead
            of the frozen ``heuristics-2026-09`` one. Only the candidate set differs; the
            weights ``compute_similar_artists`` scores with are identical either way.
    """
    top = graph._top_collected_artists(collector_id, 1)
    if not top:
        return ()
    artist_id = top[0][0]
    candidates = graph.candidate_artists_all_signals(artist_id) if all_signal_candidates else graph.candidate_artists(artist_id)
    return tuple(compute_similar_artists(graph.artist_profile(artist_id), candidates, limit=SIMILAR_ARTIST_LIMIT))


def _score_rarity(graph: GoldenGraph, current_year: int) -> dict[str, RarityResult]:
    """Replay ``fetch_all_rarity_signals`` over the fixture, percentile pass included."""
    release_ids = list(graph.golden.release_ids)
    rows = graph.core_signal_rows(release_ids)
    indexed = {fact: {row["release_id"]: row for row in fact_rows} for fact, fact_rows in rows.items()}
    pressing = {row["release_id"]: row for row in graph.grooved_pressing_rows(release_ids)}
    community = graph.community_counts(release_ids)

    scored: dict[str, RarityResult] = {}
    quality: dict[str, tuple[float, float, float, float]] = {}
    artist_degrees: list[float] = []
    label_sizes: list[float] = []
    genre_counts: list[float] = []

    for release_id in release_ids:
        media_row = indexed["media"][release_id]
        formats = media_row["formats"]
        media = resolve_media(mediums=media_row["mediums"], media_families=media_row["media_families"], formats=formats)
        temporal = indexed["temporal"][release_id]
        have, want = community[release_id]

        core_signals = {
            "label_catalog": compute_label_catalog_score(indexed["label"][release_id]["label_catalog_size"]),
            "medium_rarity": compute_medium_rarity_score(media),
            "temporal_scarcity": compute_temporal_scarcity_score(temporal["year"], temporal["latest_sibling_year"], current_year),
            "graph_isolation": compute_graph_isolation_score(indexed["degree"][release_id]["degree"]),
            "collection_prevalence": compute_collection_prevalence_score(have, want),
        }
        result = score_release(
            ReleaseContext(
                release_id=release_id,
                media=media,
                year=indexed["release"][release_id]["year"],
                facts={PRESSING_FACT: pressing[release_id]},
            ),
            core_signals,
        )

        artist_degree = float(indexed["artist_degree"][release_id]["artist_max_degree"])
        label_size = float(indexed["label_size"][release_id]["label_max_catalog"])
        genre_count = float(indexed["genre_count"][release_id]["genre_max_release_count"])
        artist_degrees.append(artist_degree)
        label_sizes.append(label_size)
        genre_counts.append(genre_count)
        quality[release_id] = (result.score, artist_degree, label_size, genre_count)

        scored[release_id] = RarityResult(
            release_id=release_id,
            families=tuple(media.families),
            score=result.score,
            tier=result.tier,
            hidden_gem_score=0.0,
            signals=dict(result.signals),
            family_signals={module: dict(signals) for module, signals in result.family_signals.items()},
            format_rarity=compute_format_rarity_score(formats),
        )

    # Hidden-gem scoring needs percentile ranks over the whole distribution, so it is a second
    # pass over the already-scored releases, exactly as the production pass does it.
    artist_degrees.sort()
    label_sizes.sort()
    genre_counts.sort()
    for release_id, (score, artist_degree, label_size, genre_count) in quality.items():
        multiplier = (
            0.4 * _percentile_rank(artist_degree, artist_degrees)
            + 0.3 * _percentile_rank(label_size, label_sizes)
            + 0.3 * _percentile_rank(genre_count, genre_counts)
        )
        current = scored[release_id]
        scored[release_id] = RarityResult(
            release_id=current.release_id,
            families=current.families,
            score=current.score,
            tier=current.tier,
            hidden_gem_score=round(score * multiplier, 1),
            signals=current.signals,
            family_signals=current.family_signals,
            format_rarity=current.format_rarity,
        )
    return scored


def run_baseline(
    source: GoldenGraph | GoldenSet,
    *,
    limit: int = RECOMMENDATION_LIMIT,
    current_year: int = BASELINE_CURRENT_YEAR,
    similar_artist_candidates: str = "legacy",
) -> BaselineRun:
    """Score the whole fixture with today's frozen heuristics.

    Args:
        source: The graph to score. A :class:`~api.evaluation.fixtures.GoldenSet` is accepted
            and scored on its full collections; the time split passes a graph holding only the
            pre-cut acquisitions.
        limit: How many recommendations to rank per collector.
        current_year: The reference year ``temporal_scarcity`` is measured against.
        similar_artist_candidates: ``"legacy"`` (default) replays the frozen
            ``heuristics-2026-09`` candidate scope and reports that version, unchanged from
            before gm-catalog-api-tsmu.1. ``"all_signals"`` replays the new candidate scope
            (:meth:`~api.evaluation.graph.GoldenGraph.candidate_artists_all_signals`) for
            ``similar_artists`` only and reports :data:`SIMILAR_ARTIST_CANDIDATES_VERSION`;
            ``recommendations``, ``discoveries``, and ``rarity`` are unaffected either way.

    Returns:
        The run: per-collector ranked recommendations, discoveries, and similar artists, plus
        per-release rarity. Two runs over the same graph are equal.

    Raises:
        ValueError: ``similar_artist_candidates`` is neither ``"legacy"`` nor ``"all_signals"``.
    """
    if similar_artist_candidates not in ("legacy", "all_signals"):
        raise ValueError(f"similar_artist_candidates must be 'legacy' or 'all_signals', got {similar_artist_candidates!r}")
    all_signals = similar_artist_candidates == "all_signals"
    version = SIMILAR_ARTIST_CANDIDATES_VERSION if all_signals else BASELINE_VERSION
    graph = source if isinstance(source, GoldenGraph) else GoldenGraph(source)
    collector_ids = sorted(graph.holdings)
    return BaselineRun(
        version=version,
        current_year=current_year,
        recommendations={collector_id: _recommend(graph, collector_id, limit) for collector_id in collector_ids},
        discoveries={collector_id: _discover(graph, collector_id) for collector_id in collector_ids},
        similar_artists={collector_id: _similar_artists(graph, collector_id, all_signal_candidates=all_signals) for collector_id in collector_ids},
        rarity=_score_rarity(graph, current_year),
    )
