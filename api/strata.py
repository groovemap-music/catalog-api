"""Discography strata: one artist's credits, masters, and releases folded into a node budget.

Concept A draws an artist's discography as three stacked planes: artist credits on top,
masters in the middle, and releases at the bottom, with x as the release year and color as
the caller's collector state. This module is the pure half of ``GET /api/explore/strata``.
The route reads the catalog and the caller's collector edges
(:mod:`api.queries.strata_queries`) and hands them here, where the payload rule from design
spikes gm-design-1h2l (Recommendation 2) and gm-design-on0x (Recommendation 7) folds them
into the per-plane budget. Nothing here touches a database, so these invariants are pinned
by unit tests rather than hoped for:

* every plane holds at most its budget of entries, nodes plus aggregates;
* every entry below the credit plane names a parent drawn on the plane above;
* every credit, master, and release of the root is drawn or held by exactly one aggregate,
  in every response; and
* paging a focus walks each of its members exactly once.

The ladder
----------

* **Credit:** the sorted set of release-level ``BY`` artists of a release. Every credit holds
  the root. The root's own credit comes first, then the rest by how many releases sit under
  them, and the tail folds into the ``c:other`` aggregate, which holds those credits whole:
  their masters and releases are counted there and not drawn below.
* **Master:** a release's ``DERIVED_FROM`` target. A master hangs under the credit of its
  original release, the earliest dated Discogs release (the graph does not carry Discogs'
  ``main_release``). A MusicBrainz-only release joined to a master hangs under that master.
* **Two lanes are part of the contract, not fallbacks.** Releases with no master form one
  ``no_master`` aggregate per drawn credit (``nm:<credit>``), which never folds into a master
  bucket. Releases with no year after the master-year fallback are *undated*: they carry
  ``"undated": true`` and ``"year": null``, aggregates count them, and the decade buckets
  name them ``undated``. Every node and aggregate counts its ``undated_releases``.

Folding
-------

Past the budget, masters fold into per-credit, per-decade buckets (``ma:<credit>:<decade>``),
then per-credit buckets (``ma:<credit>:all``). Masters that hold the caller's collector state
fold last. On the release plane each drawn master keeps its original and its stateful
releases and folds the rest into ``ra:m:<master>``; the no-master lane keeps its stateful
releases and folds the rest by decade (``ra:nm:<credit>:<decade>``), then whole
(``ra:nm:<credit>``). If that still does not fit, originals go, then stateful releases.

Focus cursors
-------------

Every aggregate's ``id`` is a focus cursor naming a fixed member set, independent of the
budget and of the view it was read from. Requesting ``focus=<id>&page=<n>`` draws page ``n``
of those members (8 per page, fewer only when the budget cannot hold 8) and holds the rest
in one aggregate that keeps the same id. Pages wrap. The other planes fold further to make
room, credits included, so the drawn members always have their parents drawn. An
aggregate's counts are the members of its set that are not drawn in that response.

Collector state
---------------

Present only for a signed-in caller. A Discogs release is ``none``, ``wanted``, ``owned``,
or ``wanted_owned`` (Discogs does not clear a want on purchase), with the collector's own
recorded ``date_added`` for each side. Every transition is observed from a Discogs sync;
GrooveMap authors none of them. A MusicBrainz-only release has no collector state at all,
so it carries ``"state": null`` and is left out of every ``states`` count.
"""

from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal


# Drill-down page size for pressings and for masters, from gm-design-on0x: the spaced
# icicle holds pages of up to 12 pressings and up to 8 masters, and 8 is the shared default.
PAGE_SIZE: Final = 8

# Upper bounds on a requested budget. The view draws legibly only up to 6/12/21
# (gm-design-on0x); the larger ceilings serve the table view and stay well inside the
# payload sizes gm-design-1h2l measured for a 500-entry budget.
MAX_CREDITS: Final = 48
MAX_MASTERS: Final = 500
MAX_RELEASES: Final = 500

# The largest root a strata read serves, counted as the root's BY edges. gm-design-1h2l
# section 5 timed the strata read on roots up to 134,051 releases (the predecessor graph's
# largest) and nothing above, so a larger root is refused rather than read at a size whose
# cost is unknown. Of the roots that spike sampled from GrooveMap's catalog, only rank 1, a
# placeholder credit at 1,356,055 releases, is refused; rank 2, at 87,683, is served.
MAX_ROOT_RELEASES: Final = 134_051

OTHER_CREDITS: Final = "c:other"
UNDATED: Final = "undated"
ALL: Final = "all"

