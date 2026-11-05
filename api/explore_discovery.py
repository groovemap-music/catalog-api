"""Discoveries for ``/api/recommend/explore``: exact genres and styles, a deterministic nearest-100 of artists and labels.

The old statement enumerated every variable-length path, sorted and collected them, and only then applied
``LIMIT 100``: Brass & Military (75K releases) took 7.4 s and 463 MB, and Rock was never run because it would
have exhausted the 3 GiB transaction pool (audit H2, ``gm-catalog-api-zazs``). What a caller gets is now defined
outright, and bounded work produces it:

* **Genres and styles are returned in full**, every one reachable within ``hops``, each with its shortest
  distance and a deterministic path. There are only about 15 genres and 740 styles, and they are what the
  taste score rewards (a blind spot gets 1.5x), so none is sampled or dropped.
* **Artists and labels keep the existing limit of 100**, nearest first, then most established first
  (``release_count`` DESC for a label; credit count DESC for an artist, which has no stored count), then id.
  The old order among equals was arbitrary, so which 100 appeared was not stable; now it is. This favours
  well-known names over obscure ones at the same distance, by design.
* Nothing is sampled. A start with fewer than :data:`HUB_MIN_RELEASES` releases is expanded breadth first over
  distinct nodes, which visits each edge once (no path enumeration). A hub genre, style or label (Rock,
  Pop Rock, Columbia) cannot be walked inside a request, so its answer is computed exactly in the background
  by :func:`refresh_hubs` and stored; until a hub is stored its request answers 503.

``hops=3`` equals ``hops=2`` for a hub: the only eligible nodes three steps away are alias and member artists
of artists already at distance 2, and a hub has far more than 100 of those, so the cap is already full.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any, Final

import structlog

from api.queries.helpers import run_query, run_single
from api.valkey_keys import valkey_key


logger = structlog.get_logger(__name__)

HUB_MIN_RELEASES: Final = 50_000
ARTIST_LABEL_LIMIT: Final = 100
# One hub's three scans, as a background budget (Rock's artist scan reads 112M edges).
HUB_QUERY_TIMEOUT_SECONDS: Final = 120.0
_EXPAND_ALL: Final = "BY|ON|IS|ALIAS_OF|MEMBER_OF|DERIVED_FROM"
_LAST_HOP_FROM_RELEASE: Final = "BY|ON|IS"
_LAST_HOP_FROM_ARTIST: Final = "ALIAS_OF|MEMBER_OF"
_ELIGIBLE: Final = frozenset({"Artist", "Label", "Genre", "Style"})
_ALLOWED: Final = {"genre": "Genre", "style": "Style", "artist": "Artist", "label": "Label", "release": "Release", "master": "Master"}


class HubNotReadyError(RuntimeError):
    """A hub entity's discoveries have not been computed yet; the route answers 503."""


@dataclass(frozen=True, slots=True)
class _Node:
    eid: str
    label: str
    name: str
    id: str


# --- start lookup ----------------------------------------------------------------------------------------------


async def _find_start(driver: Any, entity_type: str, entity_id: str) -> tuple[_Node, int] | None:
    label = _ALLOWED[entity_type]
    key = "name" if entity_type in ("genre", "style") else "id"
    row = await run_single(
        driver,
        f"""
        MATCH (n:{label} {{{key}: $entity_id}})
        RETURN elementId(n) AS eid, coalesce(n.name, n.title, n.id) AS name, coalesce(n.id, n.name) AS id,
               CASE WHEN n:Genre OR n:Style OR n:Label THEN coalesce(n.release_count, 0) ELSE 0 END AS releases
        """,
        entity_id=entity_id,
    )
    if row is None:
        return None
    return _Node(row["eid"], label, str(row["name"]), str(row["id"])), int(row["releases"])


# --- exact breadth-first expansion for a start that fits in a request -----------------------------------------


_EXPAND_Q = """
UNWIND $frontier AS fid
MATCH (f) WHERE elementId(f) = fid
MATCH (f)-[rel:{types}]-(n)
RETURN fid, elementId(n) AS eid, labels(n)[0] AS label, coalesce(n.name, n.title, n.id) AS name,
       coalesce(n.id, n.name) AS id, type(rel) AS rel
"""

_POPULARITY_Q = """
UNWIND $eids AS eid
MATCH (n) WHERE elementId(n) = eid
RETURN eid, CASE WHEN n:Label THEN coalesce(n.release_count, COUNT { (n)<-[:ON]-() }) ELSE COUNT { (n)<-[:BY]-() } END AS pop
"""


