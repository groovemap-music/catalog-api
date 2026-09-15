"""The reads behind the CrateFit item-in-hand fit profile.

A fit answer compares one candidate release against everything a collector already
holds, so the request path needs two things and nothing else: the collector's id sets,
and the candidate's own context including the release-to-master hop that no existing
query makes. Both are one Cypher round trip, because a fit profile is computed while a
person is standing in a shop holding a record.

Three shapes of the reads are worth stating:

* **The collection read is folded in the process, not in Cypher.** The Cypher returns one
  row per held release with its facets, and :func:`fold_collection` turns those rows into
  the id sets, the per-facet counts, and the per-genre label and artist sets the bridge
  heuristic needs. Keeping the traversal a single flat pattern lets the planner seek the
  ``COLLECTED`` edges once, and the fold is a pure O(collection) pass that can be tested
  without a database.
* **The collection read is cached per user for ten minutes**, under a key that
  :meth:`api.cache.RecommendCache.invalidate_user` already sweeps, so a collection sync
  invalidates the fit inputs for free rather than through a second invalidation path.
* **Rarity is read, never computed.** :func:`get_release_rarity` selects the two columns
  the profile shows from ``insights.release_rarity``, which the insights pipeline writes
  on its own schedule. Nothing here scores a release's rarity on a request.

Graph model this module depends on::

    (User)-[:COLLECTED]->(Release)
    (Release)-[:BY]->(Artist)
    (Release)-[:ON]->(Label)
    (Release)-[:IS]->(Genre)
    (Release)-[:IS]->(Style)
    (Release)-[:ISSUED_ON]->(Medium {family})
    (Release)-[:DERIVED_FROM]->(Master)
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

import structlog
from common import AsyncResilientNeo4jDriver
from psycopg.rows import dict_row

from api.queries.helpers import run_query, run_single


if TYPE_CHECKING:  # pragma: no cover
    from api.cache import RecommendCache


logger = structlog.get_logger(__name__)

__all__ = [
    "COLLECTION_CACHE_TTL",
    "collection_cache_key",
    "empty_collection",
    "fold_collection",
    "get_collection_ids",
    "get_release_context",
    "get_release_rarity",
    "held_title_key",
]

# Ten minutes, as the epic design asks. Long enough that a collector paging through a
# shop's racks pays for the collection read once, short enough that a sync they ran on
# their phone a moment ago is reflected even if the invalidation never fires.
COLLECTION_CACHE_TTL: Final = 600


def collection_cache_key(user_id: str) -> str:
    """Return the cache key the folded collection is stored under.

    Deliberately shaped as a ``recommend:explore:{user_id}:…`` key: that is the pattern
    :meth:`api.cache.RecommendCache.invalidate_user` scans, and the collection sync
    already calls it. A key outside that pattern would survive a sync and serve a fit
    profile computed against a collection the collector no longer has.
    """
    return f"recommend:explore:{user_id}:fit:collection"


# One row per held release, carrying only the facets a fit component reads. `collect`
# skips nulls, so a release with no label contributes an empty list rather than a `[null]`.
_COLLECTION_CYPHER: Final = """
MATCH (u:User {id: $user_id})-[:COLLECTED]->(r:Release)
OPTIONAL MATCH (r)-[:BY]->(artist:Artist)
OPTIONAL MATCH (r)-[:ON]->(label:Label)
OPTIONAL MATCH (r)-[:IS]->(genre:Genre)
OPTIONAL MATCH (r)-[:IS]->(style:Style)
OPTIONAL MATCH (r)-[:DERIVED_FROM]->(master:Master)
RETURN r.id AS release_id,
       r.title AS title,
       collect(DISTINCT artist.id) AS artist_ids,
       collect(DISTINCT label.id) AS label_ids,
       collect(DISTINCT genre.name) AS genres,
       collect(DISTINCT style.name) AS styles,
       collect(DISTINCT master.id) AS master_ids