Provider = Literal["discogs", "musicbrainz"]
Credit = tuple[str, ...]


class StrataError(ValueError):
    """A request the strata rule cannot answer as asked."""


class BudgetError(StrataError):
    """The budget is malformed or outside what the rule can guarantee."""


class FocusNotFoundError(StrataError):
    """The focus cursor names nothing in this root's discography."""


@dataclass(frozen=True, slots=True)
class Budget:
    """Most entries per plane, nodes plus aggregates: ``credits/masters/releases``.

    The floors are what makes every view fit, focused or not: the credit plane needs the
    root, one focused credit, and ``c:other``; each drawn credit may need a no-master lane
    and one master bucket; and the release plane needs one entry per lane plus a page.
    """

    credits: int
    masters: int
    releases: int

    @classmethod
    def parse(cls, text: str) -> Budget:
        parts = text.split("/")
        if len(parts) != 3 or not all(part.strip().isdigit() for part in parts):
            raise BudgetError("budget must be credits/masters/releases, for example 3/12/16")
        budget = cls(*(int(part) for part in parts))
        budget.validate()
        return budget

    def validate(self) -> None:
        if not 3 <= self.credits <= MAX_CREDITS:
            raise BudgetError(f"credits must be between 3 and {MAX_CREDITS}")
        if not 2 * self.credits <= self.masters <= MAX_MASTERS:
            raise BudgetError(f"masters must be between twice the credits and {MAX_MASTERS}")
        if not self.credits + 1 <= self.releases <= MAX_RELEASES:
            raise BudgetError(f"releases must be between credits + 1 and {MAX_RELEASES}")

    def as_dict(self) -> dict[str, int]:
        return {"credits": self.credits, "masters": self.masters, "releases": self.releases}


DEFAULT_BUDGET: Final = Budget(3, 12, 16)


@dataclass(frozen=True, slots=True)
class Release:
    """One release of the root, as the catalog read returns it.

    ``credit`` is the sorted ``BY`` set for a Discogs release and empty for a
    MusicBrainz-only release, which takes the credit of the master it joins. ``year`` is the
    release's own year; the master-year fallback is applied here, not by the caller.
    """

    id: str
    master_id: str | None
    year: int | None
    credit: Credit = ()
    provider: Provider = "discogs"
    title: str | None = None


@dataclass(frozen=True, slots=True)
class Catalog:
    """The catalog side of one root: everything a strata read needs except the caller."""

    root_id: str
    root_name: str | None
    releases: tuple[Release, ...]
    master_years: Mapping[str, int | None]


@dataclass(frozen=True, slots=True)
class Mark:
    """The caller's ``COLLECTED`` and ``WANTS`` edges on one Discogs release.

    ``owned_at`` is the earliest ``date_added`` over the caller's copies and ``wanted_at``
    the want's ``date_added``: the collector's own recorded time, kept by Discogs. Either can
    be ``None`` while the edge exists, because Discogs may omit the field.
    """

    owned: bool = False
    wanted: bool = False
    owned_at: str | None = None
    wanted_at: str | None = None

    @property
    def state(self) -> str:
        if self.owned and self.wanted:
            return "wanted_owned"
        if self.owned:
            return "owned"
        return "wanted" if self.wanted else "none"


def credit_id(credit: Credit) -> str:
    return "c:" + "+".join(credit)


def id_key(value: str) -> tuple[int, int, str]:
    """Order catalog ids numerically when they are numbers, else as text."""
    return (0, int(value), "") if value.isdigit() else (1, 0, value)


def decade(year: int | None) -> str:
    return UNDATED if year is None else f"{year // 10 * 10}s"


# Bumped whenever the cached form of a Catalog changes, so an entry written by an older
# build reads as a miss instead of as a wrong ladder.
CATALOG_FORMAT: Final = 1


def encode_catalog(catalog: Catalog) -> str:
    """The cached form of a catalog: compact JSON with each distinct credit stored once.

    A release row is ``[id, master_id, year, credit_index]``, plus ``provider`` and
    ``title`` for a MusicBrainz-only release. Most of a root's releases share a handful of
    credits, so the credit table keeps a 134,051-release root to a few megabytes.
    """
    credits: dict[Credit, int] = {}
    rows: list[list[Any]] = []
    for release in catalog.releases:
        row: list[Any] = [release.id, release.master_id, release.year, credits.setdefault(release.credit, len(credits))]
        if release.provider != "discogs" or release.title is not None:
            row += [release.provider, release.title]
        rows.append(row)
    return json.dumps(
        {
            "format": CATALOG_FORMAT,
            "root_id": catalog.root_id,
            "root_name": catalog.root_name,
            "credits": [list(credit) for credit in credits],
            "releases": rows,
            "master_years": dict(catalog.master_years),
        },
        separators=(",", ":"),
    )


