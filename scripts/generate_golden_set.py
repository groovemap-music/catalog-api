#!/usr/bin/env python3
"""Generate the deterministic, format-balanced synthetic golden set.

The golden set is the fixture every offline baseline and every future model is scored on. It
is **synthetic**: no provider data, no real collector, nothing that could not be published.
It is **deterministic**: the same ``SEED`` produces byte-identical JSON, which is what lets
``tests/test_evaluation_golden_set.py`` assert regeneration rather than trusting a blob.

Determinism contract
--------------------
Only :meth:`random.Random.random` is used. CPython guarantees that method's sequence across
versions; the convenience helpers (``choice``, ``sample``, ``shuffle``, ``randrange``) carry
no such guarantee, so every draw here goes through the small helpers below instead. A change
to any helper, to ``SEED``, or to the entity tables re-rolls the whole set and must be
committed together with the regenerated JSON and the refreshed expected metrics.

Media blocks are produced by ``common.media.map_discogs_formats`` rather than hand-written,
so ``families_of`` resolves exactly the families the taxonomy resolves in production.

Run with ``just generate-golden-set``.
"""

from __future__ import annotations

import argparse
import json
import random
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Final

from common.media import map_discogs_formats


ROOT: Final[Path] = Path(__file__).resolve().parents[1]
GOLDEN_DIR: Final[Path] = ROOT / "tests" / "fixtures" / "golden"

# Bumping this re-rolls every entity. Regenerate the JSON and the expected metrics with it.
SEED: Final[int] = 20260915
GENERATOR_VERSION: Final[str] = "golden-2026-09"

RELEASE_COUNT: Final[int] = 120
COLLECTOR_COUNT: Final[int] = 12

# The three families the set is balanced across, one third each. The acceptance band is 30-36
# percent per family, and an exact third sits in the middle of it.
BALANCED_FAMILIES: Final[tuple[str, ...]] = ("vinyl", "optical", "tape")

# Acquisition dates straddle the split cut so every collector has both an observed collection
# and a held-out tail. See api/evaluation/split.py for the cut itself.
EARLIEST_ADDED: Final[date] = date(2018, 1, 1)
SPLIT_CUT: Final[date] = date(2023, 1, 1)
LATEST_ADDED: Final[date] = date(2024, 12, 31)

MIN_COLLECTION: Final[int] = 15
MAX_COLLECTION: Final[int] = 40
MIN_OBSERVED: Final[int] = 8
MIN_HELD_OUT: Final[int] = 5

YEAR_FIRST: Final[int] = 1960
YEAR_LAST: Final[int] = 2020


# ── Entity vocabulary ───────────────────────────────────────────────
#
# Invented names. Any resemblance to a real artist, label, or release is not intended and is
# not relied on by any test.

GENRES: Final[tuple[str, ...]] = (
    "Rock",
    "Jazz",
    "Electronic",
    "Funk / Soul",
    "Hip Hop",
    "Reggae",
    "Folk World & Country",
    "Classical",
)

STYLES_BY_GENRE: Final[dict[str, tuple[str, ...]]] = {
    "Rock": ("Psychedelic Rock", "Post-Punk", "Garage Rock"),
    "Jazz": ("Hard Bop", "Spiritual Jazz", "Free Improvisation"),
    "Electronic": ("Ambient", "Acid House", "Drum n Bass"),
    "Funk / Soul": ("Deep Funk", "Northern Soul", "Boogie"),
    "Hip Hop": ("Boom Bap", "Instrumental", "Turntablism"),
    "Reggae": ("Roots Reggae", "Dub", "Rocksteady"),
    "Folk World & Country": ("Highlife", "Nordic Folk", "Country Blues"),
    "Classical": ("Minimalism", "Baroque", "Musique Concrete"),
}

# (label name, global catalog size). The sizes straddle every compute_label_catalog_score
# threshold (10, 50, 200, 1000) so the signal varies across the set instead of saturating.
LABELS: Final[tuple[tuple[str, int], ...]] = (
    ("Ashgrove Tapes", 4),
    ("Belmont Sound", 8),
    ("Cindermill", 27),
    ("Drift Harbour", 44),
    ("Everdeen Discs", 96),
    ("Foxglove Audio", 173),
    ("Grainstore", 310),
    ("Halcyon Press", 640),
    ("Ironvale", 940),
    ("Juniper Works", 1_800),
    ("Kestrel International", 4_600),
    ("Lantern Row", 12_500),
)

ARTIST_FIRST: Final[tuple[str, ...]] = (
    "Marla",
    "Osric",
    "Petra",
    "Quill",
    "Rowan",
    "Sable",
    "Thess",
    "Ulla",
    "Vance",
    "Wren",
    "Xanthe",
    "Yarrow",
)

