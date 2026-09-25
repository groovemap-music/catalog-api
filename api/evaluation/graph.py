"""An in-memory graph that answers the scoring functions' query shapes from the golden set.

The scoring functions in :mod:`api.queries.recommend_queries` and :mod:`api.rarity` are pure,
but their inputs are Cypher result rows. Reproducing a baseline offline therefore means
reproducing those rows, not just calling the functions: a shape that is nearly right produces
a number that is confidently wrong.

So every method here mirrors one query function, returns the same keys with the same value
types, and applies the same ordering, limits, and exclusions the Cypher applies. Where Cypher
leaves an order unspecified -- ``collect(DISTINCT ...)``, ties inside an ``ORDER BY count`` --
this module picks a deterministic one, because a baseline that shuffles is not a baseline.
The mirrored shapes are pinned against the captured examples in ``tests/test_recommend_queries.py``
and ``tests/test_rarity_queries.py`` by ``tests/test_evaluation_graph.py``.

Graph model, matching the production one:

    (Release)-[:BY]->(Artist)
    (Release)-[:ON]->(Label)
    (Release)-[:IS]->(Genre)
    (Release)-[:IS]->(Style)
    (Release)-[:DERIVED_FROM]->(Master)
    (Release)-[:ISSUED_ON]->(Medium {id, family})
    (User)-[:COLLECTED]->(Release)
    (User)-[:WANTS]->(Release)

Which ``COLLECTED`` edges exist is a constructor argument rather than a property of the
fixture. That is the whole seam the time split needs: :mod:`api.evaluation.split` builds a
graph holding only the pre-cut acquisitions, so nothing a collector bought after the cut can
reach the baseline -- not through a candidate, not through an obscurity count, not through a
node degree.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from common.media import families_of

from api.evaluation.fixtures import GoldenSet, Release
from api.queries.recommend_queries import MIN_ARTIST_RELEASES


# Mirrors get_candidate_artists: the target's top genres are expanded, each expansion is
# capped, and only the top slice is profiled.
TOP_GENRES: Final[int] = 5
PER_GENRE_CANDIDATE_LIMIT: Final[int] = 500
CANDIDATE_LIMIT: Final[int] = 200
PROFILED_CANDIDATES: Final[int] = 50

# Mirrors get_user_recommendations, get_label_affinity_candidates, get_blindspot_candidates,
# and get_blind_spots.
TOP_COLLECTED_ARTISTS: Final[int] = 10
TOP_LABELS: Final[int] = 10
TOP_ARTISTS: Final[int] = 20
BLINDSPOT_SAMPLE: Final[int] = 5

# Relationship types the explore traversal walks, from api.queries.neo4j_queries.
# ALIAS_OF and MEMBER_OF are in the production pattern but have no edges in the golden set.
PATH_REL_TYPES: Final[frozenset[str]] = frozenset({"BY", "ON", "IS", "ALIAS_OF", "MEMBER_OF", "DERIVED_FROM"})

# get_explore_traversal's own allowlist and limits.
EXPLORE_ENTITY_TYPES: Final[frozenset[str]] = frozenset({"genre", "style", "artist", "label", "release", "master"})
EXPLORE_DISCOVERABLE: Final[frozenset[str]] = frozenset({"artist", "label", "genre", "style"})
EXPLORE_LIMIT: Final[int] = 100
DEFAULT_HOPS: Final[int] = 2


@dataclass(frozen=True)
class CollectorHoldings:
    """What one collector's ``COLLECTED`` and ``WANTS`` edges point at, in this graph."""

    collector_id: str
    owned: tuple[str, ...]
    wants: frozenset[str]


def _counted(names: Iterable[str]) -> list[dict[str, Any]]:
    """Return ``[{"name", "count"}]`` ordered by count descending, ties broken by name.

    This is the shape ``to_genre_vector`` consumes and the shape every profile query returns.
    Cypher's ``ORDER BY count DESC`` leaves ties unordered; the name tiebreak makes the
    baseline reproducible without changing any score, since the vector is a dict.
    """
    counts: dict[str, int] = {}
    for name in names:
        counts[name] = counts.get(name, 0) + 1
    return [{"name": name, "count": count} for name, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))]