def decode_catalog(text: str | bytes) -> Catalog | None:
    """The catalog :func:`encode_catalog` wrote, or ``None`` for an entry this build cannot read."""
    try:
        data = json.loads(text)
        if data["format"] != CATALOG_FORMAT:
            return None
        credits = [tuple(credit) for credit in data["credits"]]
        releases = tuple(Release(row[0], row[1], row[2], credits[row[3]], *row[4:]) for row in data["releases"])
        return Catalog(data["root_id"], data["root_name"], releases, data["master_years"])
    except ValueError, TypeError, KeyError, IndexError:
        return None


@dataclass(frozen=True, slots=True)
class _Item:
    """A release placed on the ladder: its effective year, credit, and collector state."""

    release: Release
    year: int | None
    year_source: str | None
    credit: Credit
    state: str | None
    mark: Mark | None

    @property
    def node_id(self) -> str:
        return f"r:{self.release.id}" if self.release.provider == "discogs" else f"mb:{self.release.id}"

    @property
    def stateful(self) -> bool:
        return self.state not in (None, "none")

    @property
    def sort_key(self) -> tuple[bool, int, int, tuple[int, int, str]]:
        # Undated first, matching the undated strip at the left edge of the year axis.
        return (self.year is not None, self.year or 0, self.release.provider != "discogs", id_key(self.release.id))


@dataclass(frozen=True, slots=True)
class _Focus:
    id: str
    level: int  # 0 credits, 1 masters, 2 releases
    members: tuple[Any, ...]
    credit: Credit | None
    master: str | None
    lane: Credit | None
    decade: str | None
    page: int
    page_size: int

    @property
    def pages(self) -> int:
        return math.ceil(len(self.members) / self.page_size)

    @property
    def page_members(self) -> tuple[Any, ...]:
        start = self.page * self.page_size
        return self.members[start : start + self.page_size]

    @property
    def rest(self) -> tuple[Any, ...]:
        start = self.page * self.page_size
        return self.members[:start] + self.members[start + self.page_size :]


@dataclass(slots=True)
class _MasterPlan:
    shown_credits: list[Credit]
    folded_credits: list[Credit]
    masters: list[str]
    buckets: dict[tuple[Credit, str], list[str]]
    remainder: list[str]
    lanes: list[Credit]


@dataclass(slots=True)
class _ReleaseAgg:
    id: str
    parent: str
    items: list[_Item]
    decade: str | None = None


def _span(items: Sequence[_Item]) -> tuple[int | None, int | None]:
    years = [item.year for item in items if item.year is not None]
    return (min(years), max(years)) if years else (None, None)


def _rollup(items: Sequence[_Item]) -> str:
    """The mockup's rule for a master or credit: owned if any release is, else wanted."""
    states = {item.state for item in items}
    if states & {"owned", "wanted_owned"}:
        return "owned"
    return "wanted" if "wanted" in states else "none"


def _page_size(members: int, *, without_rest: int, with_rest: int) -> int:
    """How many members one page draws, given the room left once the rest of the view folds.

    A focus that fits whole needs no remainder aggregate; one that does not gives up one
    slot to the aggregate holding everything off the page.
    """
    if members <= min(PAGE_SIZE, without_rest):
        return members
    return max(1, min(PAGE_SIZE, with_rest))