async def _expand(driver: Any, frontier: list[_Node], types: str) -> list[dict[str, Any]]:
    if not frontier:
        return []
    return await run_query(driver, _EXPAND_Q.replace("{types}", types), frontier=[n.eid for n in frontier])


async def _bfs(driver: Any, start: _Node, hops: int) -> list[dict[str, Any]]:
    """Every Artist, Label, Genre and Style within ``hops`` steps, shortest distance first, deterministic path."""
    nodes: dict[str, _Node] = {start.eid: start}
    dist: dict[str, int] = {start.eid: 0}
    parent: dict[str, tuple[str, str]] = {}
    frontier = [start]
    for depth in range(hops):
        last = depth == hops - 1
        if last:
            # Only a release or an artist can still lead to an eligible node, and only by these edges.
            rows = await _expand(driver, [n for n in frontier if n.label == "Release"], _LAST_HOP_FROM_RELEASE)
            rows += await _expand(driver, [n for n in frontier if n.label == "Artist"], _LAST_HOP_FROM_ARTIST)
        else:
            rows = await _expand(driver, frontier, _EXPAND_ALL)
        rows.sort(key=lambda r: (str(r["name"]), r["eid"], r["fid"], r["rel"]))
        next_frontier: list[_Node] = []
        for row in rows:
            eid = row["eid"]
            if eid not in dist:
                if last and row["label"] not in _ELIGIBLE:
                    continue
                dist[eid] = depth + 1
                nodes[eid] = _Node(eid, row["label"], str(row["name"]), str(row["id"]))
                parent[eid] = (row["fid"], row["rel"])
                next_frontier.append(nodes[eid])
            elif dist[eid] == depth + 1 and _path_names(parent, nodes, row["fid"]) < _path_names(parent, nodes, parent[eid][0]):
                parent[eid] = (row["fid"], row["rel"])
        frontier = next_frontier
    discovered = []
    for eid, d in dist.items():
        node = nodes[eid]
        if eid == start.eid or node.label not in _ELIGIBLE:
            continue
        names, rels = _path(parent, nodes, eid)
        discovered.append({"eid": eid, "label": node.label, "id": node.id, "name": node.name, "path_names": names, "rel_types": rels, "dist": d})
    return discovered


def _path_names(parent: dict[str, tuple[str, str]], nodes: dict[str, _Node], eid: str) -> list[str]:
    return _path(parent, nodes, eid)[0]


def _path(parent: dict[str, tuple[str, str]], nodes: dict[str, _Node], eid: str) -> tuple[list[str], list[str]]:
    names, rels = [nodes[eid].name], []
    while eid in parent:
        eid, rel = parent[eid]
        names.append(nodes[eid].name)
        rels.append(rel)
    names.reverse()
    rels.reverse()
    return names, rels


def _output_type(label: str) -> str:
    return label.lower()