"""

# The candidate's own context. Every dimension is aggregated in its own `WITH` stage so
# the optional matches never multiply out into a cartesian product: a master with forty
# siblings and a release with three styles would otherwise produce a hundred and twenty
# intermediate rows for a query that returns one.
_RELEASE_CONTEXT_CYPHER: Final = """
MATCH (r:Release {id: $release_id})
OPTIONAL MATCH (r)-[:BY]->(artist:Artist)
WITH r, collect(DISTINCT CASE WHEN artist IS NULL THEN NULL ELSE {id: artist.id, name: artist.name} END) AS artists
OPTIONAL MATCH (r)-[:ON]->(label:Label)
WITH r, artists, collect(DISTINCT CASE WHEN label IS NULL THEN NULL ELSE {id: label.id, name: label.name} END) AS labels
OPTIONAL MATCH (r)-[:IS]->(genre:Genre)
WITH r, artists, labels, collect(DISTINCT genre.name) AS genres
OPTIONAL MATCH (r)-[:IS]->(style:Style)
WITH r, artists, labels, genres, collect(DISTINCT style.name) AS styles
OPTIONAL MATCH (r)-[:ISSUED_ON]->(medium:Medium)
WITH r, artists, labels, genres, styles, collect(DISTINCT medium.family) AS media_families
OPTIONAL MATCH (r)-[:DERIVED_FROM]->(master:Master)
WITH r, artists, labels, genres, styles, media_families, master
OPTIONAL MATCH (master)<-[:DERIVED_FROM]-(sibling:Release)
WHERE sibling.id <> r.id
OPTIONAL MATCH (sibling)-[:ISSUED_ON]->(sibling_medium:Medium)
WITH r, artists, labels, genres, styles, media_families, master, sibling,
     collect(DISTINCT sibling_medium.family) AS sibling_families
WITH r, artists, labels, genres, styles, media_families, master,
     collect(DISTINCT CASE WHEN sibling IS NULL THEN NULL ELSE
         {id: sibling.id, title: sibling.title, year: sibling.year, media_families: sibling_families}
     END) AS siblings
RETURN r.id AS id, r.title AS title, r.year AS year,
       artists, labels, genres, styles, media_families,
       master.id AS master_id, master.title AS master_title, siblings
"""

# Two columns, not the sixteen `get_rarity_for_release` reads: the fit profile shows the
# score and the tier, and a request path has no use for the per-signal breakdown.
_RELEASE_RARITY_SQL: Final = """
SELECT rarity_score, tier
FROM insights.release_rarity
WHERE release_id = %s
"""


def empty_collection() -> dict[str, Any]:
    """Return the folded shape of a collection that holds nothing.

    A collector with an empty collection is a real caller — somebody who signed up this
    morning and is already in a shop — so every component scores against this rather than
    against a missing value.
    """
    return {
        "release_ids": [],
        "master_ids": [],
        "artist_ids": [],
        "label_ids": [],
        "genres": [],
        "styles": [],
        "artist_counts": {},
        "label_counts": {},
        "genre_counts": {},
        "style_counts": {},
        "genre_artists": {},
        "genre_labels": {},
        "genre_styles": {},
        "held_titles": {},
    }


def held_title_key(artist_id: str, title: str) -> str:
    """Return the lookup key for "the collector holds this artist's record of this name".

    Case-folded because Discogs titles are typed by contributors, and an artist id rather
    than an artist name because two artists genuinely share a name often enough that the
    name alone would report a redundancy that is not one.
    """
    return f"{artist_id}|{title.casefold().strip()}"


def fold_collection(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Fold one row per held release into the id sets and counts the scoring reads.

    Everything is returned as lists and dicts rather than sets: the result is cached as
    JSON, and a set would round-trip through Redis as the repr of a set. Lists are sorted
    so two folds of the same collection are byte-identical, which is what makes the cached
    body and the scoring deterministic.
    """
    folded = empty_collection()
    release_ids: set[str] = set()
    master_ids: set[str] = set()
    artist_ids: set[str] = set()
    label_ids: set[str] = set()
    genres: set[str] = set()
    styles: set[str] = set()
    artist_counts: dict[str, int] = {}
    label_counts: dict[str, int] = {}
    genre_counts: dict[str, int] = {}
    style_counts: dict[str, int] = {}
    genre_artists: dict[str, set[str]] = {}
    genre_labels: dict[str, set[str]] = {}
    genre_styles: dict[str, set[str]] = {}
    held_titles: dict[str, str] = {}

    for row in rows:
        release_id = row.get("release_id")
        if not release_id:
            continue
        release_ids.add(str(release_id))
        row_artists = [str(value) for value in row.get("artist_ids") or [] if value]
        row_labels = [str(value) for value in row.get("label_ids") or [] if value]
        row_genres = [str(value) for value in row.get("genres") or [] if value]
        row_styles = [str(value) for value in row.get("styles") or [] if value]
        master_ids.update(str(value) for value in row.get("master_ids") or [] if value)

        artist_ids.update(row_artists)
        label_ids.update(row_labels)
        genres.update(row_genres)
        styles.update(row_styles)

        # A count is "how many held releases carry this facet", so it increments once per
        # release even when the release names the same artist twice.
        for artist_id in dict.fromkeys(row_artists):
            artist_counts[artist_id] = artist_counts.get(artist_id, 0) + 1
        for label_id in dict.fromkeys(row_labels):
            label_counts[label_id] = label_counts.get(label_id, 0) + 1
        for style in dict.fromkeys(row_styles):
            style_counts[style] = style_counts.get(style, 0) + 1
        for genre in dict.fromkeys(row_genres):
            genre_counts[genre] = genre_counts.get(genre, 0) + 1
            genre_artists.setdefault(genre, set()).update(row_artists)
            genre_labels.setdefault(genre, set()).update(row_labels)
            genre_styles.setdefault(genre, set()).update(row_styles)

        title = row.get("title")
        if title:
            for artist_id in row_artists:
                held_titles[held_title_key(artist_id, str(title))] = str(title)

    folded["release_ids"] = sorted(release_ids)
    folded["master_ids"] = sorted(master_ids)
    folded["artist_ids"] = sorted(artist_ids)
    folded["label_ids"] = sorted(label_ids)
    folded["genres"] = sorted(genres)
    folded["styles"] = sorted(styles)
    folded["artist_counts"] = dict(sorted(artist_counts.items()))
    folded["label_counts"] = dict(sorted(label_counts.items()))
    folded["genre_counts"] = dict(sorted(genre_counts.items()))
    folded["style_counts"] = dict(sorted(style_counts.items()))
    folded["genre_artists"] = {genre: sorted(values) for genre, values in sorted(genre_artists.items())}
    folded["genre_labels"] = {genre: sorted(values) for genre, values in sorted(genre_labels.items())}
    folded["genre_styles"] = {genre: sorted(values) for genre, values in sorted(genre_styles.items())}
    folded["held_titles"] = dict(sorted(held_titles.items()))
    return folded