class _Ladder:
    """One root's discography indexed for folding. Built once per request."""

    def __init__(self, catalog: Catalog, marks: Mapping[str, Mark] | None) -> None:
        self.catalog = catalog
        self.root: Credit = (catalog.root_id,)
        self.signed_in = marks is not None
        discogs = [r for r in catalog.releases if r.provider == "discogs"]

        # A master hangs under the credit of its original: the earliest dated Discogs release.
        by_master: dict[str, list[Release]] = defaultdict(list)
        for release in discogs:
            if release.master_id is not None:
                by_master[release.master_id].append(release)
        self.anchor: dict[str, str] = {}
        master_credit: dict[str, Credit] = {}
        for master_id, releases in by_master.items():
            original = min(releases, key=lambda r: (r.year is None, r.year or 0, id_key(r.id)))
            self.anchor[master_id] = original.id
            master_credit[master_id] = original.credit

        items: list[_Item] = []
        for release in catalog.releases:
            own_master_year = catalog.master_years.get(release.master_id) if release.master_id else None
            if release.year is not None:
                year, source = release.year, "release"
            elif own_master_year is not None:
                year, source = own_master_year, "master"
            else:
                year, source = None, None
            if release.provider == "discogs":
                credit = release.credit
                mark = marks.get(release.id, Mark()) if marks is not None else None
                state = mark.state if mark is not None else None
            elif release.master_id in master_credit:
                credit, mark, state = master_credit[release.master_id], None, None
            else:
                continue  # a MusicBrainz-only release reaches the ladder only through a master
            items.append(_Item(release, year, source, credit, state, mark))

        self.items_by_master: dict[str, list[_Item]] = defaultdict(list)
        self.masterless: dict[Credit, list[_Item]] = defaultdict(list)
        for item in items:
            if item.release.master_id is not None:
                self.items_by_master[item.release.master_id].append(item)
            else:
                self.masterless[item.credit].append(item)
        for group in (*self.items_by_master.values(), *self.masterless.values()):
            group.sort(key=lambda item: item.sort_key)
        self.items = items

        self.master_credit = master_credit
        self.master_year: dict[str, int | None] = {}
        for master_id, group in self.items_by_master.items():
            own = catalog.master_years.get(master_id)
            self.master_year[master_id] = own if own is not None else _span(group)[0]
        self.masters_by_credit: dict[Credit, list[str]] = defaultdict(list)
        for master_id in self.items_by_master:
            self.masters_by_credit[master_credit[master_id]].append(master_id)
        for masters in self.masters_by_credit.values():
            masters.sort(key=lambda m: (self.master_year[m] is not None, self.master_year[m] or 0, id_key(m)))
        self.stateful_masters = {m for m, group in self.items_by_master.items() if any(item.stateful for item in group)}

        # A credit is on the ladder when something sits under it; the root always is.
        placed: Counter[Credit] = Counter()
        for credit, masters in self.masters_by_credit.items():
            placed[credit] += sum(len(self.items_by_master[m]) for m in masters)
        for credit, group in self.masterless.items():
            placed[credit] += len(group)
        others = sorted((c for c in placed if c != self.root), key=lambda c: (-placed[c], credit_id(c)))
        self.credit_order: list[Credit] = [self.root, *others]
        self.placed = placed

    # -- Credit-level helpers --------------------------------------------------------

    def credit_items(self, credit: Credit) -> list[_Item]:
        held = [item for m in self.masters_by_credit.get(credit, []) for item in self.items_by_master[m]]
        return held + self.masterless.get(credit, [])

    def _master_priority(self, master_id: str) -> tuple[bool, int, tuple[int, int, str]]:
        return (master_id not in self.stateful_masters, -len(self.items_by_master[master_id]), id_key(master_id))

    def _bucket(self, master_id: str, mode: str) -> tuple[Credit, str]:
        label = decade(self.master_year[master_id]) if mode == "decade" else ALL
        return (self.master_credit[master_id], label)

    # -- Focus -----------------------------------------------------------------------

    def resolve_focus(self, focus_id: str, page: int, budget: Budget) -> _Focus:
        """Turn a cursor into its member set and a page size that holds on every page.

        The page size depends only on the focus and the budget, never on the page index,
        so the pages partition the members.
        """
        parsed = self._parse_focus(focus_id)
        if parsed is None:
            raise FocusNotFoundError(f"focus '{focus_id}' names nothing under this artist")
        canonical, level, members, credit, master, lane, label = parsed
        if level == 0:
            size = _page_size(len(members), without_rest=budget.credits - 1, with_rest=budget.credits - 2)
        elif level == 1:
            assert credit is not None
            label_members = set(members)
            others_m = int(bool(self.masterless.get(credit)))
            others_m += int(any(m not in label_members for m in self.masters_by_credit.get(credit, [])))
            if credit != self.root:
                others_m += int(bool(self.masterless.get(self.root))) + int(bool(self.masters_by_credit.get(self.root)))
            lanes = sum(1 for c in {self.root, credit} if self.masterless.get(c))
            room = min(budget.masters - others_m, budget.releases - lanes)
            size = _page_size(len(members), without_rest=room, with_rest=min(budget.masters - others_m - 1, budget.releases - lanes))
        else:
            assert credit is not None
            lanes = 0
            for c in {self.root, credit}:
                lane_items = self.masterless.get(c, [])
                if c == lane:
                    lanes += int(len(lane_items) > len(members))
                else:
                    lanes += int(bool(lane_items))
            room = budget.releases - lanes
            size = _page_size(len(members), without_rest=room, with_rest=room - 1)
        pages = math.ceil(len(members) / size)
        return _Focus(canonical, level, tuple(members), credit, master, lane, label, page % pages, size)

    def _parse_focus(self, focus_id: str) -> tuple[str, int, list[Any], Credit | None, str | None, Credit | None, str | None] | None:
        if focus_id == OTHER_CREDITS:
            others = self.credit_order[1:]
            return (OTHER_CREDITS, 0, others, None, None, None, None) if others else None
        for prefix in ("ra:m:", "m:"):
            if focus_id.startswith(prefix):
                master_id = focus_id.removeprefix(prefix)
                if master_id not in self.items_by_master:
                    return None
                members = list(self.items_by_master[master_id])
                return (f"ra:m:{master_id}", 2, members, self.master_credit[master_id], master_id, None, None)
        for prefix in ("ra:nm:", "nm:"):
            if focus_id.startswith(prefix):
                credit, label = self._parse_credit(focus_id.removeprefix(prefix))
                lane_items = self.masterless.get(credit, []) if credit else []
                if label is not None:
                    lane_items = [item for item in lane_items if decade(item.year) == label]
                if not lane_items or credit is None:
                    return None
                canonical = f"ra:nm:{credit_id(credit)}" + (f":{label}" if label else "")
                return (canonical, 2, lane_items, credit, None, credit, label)
        if focus_id.startswith("ma:"):
            credit, label = self._parse_credit(focus_id.removeprefix("ma:"))
            if credit is None or label is None:
                return None
            masters = [m for m in self.masters_by_credit.get(credit, []) if label == ALL or self._bucket(m, "decade")[1] == label]
            if not masters:
                return None
            return (f"ma:{credit_id(credit)}:{label}", 1, masters, credit, None, None, label)
        return None

    @staticmethod
    def _parse_credit(text: str) -> tuple[Credit | None, str | None]:
        parts = text.split(":")
        if len(parts) not in (2, 3) or parts[0] != "c" or not parts[1]:
            return None, None
        credit = tuple(parts[1].split("+"))
        return credit, parts[2] if len(parts) == 3 else None

    # -- Fitting ---------------------------------------------------------------------

    def _credit_levels(self, budget: Budget, focus: _Focus | None) -> list[tuple[list[Credit], list[Credit]]]:
        """Credit planes from the finest to the coarsest the view can fall back to."""
        if focus is not None and focus.level == 0:
            return [([self.root, *focus.page_members], list(focus.rest))]
        base = [self.root]
        if focus is not None and focus.credit is not None and focus.credit != self.root:
            base.append(focus.credit)
        rest = [c for c in self.credit_order if c not in base]
        # Everything fits, or the tail folds into `c:other`, which takes one slot itself.
        most = len(rest) if len(base) + len(rest) <= budget.credits else budget.credits - len(base) - 1
        levels = []
        for extra in range(most, -1, -1):
            shown = set(base) | set(rest[:extra])
            levels.append(([c for c in self.credit_order if c in shown], rest[extra:]))
        return levels

    def _release_floor(self, pinned: Sequence[str], lanes: Sequence[Credit], focus: _Focus | None) -> int:
        """The fewest release-plane entries the drawn masters and lanes can take."""
        floor = 0
        for master_id in pinned:
            if focus is not None and focus.level == 2 and focus.master == master_id:
                floor += len(focus.page_members) + int(bool(focus.rest))
            else:
                floor += 1
        for lane in lanes:
            if focus is not None and focus.level == 2 and focus.lane == lane:
                floor += len(focus.page_members) + int(bool(focus.rest))
                floor += int(len(self.masterless[lane]) > len(focus.members))
            else:
                floor += 1
        return floor

    def _plan_masters(self, shown: list[Credit], folded: list[Credit], budget: Budget, focus: _Focus | None, mode: str) -> _MasterPlan | None:
        pinned: list[str] = []
        remainder: list[str] = []
        if focus is not None and focus.level == 1:
            pinned, remainder = list(focus.page_members), list(focus.rest)
        elif focus is not None and focus.master is not None:
            pinned = [focus.master]
        held = set(pinned) | set(remainder)
        candidates = sorted((m for c in shown for m in self.masters_by_credit.get(c, []) if m not in held), key=self._master_priority)
        lanes = [c for c in shown if self.masterless.get(c)]
        fixed_m = len(pinned) + int(bool(remainder)) + len(lanes)
        # Every kept master needs at least one release-plane entry, the same as a pinned one.
        fixed_r = self._release_floor(pinned, lanes, focus)
        keep = len(candidates)
        buckets: Counter[tuple[Credit, str]] = Counter()
        while keep > 0 and (fixed_m + keep + len(buckets) > budget.masters or fixed_r + keep > budget.releases):
            keep -= 1
            buckets[self._bucket(candidates[keep], mode)] += 1
        if fixed_m + keep + len(buckets) > budget.masters or fixed_r + keep > budget.releases:
            return None
        bucketed: dict[tuple[Credit, str], list[str]] = defaultdict(list)
        for master_id in candidates[keep:]:
            bucketed[self._bucket(master_id, mode)].append(master_id)
        return _MasterPlan(shown, folded, pinned + candidates[:keep], dict(bucketed), remainder, lanes)

    def fit(self, budget: Budget, focus: _Focus | None) -> _MasterPlan:
        for shown, folded in self._credit_levels(budget, focus):
            for mode in ("decade", ALL):
                plan = self._plan_masters(shown, folded, budget, focus, mode)
                if plan is not None:
                    return plan
        # Budget.validate's floors make the coarsest level always fit; reaching here is a bug.
        raise AssertionError("no strata view fits a validated budget")  # pragma: no cover

    def plan_releases(self, plan: _MasterPlan, budget: Budget, focus: _Focus | None) -> tuple[list[tuple[_Item, str]], list[_ReleaseAgg]]:
        """Release nodes and aggregates: the finest strategy that fits the plane."""
        fixed_nodes: list[tuple[_Item, str]] = []
        fixed_aggs: list[_ReleaseAgg] = []
        open_masters: list[str] = []
        open_lanes: list[tuple[Credit, list[_Item]]] = []
        for master_id in plan.masters:
            if focus is not None and focus.level == 2 and focus.master == master_id:
                fixed_nodes += [(item, f"m:{master_id}") for item in focus.page_members]
                if focus.rest:
                    fixed_aggs.append(_ReleaseAgg(focus.id, f"m:{master_id}", list(focus.rest)))
            else:
                open_masters.append(master_id)
        for lane in plan.lanes:
            parent = f"nm:{credit_id(lane)}"
            lane_items = self.masterless[lane]
            if focus is not None and focus.level == 2 and focus.lane == lane:
                fixed_nodes += [(item, parent) for item in focus.page_members]
                if focus.rest:
                    fixed_aggs.append(_ReleaseAgg(focus.id, parent, list(focus.rest), focus.decade))
                focused = set(focus.members)
                lane_items = [item for item in lane_items if item not in focused]
            if lane_items:
                open_lanes.append((lane, lane_items))

        room = budget.releases - len(fixed_nodes) - len(fixed_aggs)
        # (keep originals, keep stateful releases, fold a lane's rest by decade)
        strategies = ((True, True, True), (True, True, False), (False, True, False), (False, False, False))
        everything = [(item, f"m:{m}") for m in open_masters for item in self.items_by_master[m]]
        everything += [(item, f"nm:{credit_id(lane)}") for lane, lane_items in open_lanes for item in lane_items]
        if len(everything) <= room:
            return fixed_nodes + everything, fixed_aggs
        for keep_original, keep_stateful, by_decade in strategies:
            nodes, aggs = self._fold_releases(open_masters, open_lanes, keep_original=keep_original, keep_stateful=keep_stateful, by_decade=by_decade)
            if len(nodes) + len(aggs) <= room or not (keep_original or keep_stateful or by_decade):
                return fixed_nodes + nodes, fixed_aggs + aggs
        raise AssertionError("unreachable: the last strategy always returns")  # pragma: no cover

    def _fold_releases(
        self,
        masters: list[str],
        lanes: list[tuple[Credit, list[_Item]]],
        *,
        keep_original: bool,
        keep_stateful: bool,
        by_decade: bool,
    ) -> tuple[list[tuple[_Item, str]], list[_ReleaseAgg]]:
        nodes: list[tuple[_Item, str]] = []
        aggs: list[_ReleaseAgg] = []
        for master_id in masters:
            parent = f"m:{master_id}"
            kept: list[_Item] = []
            rest: list[_Item] = []
            for item in self.items_by_master[master_id]:
                original = keep_original and item.release.provider == "discogs" and item.release.id == self.anchor[master_id]
                (kept if original or (keep_stateful and item.stateful) else rest).append(item)
            nodes += [(item, parent) for item in kept]
            if rest:
                aggs.append(_ReleaseAgg(f"ra:m:{master_id}", parent, rest))
        for lane, lane_items in lanes:
            parent = f"nm:{credit_id(lane)}"
            kept = [item for item in lane_items if keep_stateful and item.stateful]
            rest = [item for item in lane_items if not (keep_stateful and item.stateful)]
            nodes += [(item, parent) for item in kept]
            if not rest:
                continue
            if by_decade:
                groups: dict[str, list[_Item]] = defaultdict(list)
                for item in rest:
                    groups[decade(item.year)].append(item)
                aggs += [_ReleaseAgg(f"ra:nm:{credit_id(lane)}:{label}", parent, group, label) for label, group in groups.items()]
            else:
                aggs.append(_ReleaseAgg(f"ra:nm:{credit_id(lane)}", parent, rest))
        return nodes, aggs

    # -- Rendering -------------------------------------------------------------------

    def _counts(self, items: Sequence[_Item]) -> dict[str, Any]:
        y0, y1 = _span(items)
        counts: dict[str, Any] = {
            "releases": len(items),
            "undated_releases": sum(1 for item in items if item.year is None),
            "musicbrainz_releases": sum(1 for item in items if item.release.provider == "musicbrainz"),
            "y0": y0,
            "y1": y1,
        }
        if self.signed_in:
            counts["states"] = dict(Counter(item.state for item in items if item.state is not None))
        return counts

    def _release_node(self, item: _Item, parent: str) -> dict[str, Any]:
        node: dict[str, Any] = {
            "id": item.node_id,
            "parent": parent,
            "provider": item.release.provider,
            "release_id": item.release.id,
            "title": item.release.title,
            "year": item.year,
            "year_source": item.year_source,
            "undated": item.year is None,
        }
        if self.signed_in:
            node["state"] = item.state
            node["recorded_at"] = None if item.mark is None else {"owned": item.mark.owned_at, "wanted": item.mark.wanted_at}
        return node

    def render(self, budget: Budget, focus: _Focus | None) -> dict[str, Any]:
        plan = self.fit(budget, focus)
        release_nodes, release_aggs = self.plan_releases(plan, budget, focus)

        credit_nodes = []
        for credit in plan.shown_credits:
            items = self.credit_items(credit)
            node: dict[str, Any] = {
                "id": credit_id(credit),
                "artists": [{"id": artist_id, "name": None} for artist_id in credit],
                "masters": len(self.masters_by_credit.get(credit, [])),
                "masterless": len(self.masterless.get(credit, [])),
                **self._counts(items),
            }
            if self.signed_in:
                node["state"] = _rollup(items)
            credit_nodes.append(node)
        credit_aggs = []
        if plan.folded_credits:
            items = [item for c in plan.folded_credits for item in self.credit_items(c)]
            credit_aggs.append(
                {
                    "id": OTHER_CREDITS,
                    "credits": len(plan.folded_credits),
                    "masters": sum(len(self.masters_by_credit.get(c, [])) for c in plan.folded_credits),
                    **self._counts(items),
                }
            )

        credit_rank = {c: i for i, c in enumerate(self.credit_order)}
        master_nodes = []
        for master_id in sorted(
            plan.masters,
            key=lambda m: (credit_rank[self.master_credit[m]], self.master_year[m] is None, self.master_year[m] or 0, id_key(m)),
        ):
            items = self.items_by_master[master_id]
            own_year = self.catalog.master_years.get(master_id)
            master_node: dict[str, Any] = {
                "id": f"m:{master_id}",
                "parent": credit_id(self.master_credit[master_id]),
                "master_id": master_id,
                "title": None,
                "year": self.master_year[master_id],
                "year_source": "master" if own_year is not None else ("releases" if self.master_year[master_id] is not None else None),
                "undated": self.master_year[master_id] is None,
                **self._counts(items),
            }
            if self.signed_in:
                master_node["state"] = _rollup(items)
            master_nodes.append(master_node)

        def bucket(credit: Credit, label: str, masters: Sequence[str]) -> dict[str, Any]:
            items = [item for m in masters for item in self.items_by_master[m]]
            return {
                "id": f"ma:{credit_id(credit)}:{label}",
                "parent": credit_id(credit),
                "kind": "masters",
                "decade": None if label == ALL else label,
                "masters": len(masters),
                **self._counts(items),
            }

        master_aggs: list[dict[str, Any]] = [
            {
                "id": f"nm:{credit_id(lane)}",
                "parent": credit_id(lane),
                "kind": "no_master",
                "decade": None,
                "masters": 0,
                **self._counts(self.masterless[lane]),
            }
            for lane in plan.lanes
        ]
        for (credit, label), masters in sorted(plan.buckets.items(), key=lambda kv: (credit_rank[kv[0][0]], kv[0][1] == UNDATED, kv[0][1])):
            master_aggs.append(bucket(credit, label, masters))
        if plan.remainder:
            # The masters off the focused page, under the focus's own id so the next page is
            # one more request for the same cursor.
            assert focus is not None and focus.credit is not None and focus.decade is not None
            master_aggs.append(bucket(focus.credit, focus.decade, plan.remainder))

        aggregates_out = []
        for agg in release_aggs:
            entry: dict[str, Any] = {"id": agg.id, "parent": agg.parent}
            if agg.decade is not None:
                entry["decade"] = agg.decade
            aggregates_out.append(entry | self._counts(agg.items))

        musicbrainz = sum(1 for item in self.items if item.release.provider == "musicbrainz")
        payload: dict[str, Any] = {
            "root": {"type": "artist", "id": self.catalog.root_id, "name": self.catalog.root_name},
            "budget": budget.as_dict(),
            "focus": None,
            "signed_in": self.signed_in,
            "totals": {
                "credits": len(self.credit_order),
                "masters": len(self.items_by_master),
                "releases": len(self.items),
                "masterless_releases": sum(len(group) for group in self.masterless.values()),
                "undated_releases": sum(1 for item in self.items if item.year is None),
                "musicbrainz_releases": musicbrainz,
            },
            "planes": [
                {"level": 0, "kind": "credit", "nodes": credit_nodes, "aggregates": credit_aggs},
                {"level": 1, "kind": "master", "nodes": master_nodes, "aggregates": master_aggs, "edges": []},
                {
                    "level": 2,
                    "kind": "release",
                    "nodes": [self._release_node(item, parent) for item, parent in release_nodes],
                    "aggregates": aggregates_out,
                },
            ],
        }
        if focus is not None:
            payload["focus"] = {
                "id": focus.id,
                "plane": ("credit", "master", "release")[focus.level],
                "page": focus.page,
                "pages": focus.pages,
                "page_size": focus.page_size,
                "members": len(focus.members),
            }
        return payload


