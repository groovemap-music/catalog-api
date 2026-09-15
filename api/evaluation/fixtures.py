"""Loading and typed access to the committed synthetic golden set.

The golden set lives as JSON under ``tests/fixtures/golden`` and is produced by
``scripts/generate_golden_set.py``. It is committed because it is the fixture the frozen
baseline and every later model are scored on: a fixture that is regenerated per run is not a
comparison point. It is synthetic and deterministic, so committing it leaks nothing and
``tests/test_evaluation_golden_set.py`` can assert byte-identical regeneration.

Nothing in this package touches a driver, a pool, or a router. The whole evaluation harness
runs offline inside ``just check``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any, Final

from common.media import families_of


# Repo-relative, because the golden set is test data rather than shipped package data. An
# installed wheel never runs the harness, so resolving this lazily (only inside
# :func:`load_golden_set`) keeps ``import api.evaluation`` working from a wheel.
GOLDEN_DIR: Final[Path] = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "golden"

CATALOG_FILE: Final[str] = "catalog.json"
COLLECTORS_FILE: Final[str] = "collectors.json"
EXPECTED_METRICS_FILE: Final[str] = "expected-metrics.json"


@dataclass(frozen=True)
class Label:
    """A label and its global catalog size, the input to ``compute_label_catalog_score``."""

    id: str
    name: str
    release_count: int


@dataclass(frozen=True)
class Artist:
    """An artist, with the genre and styles their releases are drawn from."""

    id: str
    name: str
    primary_genre: str
    styles: tuple[str, ...]


@dataclass(frozen=True)
class Master:
    """A master: the work several sibling pressings derive from."""

    id: str
    title: str
    year: int


@dataclass(frozen=True)
class Release:
    """One release, with the canonical media block the vendored taxonomy produced for it."""

    id: str
    title: str
    artist_ids: tuple[str, ...]
    label_id: str
    master_id: str | None
    year: int
    genres: tuple[str, ...]
    styles: tuple[str, ...]
    medium: str
    media: Mapping[str, Any]
    have_count: int
    want_count: int

    @property
    def families(self) -> tuple[str, ...]:
        """The taxonomy families this release's media resolve to."""
        return tuple(families_of(self.media))


@dataclass(frozen=True)
class Acquisition:
    """One ``COLLECTED`` edge: a release and the date it entered the collection."""

    release_id: str
    date_added: date


@dataclass(frozen=True)
class Collector:
    """A synthetic collector: dated acquisitions plus a small wantlist."""

    id: str
    name: str
    taste_genres: tuple[str, ...]
    collected: tuple[Acquisition, ...]
    wants: frozenset[str]

    @property
    def release_ids(self) -> tuple[str, ...]:
        """Every collected release id, in acquisition order."""
        return tuple(item.release_id for item in self.collected)


@dataclass(frozen=True)
class GoldenSet:
    """The whole fixture: the catalog and the collectors that hold parts of it."""

    generator_version: str
    seed: int
    split_cut: date
    labels: Mapping[str, Label]
    artists: Mapping[str, Artist]
    masters: Mapping[str, Master]
    releases: Mapping[str, Release]
    collectors: Mapping[str, Collector]

    @property
    def release_ids(self) -> tuple[str, ...]:
        """Every release id, sorted — the order the rarity pass walks."""
        return tuple(sorted(self.releases))

    @property
    def collector_ids(self) -> tuple[str, ...]:
        """Every collector id, sorted."""
        return tuple(sorted(self.collectors))

    def family_counts(self) -> dict[str, int]:
        """Return the number of releases covering each taxonomy family."""
        counts: dict[str, int] = {}
        for release in self.releases.values():
            for family in release.families:
                counts[family] = counts.get(family, 0) + 1
        return counts


def _parse_catalog(document: Mapping[str, Any]) -> tuple[dict[str, Label], dict[str, Artist], dict[str, Master], dict[str, Release]]:
    labels = {row["id"]: Label(id=row["id"], name=row["name"], release_count=row["release_count"]) for row in document["labels"]}
    artists = {
        row["id"]: Artist(id=row["id"], name=row["name"], primary_genre=row["primary_genre"], styles=tuple(row["styles"]))
        for row in document["artists"]
    }
    masters = {row["id"]: Master(id=row["id"], title=row["title"], year=row["year"]) for row in document["masters"]}
    releases = {
        row["id"]: Release(
            id=row["id"],
            title=row["title"],
            artist_ids=tuple(row["artist_ids"]),
            label_id=row["label_id"],
            master_id=row["master_id"],
            year=row["year"],
            genres=tuple(row["genres"]),
            styles=tuple(row["styles"]),
            medium=row["medium"],
            media=row["media"],
            have_count=row["have_count"],
            want_count=row["want_count"],
        )
        for row in document["releases"]
    }
    return labels, artists, masters, releases


def _parse_collectors(document: Mapping[str, Any]) -> dict[str, Collector]:
    return {
        row["id"]: Collector(
            id=row["id"],
            name=row["name"],
            taste_genres=tuple(row["taste_genres"]),
            collected=tuple(
                Acquisition(release_id=item["release_id"], date_added=date.fromisoformat(item["date_added"])) for item in row["collected"]
            ),
            wants=frozenset(row["wants"]),
        )
        for row in document["collectors"]
    }


def parse_golden_set(catalog: Mapping[str, Any], collectors: Mapping[str, Any]) -> GoldenSet:
    """Build a :class:`GoldenSet` from the two parsed JSON documents."""
    labels, artists, masters, releases = _parse_catalog(catalog)
    return GoldenSet(
        generator_version=catalog["generator_version"],
        seed=catalog["seed"],
        split_cut=date.fromisoformat(collectors["split_cut"]),
        labels=labels,
        artists=artists,
        masters=masters,
        releases=releases,
        collectors=_parse_collectors(collectors),
    )


@lru_cache(maxsize=4)
def load_golden_set(directory: Path = GOLDEN_DIR) -> GoldenSet:
    """Load and parse the committed golden set from ``directory``.

    Cached, because every consumer in one run wants the same immutable fixture and parsing it
    twice is pure cost.

    Args:
        directory: The directory holding ``catalog.json`` and ``collectors.json``.

    Returns:
        The parsed fixture.
    """
    catalog = json.loads((directory / CATALOG_FILE).read_text(encoding="utf-8"))
    collectors = json.loads((directory / COLLECTORS_FILE).read_text(encoding="utf-8"))
    return parse_golden_set(catalog, collectors)
