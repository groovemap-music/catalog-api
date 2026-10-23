"""Reads behind ``GET /api/explore/strata``: one root's ladder, the caller's marks, and labels.

Graph model read here:
  (Release)-[:BY]->(Artist)              release-level artist credit
  (Release)-[:DERIVED_FROM]->(Master)    a release's master; absent when it has none
  (Person)-[:CREDITED_ON {category}]->(Release)
  (User)-[:COLLECTED {date_added}]->(Release), (User)-[:WANTS {date_added}]->(Release)

The catalog read is split from the caller's read on purpose. :func:`get_strata_catalog`
depends on the root alone, so it is the part a cache can hold; :func:`get_strata_marks`
starts from the caller's own ``COLLECTED`` and ``WANTS`` edges, the same edges
``/api/user/collection`` and ``/api/user/wantlist`` read, so it costs the size of one
collection rather than one discography.

No query here filters the Discogs ``"0"`` master sentinel. It was removed at its source
(discogs-graph-enricher) on 2026-10-23, so a release with no master has no
``DERIVED_FROM`` edge, and a filter here would only hide a regression.

MusicBrainz-only releases have no graph node: musicbrainz-graph-enricher merges onto the
Discogs node a MusicBrainz record names and skips the rest. They come from PostgreSQL and
reach a master by the release group's native-id alias first (``discogs_master_id``), and
otherwise through linked sibling releases that derive from exactly one master.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Final

from common import AsyncResilientNeo4jDriver
from common.query_debug import execute_sql
from psycopg.rows import dict_row

from api.queries.helpers import run_query, run_single
from api.strata import Catalog, Mark, Release, id_key


# Every strata read carries a server-side timeout, so no caller waits on an unbounded read,
# even for a root whose real cost exceeds what gm-design-1h2l measured. The slowest read
# measured there took 2,455 ms (strata_ladder, run 0, 10,000+ band); 10 s is four times that
# and leaves room for the Bolt transfer the spike did not time. A read that runs out is a 504.
STRATA_READ_TIMEOUT_SECONDS: Final = 10.0

# ``releases`` is the root's BY degree, which the route checks against MAX_ROOT_RELEASES
# before it reads the ladder. The pattern names no label or property on the far side, so
# Neo4j answers it from the node's degree count rather than by walking the relationships.
_ROOT = """
MATCH (a:Artist {id: $artist_id})
RETURN a.id AS id, a.name AS name, COUNT { (a)<-[:BY]-() } AS releases
"""

# One row per release of the root. Titles are left out: only the few dozen entries a
# response draws need one, and get_strata_labels fetches those by id.
_LADDER = """
MATCH (:Artist {id: $artist_id})<-[:BY]-(r:Release)
WITH DISTINCT r
CALL (r) {
    OPTIONAL MATCH (r)-[:DERIVED_FROM]->(m:Master)
    RETURN m.id AS master_id, CASE WHEN m.year > 0 THEN m.year END AS master_year
    ORDER BY master_id
    LIMIT 1
}
CALL (r) {
    MATCH (r)-[:BY]->(credited:Artist)
    RETURN collect(DISTINCT credited.id) AS credit
}
RETURN r.id AS id, CASE WHEN r.year > 0 THEN r.year END AS year, master_id, master_year, credit
"""

_MARKS = """
MATCH (:User {id: $user_id})-[edge:COLLECTED|WANTS]->(r:Release)-[:BY]->(:Artist {id: $artist_id})
RETURN r.id AS id, type(edge) AS kind, min(edge.date_added) AS recorded_at
"""

_LABELS = """
CALL () {
    UNWIND $release_ids AS release_id
    MATCH (r:Release {id: release_id})
    RETURN collect([r.id, r.title]) AS releases
}
CALL () {
    UNWIND $master_ids AS master_id
    MATCH (m:Master {id: master_id})
    RETURN collect([m.id, m.title]) AS masters
}
CALL () {
    UNWIND $artist_ids AS artist_id
    MATCH (a:Artist {id: artist_id})
    RETURN collect([a.id, a.name]) AS artists
}
RETURN releases, masters, artists
"""

# Two drawn masters share personnel when one person holds the same CREDITED_ON category on
# releases of both. The category is the key, never the raw role string: Discogs roles carry
# free-text bracket qualifiers, so 2.66 million raw strings collapse to 9,187 bare roles
# (gm-design-1h2l, finding 1.3), and the category is coarser still.
_SHARED_PERSONNEL = """
UNWIND $master_ids AS master_id
MATCH (m:Master {id: master_id})<-[:DERIVED_FROM]-(:Release)<-[credit:CREDITED_ON]-(p:Person)
WHERE credit.category IS NOT NULL
WITH p, credit.category AS category, collect(DISTINCT m.id) AS masters
WHERE size(masters) > 1
UNWIND masters AS source
UNWIND masters AS target
WITH source, target, category, p
WHERE source < target
WITH source, target, collect(DISTINCT category) AS categories, count(DISTINCT p) AS people
RETURN source, target, categories, people
ORDER BY people DESC, source, target
LIMIT $limit
"""

_SIBLING_MASTERS = """
UNWIND $release_ids AS release_id
MATCH (r:Release {id: release_id})
OPTIONAL MATCH (r)-[:DERIVED_FROM]->(m:Master)
RETURN r.id AS id, collect(DISTINCT m.id) AS masters
"""

# MusicBrainz-only releases (no discogs_release_id) in any release group that either names
# one of the root's masters or holds a linked sibling of one of the root's releases.
_MB_CANDIDATES = """
WITH candidate_groups AS (
    SELECT rg.mbid
    FROM musicbrainz.release_groups rg
    WHERE rg.discogs_master_id = ANY(%(master_ids)s::bigint[])
    UNION
    SELECT r.release_group_mbid
    FROM musicbrainz.releases r
    WHERE r.discogs_release_id = ANY(%(release_ids)s::bigint[]) AND r.release_group_mbid IS NOT NULL
)
SELECT r.mbid::text AS mbid,
       r.name AS title,
       rg.mbid::text AS group_mbid,
       rg.discogs_master_id AS group_master_id,
       CASE WHEN jsonb_typeof(r.data->'release_events') = 'array' THEN r.data->'release_events' END AS release_events
