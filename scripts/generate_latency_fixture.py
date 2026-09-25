"""A synthetic, catalog-shaped fixture for measuring similar-artist candidate latency.

gm-catalog-api-tsmu.1's acceptance criteria calls for the similar-artist endpoint's p95 to be
measured on the PG19 integration tier "with a realistic fixture" -- one shaped like the real
catalog, not the small (120-release) golden set `tests/fixtures/golden/` uses for offline
metrics. The property that matters here is specifically the one the old candidate generator's
per-genre caps existed to survive: a handful of very broad genres/styles/labels (a release in
"Rock" is one of many; a release on a mega-label is one of many) against a long tail of niche
ones, and a few prolific artists sitting at the high end of the release-count distribution.

Nothing here is committed. This module is a deterministic generator (seeded ``random.Random``,
same style as ``scripts/generate_golden_set.py``); a benchmark run builds a fresh fixture,
seeds it into a throwaway integration-tier database, measures, and discards it. See
``tests/test_recommend_candidate_latency.py`` for the benchmark that consumes this module.

Every artist, label, genre, style, and title is invented -- no Discogs or MusicBrainz data, or
data derived from either, appears here or is ever committed alongside it.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Final


#: Mega facets carry most of the weight; niche facets split the remainder over a long tail.
#: Ratios are chosen to resemble a real catalog's genre skew (a handful of genres like "Rock"
#: or "Electronic" dominate; most genres are thin), not to hit an exact percentage.
MEGA_GENRE_NAMES: Final[tuple[str, ...]] = ("Rock", "Electronic", "Pop")
NICHE_GENRE_COUNT: Final[int] = 30
MEGA_GENRE_WEIGHT: Final[int] = 25
NICHE_GENRE_WEIGHT: Final[int] = 1

MEGA_STYLE_NAMES: Final[tuple[str, ...]] = ("Deep House", "Ambient", "Punk")
NICHE_STYLE_COUNT: Final[int] = 30
MEGA_STYLE_WEIGHT: Final[int] = 20
NICHE_STYLE_WEIGHT: Final[int] = 1

MEGA_LABEL_COUNT: Final[int] = 5
NICHE_LABEL_COUNT: Final[int] = 40
MEGA_LABEL_WEIGHT: Final[int] = 15
NICHE_LABEL_WEIGHT: Final[int] = 1

#: Hub artists get most of the release credits (main or collaborator), so their release count
#: sits at the high end of the distribution -- the p99 target the acceptance criteria asks
#: for. Non-hub artists are the long tail: a handful of releases each, some below
#: MIN_ARTIST_RELEASES and so never a valid similar-artist query target on their own.
HUB_ARTIST_COUNT: Final[int] = 15
HUB_ARTIST_WEIGHT: Final[int] = 40
TAIL_ARTIST_WEIGHT: Final[int] = 1

#: The single most prolific artist is pinned to only mega facets, guaranteeing the worst case
#: the acceptance criteria describes (a mega-genre artist at ~p99 release count) shows up even
#: if the weighted draw happens not to produce one on its own.
MEGA_ARTIST_INDEX: Final[int] = 0

#: The niche control artist is excluded from the general weighted draw entirely (a tail
#: artist's weight is low but not zero, so across thousands of releases it would otherwise
#: pick up several incidental credits drawn from the population-wide, mostly-mega facet
#: distribution -- contaminating the one target the benchmark needs to be genuinely
#: best-case). Its releases are appended deterministically instead, all on the same single
#: niche genre/style/label, which is also what makes its expected candidate pool modest
#: rather than empty: other tail artists' incidental niche draws land on that combination too.
NICHE_ARTIST_RELEASE_COUNT: Final[int] = 5

DEFAULT_ARTIST_COUNT: Final[int] = 2500
DEFAULT_RELEASE_COUNT: Final[int] = 8000
DEFAULT_SEED: Final[int] = 20260924


@dataclass(frozen=True)
class LatencyRelease:
    id: str
    year: int
    artist_ids: tuple[str, ...]
    label_id: str
    genre: str
    style: str


@dataclass(frozen=True)
class LatencyFixture:
    """A synthetic catalog: artists, releases, and the facets they're tagged with."""

    artists: dict[str, str]  # artist_id -> name
    labels: dict[str, str]  # label_id -> name
    releases: tuple[LatencyRelease, ...]
    mega_genres: tuple[str, ...]
    niche_genres: tuple[str, ...]
    mega_styles: tuple[str, ...]
    niche_styles: tuple[str, ...]
    #: Deliberately constructed, not drawn from the weighted population: the worst case
    #: (every facet mega, release count at the distribution's high end) and the best case
    #: (every facet niche, a handful of releases) a benchmark needs, guaranteed to exist
    #: regardless of how the weighted draw for everyone else came out.
    mega_artist_id: str
    niche_artist_id: str

    def release_counts(self) -> dict[str, int]:
        """Each artist's release count, for picking benchmark targets by percentile."""
        counts: dict[str, int] = dict.fromkeys(self.artists, 0)
        for release in self.releases:
            for artist_id in release.artist_ids:
                counts[artist_id] += 1
        return counts