ARTIST_LAST: Final[tuple[str, ...]] = (
    "Ashdown",
    "Brill",
    "Calder",
    "Dunmore",
    "Ellery",
    "Fairholme",
    "Garrow",
    "Hallam",
    "Ingram",
    "Jessop",
    "Kirkby",
    "Lowther",
)

TITLE_HEAD: Final[tuple[str, ...]] = (
    "Amber",
    "Bramble",
    "Cinder",
    "Dovetail",
    "Ember",
    "Fathom",
    "Glasshouse",
    "Harrow",
    "Isinglass",
    "Jetty",
    "Kindling",
    "Lowtide",
    "Meridian",
    "Nightjar",
    "Orchard",
    "Pennyroyal",
)

TITLE_TAIL: Final[tuple[str, ...]] = (
    "Anthem",
    "Broadcast",
    "Cartography",
    "Divisions",
    "Echoes",
    "Foundry",
    "Garden",
    "Hymnal",
    "Interval",
    "Junction",
    "Lantern",
    "Migration",
)

# Pressing variants, applied to the second and later releases of one master so sibling
# pressings are distinguishable in the fixture the way reissues are in the catalog.
PRESSING_SUFFIXES: Final[tuple[str, ...]] = (
    "Reissue",
    "Remastered",
    "Anniversary Edition",
    "Repress",
)

# Canonical medium id to the raw Discogs format entry that produces it. The mapping runs
# through the vendored taxonomy, so these entries -- not the ids -- are the source of truth.
MEDIUM_FORMATS: Final[dict[str, dict[str, Any]]] = {
    "vinyl_12": {"name": "Vinyl", "qty": "1", "descriptions": ["LP", "Album"]},
    "vinyl_7": {"name": "Vinyl", "qty": "1", "descriptions": ['7"', "Single"]},
    "vinyl_10": {"name": "Vinyl", "qty": "1", "descriptions": ['10"', "EP"]},
    "optical_cd": {"name": "CD", "qty": "1", "descriptions": ["Album"]},
    "optical_cdr": {"name": "CDr", "qty": "1", "descriptions": ["Album"]},
    "tape_cassette": {"name": "Cassette", "qty": "1", "descriptions": ["Album"]},
    "tape_8_track": {"name": "8-Track Cartridge", "qty": "1", "descriptions": ["Album"]},
}

# Weighted medium choice per family: the mainstream medium dominates, the scarcer ones appear
# often enough to move medium_rarity. Weights are integers so the draw stays exact.
FAMILY_MEDIUMS: Final[dict[str, tuple[tuple[str, int], ...]]] = {
    "vinyl": (("vinyl_12", 7), ("vinyl_7", 2), ("vinyl_10", 1)),
    "optical": (("optical_cd", 8), ("optical_cdr", 2)),
    "tape": (("tape_cassette", 8), ("tape_8_track", 2)),
}


# ── Deterministic draw helpers ──────────────────────────────────────


def _below(rng: random.Random, bound: int) -> int:
    """Return an integer in ``[0, bound)`` using only ``Random.random``."""
    return min(bound - 1, int(rng.random() * bound))


def _pick(rng: random.Random, options: tuple[Any, ...] | list[Any]) -> Any:
    """Return one element of ``options``."""
    return options[_below(rng, len(options))]


def _weighted(rng: random.Random, options: tuple[tuple[Any, int], ...]) -> Any:
    """Return one element of ``(value, weight)`` pairs, proportionally to weight."""
    total = sum(weight for _value, weight in options)
    cut = _below(rng, total)
    seen = 0
    for value, weight in options:
        seen += weight
        if cut < seen:
            return value
    return options[-1][0]


def _shuffled(rng: random.Random, items: list[Any]) -> list[Any]:
    """Return a Fisher-Yates shuffle of ``items``, leaving the input untouched."""
    out = list(items)
    for index in range(len(out) - 1, 0, -1):
        swap = _below(rng, index + 1)
        out[index], out[swap] = out[swap], out[index]
    return out


def _sample(rng: random.Random, items: list[Any], count: int) -> list[Any]:
    """Return ``count`` distinct elements of ``items`` in shuffled order."""
    return _shuffled(rng, items)[:count]


def _between(rng: random.Random, low: int, high: int) -> int:
    """Return an integer in the inclusive range ``[low, high]``."""
    return low + _below(rng, high - low + 1)


def _date_between(rng: random.Random, start: date, end: date) -> date:
    """Return a date in the inclusive range ``[start, end]``."""
    return start + timedelta(days=_below(rng, (end - start).days + 1))


# ── Catalog generation ──────────────────────────────────────────────