FROM candidate_groups cg
JOIN musicbrainz.release_groups rg ON rg.mbid = cg.mbid
JOIN musicbrainz.releases r ON r.release_group_mbid = rg.mbid
WHERE r.discogs_release_id IS NULL
"""

_STATEMENT_TIMEOUT = "SELECT set_config('statement_timeout', %(timeout)s, true)"

_MB_SIBLINGS = """
SELECT release_group_mbid::text AS group_mbid, array_agg(DISTINCT discogs_release_id) AS release_ids
FROM musicbrainz.releases
WHERE release_group_mbid = ANY(%(group_mbids)s::uuid[]) AND discogs_release_id IS NOT NULL
GROUP BY release_group_mbid
"""


async def get_strata_root(driver: AsyncResilientNeo4jDriver, artist_id: str) -> dict[str, Any] | None:
    return await run_single(driver, _ROOT, timeout=STRATA_READ_TIMEOUT_SECONDS, artist_id=artist_id)


async def get_strata_ladder(driver: AsyncResilientNeo4jDriver, artist_id: str) -> tuple[list[Release], dict[str, int | None]]:
    """Every release of the root with its master, year, and credit, plus each master's year."""
    rows = await run_query(driver, _LADDER, timeout=STRATA_READ_TIMEOUT_SECONDS, artist_id=artist_id)
    releases: list[Release] = []
    master_years: dict[str, int | None] = {}
    for row in rows:
        master_id = None if row["master_id"] is None else str(row["master_id"])
        if master_id is not None:
            master_years[master_id] = row["master_year"]
        credit = tuple(sorted((str(artist) for artist in row["credit"]), key=id_key))
        releases.append(Release(str(row["id"]), master_id, row["year"], credit))
    return releases, master_years


async def get_strata_catalog(driver: AsyncResilientNeo4jDriver, pg_pool: Any, artist_id: str, artist_name: str | None) -> Catalog:
    """The caller-independent side of a strata read: the Neo4j ladder plus MusicBrainz-only releases.

    ``pg_pool`` may be ``None``, and then the ladder carries Discogs releases only.
    """
    releases, master_years = await get_strata_ladder(driver, artist_id)
    if pg_pool is not None and master_years:
        releases = releases + await get_musicbrainz_only_releases(driver, pg_pool, releases, set(master_years))
    return Catalog(artist_id, artist_name, tuple(releases), master_years)


async def get_strata_marks(driver: AsyncResilientNeo4jDriver, user_id: str, artist_id: str) -> dict[str, Mark]:
    """The caller's collector state on the root's releases, keyed by Discogs release id."""
    rows = await run_query(driver, _MARKS, timeout=STRATA_READ_TIMEOUT_SECONDS, user_id=user_id, artist_id=artist_id)
    found: dict[str, dict[str, Any]] = defaultdict(dict)
    for row in rows:
        found[str(row["id"])][row["kind"]] = _recorded(row["recorded_at"])
    return {
        release_id: Mark(
            owned="COLLECTED" in kinds,
            wanted="WANTS" in kinds,
            owned_at=kinds.get("COLLECTED"),
            wanted_at=kinds.get("WANTS"),
        )
        for release_id, kinds in found.items()
    }