# ── Deterministic draw helpers (same style as scripts/generate_golden_set.py) ────────────────


def _below(rng: random.Random, bound: int) -> int:
    return min(bound - 1, int(rng.random() * bound))


def _weighted_index(rng: random.Random, weights: list[int]) -> int:
    total = sum(weights)
    cut = _below(rng, total)
    seen = 0
    for index, weight in enumerate(weights):
        seen += weight
        if cut < seen:
            return index
    return len(weights) - 1


def _weighted_count(rng: random.Random, low: int, high: int) -> int:
    """A skewed integer in ``[low, high]``, biased toward ``low`` (most releases are simple)."""
    span = high - low
    return low + int((rng.random() ** 2) * (span + 1))


def build_fixture(
    seed: int = DEFAULT_SEED,
    n_artists: int = DEFAULT_ARTIST_COUNT,
    n_releases: int = DEFAULT_RELEASE_COUNT,
) -> LatencyFixture:
    """Build a deterministic, catalog-shaped synthetic fixture.

    Args:
        seed: RNG seed. The same seed and sizes always produce the same fixture.
        n_artists: Total distinct artists (hubs included).
        n_releases: Total releases to generate.

    Returns:
        The fixture: artists, labels, releases, and the mega/niche facet name lists.
    """
    rng = random.Random(seed)

    mega_genres = MEGA_GENRE_NAMES
    niche_genres = tuple(f"Niche Genre {i}" for i in range(NICHE_GENRE_COUNT))
    genre_pool = list(mega_genres) + list(niche_genres)
    genre_weights = [MEGA_GENRE_WEIGHT] * len(mega_genres) + [NICHE_GENRE_WEIGHT] * len(niche_genres)

    mega_styles = MEGA_STYLE_NAMES
    niche_styles = tuple(f"Niche Style {i}" for i in range(NICHE_STYLE_COUNT))
    style_pool = list(mega_styles) + list(niche_styles)
    style_weights = [MEGA_STYLE_WEIGHT] * len(mega_styles) + [NICHE_STYLE_WEIGHT] * len(niche_styles)

    labels = {
        f"9{i:05d}": (f"Mega Label {i}" if i < MEGA_LABEL_COUNT else f"Niche Label {i - MEGA_LABEL_COUNT}")
        for i in range(MEGA_LABEL_COUNT + NICHE_LABEL_COUNT)
    }
    label_ids = list(labels)
    label_weights = [MEGA_LABEL_WEIGHT] * MEGA_LABEL_COUNT + [NICHE_LABEL_WEIGHT] * NICHE_LABEL_COUNT

    artists = {f"8{i:06d}": f"Artist {i}" for i in range(n_artists)}
    artist_ids = list(artists)
    hub_ids = artist_ids[:HUB_ARTIST_COUNT]
    mega_artist_id = hub_ids[MEGA_ARTIST_INDEX]
    niche_artist_id = artist_ids[HUB_ARTIST_COUNT]

    # The niche control artist is reserved out of the general draw entirely -- see its
    # constant's docstring above.
    pool_ids = hub_ids + artist_ids[HUB_ARTIST_COUNT + 1 :]
    pool_weights = [HUB_ARTIST_WEIGHT] * HUB_ARTIST_COUNT + [TAIL_ARTIST_WEIGHT] * (len(pool_ids) - HUB_ARTIST_COUNT)

    releases: list[LatencyRelease] = []
    for i in range(n_releases):
        release_id = f"7{i:06d}"
        year = 1970 + _below(rng, 56)

        # The mega artist is forced onto ~15% of releases so its own release count, and the
        # size of the candidate pool a query against it produces, both land at the
        # distribution's high end without depending on the weighted draw alone.
        main_artist = mega_artist_id if rng.random() < 0.15 else pool_ids[_weighted_index(rng, pool_weights)]
        credited = [main_artist]
        for _ in range(_weighted_count(rng, 0, 2)):
            other = pool_ids[_weighted_index(rng, pool_weights)]
            if other not in credited:
                credited.append(other)

        label_id = label_ids[_weighted_index(rng, label_weights)]
        # The mega artist's own facets are pinned to mega genre/style, so its candidate query
        # exercises the worst case (broad signal on every dimension) rather than an average one.
        if main_artist == mega_artist_id:
            genre = mega_genres[_below(rng, len(mega_genres))]
            style = mega_styles[_below(rng, len(mega_styles))]
        else:
            genre = genre_pool[_weighted_index(rng, genre_weights)]
            style = style_pool[_weighted_index(rng, style_weights)]

        releases.append(LatencyRelease(id=release_id, year=year, artist_ids=tuple(credited), label_id=label_id, genre=genre, style=style))

    # The niche control artist's releases, appended deterministically: all on the same single
    # niche genre/style/label, so any candidate this query finds shares that one combination
    # with it -- the best case the acceptance criteria's fixture needs alongside the worst one.
    niche_label_id = label_ids[MEGA_LABEL_COUNT]
    for j in range(NICHE_ARTIST_RELEASE_COUNT):
        releases.append(
            LatencyRelease(
                id=f"7{n_releases + j:06d}",
                year=1970 + j,
                artist_ids=(niche_artist_id,),
                label_id=niche_label_id,
                genre=niche_genres[0],
                style=niche_styles[0],
            )
        )

    return LatencyFixture(
        artists=artists,
        labels=labels,
        releases=tuple(releases),
        mega_genres=mega_genres,
        niche_genres=niche_genres,
        mega_styles=mega_styles,
        niche_styles=niche_styles,
        mega_artist_id=mega_artist_id,
        niche_artist_id=niche_artist_id,
    )


