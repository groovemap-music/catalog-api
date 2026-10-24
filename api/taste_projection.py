"""The collector's taste projection for concept C' (Taste Strata): the caller's items folded to atlas ids.

``GET /api/user/taste-projection`` answers one question for graph-explorer: where on the
atlas do this collector's items land, how many, in which collector state, and since when.
The rule follows ADR 0016 sections 4 to 6 and design spike gm-design-4804 (C1, Recommendation
for C 1, 2, 4, and 5):

* An item is a Discogs release the caller ``COLLECTED`` or ``WANTS``. A release in both is one
  item, ``wanted_owned``, as in :class:`api.strata.Mark`.
* An item lands on every genre (L0) and every style (L1) it carries, never at a centroid. It
  is counted once per node it carries, so a genre's or style's ``items`` do not sum to the
  total. An item with no style is genre-only and lands on L0 alone.
* An item with a master lands on L2 under that master. The position inside the master's
  dominant style cell is graph-explorer's to compute with the atlas layout, so each master
  carries its ``year``, ``genres``, and ``styles``. An item without a master is counted in
  ``totals.masterless_items`` and gets no L2 entry: it belongs to concept A's "no master"
  container, not to a position invented here.
* Transaction time is the collector's recorded ``date_added``: for each state, the earliest
  and latest time Discogs recorded, and how many edges Discogs gave none for. Spans compare
  instants, not strings, because Discogs times carry UTC offsets.

The read is the caller's alone and the response is personal data. The route marks it
``no-store``, nothing here is cached or keyed across users, and none of it may enter a
shareable snapshot (ADR 0016 section 6).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from api.strata import id_key


@dataclass(frozen=True, slots=True)
class HeldItem:
    """One release the caller holds, as the read returns it.

    ``owned_at`` and ``wanted_at`` hold every recorded ``date_added`` of that state's edges
    (a release owned in several copies has one edge per copy); an edge Discogs gave no time
    for contributes nothing, and ``owned_edges`` and ``wanted_edges`` count all of them.
    """

    release_id: str
    owned_edges: int
    wanted_edges: int
    owned_at: Sequence[str] = ()
    wanted_at: Sequence[str] = ()
    master_id: str | None = None
    master_year: int | None = None
    genres: Sequence[str] = ()
    styles: Sequence[str] = ()

    @property
    def owned(self) -> bool:
        return self.owned_edges > 0

    @property
    def wanted(self) -> bool:
        return self.wanted_edges > 0


def instant(value: str) -> datetime | None:
    """A recorded time as an aware instant, or ``None`` when it is not ISO 8601."""
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


@dataclass(slots=True)
class _Span:
    """The earliest and latest recorded time of one state, and the edges with none."""

    first: tuple[datetime, str] | None = None
    last: tuple[datetime, str] | None = None
    undated: int = 0

    def add(self, recorded: Iterable[str], edges: int) -> None:
        seen = 0
        for text in recorded:
            when = instant(text)
            if when is None:
                continue
            seen += 1
            if self.first is None or when < self.first[0]:
                self.first = (when, text)
            if self.last is None or when > self.last[0]:
                self.last = (when, text)
        self.undated += max(edges - seen, 0)

    def as_dict(self) -> dict[str, Any]:
        return {"first": self.first[1] if self.first else None, "last": self.last[1] if self.last else None, "undated": self.undated}


@dataclass(slots=True)
class _Bucket:
    """The items that land on one atlas node."""

    owned: int = 0
    wanted: int = 0
    items: int = 0
    both: int = 0
    owned_span: _Span = field(default_factory=_Span)
    wanted_span: _Span = field(default_factory=_Span)
    year: int | None = None
    genres: set[str] = field(default_factory=set)
    styles: set[str] = field(default_factory=set)

    def add(self, item: HeldItem) -> None:
        self.items += 1
        if item.owned:
            self.owned += 1
            self.owned_span.add(item.owned_at, item.owned_edges)
        if item.wanted:
            self.wanted += 1
            self.wanted_span.add(item.wanted_at, item.wanted_edges)
        if item.owned and item.wanted:
            self.both += 1

    @property
    def state(self) -> str:
        """Rolled up from the items, as masters and credits roll up in the strata planes."""
        if self.owned and self.wanted:
            return "wanted_owned"
        return "owned" if self.owned else "wanted"

    def counts(self) -> dict[str, Any]:
        return {
            "items": self.items,
            "owned": self.owned,
            "wanted": self.wanted,
            "state": self.state,
            "recorded_at": {
                "owned": self.owned_span.as_dict() if self.owned else None,
                "wanted": self.wanted_span.as_dict() if self.wanted else None,
            },
        }


def _ranked(buckets: Mapping[str, _Bucket]) -> list[dict[str, Any]]:
    """Genre or style entries, the most held first, then by name so the order is deterministic."""
    return [{"id": name, **bucket.counts()} for name, bucket in sorted(buckets.items(), key=lambda pair: (-pair[1].items, pair[0]))]


def build_taste_projection(items: Iterable[HeldItem]) -> dict[str, Any]:
    """Fold the caller's held releases into genre, style, and master aggregates."""
    genres: dict[str, _Bucket] = {}
    styles: dict[str, _Bucket] = {}
    masters: dict[str, _Bucket] = {}
    total = _Bucket()
    masterless = genre_only = no_genre = 0

    for item in items:
        total.add(item)
        # A release can carry the same genre or style twice only if the read repeated it.
        for name in set(item.genres):
            genres.setdefault(name, _Bucket()).add(item)
        for name in set(item.styles):
            styles.setdefault(name, _Bucket()).add(item)
        no_genre += not item.genres
        genre_only += bool(item.genres) and not item.styles
        if item.master_id is None:
            masterless += 1
            continue
        master = masters.setdefault(item.master_id, _Bucket())
        master.add(item)
        master.year = item.master_year if item.master_year is not None else master.year
        master.genres.update(item.genres)
        master.styles.update(item.styles)

    return {
        "totals": {
            "items": total.items,
            "owned": total.owned,
            "wanted": total.wanted,
            "wanted_owned": total.both,
            "masterless_items": masterless,
            "genre_only_items": genre_only,
            "unplaced_items": no_genre,
        },
        "recorded_at": {
            "owned": total.owned_span.as_dict() if total.owned else None,
            "wanted": total.wanted_span.as_dict() if total.wanted else None,
        },
        "genres": _ranked(genres),
        "styles": _ranked(styles),
        "masters": [
            {"id": master_id, "year": bucket.year, "genres": sorted(bucket.genres), "styles": sorted(bucket.styles), **bucket.counts()}
            for master_id, bucket in sorted(masters.items(), key=lambda pair: id_key(pair[0]))
        ],
    }