def _recorded(value: Any) -> str | None:
    """``date_added`` as text: the sync stores Discogs' ISO string, but a driver temporal is tolerated."""
    if value is None:
        return None
    iso_format = getattr(value, "iso_format", None)
    return str(iso_format()) if callable(iso_format) else str(value)


async def get_strata_labels(
    driver: AsyncResilientNeo4jDriver, release_ids: list[str], master_ids: list[str], artist_ids: list[str]
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Titles and names for the entries a response draws, by id."""
    row = await run_single(
        driver, _LABELS, timeout=STRATA_READ_TIMEOUT_SECONDS, release_ids=release_ids, master_ids=master_ids, artist_ids=artist_ids
    )
    if row is None:
        return {}, {}, {}
    return (
        {str(key): value for key, value in row["releases"]},
        {str(key): value for key, value in row["masters"]},
        {str(key): value for key, value in row["artists"]},
    )


async def get_shared_personnel(driver: AsyncResilientNeo4jDriver, master_ids: list[str], limit: int) -> list[dict[str, Any]]:
    """Pairs of the given masters that share credited people, keyed on ``CREDITED_ON.category``."""
    if len(master_ids) < 2:
        return []
    return await run_query(driver, _SHARED_PERSONNEL, timeout=STRATA_READ_TIMEOUT_SECONDS, master_ids=master_ids, limit=limit)


async def get_musicbrainz_only_releases(
    driver: AsyncResilientNeo4jDriver, pg_pool: Any, releases: list[Release], master_ids: set[str]
) -> list[Release]:
    """MusicBrainz-only releases that join one of the root's masters, native id first.

    A release group that names a master (``discogs_master_id``) attaches to that master and
    nowhere else, even when its linked siblings say otherwise. A group without one attaches
    only when its linked siblings derive from exactly one master; two or more is ambiguous
    and siblings with no master give no path. A release that reaches a master outside this
    root's discography is not part of this ladder.
    """
    release_ids = [int(r.id) for r in releases if r.id.isdigit()]
    numeric_masters = [int(m) for m in master_ids if m.isdigit()]
    async with pg_pool.connection() as conn, conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        # The same bound as the graph reads, scoped to this transaction (a QueryCanceled past it).
        await cur.execute(_STATEMENT_TIMEOUT, {"timeout": f"{int(STRATA_READ_TIMEOUT_SECONDS * 1000)}ms"})
        await execute_sql(cur, _MB_CANDIDATES, {"master_ids": numeric_masters, "release_ids": release_ids})
        candidates: list[dict[str, Any]] = await cur.fetchall()
        unlinked = sorted({row["group_mbid"] for row in candidates if row["group_master_id"] is None})
        siblings: dict[str, list[str]] = {}
        if unlinked:
            await execute_sql(cur, _MB_SIBLINGS, {"group_mbids": unlinked})
            siblings = {row["group_mbid"]: [str(rid) for rid in row["release_ids"]] for row in await cur.fetchall()}

    known = {r.id: r.master_id for r in releases}
    sibling_masters: dict[str, set[str]] = {}
    unknown = sorted({rid for ids in siblings.values() for rid in ids if rid not in known}, key=id_key)
    if unknown:
        for row in await run_query(driver, _SIBLING_MASTERS, timeout=STRATA_READ_TIMEOUT_SECONDS, release_ids=unknown):
            sibling_masters[str(row["id"])] = {str(m) for m in row["masters"]}
    for rid, known_master in known.items():
        sibling_masters[rid] = {known_master} if known_master is not None else set()

    attached: list[Release] = []
    for row in candidates:
        if row["group_master_id"] is not None:
            master_id: str | None = str(row["group_master_id"])
        else:
            masters = {m for rid in siblings.get(row["group_mbid"], []) for m in sibling_masters.get(rid, set())}
            master_id = next(iter(masters)) if len(masters) == 1 else None
        if master_id is None or master_id not in master_ids:
            continue
        attached.append(Release(row["mbid"], master_id, _earliest_event_year(row["release_events"]), (), "musicbrainz", row["title"]))
    return attached


def _earliest_event_year(events: list[Any] | None) -> int | None:
    """A MusicBrainz release's own year: the earliest release event carrying a four-digit year."""
    years = []
    for event in events or []:
        head = str(event.get("date") or "")[:4] if isinstance(event, dict) else ""
        if head.isdigit() and int(head) > 0:
            years.append(int(head))
    return min(years) if years else None