def percentile(counts: list[int], p: float) -> int:
    """The value at percentile ``p`` (0-100) of a sorted-ascending copy of ``counts``."""
    if not counts:
        return 0
    ordered = sorted(counts)
    index = min(len(ordered) - 1, int(len(ordered) * p / 100))
    return ordered[index]


def main() -> int:
    """Print summary statistics for the default-sized fixture, for eyeballing its shape."""
    fixture = build_fixture()
    counts = fixture.release_counts()
    values = list(counts.values())
    genre_totals: dict[str, int] = {}
    for release in fixture.releases:
        genre_totals[release.genre] = genre_totals.get(release.genre, 0) + 1

    print(f"artists={len(fixture.artists)} releases={len(fixture.releases)} labels={len(fixture.labels)}")
    print(f"release-count percentiles: p50={percentile(values, 50)} p90={percentile(values, 90)} p99={percentile(values, 99)} max={max(values)}")
    print(f"mega_artist={fixture.mega_artist_id} release_count={counts[fixture.mega_artist_id]}")
    print(f"niche_artist={fixture.niche_artist_id} release_count={counts[fixture.niche_artist_id]}")
    print("top 5 genres by release count:")
    for genre, count in sorted(genre_totals.items(), key=lambda item: -item[1])[:5]:
        print(f"  {genre}: {count}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
