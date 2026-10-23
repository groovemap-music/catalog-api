"""The strata folding rule (api/strata.py): budget, conservation, lanes, paging, and state."""

from __future__ import annotations

import json
import random
from collections.abc import Iterator
from typing import Any

import pytest

from api.strata import (
    DEFAULT_BUDGET,
    MAX_CREDITS,
    MAX_MASTERS,
    MAX_RELEASES,
    PAGE_SIZE,
    Budget,
    BudgetError,
    Catalog,
    FocusNotFoundError,
    Mark,
    Release,
    apply_labels,
    build_strata,
    decade,
    decode_catalog,
    encode_catalog,
    label_targets,
)


ROOT = "1"


def _walk(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _entries(payload: dict[str, Any], level: int) -> list[dict[str, Any]]:
    plane = payload["planes"][level]
    return plane["nodes"] + plane["aggregates"]


def assert_invariants(payload: dict[str, Any], budget: Budget) -> None:
    """The rule's guarantees, checked on one response."""
    credits, masters, releases = payload["planes"]
    totals = payload["totals"]

    # Each plane holds at most its budget of entries.
    assert len(_entries(payload, 0)) <= budget.credits
    assert len(_entries(payload, 1)) <= budget.masters
    assert len(_entries(payload, 2)) <= budget.releases

    # Ids are unique, and every entry below the credit plane names a parent drawn above.
    ids = [entry["id"] for level in range(3) for entry in _entries(payload, level)]
    assert len(ids) == len(set(ids))
    credit_ids = {node["id"] for node in credits["nodes"]}
    assert all(entry["parent"] in credit_ids for entry in _entries(payload, 1))
    master_parents = {node["id"] for node in masters["nodes"]} | {agg["id"] for agg in masters["aggregates"] if agg["kind"] == "no_master"}
    assert all(entry["parent"] in master_parents for entry in _entries(payload, 2))

    # Credits: each is drawn or held by c:other, once.
    other = credits["aggregates"][0] if credits["aggregates"] else {"credits": 0, "masters": 0, "releases": 0}
    assert len(credits["nodes"]) + other["credits"] == totals["credits"]

    # Releases: every release sits under exactly one credit entry.
    assert sum(entry["releases"] for entry in _entries(payload, 0)) == totals["releases"]
    placed = totals["releases"] - other["releases"]
    assert sum(entry["releases"] for entry in _entries(payload, 1)) == placed

    # Masters: drawn, in a master bucket, or inside c:other.
    buckets = [agg for agg in masters["aggregates"] if agg["kind"] == "masters"]
    assert len(masters["nodes"]) + sum(agg["masters"] for agg in buckets) + other["masters"] == totals["masters"]

    # The release plane holds what the drawn masters and lanes hold, nothing more or less.
    held_by_buckets = sum(agg["releases"] for agg in buckets)
    assert len(releases["nodes"]) + sum(agg["releases"] for agg in releases["aggregates"]) == placed - held_by_buckets
    under = dict.fromkeys(master_parents, 0)
    for node in releases["nodes"]:
        under[node["parent"]] += 1
    for agg in releases["aggregates"]:
        under[agg["parent"]] += agg["releases"]
    for entry in masters["nodes"] + [agg for agg in masters["aggregates"] if agg["kind"] == "no_master"]:
        assert under[entry["id"]] == entry["releases"], entry["id"]

    # The no-master lane never folds into a master bucket.
    lanes = [agg for agg in masters["aggregates"] if agg["kind"] == "no_master"]
    assert sum(agg["releases"] for agg in lanes) + sum(agg["releases"] for agg in buckets) + sum(n["releases"] for n in masters["nodes"]) == placed
    assert all(agg["masters"] == 0 for agg in lanes)


def random_catalog(seed: int, *, masters: int, masterless: int, collaborators: int = 4, musicbrainz: int = 0) -> tuple[Catalog, dict[str, Mark]]:
    rnd = random.Random(seed)  # noqa: S311 — seeded fixture shapes, not a secret
    releases: list[Release] = []
    master_years: dict[str, int | None] = {}
    next_id = 100

    def credit() -> tuple[str, ...]:
        if rnd.random() < 0.6:
            return (ROOT,)
        return (ROOT, str(rnd.randint(2, 1 + collaborators)))

    def year() -> int | None:
        return None if rnd.random() < 0.15 else rnd.randint(1955, 2025)

    for index in range(masters):
        master_id = str(5000 + index)
        master_years[master_id] = year()
        owner = credit()
        for _ in range(rnd.randint(1, 25)):
            releases.append(Release(str(next_id), master_id, year(), owner if rnd.random() < 0.8 else credit()))
            next_id += 1
    for _ in range(masterless):
        releases.append(Release(str(next_id), None, year(), credit()))
        next_id += 1
    for index in range(musicbrainz):
        if master_years:
            releases.append(Release(f"mb-{index}", rnd.choice(sorted(master_years)), year(), (), "musicbrainz", f"MB {index}"))
    marks = {
        release.id: Mark(
            owned=rnd.random() < 0.5, wanted=rnd.random() < 0.5, owned_at="2020-02-02T10:00:00-08:00", wanted_at="2021-03-03T10:00:00-08:00"
        )
        for release in releases
        if release.provider == "discogs" and rnd.random() < 0.08
    }
    return Catalog(ROOT, "Root", tuple(releases), master_years), marks


SHAPES = [
    {"masters": 0, "masterless": 0},
    {"masters": 0, "masterless": 7},
    {"masters": 3, "masterless": 2},
    {"masters": 12, "masterless": 30},
    {"masters": 60, "masterless": 120, "collaborators": 12, "musicbrainz": 15},
    {"masters": 200, "masterless": 40, "collaborators": 30, "musicbrainz": 40},
]
BUDGETS = [DEFAULT_BUDGET, Budget(6, 12, 16), Budget(3, 6, 4), Budget(5, 10, 6), Budget(48, 500, 500)]


class TestBudget:
    def test_default_is_the_measured_3_12_16(self) -> None:
        assert DEFAULT_BUDGET.as_dict() == {"credits": 3, "masters": 12, "releases": 16}
        assert Budget.parse("3/12/16") == DEFAULT_BUDGET
        assert Budget.parse("6/12/16") == Budget(6, 12, 16)

    @pytest.mark.parametrize("text", ["", "3/12", "3/12/16/1", "a/b/c", "3/-1/16", "3/12/"])
    def test_malformed_budgets_are_refused(self, text: str) -> None:
        with pytest.raises(BudgetError, match="credits/masters/releases"):
            Budget.parse(text)

    @pytest.mark.parametrize(
        ("text", "message"),
        [
            ("2/12/16", "credits"),
            (f"{MAX_CREDITS + 1}/500/500", "credits"),
            ("3/5/16", "masters"),
            (f"3/{MAX_MASTERS + 1}/16", "masters"),
            ("3/12/3", "releases"),
            (f"3/12/{MAX_RELEASES + 1}", "releases"),
        ],
    )
    def test_budgets_outside_the_guaranteed_floors_are_refused(self, text: str, message: str) -> None:
        with pytest.raises(BudgetError, match=message):
            Budget.parse(text)


class TestFolding:
    @pytest.mark.parametrize("shape", SHAPES)
    @pytest.mark.parametrize("budget", BUDGETS)
    @pytest.mark.parametrize("signed_in", [False, True])
    def test_every_view_holds_the_budget_and_conserves_every_node(self, shape: dict[str, int], budget: Budget, signed_in: bool) -> None:
        for seed in range(3):
            catalog, marks = random_catalog(seed, **shape)
            payload = build_strata(catalog, budget, marks=marks if signed_in else None)
            assert_invariants(payload, budget)
            assert payload["focus"] is None
            assert payload["budget"] == budget.as_dict()

    def test_a_small_discography_is_drawn_whole(self) -> None:
        catalog = Catalog(
            ROOT,
            "Root",
            (
                Release("10", "500", 1971, (ROOT,)),
                Release("11", "500", 1990, (ROOT,)),
                Release("12", "501", 1975, (ROOT, "2")),
                Release("13", None, 1980, (ROOT,)),
            ),
            {"500": 1971, "501": 1975},
        )
        payload = build_strata(catalog)
        credits, masters, releases = payload["planes"]
        assert [node["id"] for node in credits["nodes"]] == ["c:1", "c:1+2"]
        assert credits["aggregates"] == []
        assert [node["id"] for node in masters["nodes"]] == ["m:500", "m:501"]
        assert [agg["id"] for agg in masters["aggregates"]] == ["nm:c:1"]
        assert {node["id"] for node in releases["nodes"]} == {"r:10", "r:11", "r:12", "r:13"}
        assert releases["aggregates"] == []
        assert payload["totals"] == {
            "credits": 2,
            "masters": 2,
            "releases": 4,
            "masterless_releases": 1,
            "undated_releases": 0,
            "musicbrainz_releases": 0,
        }
        assert_invariants(payload, DEFAULT_BUDGET)

    def test_root_without_releases_still_draws_its_credit(self) -> None:
        payload = build_strata(Catalog(ROOT, "Root", (), {}))
        assert [node["id"] for node in payload["planes"][0]["nodes"]] == ["c:1"]
        assert payload["planes"][0]["nodes"][0]["releases"] == 0
        assert_invariants(payload, DEFAULT_BUDGET)

    def test_masters_fold_by_decade_and_keep_the_largest(self) -> None:
        releases = []
        for index in range(30):
            master_id = str(600 + index)
            releases += [Release(f"{master_id}{k}", master_id, 1960 + index, (ROOT,)) for k in range(1 + index % 4)]
        catalog = Catalog(ROOT, "Root", tuple(releases), {str(600 + i): 1960 + i for i in range(30)})
        payload = build_strata(catalog)
        masters = payload["planes"][1]
        decades = sorted(agg["decade"] for agg in masters["aggregates"])
        assert decades == ["1960s", "1970s", "1980s"]
        drawn = [node["releases"] for node in masters["nodes"]]
        assert drawn and min(drawn) >= max(agg["releases"] / agg["masters"] for agg in masters["aggregates"]) - 1
        assert all(agg["id"] == f"ma:c:1:{agg['decade']}" for agg in masters["aggregates"])
        assert_invariants(payload, DEFAULT_BUDGET)

    def test_masters_fold_per_credit_when_decades_overflow(self) -> None:
        catalog, _ = random_catalog(7, masters=120, masterless=0, collaborators=1)
        payload = build_strata(catalog, Budget(3, 6, 16))
        buckets = [agg for agg in payload["planes"][1]["aggregates"] if agg["kind"] == "masters"]
        assert buckets and all(agg["decade"] is None and agg["id"].endswith(":all") for agg in buckets)
        assert_invariants(payload, Budget(3, 6, 16))

    def test_stateful_masters_fold_last(self) -> None:
        releases = [Release(f"{600 + i}0", str(600 + i), 1960 + i, (ROOT,)) for i in range(40)]
        releases += [Release(f"{600 + i}1", str(600 + i), 1961 + i, (ROOT,)) for i in range(39)]
        catalog = Catalog(ROOT, "Root", tuple(releases), {str(600 + i): 1960 + i for i in range(40)})
        payload = build_strata(catalog, marks={"6390": Mark(owned=True, owned_at="2020-01-01")})
        drawn = {node["master_id"]: node for node in payload["planes"][1]["nodes"]}
        assert "639" in drawn  # one release only, but the caller owns it
        assert drawn["639"]["state"] == "owned"

    def test_credits_past_the_budget_fold_whole_into_other_credits(self) -> None:
        releases = [Release(str(100 + i), None, 1990, (ROOT, str(2 + i % 6))) for i in range(60)]
        releases.append(Release("99", "700", 1980, (ROOT, "7")))
        catalog = Catalog(ROOT, "Root", tuple(releases), {"700": 1980})
        payload = build_strata(catalog)
        credits = payload["planes"][0]
        assert credits["nodes"][0]["id"] == "c:1"
        assert len(credits["nodes"]) == 2
        other = credits["aggregates"][0]
        assert other["id"] == "c:other"
        assert other["credits"] == payload["totals"]["credits"] - 2
        # Nothing of a folded credit is drawn below it.
        drawn_parents = {entry["parent"] for entry in _entries(payload, 1)}
        assert drawn_parents <= {node["id"] for node in credits["nodes"]}
        assert_invariants(payload, DEFAULT_BUDGET)

    def test_a_master_hangs_under_the_credit_of_its_earliest_dated_release(self) -> None:
        catalog = Catalog(
            ROOT,
            "Root",
            (
                Release("10", "500", None, (ROOT,)),
                Release("11", "500", 1990, (ROOT,)),
                Release("12", "500", 1969, (ROOT, "2")),
            ),
            {"500": None},
        )
        payload = build_strata(catalog)
        assert payload["planes"][1]["nodes"][0]["parent"] == "c:1+2"
        assert payload["planes"][1]["nodes"][0]["year"] == 1969
        assert payload["planes"][1]["nodes"][0]["year_source"] == "releases"

    def test_release_plane_keeps_originals_and_stateful_releases_first(self) -> None:
        releases = [Release(str(1000 + k), "500", 1970 + k, (ROOT,)) for k in range(40)]
        catalog = Catalog(ROOT, "Root", tuple(releases), {"500": 1970})
        marks = {"1020": Mark(wanted=True, wanted_at="2024-01-01T00:00:00Z")}
        payload = build_strata(catalog, marks=marks)
        release_plane = payload["planes"][2]
        assert {node["release_id"] for node in release_plane["nodes"]} == {"1000", "1020"}
        assert release_plane["aggregates"][0]["id"] == "ra:m:500"
        assert release_plane["aggregates"][0]["releases"] == 38
        assert_invariants(payload, DEFAULT_BUDGET)

    def test_release_plane_drops_originals_then_stateful_releases_when_it_must(self) -> None:
        releases = [Release(f"{m}{k}", m, 1970, (ROOT,)) for m in ("500", "501", "502", "503") for k in range(10)]
        catalog = Catalog(ROOT, "Root", tuple(releases), dict.fromkeys(("500", "501", "502", "503"), 1970))
        marks = {f"{m}{k}": Mark(owned=True) for m in ("500", "501", "502", "503") for k in range(3)}
        budget = Budget(3, 6, 4)
        payload = build_strata(catalog, budget, marks=marks)
        assert payload["planes"][2]["nodes"] == []
        assert len(payload["planes"][2]["aggregates"]) == 4
        assert_invariants(payload, budget)


class TestLanes:
    def test_masterless_releases_form_one_no_master_lane_per_credit(self) -> None:
        catalog, _ = random_catalog(11, masters=200, masterless=60, collaborators=1)
        payload = build_strata(catalog)
        lanes = [agg for agg in payload["planes"][1]["aggregates"] if agg["kind"] == "no_master"]
        credits = {node["id"] for node in payload["planes"][0]["nodes"]}
        assert {lane["parent"] for lane in lanes} == credits
        assert {lane["id"] for lane in lanes} == {f"nm:{credit}" for credit in credits}
        assert sum(lane["releases"] for lane in lanes) == payload["totals"]["masterless_releases"]
        assert_invariants(payload, DEFAULT_BUDGET)

    def test_the_lane_survives_the_coarsest_master_fold(self) -> None:
        catalog, _ = random_catalog(12, masters=300, masterless=5, collaborators=2)
        budget = Budget(3, 6, 4)
        payload = build_strata(catalog, budget)
        kinds = [agg["kind"] for agg in payload["planes"][1]["aggregates"]]
        assert "no_master" in kinds
        assert_invariants(payload, budget)

    def test_undated_releases_take_the_master_year_first_and_then_the_undated_lane(self) -> None:
        catalog = Catalog(
            ROOT,
            "Root",
            (
                Release("10", "500", None, (ROOT,)),
                Release("11", "501", None, (ROOT,)),
                Release("12", None, None, (ROOT,)),
                Release("13", None, 1984, (ROOT,)),
            ),
            {"500": 1977, "501": None},
        )
        payload = build_strata(catalog)
        nodes = {node["release_id"]: node for node in payload["planes"][2]["nodes"]}
        assert (nodes["10"]["year"], nodes["10"]["year_source"], nodes["10"]["undated"]) == (1977, "master", False)
        assert (nodes["11"]["year"], nodes["11"]["year_source"], nodes["11"]["undated"]) == (None, None, True)
        assert (nodes["12"]["year"], nodes["12"]["undated"]) == (None, True)
        assert (nodes["13"]["year"], nodes["13"]["year_source"]) == (1984, "release")
        masters = {node["master_id"]: node for node in payload["planes"][1]["nodes"]}
        assert masters["501"]["undated"] is True and masters["501"]["year"] is None
        lane = payload["planes"][1]["aggregates"][0]
        assert (lane["undated_releases"], lane["y0"], lane["y1"]) == (1, 1984, 1984)
        assert payload["totals"]["undated_releases"] == 2

    def test_undated_masters_and_releases_fold_into_undated_buckets(self) -> None:
        releases = [Release(str(1000 + i), str(600 + i), None, (ROOT,)) for i in range(20)]
        releases += [Release(str(2000 + i), None, None if i % 2 else 1990 + i, (ROOT,)) for i in range(40)]
        catalog = Catalog(ROOT, "Root", tuple(releases), {str(600 + i): None for i in range(20)})
        payload = build_strata(catalog)
        assert "ma:c:1:undated" in {agg["id"] for agg in payload["planes"][1]["aggregates"]}
        assert "ra:nm:c:1:undated" in {agg["id"] for agg in payload["planes"][2]["aggregates"]}
        undated = next(agg for agg in payload["planes"][2]["aggregates"] if agg["id"] == "ra:nm:c:1:undated")
        assert undated["decade"] == "undated" and undated["undated_releases"] == undated["releases"] == 20
        assert_invariants(payload, DEFAULT_BUDGET)

    def test_decade_labels(self) -> None:
        assert decade(None) == "undated"
        assert decade(1969) == "1960s"
        assert decade(2000) == "2000s"


class TestMusicBrainzOnly:
    def test_joined_releases_hang_under_their_master_without_collector_state(self) -> None:
        catalog = Catalog(
            ROOT,
            "Root",
            (
                Release("10", "500", 1971, (ROOT, "2")),
                Release("aaaa-1", "500", 1972, (), "musicbrainz", "Reissue"),
                Release("aaaa-2", "999", 1972, (), "musicbrainz", "Elsewhere"),
            ),
            {"500": 1971},
        )
        payload = build_strata(catalog, marks={"10": Mark(owned=True, owned_at="2020-01-01")})
        nodes = {node["id"]: node for node in payload["planes"][2]["nodes"]}
        assert set(nodes) == {"r:10", "mb:aaaa-1"}
        mb = nodes["mb:aaaa-1"]
        assert (mb["provider"], mb["parent"], mb["title"], mb["state"], mb["recorded_at"]) == ("musicbrainz", "m:500", "Reissue", None, None)
        master = payload["planes"][1]["nodes"][0]
        assert (master["releases"], master["musicbrainz_releases"], master["states"]) == (2, 1, {"owned": 1})
        assert master["parent"] == "c:1+2"
        assert payload["totals"]["musicbrainz_releases"] == 1
        assert_invariants(payload, DEFAULT_BUDGET)

    def test_musicbrainz_releases_never_count_toward_states(self) -> None:
        catalog, marks = random_catalog(5, masters=40, masterless=10, musicbrainz=30)
        payload = build_strata(catalog, marks=marks)
        for entry in _walk(payload["planes"]):
            if "states" in entry:
                assert sum(entry["states"].values()) == entry["releases"] - entry["musicbrainz_releases"]


class TestCollectorState:
    def _catalog(self) -> Catalog:
        return Catalog(
            ROOT,
            "Root",
            (
                Release("10", "500", 1971, (ROOT,)),
                Release("11", "500", 1972, (ROOT,)),
                Release("12", "501", 1980, (ROOT,)),
                Release("13", None, 1990, (ROOT,)),
            ),
            {"500": 1971, "501": 1980},
        )

    def test_anonymous_callers_get_no_state_field_anywhere(self) -> None:
        payload = build_strata(self._catalog(), marks=None)
        assert payload["signed_in"] is False
        for entry in _walk(payload):
            assert not {"state", "states", "recorded_at"} & set(entry)

    def test_signed_in_callers_get_state_and_the_recorded_time(self) -> None:
        marks = {
            "10": Mark(owned=True, wanted=True, owned_at="2019-05-01T12:00:00-07:00", wanted_at="2018-01-01T00:00:00-08:00"),
            "12": Mark(wanted=True, wanted_at=None),
        }
        payload = build_strata(self._catalog(), marks=marks)
        assert payload["signed_in"] is True
        nodes = {node["release_id"]: node for node in payload["planes"][2]["nodes"]}
        assert nodes["10"]["state"] == "wanted_owned"
        assert nodes["10"]["recorded_at"] == {"owned": "2019-05-01T12:00:00-07:00", "wanted": "2018-01-01T00:00:00-08:00"}
        assert nodes["12"]["state"] == "wanted"
        assert nodes["12"]["recorded_at"] == {"owned": None, "wanted": None}
        assert nodes["11"]["state"] == "none"
        masters = {node["master_id"]: node for node in payload["planes"][1]["nodes"]}
        assert (masters["500"]["state"], masters["501"]["state"]) == ("owned", "wanted")
        assert masters["500"]["states"] == {"wanted_owned": 1, "none": 1}
        credit = payload["planes"][0]["nodes"][0]
        assert credit["state"] == "owned"
        assert credit["states"] == {"wanted_owned": 1, "none": 2, "wanted": 1}

    def test_signed_in_without_marks_is_all_none(self) -> None:
        payload = build_strata(self._catalog(), marks={})
        assert {node["state"] for node in payload["planes"][2]["nodes"]} == {"none"}
        assert payload["planes"][0]["nodes"][0]["state"] == "none"

    @pytest.mark.parametrize(
        ("mark", "state"),
        [(Mark(), "none"), (Mark(wanted=True), "wanted"), (Mark(owned=True), "owned"), (Mark(owned=True, wanted=True), "wanted_owned")],
    )
    def test_mark_states(self, mark: Mark, state: str) -> None:
        assert mark.state == state


def _drawn_members(payload: dict[str, Any]) -> set[str]:
    """The focus members this page draws, read back from the response alone."""
    focus = payload["focus"]
    focus_id = focus["id"]
    if focus["plane"] == "credit":
        return {node["id"] for node in payload["planes"][0]["nodes"][1:]}
    if focus["plane"] == "master":
        credit, label = focus_id.removeprefix("ma:").rsplit(":", 1)
        return {
            node["id"] for node in payload["planes"][1]["nodes"] if node["parent"] == credit and (label == "all" or decade(node["year"]) == label)
        }
    if focus_id.startswith("ra:m:"):
        return {node["id"] for node in payload["planes"][2]["nodes"] if node["parent"] == "m:" + focus_id.removeprefix("ra:m:")}
    lane = focus_id.removeprefix("ra:")
    parts = lane.split(":")
    label = parts[3] if len(parts) == 4 else None
    parent = ":".join(parts[:3])
    return {node["id"] for node in payload["planes"][2]["nodes"] if node["parent"] == parent and (label is None or decade(node["year"]) == label)}


def _cursors(payload: dict[str, Any]) -> list[str]:
    return [agg["id"] for level in range(3) for agg in payload["planes"][level]["aggregates"]] + [
        node["id"] for node in payload["planes"][1]["nodes"]
    ]


class TestPaging:
    @pytest.mark.parametrize("budget", [DEFAULT_BUDGET, Budget(6, 12, 16), Budget(3, 6, 4)])
    @pytest.mark.parametrize("seed", [1, 2, 3])
    def test_every_cursor_pages_each_member_exactly_once(self, budget: Budget, seed: int) -> None:
        catalog, marks = random_catalog(seed, masters=80, masterless=90, collaborators=10, musicbrainz=10)
        default = build_strata(catalog, budget, marks=marks)
        cursors = _cursors(default)
        assert cursors
        for cursor in cursors:
            first = build_strata(catalog, budget, focus=cursor, marks=marks)
            focus = first["focus"]
            seen: list[str] = []
            for page in range(focus["pages"]):
                view = build_strata(catalog, budget, focus=cursor, page=page, marks=marks)
                assert view["focus"] | {"page": 0} == focus | {"page": 0}
                assert view["focus"]["page"] == page
                assert_invariants(view, budget)
                drawn = _drawn_members(view)
                assert 1 <= len(drawn) <= min(PAGE_SIZE, focus["page_size"])
                seen += sorted(drawn)
                # Everything off the page is held by one aggregate that keeps the cursor.
                remainder = [agg for level in range(3) for agg in view["planes"][level]["aggregates"] if agg["id"] == focus["id"]]
                assert len(remainder) == (1 if focus["pages"] > 1 else 0)
            assert len(seen) == len(set(seen)) == focus["members"], cursor

    def test_pages_wrap(self) -> None:
        catalog, _ = random_catalog(4, masters=10, masterless=40)
        first = build_strata(catalog, focus="nm:c:1")
        pages = first["focus"]["pages"]
        assert pages > 1
        wrapped = build_strata(catalog, focus="nm:c:1", page=pages)
        assert wrapped["focus"]["page"] == 0
        assert wrapped["planes"] == first["planes"]

    def test_master_pages_hold_eight(self) -> None:
        releases = [Release(str(1000 + i), str(600 + i), 1970, (ROOT,)) for i in range(30)]
        catalog = Catalog(ROOT, "Root", tuple(releases), {str(600 + i): 1970 for i in range(30)})
        payload = build_strata(catalog, focus="ma:c:1:1970s", page=1)
        assert payload["focus"] == {"id": "ma:c:1:1970s", "plane": "master", "page": 1, "pages": 4, "page_size": 8, "members": 30}
        assert [node["master_id"] for node in payload["planes"][1]["nodes"]] == [str(608 + i) for i in range(8)]
        remainder = next(agg for agg in payload["planes"][1]["aggregates"] if agg["id"] == "ma:c:1:1970s")
        assert remainder["masters"] == 22

    def test_pressing_pages_hold_eight_and_the_rest_keeps_the_cursor(self) -> None:
        releases = [Release(str(1000 + k), "500", 1970 + k, (ROOT,)) for k in range(20)]
        catalog = Catalog(ROOT, "Root", tuple(releases), {"500": 1970})
        payload = build_strata(catalog, focus="m:500", page=2)
        assert payload["focus"] == {"id": "ra:m:500", "plane": "release", "page": 2, "pages": 3, "page_size": 8, "members": 20}
        assert [node["release_id"] for node in payload["planes"][2]["nodes"]] == [str(1016 + k) for k in range(4)]
        assert payload["planes"][2]["aggregates"] == [
            {"id": "ra:m:500", "parent": "m:500", "releases": 16, "undated_releases": 0, "musicbrainz_releases": 0, "y0": 1970, "y1": 1985}
        ]

    def test_a_focus_that_fits_whole_has_one_page_and_no_remainder(self) -> None:
        releases = [Release(str(1000 + k), "500", 1970 + k, (ROOT,)) for k in range(3)]
        catalog = Catalog(ROOT, "Root", tuple(releases), {"500": 1970})
        payload = build_strata(catalog, focus="ra:m:500")
        assert (payload["focus"]["pages"], payload["focus"]["page_size"]) == (1, 3)
        assert payload["planes"][2]["aggregates"] == []

    def test_focusing_a_folded_credit_page_draws_it_and_its_ladder(self) -> None:
        releases = [Release(str(100 + i), str(700 + i % 3), 1990, (ROOT, str(2 + i % 5))) for i in range(30)]
        catalog = Catalog(ROOT, "Root", tuple(releases), {str(700 + i): 1990 for i in range(3)})
        default = build_strata(catalog)
        assert default["planes"][0]["aggregates"][0]["id"] == "c:other"
        page = build_strata(catalog, focus="c:other", page=3)
        assert page["focus"]["plane"] == "credit"
        assert page["focus"]["page_size"] == 1
        assert page["planes"][0]["nodes"][0]["id"] == "c:1"
        assert_invariants(page, DEFAULT_BUDGET)

    @pytest.mark.parametrize(
        "cursor",
        [
            "",
            "x:1",
            "m:404",
            "ra:m:404",
            "nm:c:9",
            "nm:c:1:1850s",
            "ra:nm:c:1",
            "ma:c:1",
            "ma:c:1:1850s",
            "ma:x:1:all",
            "ma:c::all",
            "nm:c",
            "c:other",
        ],
    )
    def test_unknown_cursors_are_refused(self, cursor: str) -> None:
        catalog = Catalog(ROOT, "Root", (Release("10", "500", 1971, (ROOT,)),), {"500": 1971})
        with pytest.raises(FocusNotFoundError):
            build_strata(catalog, focus=cursor or "?")


class TestLabels:
    def test_targets_and_labels_fill_only_what_is_drawn(self) -> None:
        catalog = Catalog(
            ROOT,
            None,
            (
                Release("10", "500", 1971, (ROOT, "2")),
                Release("11", "501", 1980, (ROOT,)),
                Release("aaaa", "500", 1972, (), "musicbrainz", "From MusicBrainz"),
            ),
            {"500": 1971, "501": 1980},
        )
        payload = build_strata(catalog)
        releases, masters, artists = label_targets(payload)
        assert sorted(releases) == ["10", "11"]
        assert masters == ["501", "500"]  # credit order: the root's own credit first
        assert artists == ["1", "2"]
        apply_labels(
            payload,
            release_titles={"10": "First", "11": "Second"},
            master_titles={"500": "Master A", "501": "Master B"},
            artist_names={"1": "Root Name", "2": "Guest"},
            edges=[
                {"source": "500", "target": "501", "categories": ["Production"], "people": 2},
                {"source": "500", "target": "999", "categories": ["Vocals"], "people": 9},
            ],
        )
        assert payload["root"]["name"] == "Root Name"
        assert payload["planes"][0]["nodes"][1]["artists"] == [{"id": "1", "name": "Root Name"}, {"id": "2", "name": "Guest"}]
        titles = {node["release_id"]: node["title"] for node in payload["planes"][2]["nodes"]}
        assert titles == {"10": "First", "11": "Second", "aaaa": "From MusicBrainz"}
        assert [node["title"] for node in payload["planes"][1]["nodes"]] == ["Master B", "Master A"]
        assert payload["planes"][1]["edges"] == [
            {"source": "m:500", "target": "m:501", "kind": "shared_personnel", "categories": ["Production"], "people": 2}
        ]

    def test_default_payload_stays_small(self) -> None:
        catalog, marks = random_catalog(9, masters=200, masterless=100, collaborators=20, musicbrainz=20)
        payload = build_strata(catalog, marks=marks)
        entries = sum(len(_entries(payload, level)) for level in range(3))
        assert entries <= 31
        assert len(json.dumps(payload, separators=(",", ":"))) < 12_000


class TestCachedForm:
    """The catalog survives the cache unchanged, and an entry this build cannot read is a miss."""

    @pytest.mark.parametrize("seed", range(5))
    def test_a_catalog_round_trips_and_folds_the_same(self, seed: int) -> None:
        catalog, marks = random_catalog(seed, masters=40, masterless=25, musicbrainz=12)
        catalog = Catalog(catalog.root_id, "Root", (*catalog.releases, Release("99", None, None, (ROOT,), "discogs", "Titled")), catalog.master_years)

        decoded = decode_catalog(encode_catalog(catalog))

        assert decoded is not None
        assert decoded.releases == catalog.releases
        assert dict(decoded.master_years) == dict(catalog.master_years)
        assert (decoded.root_id, decoded.root_name) == (catalog.root_id, catalog.root_name)
        for budget in (DEFAULT_BUDGET, Budget(6, 12, 21)):
            assert build_strata(decoded, budget, marks=marks) == build_strata(catalog, budget, marks=marks)

    def test_each_credit_is_stored_once(self) -> None:
        releases = tuple(Release(str(10 + i), None, 1990, (ROOT,)) for i in range(100))
        stored = json.loads(encode_catalog(Catalog(ROOT, None, releases, {})))
        assert stored["credits"] == [[ROOT]]
        assert stored["releases"][0] == ["10", None, 1990, 0]

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "not json",
            "[]",
            '{"format": 0}',
            '{"format": 1}',
            '{"format": 1, "root_id": "1", "root_name": null, "credits": [], "releases": [["10", null, null, 3]], "master_years": {}}',
        ],
    )
    def test_an_unreadable_entry_is_none(self, text: str) -> None:
        assert decode_catalog(text) is None