def build_strata(
    catalog: Catalog,
    budget: Budget = DEFAULT_BUDGET,
    *,
    focus: str | None = None,
    page: int = 0,
    marks: Mapping[str, Mark] | None = None,
) -> dict[str, Any]:
    """Fold one root's discography into the three planes of a strata response.

    ``marks`` is ``None`` for an anonymous caller, and then no collector field appears
    anywhere in the payload. A signed-in caller with no marks passes an empty mapping. Names
    and titles the catalog read did not carry are ``None`` here; :func:`apply_labels` fills
    them for the entries that were drawn.

    Raises:
        FocusNotFoundError: ``focus`` names nothing under this root.
    """
    ladder = _Ladder(catalog, marks)
    resolved = ladder.resolve_focus(focus, page, budget) if focus else None
    return ladder.render(budget, resolved)


def label_targets(payload: Mapping[str, Any]) -> tuple[list[str], list[str], list[str]]:
    """The drawn Discogs release ids, master ids, and artist ids that still need a label."""
    credit_plane, master_plane, release_plane = payload["planes"]
    artists = {artist["id"] for node in credit_plane["nodes"] for artist in node["artists"]}
    artists.add(payload["root"]["id"])
    masters = [node["master_id"] for node in master_plane["nodes"]]
    releases = [node["release_id"] for node in release_plane["nodes"] if node["provider"] == "discogs" and node["title"] is None]
    return releases, masters, sorted(artists, key=id_key)


def apply_labels(
    payload: dict[str, Any],
    *,
    release_titles: Mapping[str, str | None],
    master_titles: Mapping[str, str | None],
    artist_names: Mapping[str, str | None],
    edges: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Fill drawn names and titles, and the shared-personnel edges between drawn masters."""
    credit_plane, master_plane, release_plane = payload["planes"]
    if payload["root"]["name"] is None:
        payload["root"]["name"] = artist_names.get(payload["root"]["id"])
    for node in credit_plane["nodes"]:
        for artist in node["artists"]:
            artist["name"] = artist_names.get(artist["id"])
    for node in master_plane["nodes"]:
        node["title"] = master_titles.get(node["master_id"])
    for node in release_plane["nodes"]:
        if node["title"] is None:
            node["title"] = release_titles.get(node["release_id"])
    drawn = {node["master_id"] for node in master_plane["nodes"]}
    master_plane["edges"] = [
        {
            "source": f"m:{edge['source']}",
            "target": f"m:{edge['target']}",
            "kind": "shared_personnel",
            "categories": list(edge["categories"]),
            "people": edge["people"],
        }
        for edge in edges
        if edge["source"] in drawn and edge["target"] in drawn
    ]
    return payload
