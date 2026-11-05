"""The Python half of explore discoveries: breadth-first bookkeeping, selection, hub storage and the refresh loop.

What the Cypher returns is proven on a real Neo4j (``test_explore_discovery_integration.py``); here the graph is a
small in-memory adjacency list answering the same statements, so the logic around them is exercised without a database.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from api import explore_discovery as ed


# (id, label, name, key id) per node; undirected edges (a, b, relationship type).
NODES = {
    "A": ("Artist", "Alpha", "a"),
    "B": ("Artist", "Beta", "b"),
    "R1": ("Release", "First", "r1"),
    "R2": ("Release", "Second", "r2"),
    "L": ("Label", "One", "l"),
    "G": ("Genre", "Rock", "Rock"),
    "S": ("Style", "Punk", "Punk"),
    "AL": ("Artist", "Alias", "al"),
}
EDGES = [
    ("R1", "A", "BY"), ("R1", "B", "BY"), ("R2", "A", "BY"), ("R1", "L", "ON"), ("R2", "L", "ON"),
    ("R1", "G", "IS"), ("R2", "G", "IS"), ("R1", "S", "IS"), ("AL", "A", "ALIAS_OF"),
]  # fmt: skip


def _neighbours(eid: str, types: set[str]) -> list[dict[str, Any]]:
    out = []
    for a, b, rel in EDGES:
        if rel in types and eid in (a, b):
            other = b if a == eid else a
            label, name, key = NODES[other]
            out.append({"fid": eid, "eid": other, "label": label, "name": name, "id": key, "nameless": False, "rel": rel})
    return out


async def fake_run_query(_driver: Any, cypher: str, **params: Any) -> list[dict[str, Any]]:
    if "UNWIND range(0, size($frontier)" in cypher:
        types = set(cypher.split("[rel:")[1].split("]")[0].split("|"))
        codes = {"ALIAS_OF": 0, "BY": 1, "DERIVED_FROM": 2, "IS": 3, "MEMBER_OF": 4, "ON": 5}
        best: dict[str, dict[str, Any]] = {}
        for i, fid in enumerate(params["frontier"]):
            for row in _neighbours(fid, types):
                code = i * 8 + codes[row["rel"]]
                if row["eid"] not in best or code < best[row["eid"]]["best"]:
                    best[row["eid"]] = {**{k: v for k, v in row.items() if k not in ("fid", "rel")}, "best": code}
        return list(best.values())
    if "AS pop" in cypher and "$eids" in cypher:
        return [{"eid": e, "pop": {"A": 9, "B": 3, "L": 5, "AL": 1}.get(e, 0)} for e in params["eids"]]
    raise AssertionError(cypher)


def _start(eid: str, label: str | None = None) -> ed._Node:
    lab, name, key = NODES[eid]
    return ed._Node(eid, label or lab, name, key)


@pytest.mark.asyncio
async def test_bfs_finds_each_node_once_at_its_shortest_distance_with_a_real_path() -> None:
    with patch.object(ed, "run_query", fake_run_query):
        found = {d["id"]: d for d in await ed._bfs(object(), _start("A"), 3)}
    assert {k: v["dist"] for k, v in found.items()} == {"al": 1, "b": 2, "l": 2, "Rock": 2, "Punk": 2}
    assert found["Punk"]["path_names"] == ["Alpha", "First", "Punk"]
    assert found["Punk"]["rel_types"] == ["BY", "IS"]
    assert found["Rock"]["path_names"] == ["Alpha", "First", "Rock"]  # First < Second, an equal-length tie broken by name


@pytest.mark.asyncio
async def test_the_last_hop_does_not_expand_a_label_or_genre() -> None:
    calls: list[str] = []

    async def spy(driver: Any, cypher: str, **params: Any) -> list[dict[str, Any]]:
        calls.append(cypher)
        return await fake_run_query(driver, cypher, **params)

    with patch.object(ed, "run_query", spy):
        await ed._bfs(object(), _start("L"), 1)  # a label's only neighbours are releases: nothing eligible, nothing asked
    assert calls == []


@pytest.mark.asyncio
async def test_select_keeps_every_genre_and_style_and_caps_artists_and_labels() -> None:
    discovered = [
        {
            "eid": f"x{i}",
            "label": "Artist",
            "id": f"x{i:03d}",
            "name": f"X{i}",
            "path_names": ["s", "r", f"X{i}"],
            "rel_types": ["BY", "BY"],
            "dist": 2,
        }
        for i in range(150)
    ] + [
        {"eid": "g", "label": "Genre", "id": "Rock", "name": "Rock", "path_names": ["s", "r", "Rock"], "rel_types": ["BY", "IS"], "dist": 2},
        {"eid": "s", "label": "Style", "id": "Punk", "name": "Punk", "path_names": ["s", "r", "Punk"], "rel_types": ["BY", "IS"], "dist": 2},
        {"eid": "n", "label": "Artist", "id": "near", "name": "Near", "path_names": ["s", "Near"], "rel_types": ["ALIAS_OF"], "dist": 1},
    ]

    async def pops(_driver: Any, _cypher: str, eids: list[str]) -> list[dict[str, Any]]:
        return [{"eid": e, "pop": int(e[1:])} for e in eids]  # x149 most credited

    with patch.object(ed, "run_query", pops):
        rows = await ed._select(object(), discovered)
    people = [r for r in rows if r["type"] == "artist"]
    assert len(people) == 100
    assert people[0]["id"] == "near"  # nearest first
    assert [r["id"] for r in people[1:4]] == ["x149", "x148", "x147"]  # then most credited
    assert {r["type"] for r in rows} == {"artist", "genre", "style"}
    assert {r["name"] for r in rows if r["type"] in ("genre", "style")} == {"Rock", "Punk"}


@pytest.mark.asyncio
async def test_select_orders_a_small_tier_by_id() -> None:
    discovered = [{"eid": e, "label": "Artist", "id": e, "name": e, "path_names": ["s", e], "rel_types": ["BY"], "dist": 1} for e in ("b", "a", "c")]
    assert [r["id"] for r in await ed._select(object(), discovered)] == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_find_start_maps_a_row() -> None:
    row = {"eid": "e1", "name": "Rock", "id": "Rock", "releases": 7}
    with patch.object(ed, "run_single", AsyncMock(return_value=row)):
        node, releases = await ed._find_start(object(), "genre", "Rock")  # type: ignore[misc]
    assert (node.label, node.name, releases) == ("Genre", "Rock", 7)
    with patch.object(ed, "run_single", AsyncMock(return_value=None)):
        assert await ed._find_start(object(), "genre", "Nope") is None


# --- hubs ---


class FakeValkey:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self.fail = False

    async def get(self, key: str) -> str | None:
        if self.fail:
            raise ConnectionError("down")
        return self.data.get(key)

    async def set(self, key: str, value: str) -> None:
        if self.fail:
            raise ConnectionError("down")
        self.data[key] = value


def _reset() -> None:
    ed.configure(None)
    ed._failures.clear()
    ed._stages.clear()


def _hub_queries(release_count: int = 60_000) -> Any:
    async def run(_driver: Any, cypher: str, **params: Any) -> list[dict[str, Any]]:
        if "n.release_count) >= $min" in cypher.replace(" ", "").replace("coalesce(", "").replace("\n", "") or "AS releases" in cypher:
            return [{"type": "genre", "id": "Rock", "releases": release_count}]
        if "o:Genre OR o:Style" in cypher:
            return [{"label": "Style", "id": "Punk", "name": "Punk", "witness": "First"}]
        if "AS pop" in cypher and "Artist" in cypher:
            return [
                {"label": "Artist", "id": "a", "name": "Alpha", "pop": 9, "placeholder": False},
                {"label": "Artist", "id": "b", "name": "Beta", "pop": 3, "placeholder": False},
            ]
        if "AS pop" in cypher and "Label" in cypher:
            return [{"label": "Label", "id": "l", "name": "One", "pop": 5, "placeholder": False}]
        if "UNWIND $ids" in cypher:
            return [{"id": i, "witness": f"W-{i}"} for i in params["ids"]]
        raise AssertionError(cypher)

    return run


@pytest.mark.asyncio
async def test_refresh_builds_stores_and_survives_a_restart() -> None:
    valkey = FakeValkey()
    ed.configure(valkey)
    with patch.object(ed, "run_query", _hub_queries()):
        assert (await ed.refresh_hubs(object()))[0] == 1
        assert (await ed.refresh_hubs(object()))[0] == 0  # unchanged
    held = ed._hubs[("genre", "Rock")]
    assert [r["id"] for r in held.rows] == ["Punk", "a", "l", "b"]  # genres/styles, then people by credits
    assert held.rows[1]["path_names"] == ["Rock", "W-a", "Alpha"]
    assert held.rows[1]["rel_types"] == ["IS", "BY"]
    ed._hubs.clear()  # a new process: memory empty, Valkey kept
    assert (await ed._hub_result("genre", "Rock")).rows == held.rows  # type: ignore[union-attr]
    ed.configure(None)


@pytest.mark.asyncio
async def test_a_changed_release_count_or_age_rebuilds() -> None:
    ed.configure(None)
    with patch.object(ed, "run_query", _hub_queries(60_000)):
        await ed.refresh_hubs(object())
    with patch.object(ed, "run_query", _hub_queries(61_000)):
        assert (await ed.refresh_hubs(object()))[0] == 1
        assert (await ed.refresh_hubs(object(), max_age=-1))[0] == 1
    ed.configure(None)


@pytest.mark.asyncio
async def test_a_failing_hub_is_skipped_and_logged_and_the_listing_failure_returns_zero() -> None:
    ed.configure(None)

    async def boom(_driver: Any, cypher: str, **_params: Any) -> list[dict[str, Any]]:
        if "AS releases" in cypher:
            return [{"type": "genre", "id": "Rock", "releases": 60_000}]
        raise RuntimeError("neo4j restarting")

    with patch.object(ed, "run_query", boom):
        assert (await ed.refresh_hubs(object()))[0] == 0
        ed._failures.clear()
    with patch.object(ed, "run_query", AsyncMock(side_effect=RuntimeError("down"))):
        assert (await ed.refresh_hubs(object())) == (0, 1)
    ed.configure(None)


@pytest.mark.asyncio
async def test_valkey_failures_never_fail_a_hub_read_or_write() -> None:
    valkey = FakeValkey()
    valkey.fail = True
    ed.configure(valkey)
    with patch.object(ed, "run_query", _hub_queries()):
        assert (await ed.refresh_hubs(object()))[0] == 1  # the write failed, memory still holds it
    assert await ed._hub_result("genre", "Rock") is not None
    ed._hubs.clear()
    assert await ed._hub_result("genre", "Rock") is None  # the read failed too
    valkey.fail = False
    valkey.data[ed._hub_key("genre", "Rock")] = "{not json"
    assert await ed._hub_result("genre", "Rock") is None
    ed.configure(None)


@pytest.mark.asyncio
async def test_loop_retries_quickly_until_every_hub_exists_then_uses_the_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    _reset()
    monkeypatch.setattr(ed, "_RETRY_BASE_SECONDS", 0.0)  # the per-hub backoff is tested on its own
    delays: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        delays.append(seconds)
        if len(delays) == 4:
            raise asyncio.CancelledError

    calls = {"n": 0}
    healthy = _hub_queries()

    async def flaky(driver: Any, cypher: str, **params: Any) -> list[dict[str, Any]]:
        if "o:Genre OR o:Style" in cypher:
            calls["n"] += 1
            if calls["n"] <= 2:
                raise RuntimeError("neo4j restarting")
        return await healthy(driver, cypher, **params)

    with patch.object(ed, "run_query", flaky), patch.object(ed.asyncio, "sleep", fake_sleep), pytest.raises(asyncio.CancelledError):
        await ed.run_refresh_loop(object(), interval=3600)
    assert delays == [30, 60, 3600, 3600]
    ed.configure(None)


@pytest.mark.asyncio
async def test_a_failed_stage_is_retried_alone_and_the_hub_backs_off() -> None:
    _reset()
    healthy = _hub_queries()
    calls: dict[str, int] = {"gs": 0, "artist": 0, "label": 0}

    async def label_fails_once(driver: Any, cypher: str, **params: Any) -> list[dict[str, Any]]:
        if "o:Genre OR o:Style" in cypher:
            calls["gs"] += 1
        if "AS pop" in cypher and "Artist" in cypher:
            calls["artist"] += 1
        if "AS pop" in cypher and "Label" in cypher:
            calls["label"] += 1
            if calls["label"] == 1:
                raise RuntimeError("scan exceeded its budget")
        return await healthy(driver, cypher, **params)

    with patch.object(ed, "run_query", label_fails_once):
        assert await ed.refresh_hubs(object()) == (0, 1)
        failures, retry_at = ed._failures[("genre", "Rock")]
        assert failures == 1 and retry_at > time.time() + 3000  # a slower, capped retry
        assert await ed.refresh_hubs(object()) == (0, 1)  # still backing off: no new scan
        assert calls == {"gs": 1, "artist": 1, "label": 1}
        ed._failures[("genre", "Rock")] = (1, 0.0)  # the backoff has passed
        assert await ed.refresh_hubs(object()) == (1, 0)
    assert calls == {"gs": 1, "artist": 1, "label": 2}  # genres/styles and artists were not scanned again
    _reset()


@pytest.mark.asyncio
async def test_backoff_doubles_and_is_capped() -> None:
    _reset()

    async def always_fails(_driver: Any, cypher: str, **_params: Any) -> list[dict[str, Any]]:
        if "AS releases" in cypher:
            return [{"type": "genre", "id": "Rock", "releases": 60_000}]
        raise RuntimeError("too slow")

    with patch.object(ed, "run_query", always_fails):
        for expected in (3600, 7200, 14400, 28800, 57600, 86400, 86400):
            await ed.refresh_hubs(object())
            failures, retry_at = ed._failures[("genre", "Rock")]
            assert abs((retry_at - time.time()) - expected) < 5, (failures, expected)
            ed._failures[("genre", "Rock")] = (failures, 0.0)
    _reset()


@pytest.mark.asyncio
async def test_a_hub_another_replica_is_building_is_skipped_and_one_stored_meanwhile_is_not_rebuilt() -> None:
    valkey = FakeValkey()
    ed.configure(valkey)
    ed._failures.clear()
    ed._stages.clear()

    class LockingValkey(FakeValkey):
        async def set(self, key: str, value: str, nx: bool = False, ex: int | None = None) -> bool | None:  # noqa: ARG002
            if nx:
                if key in self.data:
                    return None
                self.data[key] = value
                return True
            return await super().set(key, value)

        async def delete(self, key: str) -> None:
            self.data.pop(key, None)

    locking = LockingValkey()
    ed.configure(locking)
    locking.data[ed._lock_key("genre", "Rock")] = "1"  # replica B holds it
    builds = {"n": 0}
    healthy = _hub_queries()

    async def counting(driver: Any, cypher: str, **params: Any) -> list[dict[str, Any]]:
        if "o:Genre OR o:Style" in cypher:
            builds["n"] += 1
        return await healthy(driver, cypher, **params)

    with patch.object(ed, "run_query", counting):
        assert await ed.refresh_hubs(object()) == (0, 1)  # skipped
        assert builds["n"] == 0
        del locking.data[ed._lock_key("genre", "Rock")]
        assert await ed.refresh_hubs(object()) == (1, 0)  # lock taken, built, released
        assert ed._lock_key("genre", "Rock") not in locking.data
        assert builds["n"] == 1
    _reset()


@pytest.mark.asyncio
async def test_placeholder_artists_go_last_not_out() -> None:
    discovered = [
        {
            "eid": "ph",
            "label": "Artist",
            "id": "194",
            "name": "194",
            "path_names": ["s", "r", "194"],
            "rel_types": ["BY", "BY"],
            "dist": 2,
            "placeholder": True,
        },
        {
            "eid": "a",
            "label": "Artist",
            "id": "a",
            "name": "A",
            "path_names": ["s", "r", "A"],
            "rel_types": ["BY", "BY"],
            "dist": 2,
            "placeholder": False,
        },
        {
            "eid": "b",
            "label": "Artist",
            "id": "b",
            "name": "B",
            "path_names": ["s", "r", "B"],
            "rel_types": ["BY", "BY"],
            "dist": 2,
            "placeholder": False,
        },
    ]
    assert [r["id"] for r in await ed._select(object(), discovered)] == ["a", "b", "194"]

    async def pops(_driver: Any, _cypher: str, eids: list[str]) -> list[dict[str, Any]]:
        return [{"eid": e, "pop": {"ph": 1_500_000, "a": 5, "b": 9, "c": 1}[e]} for e in eids]

    with patch.object(ed, "run_query", pops), patch.object(ed, "ARTIST_LABEL_LIMIT", 3):
        many = [*discovered, {**discovered[1], "eid": "c", "id": "c", "name": "C"}]
        picked = [r["id"] for r in await ed._select(object(), many)]
    assert picked[-1] != "194" or len(picked) == 3
    assert picked.index("b") < picked.index("a")  # established first among real names
    assert "194" not in picked  # cut by the limit only after every real name is in, never ahead of one