def _build_labels() -> list[dict[str, Any]]:
    return [{"id": f"l{index:03d}", "name": name, "release_count": size} for index, (name, size) in enumerate(LABELS, start=1)]


def _build_artists(rng: random.Random) -> list[dict[str, Any]]:
    """Build the artist roster, each with a primary genre and one or two styles."""
    artists: list[dict[str, Any]] = []
    index = 0
    for first in ARTIST_FIRST:
        for last in ARTIST_LAST[: len(ARTIST_LAST) // 4]:
            index += 1
            genre = GENRES[index % len(GENRES)]
            styles = _sample(rng, list(STYLES_BY_GENRE[genre]), _between(rng, 1, 2))
            artists.append(
                {
                    "id": f"a{index:03d}",
                    "name": f"{first} {last}",
                    "primary_genre": genre,
                    "styles": sorted(styles),
                }
            )
    return artists


def _build_works(rng: random.Random, artists: list[dict[str, Any]], labels: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group the release budget into works: a master with pressings, or a standalone release.

    A work with more than one pressing becomes a ``Master`` node with that many ``Release``
    nodes hanging off it, which is what gives ``pressing_scarcity`` something to count. Roughly
    one work in five is standalone (no master link at all), the case that scores 90 rather
    than 100.
    """
    works: list[dict[str, Any]] = []
    budget = RELEASE_COUNT
    while budget > 0:
        standalone = rng.random() < 0.20
        pressings = 1 if standalone else min(budget, _between(rng, 1, 4))
        primary = _pick(rng, artists)
        collaborator = _pick(rng, artists) if rng.random() < 0.30 else None
        genre = primary["primary_genre"]
        extra_genre = _pick(rng, GENRES) if rng.random() < 0.25 else None
        genres = sorted({genre, extra_genre} - {None}) if extra_genre else [genre]
        styles = sorted(set(primary["styles"]))
        works.append(
            {
                "standalone": standalone,
                "pressings": pressings,
                "artist_ids": [primary["id"]] + ([collaborator["id"]] if collaborator and collaborator["id"] != primary["id"] else []),
                "label_id": _pick(rng, labels)["id"],
                "genres": genres,
                "styles": styles,
                "title": f"{_pick(rng, TITLE_HEAD)} {_pick(rng, TITLE_TAIL)}",
                "first_year": _between(rng, YEAR_FIRST, YEAR_LAST),
            }
        )
        budget -= pressings
    return works


def _balanced_families(rng: random.Random) -> list[str]:
    """Return one family per release, exactly balanced across the three, in shuffled order."""
    per_family, remainder = divmod(RELEASE_COUNT, len(BALANCED_FAMILIES))
    assigned = [family for family in BALANCED_FAMILIES for _ in range(per_family)]
    assigned.extend(BALANCED_FAMILIES[:remainder])
    return _shuffled(rng, assigned)


def _community_counts(rng: random.Random) -> tuple[int, int]:
    """Draw a power-law-ish have/want pair, so collection_prevalence spans its thresholds."""
    have = int(rng.random() ** 3 * 4_000)
    want = int(rng.random() ** 2 * max(1, have) * 1.4)
    return have, want


def _build_releases(
    rng: random.Random,
    works: list[dict[str, Any]],
    families: list[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Realise every work as releases, assigning the pre-balanced family to each in turn."""
    releases: list[dict[str, Any]] = []
    masters: list[dict[str, Any]] = []
    for work_index, work in enumerate(works, start=1):
        master_id = None if work["standalone"] else f"m{len(masters) + 1:03d}"
        years: list[int] = []
        for pressing in range(work["pressings"]):
            number = len(releases) + 1
            family = families[number - 1]
            medium = _weighted(rng, FAMILY_MEDIUMS[family])
            year = min(YEAR_LAST, work["first_year"] + (0 if pressing == 0 else _between(rng, 2, 25)))
            years.append(year)
            suffix = "" if pressing == 0 else f" ({PRESSING_SUFFIXES[(pressing - 1) % len(PRESSING_SUFFIXES)]})"
            have, want = _community_counts(rng)
            releases.append(
                {
                    "id": f"r{number:04d}",
                    "title": f"{work['title']}{suffix}",
                    "artist_ids": list(work["artist_ids"]),
                    "label_id": work["label_id"],
                    "master_id": master_id,
                    "year": year,
                    "genres": list(work["genres"]),
                    "styles": list(work["styles"]),
                    "medium": medium,
                    "media": map_discogs_formats([MEDIUM_FORMATS[medium]]),
                    "have_count": have,
                    "want_count": want,
                }
            )
        if master_id is not None:
            masters.append({"id": master_id, "title": work["title"], "year": min(years), "work_index": work_index})
    return releases, masters


def build_catalog(rng: random.Random) -> dict[str, Any]:
    """Build the whole synthetic catalog: labels, artists, masters, releases."""
    labels = _build_labels()
    artists = _build_artists(rng)
    works = _build_works(rng, artists, labels)
    releases, masters = _build_releases(rng, works, _balanced_families(rng))
    return {
        "generator_version": GENERATOR_VERSION,
        "seed": SEED,
        "genres": list(GENRES),
        "styles": sorted({style for styles in STYLES_BY_GENRE.values() for style in styles}),
        "labels": labels,
        "artists": artists,
        "masters": [{"id": master["id"], "title": master["title"], "year": master["year"]} for master in masters],
        "releases": releases,
    }


# ── Collector generation ────────────────────────────────────────────


def build_collectors(rng: random.Random, catalog: dict[str, Any]) -> dict[str, Any]:
    """Build synthetic collectors with taste-correlated holdings and acquisition dates.

    Each collector draws from three genres only, which leaves the remaining genres as genuine
    blind spots for ``get_blindspot_candidates``' shape to find. Acquisition dates are split so
    every collector has at least :data:`MIN_OBSERVED` items before the cut and
    :data:`MIN_HELD_OUT` after it, which is what makes the time split well defined for all of
    them rather than for the lucky ones.
    """
    releases = catalog["releases"]
    by_genre: dict[str, list[str]] = {genre: [] for genre in GENRES}
    for release in releases:
        for genre in release["genres"]:
            by_genre[genre].append(release["id"])

    collectors: list[dict[str, Any]] = []
    for index in range(1, COLLECTOR_COUNT + 1):
        taste = sorted(_sample(rng, list(GENRES), 3))
        pool = sorted({release_id for genre in taste for release_id in by_genre[genre]})
        wanted = _between(rng, MIN_COLLECTION, MAX_COLLECTION)
        size = min(wanted, len(pool))
        chosen = sorted(_sample(rng, pool, size))

        held_out = max(MIN_HELD_OUT, size // 4)
        observed = size - held_out
        if observed < MIN_OBSERVED:
            observed = min(size - MIN_HELD_OUT, MIN_OBSERVED)
            held_out = size - observed

        ordered = _shuffled(rng, chosen)
        collected = [
            {
                "release_id": release_id,
                "date_added": _date_between(rng, EARLIEST_ADDED, SPLIT_CUT - timedelta(days=1)).isoformat(),
            }
            for release_id in ordered[:observed]
        ]
        collected.extend(
            {
                "release_id": release_id,
                "date_added": _date_between(rng, SPLIT_CUT, LATEST_ADDED).isoformat(),
            }
            for release_id in ordered[observed:]
        )
        collected.sort(key=lambda item: (item["date_added"], item["release_id"]))

        outside = sorted(set(pool) - set(chosen))
        wants = sorted(_sample(rng, outside, min(len(outside), _between(rng, 0, 4))))

        collectors.append(
            {
                "id": f"u{index:03d}",
                "name": f"Collector {index:02d}",
                "taste_genres": taste,
                "collected": collected,
                "wants": wants,
            }
        )

    return {
        "generator_version": GENERATOR_VERSION,
        "seed": SEED,
        "split_cut": SPLIT_CUT.isoformat(),
        "collectors": collectors,
    }


# ── Emission ────────────────────────────────────────────────────────


def generate(seed: int = SEED) -> dict[str, dict[str, Any]]:
    """Generate both golden-set documents from one seeded stream."""
    rng = random.Random(seed)
    catalog = build_catalog(rng)
    collectors = build_collectors(rng, catalog)
    return {"catalog": catalog, "collectors": collectors}


def serialize(document: dict[str, Any]) -> str:
    """Render one document as the exact bytes committed under tests/fixtures/golden."""
    return json.dumps(document, indent=2, ensure_ascii=False, sort_keys=False) + "\n"


def write(directory: Path = GOLDEN_DIR, seed: int = SEED) -> list[Path]:
    """Write ``catalog.json`` and ``collectors.json`` into ``directory``."""
    directory.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name, document in generate(seed).items():
        path = directory / f"{name}.json"
        path.write_text(serialize(document), encoding="utf-8")
        written.append(path)
    return written


def main() -> None:
    """Regenerate the committed golden set."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=GOLDEN_DIR, help="directory to write the golden set into")
    parser.add_argument("--seed", type=int, default=SEED, help="generator seed (changing it re-rolls the whole set)")
    arguments = parser.parse_args()
    for path in write(arguments.out, arguments.seed):
        print(f"wrote {path}")


if __name__ == "__main__":
    main()