async def _select(driver: Any, discovered: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """All genres and styles; of artists and labels, the nearest :data:`ARTIST_LABEL_LIMIT`, most established first."""
    gs = [d for d in discovered if d["label"] in ("Genre", "Style")]
    al = [d for d in discovered if d["label"] in ("Artist", "Label")]
    chosen: list[dict[str, Any]] = []
    for dist in sorted({d["dist"] for d in al}):
        tier = [d for d in al if d["dist"] == dist]
        room = ARTIST_LABEL_LIMIT - len(chosen)
        if room <= 0:
            break
        if len(tier) > room:
            pops = {r["eid"]: r["pop"] for r in await run_query(driver, _POPULARITY_Q, eids=[d["eid"] for d in tier])}
            tier.sort(key=lambda d: (-pops.get(d["eid"], 0), d["id"]))
            tier = tier[:room]
        else:
            tier.sort(key=lambda d: d["id"])
        chosen += tier
    out = sorted(gs, key=lambda d: (d["dist"], d["label"], d["name"])) + chosen
    out.sort(key=lambda d: d["dist"])
    return [_row(d) for d in out]


def _row(d: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": d["id"],
        "name": d["name"],
        "type": _output_type(d["label"]),
        "path_names": d["path_names"],
        "rel_types": d["rel_types"],
        "dist": d["dist"],
    }


# --- hubs: computed exactly in the background, stored ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HubResult:
    rows: list[dict[str, Any]]
    release_count: int
    built_at: float


_hubs: dict[tuple[str, str], HubResult] = {}
_valkey: Any = None


def configure(valkey: Any = None) -> None:
    global _valkey
    _valkey = valkey
    _hubs.clear()


def _hub_key(entity_type: str, entity_id: str) -> str:
    return valkey_key(f"explore-hub:v1:{entity_type}:{entity_id}")


def _encode(result: HubResult) -> str:
    return json.dumps({"rows": result.rows, "release_count": result.release_count, "built_at": result.built_at})


def _decode(raw: str | bytes) -> HubResult | None:
    try:
        data = json.loads(raw)
        return HubResult(rows=list(data["rows"]), release_count=int(data["release_count"]), built_at=float(data["built_at"]))
    except ValueError, KeyError, TypeError:
        return None


async def _hub_result(entity_type: str, entity_id: str) -> HubResult | None:
    held = _hubs.get((entity_type, entity_id))
    if held is not None or _valkey is None:
        return held
    try:
        raw = await _valkey.get(_hub_key(entity_type, entity_id))
    except Exception as exc:
        logger.warning("⚠️ Explore hub could not be read from Valkey", error_type=type(exc).__name__)
        return None
    stored = _decode(raw) if raw else None
    if stored is not None:
        _hubs[(entity_type, entity_id)] = stored
    return stored


_HUBS_Q = """
MATCH (n) WHERE (n:Genre OR n:Style OR n:Label) AND coalesce(n.release_count, 0) >= $min
RETURN toLower(labels(n)[0]) AS type, CASE WHEN n:Label THEN n.id ELSE n.name END AS id, n.release_count AS releases
ORDER BY releases, id
"""

# For a hub, the releases it holds are one hop away; everything eligible sits one hop further (distance 2).
_HUB_RELEASES = {
    "genre": "(h:Genre {name: $id})<-[:IS]-(r:Release)",
    "style": "(h:Style {name: $id})<-[:IS]-(r:Release)",
    "label": "(h:Label {id: $id})<-[:ON]-(r:Release)",
}
_HUB_GENRE_STYLE_Q = """
MATCH {releases}-[:IS]->(o)
WHERE (o:Genre OR o:Style) AND o <> h
RETURN labels(o)[0] AS label, o.name AS id, o.name AS name, min(r.title) AS witness
"""
_HUB_ARTIST_Q = """
MATCH {releases}-[:BY]->(o:Artist)
WITH DISTINCT o
WITH o, COUNT { (o)<-[:BY]-() } AS pop
ORDER BY pop DESC, o.id
LIMIT $limit
RETURN 'Artist' AS label, o.id AS id, o.name AS name, pop
"""
_HUB_LABEL_Q = """
MATCH {releases}-[:ON]->(o:Label)
WHERE o <> h
WITH DISTINCT o
WITH o, coalesce(o.release_count, 0) AS pop
ORDER BY pop DESC, o.id
LIMIT $limit
RETURN 'Label' AS label, o.id AS id, o.name AS name, pop
"""
# The display path needs one release that links hub and discovery. Fetched for the chosen 100 only, so the
# scan above keeps one row per candidate instead of a title per candidate (Rock has 953,658 artists).
_HUB_WITNESS_Q = """
UNWIND $ids AS oid
MATCH {releases}-[:{rel}]->(o {{id: oid}})
RETURN oid AS id, min(r.title) AS witness
"""


async def _build_hub(driver: Any, entity_type: str, entity_id: str, name: str, release_count: int) -> HubResult:
    rel_to_hub = "ON" if entity_type == "label" else "IS"
    scope: dict[str, Any] = {"id": entity_id, "limit": ARTIST_LABEL_LIMIT}
    patt = _HUB_RELEASES[entity_type]

    async def scan(template: str) -> list[dict[str, Any]]:
        return await run_query(
            driver, template.replace("{releases}", patt).replace("{{", "{").replace("}}", "}"), timeout=HUB_QUERY_TIMEOUT_SECONDS, **scope
        )

    gs = await scan(_HUB_GENRE_STYLE_Q)
    artists = await scan(_HUB_ARTIST_Q)
    labels = await scan(_HUB_LABEL_Q)
    al = sorted(artists + labels, key=lambda r: (-r["pop"], r["id"]))[:ARTIST_LABEL_LIMIT]
    for kind, rel in (("Artist", "BY"), ("Label", "ON")):
        chosen = [r["id"] for r in al if r["label"] == kind]
        if chosen:
            template = _HUB_WITNESS_Q.replace("{rel}", rel)
            found = {
                w["id"]: w["witness"]
                for w in await run_query(
                    driver,
                    template.replace("{releases}", patt).replace("{{", "{").replace("}}", "}"),
                    timeout=HUB_QUERY_TIMEOUT_SECONDS,
                    ids=chosen,
                    id=entity_id,
                )
            }
            for r in al:
                if r["label"] == kind:
                    r["witness"] = found.get(r["id"], r["name"])
    rows = [
        {
            "id": r["id"],
            "name": r["name"],
            "type": _output_type(r["label"]),
            "path_names": [name, r["witness"], r["name"]],
            "rel_types": [rel_to_hub, "IS" if r["label"] in ("Genre", "Style") else ("BY" if r["label"] == "Artist" else "ON")],
            "dist": 2,
        }
        for r in sorted(gs, key=lambda r: (r["label"], r["name"])) + al
    ]
    return HubResult(rows=rows, release_count=release_count, built_at=time.time())


async def refresh_hubs(driver: Any, *, max_age: float = 7 * 86400, pause: float = 0.0) -> int:
    """Compute every hub that is missing, stale, or whose release count changed. Returns how many were built.

    One hub failing is logged and skipped; the others, and the previous result of the failed one, keep serving.
    """
    built = 0
    try:
        hubs = await run_query(driver, _HUBS_Q, timeout=30.0, min=HUB_MIN_RELEASES)
    except Exception as exc:
        logger.error("❌ Explore hub list failed", error_type=type(exc).__name__, alert="explore_hub_refresh_failed")
        return 0
    for hub in hubs:
        key = (hub["type"], str(hub["id"]))
        held = await _hub_result(*key)
        if held is not None and held.release_count == hub["releases"] and time.time() - held.built_at < max_age:
            continue
        try:
            name = key[1]
            result = await _build_hub(driver, key[0], key[1], name, int(hub["releases"]))
        except Exception as exc:
            logger.error("❌ Explore hub rebuild failed", hub=f"{key[0]}:{key[1]}", error_type=type(exc).__name__, alert="explore_hub_refresh_failed")
            continue
        _hubs[key] = result
        if _valkey is not None:
            try:
                await _valkey.set(_hub_key(*key), _encode(result))
            except Exception as exc:
                logger.warning("⚠️ Explore hub could not be stored in Valkey", error_type=type(exc).__name__)
        built += 1
        if pause:
            await asyncio.sleep(pause)
    if built:
        logger.info("🌳 Explore hubs rebuilt", built=built)
    return built


RETRY_INITIAL_SECONDS: Final = 30.0
RETRY_MAX_SECONDS: Final = 300.0


async def run_refresh_loop(driver: Any, *, interval: float, max_age: float = 7 * 86400) -> None:
    """Build hubs at once and then every ``interval``; retry quickly while any hub has never been computed."""
    backoff = RETRY_INITIAL_SECONDS
    while True:
        await refresh_hubs(driver, max_age=max_age)
        missing = await _missing_hubs(driver)
        if missing:
            delay = min(backoff, interval)
            backoff = min(backoff * 2, RETRY_MAX_SECONDS)
        else:
            delay = interval
            backoff = RETRY_INITIAL_SECONDS
        await asyncio.sleep(delay)


async def _missing_hubs(driver: Any) -> bool:
    try:
        hubs = await run_query(driver, _HUBS_Q, timeout=30.0, min=HUB_MIN_RELEASES)
    except Exception:
        return True
    for hub in hubs:
        if await _hub_result(hub["type"], str(hub["id"])) is None:
            return True
    return False


# --- entry point -----------------------------------------------------------------------------------------------


async def get_explore_traversal(driver: Any, entity_type: str, entity_id: str, hops: int = 2) -> list[dict[str, Any]]:
    """Discoveries from an entity, as :func:`api.queries.recommend_queries.score_discoveries` consumes them."""
    if not 1 <= hops <= 3:
        hops = 2
    if entity_type not in _ALLOWED:
        return []
    found = await _find_start(driver, entity_type, entity_id)
    if found is None:
        return []
    start, releases = found
    if entity_type in ("genre", "style", "label") and releases >= HUB_MIN_RELEASES:
        if hops == 1:
            return []  # a hub's neighbours at distance 1 are releases, which are never discoveries
        stored = await _hub_result(entity_type, entity_id)
        if stored is None:
            raise HubNotReadyError(f"{entity_type} {entity_id!r} is being computed")
        return [dict(r) for r in stored.rows]
    return await _select(driver, await _bfs(driver, start, hops))