async def get_collection_ids(
    driver: AsyncResilientNeo4jDriver,
    user_id: str,
    *,
    cache: RecommendCache | None = None,
) -> dict[str, Any]:
    """Return the collector's id sets, per-facet counts, and per-genre neighbourhoods.

    One Cypher round trip, folded by :func:`fold_collection` and cached for
    :data:`COLLECTION_CACHE_TTL` under :func:`collection_cache_key`.

    Args:
        driver: The async Neo4j driver.
        user_id: The collector whose collection is read.
        cache: The recommendation cache, when one is wired. Absent, every call reads.

    Returns:
        The folded collection. An unknown user and an empty collection are the same
        answer — :func:`empty_collection` — because from a fit profile's point of view
        they are the same situation.
    """
    cache_key = collection_cache_key(user_id)
    if cache is not None:
        cached = await cache.get(cache_key)
        if cached is not None:
            return cached

    rows = await run_query(driver, _COLLECTION_CYPHER, user_id=user_id)
    folded = fold_collection(rows)

    if cache is not None:
        await cache.set(cache_key, folded, ttl=COLLECTION_CACHE_TTL)
    return folded


async def get_release_context(driver: AsyncResilientNeo4jDriver, release_id: str) -> dict[str, Any] | None:
    """Return one candidate release's context, including its master and that master's siblings.

    The master hop is the reason this query exists: redundancy has to know whether the
    collector already holds *another pressing of the same record*, and no query in the
    service walked ``(r)-[:DERIVED_FROM]->(m)<-[:DERIVED_FROM]-(sibling)`` before.

    Returns ``None`` when the graph carries no such release, which the endpoint reports
    as a 404.
    """
    row = await run_single(driver, _RELEASE_CONTEXT_CYPHER, release_id=release_id)
    if row is None:
        return None
    return {
        "id": str(row.get("id")),
        "title": row.get("title"),
        "year": row.get("year"),
        "artists": [dict(entry) for entry in row.get("artists") or []],
        "labels": [dict(entry) for entry in row.get("labels") or []],
        "genres": [str(value) for value in row.get("genres") or [] if value],
        "styles": [str(value) for value in row.get("styles") or [] if value],
        "media_families": [str(value) for value in row.get("media_families") or [] if value],
        "master_id": str(row["master_id"]) if row.get("master_id") else None,
        "master_title": row.get("master_title"),
        "siblings": [dict(entry) for entry in row.get("siblings") or []],
    }


async def get_release_rarity(pool: Any, release_id: str) -> dict[str, Any] | None:
    """Return the precomputed rarity score and tier for a release, or ``None``.

    Read only: ``insights.release_rarity`` is written by the insights pipeline on its own
    schedule, and a fit request never scores rarity itself. Never raises — rarity is
    context beside the fit answer, not the answer, so an unreadable insights schema
    degrades the profile rather than the request.
    """
    if pool is None:
        return None
    try:
        numeric_id = int(release_id)
    except TypeError, ValueError:
        return None
    try:
        async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(_RELEASE_RARITY_SQL, (numeric_id,))
            row: dict[str, Any] | None = await cur.fetchone()
    except Exception:
        logger.debug("⚠️ Rarity lookup skipped", release_id=release_id, exc_info=True)
        return None
    if row is None:
        return None
    score = row.get("rarity_score")
    return {"score": float(score) if score is not None else None, "tier": row.get("tier")}