class GoldenGraph:
    """The golden set, indexed the way the Cypher queries traverse it.

    Args:
        golden: The parsed fixture.
        holdings: The visible collector holdings, keyed by collector id. Defaults to every
            acquisition in the fixture; :mod:`api.evaluation.split` passes the pre-cut subset.
    """

    def __init__(self, golden: GoldenSet, holdings: Mapping[str, CollectorHoldings] | None = None) -> None:
        self.golden = golden
        self.holdings: Mapping[str, CollectorHoldings] = (
            dict(holdings)
            if holdings is not None
            else {
                collector.id: CollectorHoldings(collector_id=collector.id, owned=collector.release_ids, wants=collector.wants)
                for collector in golden.collectors.values()
            }
        )

        releases = golden.releases
        self._by_artist: dict[str, list[str]] = {}
        self._by_label: dict[str, list[str]] = {}
        self._by_genre: dict[str, list[str]] = {}
        self._by_style: dict[str, list[str]] = {}
        self._by_master: dict[str, list[str]] = {}
        for release_id in sorted(releases):
            release = releases[release_id]
            for artist_id in release.artist_ids:
                self._by_artist.setdefault(artist_id, []).append(release_id)
            self._by_label.setdefault(release.label_id, []).append(release_id)
            for genre in release.genres:
                self._by_genre.setdefault(genre, []).append(release_id)
            for style in release.styles:
                self._by_style.setdefault(style, []).append(release_id)
            if release.master_id is not None:
                self._by_master.setdefault(release.master_id, []).append(release_id)

        self._collectors_of: dict[str, set[str]] = {}
        self._wanters_of: dict[str, set[str]] = {}
        for holding in sorted(self.holdings.values(), key=lambda item: item.collector_id):
            for release_id in holding.owned:
                self._collectors_of.setdefault(release_id, set()).add(holding.collector_id)
            for release_id in sorted(holding.wants):
                self._wanters_of.setdefault(release_id, set()).add(holding.collector_id)

    # ── Helpers ─────────────────────────────────────────────────────

    def _release(self, release_id: str) -> Release:
        return self.golden.releases[release_id]

    def _artist_name(self, release: Release) -> str | None:
        """The name ``collect(DISTINCT a.name)[0]`` resolves to: the release's first artist."""
        for artist_id in release.artist_ids:
            artist = self.golden.artists.get(artist_id)
            if artist is not None:
                return artist.name
        return None

    def _owned(self, collector_id: str) -> tuple[str, ...]:
        holding = self.holdings.get(collector_id)
        return holding.owned if holding else ()

    def _wants(self, collector_id: str) -> frozenset[str]:
        holding = self.holdings.get(collector_id)
        return holding.wants if holding else frozenset()

    def _candidate_row(self, release: Release, genres: Sequence[str], score: int, source: str) -> dict[str, Any]:
        """The row shape every recommendation candidate query returns.

        ``score`` stays an ``int`` because every candidate query derives it from a Cypher
        ``count``. ``_normalize_scores`` turns it into a ratio downstream; emitting a float
        here instead would be a silent shape drift from the rows the real queries return.
        """
        return {
            "id": release.id,
            "title": release.title,
            "artist": self._artist_name(release),
            "label": self.golden.labels[release.label_id].name,
            "year": release.year,
            "genres": list(genres),
            "score": score,
            "source": source,
        }

    # ── Artist similarity shapes ────────────────────────────────────

    def artist_identity(self, artist_id: str) -> dict[str, Any] | None:
        """Mirror ``get_artist_identity``: id, name, and release count, or ``None``."""
        artist = self.golden.artists.get(artist_id)
        if artist is None:
            return None
        return {"artist_id": artist.id, "artist_name": artist.name, "release_count": len(self._by_artist.get(artist_id, []))}

    def artist_profile(self, artist_id: str) -> dict[str, Any]:
        """Mirror ``get_artist_profile``: counted genres, styles, labels, and collaborators."""
        release_ids = self._by_artist.get(artist_id, [])
        genres: list[str] = []
        styles: list[str] = []
        labels: list[str] = []
        collaborators: list[str] = []
        for release_id in release_ids:
            release = self._release(release_id)
            genres.extend(release.genres)
            styles.extend(release.styles)
            labels.append(self.golden.labels[release.label_id].name)
            collaborators.extend(
                self.golden.artists[other].name for other in release.artist_ids if other != artist_id and other in self.golden.artists
            )
        return {
            "genres": _counted(genres),
            "styles": _counted(styles),
            "labels": _counted(labels),
            "collaborators": _counted(collaborators),
        }

    def batch_artist_profiles(self, artist_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
        """Mirror ``_batch_artist_profiles``: one profile per requested id, always present."""
        return {artist_id: self.artist_profile(artist_id) for artist_id in artist_ids}

    def candidate_artists(self, artist_id: str) -> list[dict[str, Any]]:
        """Mirror the frozen ``heuristics-2026-09`` candidate generator, genre-shared, capped.

        This is the *historical* shape: it reproduces the candidate generator
        ``api.queries.recommend_queries.get_candidate_artists`` used before
        gm-catalog-api-tsmu.1, which expanded only the target's top :data:`TOP_GENRES`
        genres, capped each expansion at :data:`PER_GENRE_CANDIDATE_LIMIT` candidates, kept
        the top :data:`CANDIDATE_LIMIT`, and profiled only the first
        :data:`PROFILED_CANDIDATES` of those. It is kept exactly as it was -- unconnected to
        the current production query -- because :data:`~api.evaluation.baseline.BASELINE_VERSION`
        ``heuristics-2026-09`` replays it and its committed ``expected-metrics.json`` must stay
        byte-identical. See :meth:`candidate_artists_all_signals` for the replacement shape.
        On a 120-release fixture none of the four limits bites, which is the point -- the
        shape is identical whether or not the data is large enough to trigger them.
        """
        profile = self.artist_profile(artist_id)
        top_genres = [entry["name"] for entry in profile["genres"][:TOP_GENRES]]

        shared: dict[str, int] = {}
        for genre in top_genres:
            per_genre: dict[str, int] = {}
            for release_id in self._by_genre.get(genre, []):
                for other in self._release(release_id).artist_ids:
                    if other == artist_id or other not in self.golden.artists:
                        continue
                    per_genre[other] = per_genre.get(other, 0) + 1
            for other, count in sorted(per_genre.items(), key=lambda item: (-item[1], item[0]))[:PER_GENRE_CANDIDATE_LIMIT]:
                shared[other] = shared.get(other, 0) + count

        ranked = sorted(
            ((other, count) for other, count in shared.items() if count >= MIN_ARTIST_RELEASES),
            key=lambda item: (-item[1], item[0]),
        )[:CANDIDATE_LIMIT]
        if not ranked:
            return []

        profiled = ranked[:PROFILED_CANDIDATES]
        profiles = self.batch_artist_profiles([other for other, _count in profiled])
        return [
            {"artist_id": other, "artist_name": self.golden.artists[other].name, "release_count": count, **profiles[other]}
            for other, count in profiled
        ]

    def candidate_artists_all_signals(self, artist_id: str) -> list[dict[str, Any]]:
        """Mirror the gm-catalog-api-tsmu.1 candidate generator: every shared-signal artist.

        A candidate qualifies by sharing at least one genre, style, or label with any of the
        target's releases, or by appearing on the same release as the target (collaborator),
        and by clearing the :data:`MIN_ARTIST_RELEASES` floor on that shared release count.
        There is no per-genre cap and no top-N truncation -- every qualifying artist is
        profiled and scored. This is the shape
        ``api.queries.recommend_queries.get_candidate_artists`` and
        ``api.queries.recommend_pg_queries.get_candidate_artists`` were rewritten to; it backs
        the new baseline version registered alongside the frozen ``heuristics-2026-09`` one
        (see :data:`~api.evaluation.baseline.SIMILAR_ARTIST_CANDIDATES_VERSION`).
        """
        target_release_ids = self._by_artist.get(artist_id, [])
        if not target_release_ids:
            return []
        target_genres = {genre for release_id in target_release_ids for genre in self._release(release_id).genres}
        target_styles = {style for release_id in target_release_ids for style in self._release(release_id).styles}
        target_labels = {self._release(release_id).label_id for release_id in target_release_ids}

        hits: dict[str, set[str]] = {}

        def _add(other: str, release_id: str) -> None:
            if other == artist_id or other not in self.golden.artists:
                return
            hits.setdefault(other, set()).add(release_id)

        for genre in target_genres:
            for release_id in self._by_genre.get(genre, []):
                for other in self._release(release_id).artist_ids:
                    _add(other, release_id)
        for style in target_styles:
            for release_id in self._by_style.get(style, []):
                for other in self._release(release_id).artist_ids:
                    _add(other, release_id)
        for label in target_labels:
            for release_id in self._by_label.get(label, []):
                for other in self._release(release_id).artist_ids:
                    _add(other, release_id)
        for release_id in target_release_ids:
            for other in self._release(release_id).artist_ids:
                _add(other, release_id)

        ranked = sorted(
            ((other, len(releases)) for other, releases in hits.items() if len(releases) >= MIN_ARTIST_RELEASES),
            key=lambda item: (-item[1], item[0]),
        )
        if not ranked:
            return []

        profiles = self.batch_artist_profiles([other for other, _count in ranked])
        return [
            {"artist_id": other, "artist_name": self.golden.artists[other].name, "release_count": count, **profiles[other]} for other, count in ranked
        ]

    # ── Recommendation candidate shapes ─────────────────────────────

    def _top_collected_artists(self, collector_id: str, top: int) -> list[tuple[str, int]]:
        """The collector's most-collected artists, highest first, ties broken by artist id."""
        counts: dict[str, int] = {}
        for release_id in self._owned(collector_id):
            for artist_id in self._release(release_id).artist_ids:
                counts[artist_id] = counts.get(artist_id, 0) + 1
        return sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:top]

    def artist_affinity_candidates(self, collector_id: str, limit: int = 50) -> list[dict[str, Any]]:
        """Mirror ``get_user_recommendations``: unowned releases by already-collected artists.

        The score is the sum of the collected counts of every top artist on the release, which
        is what ``sum(collected_count)`` produces after the per-artist ``LIMIT 10``.
        """
        owned = set(self._owned(collector_id))
        wants = self._wants(collector_id)
        top = dict(self._top_collected_artists(collector_id, TOP_COLLECTED_ARTISTS))

        scores: dict[str, int] = {}
        for artist_id, collected_count in top.items():
            for release_id in self._by_artist.get(artist_id, []):
                if release_id in owned or release_id in wants:
                    continue
                scores[release_id] = scores.get(release_id, 0) + collected_count

        rows = [
            self._candidate_row(
                self._release(release_id),
                sorted(set(self._release(release_id).genres)),
                score,
                f"artist: collected {score} releases",
            )
            for release_id, score in scores.items()
        ]
        rows.sort(key=lambda row: (-row["score"], row["id"]))
        return rows[:limit]

    def taste_genre_vector(self, collector_id: str) -> dict[str, float]:
        """Mirror the flat genre vector ``get_taste_heatmap``'s cells aggregate into."""
        counts: dict[str, int] = {}
        for release_id in self._owned(collector_id):
            for genre in self._release(release_id).genres:
                counts[genre] = counts.get(genre, 0) + 1
        total = sum(counts.values())
        if not total:
            return {}
        return {genre: count / total for genre, count in sorted(counts.items())}

    def blind_spot_genres(self, collector_id: str, limit: int = 5) -> set[str]:
        """Mirror ``get_blind_spots``: genres the top artists reach that the collector lacks."""
        owned = set(self._owned(collector_id))
        owned_genres = {genre for release_id in owned for genre in self._release(release_id).genres}
        overlap: dict[str, set[str]] = {}
        for artist_id, _count in self._top_collected_artists(collector_id, TOP_ARTISTS):
            for release_id in self._by_artist.get(artist_id, []):
                if release_id in owned:
                    continue
                for genre in self._release(release_id).genres:
                    if genre not in owned_genres:
                        overlap.setdefault(genre, set()).add(artist_id)
        ranked = sorted(overlap.items(), key=lambda item: (-len(item[1]), item[0]))[:limit]
        return {genre for genre, _artists in ranked}

    def label_affinity_candidates(self, collector_id: str, limit: int = 50) -> list[dict[str, Any]]:
        """Mirror ``get_label_affinity_candidates``: unowned releases on the top labels."""
        owned = set(self._owned(collector_id))
        wants = self._wants(collector_id)

        label_counts: dict[str, int] = {}
        for release_id in self._owned(collector_id):
            label_id = self._release(release_id).label_id
            label_counts[label_id] = label_counts.get(label_id, 0) + 1
        top = sorted(label_counts.items(), key=lambda item: (-item[1], item[0]))[:TOP_LABELS]

        rows: list[dict[str, Any]] = []
        for label_id, label_count in top:
            label_name = self.golden.labels[label_id].name
            for release_id in self._by_label.get(label_id, []):
                if release_id in owned or release_id in wants:
                    continue
                release = self._release(release_id)
                rows.append(self._candidate_row(release, sorted(set(release.genres)), label_count, f"label: {label_name} (top label)"))
        rows.sort(key=lambda row: (-row["score"], row["id"]))
        return rows[:limit]

    def blindspot_candidates(self, collector_id: str, limit: int = 50) -> list[dict[str, Any]]:
        """Mirror ``get_blindspot_candidates``: releases in genres the collector owns none of.

        The production query keeps a genre only when ``already_have = 0``, samples at most
        :data:`BLINDSPOT_SAMPLE` releases per genre, and scores by how many of the collector's
        top artists reach into it. A genre the collector already owns something in is dropped
        entirely -- that filter, not the sampling, is what makes this a blind-spot signal.
        """
        owned = set(self._owned(collector_id))
        owned_genres = {genre for release_id in owned for genre in self._release(release_id).genres}

        artist_counts: dict[str, int] = {}
        for release_id in self._owned(collector_id):
            for artist_id in self._release(release_id).artist_ids:
                artist_counts[artist_id] = artist_counts.get(artist_id, 0) + 1
        top_artists = [artist_id for artist_id, _count in sorted(artist_counts.items(), key=lambda item: (-item[1], item[0]))[:TOP_ARTISTS]]

        overlap: dict[str, set[str]] = {}
        reachable: dict[str, set[str]] = {}
        for artist_id in top_artists:
            for release_id in self._by_artist.get(artist_id, []):
                if release_id in owned:
                    continue
                for genre in self._release(release_id).genres:
                    overlap.setdefault(genre, set()).add(artist_id)
                    reachable.setdefault(genre, set()).add(release_id)

        rows: list[dict[str, Any]] = []
        for genre in sorted(reachable):
            if genre in owned_genres:
                continue
            score = len(overlap[genre])
            for release_id in sorted(reachable[genre])[:BLINDSPOT_SAMPLE]:
                rows.append(self._candidate_row(self._release(release_id), [genre], score, f"blind_spot: {genre}"))
        rows.sort(key=lambda row: (-row["score"], row["id"]))
        return rows[:limit]

    def collector_counts(self, release_ids: Sequence[str]) -> dict[str, int]:
        """Mirror ``get_collector_counts``: distinct collectors per known release id."""
        if not release_ids:
            return {}
        return {release_id: len(self._collectors_of.get(release_id, ())) for release_id in release_ids if release_id in self.golden.releases}

    # ── Explore traversal shape ─────────────────────────────────────

    def _neighbours(self, node: tuple[str, str]) -> list[tuple[str, tuple[str, str]]]:
        """Return ``(relationship type, neighbour)`` pairs, in a deterministic order."""
        kind, key = node
        if kind == "release":
            edges: list[tuple[str, tuple[str, str]]] = []
            release = self._release(key)
            edges.extend(("BY", ("artist", artist_id)) for artist_id in release.artist_ids)
            edges.append(("ON", ("label", release.label_id)))
            edges.extend(("IS", ("genre", genre)) for genre in release.genres)
            edges.extend(("IS", ("style", style)) for style in release.styles)
            if release.master_id is not None:
                edges.append(("DERIVED_FROM", ("master", release.master_id)))
            return edges
        # Every other node kind reaches the graph only through releases, so one reverse index
        # per kind covers them. The lookup is total over EXPLORE_ENTITY_TYPES.
        lookup, rel_type = {
            "artist": (self._by_artist, "BY"),
            "label": (self._by_label, "ON"),
            "genre": (self._by_genre, "IS"),
            "style": (self._by_style, "IS"),
            "master": (self._by_master, "DERIVED_FROM"),
        }[kind]
        return [(rel_type, ("release", release_id)) for release_id in lookup.get(key, [])]

    def _display_name(self, node: tuple[str, str]) -> str:
        """``coalesce(n.name, n.title, n.id)`` for one node."""
        kind, key = node
        if kind == "artist":
            return self.golden.artists[key].name
        if kind == "label":
            return self.golden.labels[key].name
        if kind == "release":
            return self._release(key).title
        if kind == "master":
            return self.golden.masters[key].title
        return key

    def _node_exists(self, node: tuple[str, str]) -> bool:
        kind, key = node
        return {
            "artist": key in self.golden.artists,
            "label": key in self.golden.labels,
            "release": key in self.golden.releases,
            "master": key in self.golden.masters,
            "genre": key in self._by_genre,
            "style": key in self._by_style,
        }[kind]

    def explore_traversal(self, entity_type: str, entity_id: str, hops: int = DEFAULT_HOPS) -> list[dict[str, Any]]:
        """Mirror ``get_explore_traversal``: shortest path to every discovered node.

        Genre and Style nodes are keyed by name and carry no ``id``, which is why the returned
        ``id`` is ``coalesce(discovered.id, discovered.name)`` and why ``score_discoveries``
        falls back with ``or`` rather than with ``dict.get``'s default.
        """
        if entity_type not in EXPLORE_ENTITY_TYPES:
            return []
        if not 1 <= hops <= 3:
            hops = DEFAULT_HOPS
        start = (entity_type, entity_id)
        if not self._node_exists(start):
            return []

        best: dict[tuple[str, str], tuple[list[str], list[str], int]] = {}
        frontier: list[tuple[tuple[str, str], list[tuple[str, str]], list[str]]] = [(start, [start], [])]
        seen: set[tuple[str, str]] = {start}
        for distance in range(1, hops + 1):
            nxt: list[tuple[tuple[str, str], list[tuple[str, str]], list[str]]] = []
            for node, path, rel_types in frontier:
                for rel_type, neighbour in self._neighbours(node):
                    if neighbour in seen:
                        continue
                    next_path = [*path, neighbour]
                    next_rels = [*rel_types, rel_type]
                    nxt.append((neighbour, next_path, next_rels))
                    if neighbour[0] in EXPLORE_DISCOVERABLE and neighbour not in best:
                        best[neighbour] = ([self._display_name(step) for step in next_path], next_rels, distance)
            seen.update(node for node, _path, _rels in nxt)
            frontier = nxt

        rows = [
            {
                # coalesce(discovered.id, discovered.name): Genre and Style are keyed by
                # name and carry no id, so the node key is already the right answer for both.
                "id": key,
                "name": self._display_name((kind, key)),
                "type": kind,
                "path_names": path_names,
                "rel_types": rel_types,
                "dist": distance,
            }
            for (kind, key), (path_names, rel_types, distance) in best.items()
        ]
        rows.sort(key=lambda row: (row["dist"], row["type"], row["id"]))
        return rows[:EXPLORE_LIMIT]

    # ── Rarity signal shapes ────────────────────────────────────────

    def _genre_release_counts(self) -> dict[str, int]:
        return {genre: len(release_ids) for genre, release_ids in self._by_genre.items()}

    def release_degree(self, release_id: str) -> int:
        """``COUNT { (r)--() }``: every relationship incident to one release node."""
        release = self._release(release_id)
        return (
            len(release.artist_ids)
            + 1
            + len(release.genres)
            + len(release.styles)
            + (1 if release.master_id is not None else 0)
            + len(self._collectors_of.get(release_id, ()))
            + len(self._wanters_of.get(release_id, ()))
        )

    def pressing_count(self, release_id: str) -> int:
        """Sibling pressings of this release's master, counting this one. ``0`` = no master."""
        release = self._release(release_id)
        if release.master_id is None:
            return 0
        return len(self._by_master.get(release.master_id, []))

    def core_signal_rows(self, release_ids: Sequence[str]) -> dict[str, list[dict[str, Any]]]:
        """Mirror the ``_CORE_QUERIES`` page in :mod:`api.queries.rarity_queries`.

        Returns one list of rows per fact name, keyed exactly as ``_fetch_page_signals`` keys
        them, so the scoring loop in ``fetch_all_rarity_signals`` can be replayed verbatim.
        """
        genre_counts = self._genre_release_counts()
        known = [release_id for release_id in release_ids if release_id in self.golden.releases]
        rows: dict[str, list[dict[str, Any]]] = {
            fact: [] for fact in ("release", "media", "label", "temporal", "degree", "artist_degree", "label_size", "genre_count")
        }
        for release_id in known:
            release = self._release(release_id)
            label = self.golden.labels[release.label_id]
            items = release.media["items"]
            siblings = [other for other in self._by_master.get(release.master_id or "", []) if other != release_id]
            sibling_years = [self._release(other).year for other in siblings]

            rows["release"].append(
                {"release_id": release_id, "title": release.title, "artist_name": self._artist_name(release), "year": release.year}
            )
            rows["media"].append(
                {
                    "release_id": release_id,
                    "mediums": [{"id": item["medium"], "family": item["family"]} for item in items],
                    "media_families": list(families_of(release.media)),
                    "formats": [item["source"]["name"] for item in items if item["source"]["name"]],
                }
            )
            rows["label"].append({"release_id": release_id, "label_catalog_size": label.release_count})
            rows["temporal"].append(
                {"release_id": release_id, "year": release.year, "latest_sibling_year": max(sibling_years) if sibling_years else None}
            )
            rows["degree"].append({"release_id": release_id, "degree": self.release_degree(release_id)})
            rows["artist_degree"].append(
                {"release_id": release_id, "artist_max_degree": max((len(self._by_artist.get(a, [])) for a in release.artist_ids), default=0)}
            )
            rows["label_size"].append({"release_id": release_id, "label_max_catalog": label.release_count})
            rows["genre_count"].append(
                {"release_id": release_id, "genre_max_release_count": max((genre_counts.get(g, 0) for g in release.genres), default=0)}
            )
        return rows

    def grooved_pressing_rows(self, release_ids: Sequence[str]) -> list[dict[str, Any]]:
        """Mirror the grooved module's ``PRESSING_QUERY``: ``release_id`` and ``pressing_count``."""
        return [
            {"release_id": release_id, "pressing_count": self.pressing_count(release_id)}
            for release_id in release_ids
            if release_id in self.golden.releases
        ]

    def community_counts(self, release_ids: Collection[str] | None = None) -> dict[str, tuple[int, int]]:
        """Mirror the ``insights.community_counts`` lookup: release id to ``(have, want)``."""
        wanted = set(release_ids) if release_ids is not None else set(self.golden.releases)
        return {release.id: (release.have_count, release.want_count) for release in self.golden.releases.values() if release.id in wanted}
