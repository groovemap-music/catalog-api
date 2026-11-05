"""Neo4j Cypher queries for Label DNA fingerprinting.

Computes multi-dimensional fingerprints for record labels from graph data:
genre/style profiles, era distribution, artist diversity, format preference,
and label-to-label similarity via cosine similarity on genre vectors.
"""

import asyncio
from typing import Any

from common import AsyncResilientNeo4jDriver

from api.queries.helpers import run_query, run_single
from api.queries.similarity import cosine_similarity, to_genre_vector


# Minimum releases for a label to have a meaningful fingerprint
MIN_RELEASES = 5


async def get_label_identity(driver: AsyncResilientNeo4jDriver, label_id: str) -> dict[str, Any] | None:
    """Get basic label info and release/artist counts.

    Reads ``Label.release_count`` and ``Label.artist_count`` (written by the enricher; equal to the live counts for
    Columbia: 351,183 and 37,985). Counts them live, as before, only when the label lacks them. The live count of
    label 1818 took 9.4M hits and 5.4 s (audit H9).
    """
    stored = await run_single(
        driver,
        """
        MATCH (l:Label {id: $label_id})
        RETURN l.id AS label_id, l.name AS label_name, l.release_count AS release_count, l.artist_count AS artist_count
        """,
        label_id=label_id,
    )
    if stored is None or (stored["release_count"] is not None and stored["artist_count"] is not None):
        return stored
    cypher = """
    MATCH (l:Label {id: $label_id})
    OPTIONAL MATCH (l)<-[:ON]-(r:Release)
    OPTIONAL MATCH (r)-[:BY]->(a:Artist)
    RETURN l.id AS label_id, l.name AS label_name,
           count(DISTINCT r) AS release_count,
           count(DISTINCT a) AS artist_count
    """
    return await run_single(driver, cypher, label_id=label_id)


async def get_label_genre_profile(driver: AsyncResilientNeo4jDriver, label_id: str) -> list[dict[str, Any]]:
    """Get genre distribution for a label's releases."""
    cypher = """
    MATCH (r:Release)-[:ON]->(l:Label {id: $label_id}), (r)-[:IS]->(g:Genre)
    WITH g.name AS name, count(DISTINCT r) AS count
    RETURN name, count
    ORDER BY count DESC
    """
    return await run_query(driver, cypher, label_id=label_id)


async def get_label_style_profile(driver: AsyncResilientNeo4jDriver, label_id: str) -> list[dict[str, Any]]:
    """Get style distribution for a label's releases."""
    cypher = """
    MATCH (r:Release)-[:ON]->(l:Label {id: $label_id}), (r)-[:IS]->(s:Style)
    WITH s.name AS name, count(DISTINCT r) AS count
    RETURN name, count
    ORDER BY count DESC
    """
    return await run_query(driver, cypher, label_id=label_id)


async def get_label_genre_style_profile(driver: AsyncResilientNeo4jDriver, label_id: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Get a label's genre and style distributions from one pass over its releases.

    The two separate reads each walked every release and its ``IS`` edges (Columbia: 7.05M and 7.02M db hits). One pass
    groups by kind and name, 8.5M hits and 39 MB, with the same counts and the same order.
    """
    cypher = """
    MATCH (r:Release)-[:ON]->(l:Label {id: $label_id}), (r)-[:IS]->(x)
    WHERE x:Genre OR x:Style
    WITH labels(x)[0] AS kind, x.name AS name, count(DISTINCT r) AS count
    RETURN kind, name, count
    ORDER BY count DESC
    """
    rows = await run_query(driver, cypher, label_id=label_id)
    genres = [{"name": r["name"], "count": r["count"]} for r in rows if r["kind"] == "Genre"]
    styles = [{"name": r["name"], "count": r["count"]} for r in rows if r["kind"] == "Style"]
    return genres, styles


async def get_label_year_counts(driver: AsyncResilientNeo4jDriver, label_id: str) -> list[dict[str, Any]]:
    """Get the number of releases per year for a label: the one read behind the decade profile and the active years."""
    cypher = """
    MATCH (r:Release)-[:ON]->(l:Label {id: $label_id})
    WHERE r.year > 0
    WITH r.year AS year, count(DISTINCT r) AS count
    RETURN year, count
    ORDER BY year
    """
    return await run_query(driver, cypher, label_id=label_id)


def decades_from_years(year_counts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Release count by decade, from per-year counts (a release has one year, so the counts add)."""
    by_decade: dict[int, int] = {}
    for row in year_counts:
        decade = (row["year"] // 10) * 10
        by_decade[decade] = by_decade.get(decade, 0) + row["count"]
    return [{"decade": decade, "count": by_decade[decade]} for decade in sorted(by_decade)]


async def get_label_decade_profile(driver: AsyncResilientNeo4jDriver, label_id: str) -> list[dict[str, Any]]:
    """Get release count by decade for a label."""
    return decades_from_years(await get_label_year_counts(driver, label_id))


async def get_label_active_years(driver: AsyncResilientNeo4jDriver, label_id: str) -> list[int]:
    """Get sorted list of years in which a label had releases."""
    return [row["year"] for row in await get_label_year_counts(driver, label_id)]


async def get_label_format_profile(driver: AsyncResilientNeo4jDriver, label_id: str) -> list[dict[str, Any]]:
    """Get format distribution for a label's releases."""
    cypher = """
    MATCH (r:Release)-[:ON]->(l:Label {id: $label_id})
    WHERE r.formats IS NOT NULL
    UNWIND r.formats AS fmt
    WITH fmt AS name, count(DISTINCT r) AS count
    RETURN name, count
    ORDER BY count DESC
    """
    return await run_query(driver, cypher, label_id=label_id)


async def get_label_media_family_counts(driver: AsyncResilientNeo4jDriver, label_id: str) -> list[dict[str, Any]]:
    """Get release counts per media family for a label, from ``ISSUED_ON`` edges.

    Counts each (release, family) once even when the graph carries both a
    Discogs- and a MusicBrainz-sourced ``ISSUED_ON`` edge for the same release,
    and even when a release has more than one medium in the same family.
    """
    cypher = """
    MATCH (l:Label {id: $label_id})<-[:ON]-(r:Release)-[:ISSUED_ON]->(m:Medium)-[:IN_FAMILY]->(f:MediaFamily)
    WITH DISTINCT r, f
    WITH f.name AS family, count(DISTINCT r) AS count
    RETURN family, count
    ORDER BY count DESC
    """
    return await run_query(driver, cypher, label_id=label_id)


async def get_label_medium_counts(driver: AsyncResilientNeo4jDriver, label_id: str) -> list[dict[str, Any]]:
    """Get release counts per medium (within its family) for a label, from ``ISSUED_ON`` edges.

    Counts each (release, medium) once even when the graph carries both a
    Discogs- and a MusicBrainz-sourced ``ISSUED_ON`` edge over the same node.
    """
    cypher = """
    MATCH (l:Label {id: $label_id})<-[:ON]-(r:Release)-[:ISSUED_ON]->(m:Medium)-[:IN_FAMILY]->(f:MediaFamily)
    WITH DISTINCT r, m, f
    WITH f.name AS family, m.id AS medium_id, m.label AS medium_label, count(DISTINCT r) AS count
    RETURN family, medium_id, medium_label, count
    ORDER BY family, count DESC
    """
    return await run_query(driver, cypher, label_id=label_id)


async def get_label_media_families_fallback(driver: AsyncResilientNeo4jDriver, label_id: str) -> list[dict[str, Any]]:
    """Fall back to ``Release.media_families`` when a label's releases carry no ``ISSUED_ON`` edges yet.

    Pre-cutover graphs only have the raw ``media_families`` list property, not
    the ``ISSUED_ON`` → ``Medium`` → ``IN_FAMILY`` → ``MediaFamily`` traversal;
    this yields family-level counts only, with no per-medium detail.
    """
    cypher = """
    MATCH (r:Release)-[:ON]->(l:Label {id: $label_id})
    WHERE r.media_families IS NOT NULL
    UNWIND r.media_families AS family
    WITH family, count(DISTINCT r) AS count
    RETURN family, count
    ORDER BY count DESC
    """
    return await run_query(driver, cypher, label_id=label_id)


async def get_label_media_profile(driver: AsyncResilientNeo4jDriver, label_id: str) -> list[dict[str, Any]]:
    """Get a label's media profile grouped by family, with per-medium detail nested inside.

    Runs the family- and medium-level ``ISSUED_ON`` queries in parallel. When
    the label has no ``ISSUED_ON`` edges yet (pre-cutover graph), falls back
    to :func:`get_label_media_families_fallback` and returns family-level
    counts with an empty ``mediums`` list for each.
    """
    families, mediums = await asyncio.gather(
        get_label_media_family_counts(driver, label_id),
        get_label_medium_counts(driver, label_id),
    )

    if not families:
        fallback = await get_label_media_families_fallback(driver, label_id)
        return [{"family": row["family"], "count": row["count"], "mediums": []} for row in fallback]

    mediums_by_family: dict[str, list[dict[str, Any]]] = {}
    for row in mediums:
        mediums_by_family.setdefault(row["family"], []).append({"id": row["medium_id"], "label": row["medium_label"], "count": row["count"]})

    return [{"family": row["family"], "count": row["count"], "mediums": mediums_by_family.get(row["family"], [])} for row in families]


async def get_label_full_profile(
    driver: AsyncResilientNeo4jDriver,
    label_id: str,
) -> dict[str, Any] | None:
    """Get identity, genre, style, and decade profiles in parallel.

    Runs 4 lightweight queries concurrently instead of 6 sequential ones.
    The identity query includes artist counts; genre, style, and decade
    profiles are fetched in parallel with asyncio.gather.
    """
    identity = await get_label_identity(driver, label_id)
    if not identity:
        return None

    # Return identity early if release count is below threshold —
    # avoids running 3 parallel profile queries for labels that will
    # be rejected by _build_dna anyway.
    if identity["release_count"] < MIN_RELEASES:
        return {
            "label_id": identity["label_id"],
            "label_name": identity["label_name"],
            "release_count": identity["release_count"],
            "artist_count": identity["artist_count"],
            "genres": [],
            "styles": [],
            "decades": [],
            "years": [],
        }

    (genres, styles), year_counts = await asyncio.gather(
        get_label_genre_style_profile(driver, label_id),
        get_label_year_counts(driver, label_id),
    )
    decades = decades_from_years(year_counts)

    return {
        "label_id": identity["label_id"],
        "label_name": identity["label_name"],
        "release_count": identity["release_count"],
        "artist_count": identity["artist_count"],
        "genres": genres,
        "styles": styles,
        "decades": decades,
        "years": [row["year"] for row in year_counts],
    }


# Top five styles of the target by release count, ties by name; for each, every other label's release count in that style,
# summed over the five. ``total_shared`` is the same number the live form computes.
_CANDIDATES_STORED = """
MATCH (l:Label {id: $label_id})-[h:HAS_STYLE]->(s:Style)
WITH l, s, h.release_count AS style_count
ORDER BY style_count DESC, s.name
LIMIT 5
WITH l, collect(s) AS top_styles
UNWIND top_styles AS s2
MATCH (s2)<-[h2:HAS_STYLE]-(l2:Label)
WHERE l2 <> l
WITH l2, sum(h2.release_count) AS total_shared
WHERE total_shared >= $min_releases
RETURN l2.id AS label_id, l2.name AS label_name, total_shared
ORDER BY total_shared DESC, l2.id
LIMIT 100
"""

_CANDIDATES_LIVE = """
MATCH (l:Label {id: $label_id})<-[:ON]-(r:Release)-[:IS]->(s:Style)
WITH l, s, count(DISTINCT r) AS style_count
ORDER BY style_count DESC, s.name
LIMIT 5
WITH l, collect(s) AS top_styles
UNWIND top_styles AS s2
CALL {
    WITH s2, l
    MATCH (s2)<-[:IS]-(r2:Release)-[:ON]->(l2:Label)
    WHERE l2 <> l
    RETURN l2, count(DISTINCT r2) AS shared_in_style
}
WITH l2, sum(shared_in_style) AS total_shared
WHERE total_shared >= $min_releases
RETURN l2.id AS label_id, l2.name AS label_name, total_shared
ORDER BY total_shared DESC, l2.id
LIMIT 100
"""


async def get_candidate_labels_genre_vectors(driver: AsyncResilientNeo4jDriver, label_id: str) -> list[dict[str, Any]]:
    """Get genre vectors for labels sharing styles with the target label.

    Returns each candidate label with its genre distribution,
    filtered to labels with at least MIN_RELEASES releases.

    Optimizations:
    - Uses style-based similarity instead of genre-based.  Styles are far
      more specific than genres (757 styles vs 16 genres), so each style
      traversal processes 5-10x fewer releases.  e.g. Trance (~671K releases)
      vs Electronic (~4.9M releases).
    - Two-phase approach: first find candidate IDs (fast), then batch-fetch
      genre profiles for the top 100 candidates only.
    - Uses CALL {} per-style to prevent cross-style row explosion.
    - Phase 2 splits into 25-label batches with 2 lightweight queries each.
    - Timeout protection on each phase.
    """
    # Phase 1: Find candidate label IDs via style overlap (top 5 styles).
    # With the enricher's (:Label)-[:HAS_STYLE {release_count}]->(:Style) edges, a label's styles and, for every other label
    # in a style, how many of its releases are in it are single edge reads: no release is scanned and nothing is capped
    # per style (the live form took 54.7M hits and 28.3 s, driven by the size of the styles). Without those edges the
    # live form below gives the same candidates.
    has_edges = await run_single(
        driver,
        "MATCH (l:Label {id: $label_id}) RETURN EXISTS { (l)-[:HAS_STYLE]->() } AS has_edges",
        label_id=label_id,
    )
    if has_edges and has_edges["has_edges"]:
        candidates = await run_query(driver, _CANDIDATES_STORED, timeout=60, label_id=label_id, min_releases=MIN_RELEASES)
    else:
        candidates = await run_query(driver, _CANDIDATES_LIVE, timeout=60, label_id=label_id, min_releases=MIN_RELEASES)

    if not candidates:
        return []

    # Phase 2: Batch-fetch genre profiles + release counts for candidates.
    # Split into 2 lightweight queries per batch (release counts + genre
    # distribution), run concurrently.  Batches of 25 labels keep peak
    # memory low (~60-75MB per batch vs 585MB for 100 labels in one query).
    candidate_ids = [c["label_id"] for c in candidates]

    batch_size = 25
    batches = [candidate_ids[i : i + batch_size] for i in range(0, len(candidate_ids), batch_size)]

    async def _fetch_batch(batch_ids: list[str]) -> list[dict[str, Any]]:
        count_cypher = """
        UNWIND $label_ids AS lid
        MATCH (l:Label {id: lid})
        RETURN l.id AS label_id, l.name AS label_name, l.release_count AS release_count
        """
        live_count_cypher = """
        UNWIND $label_ids AS lid
        MATCH (l:Label {id: lid})<-[:ON]-(r:Release)
        RETURN l.id AS label_id, l.name AS label_name, count(DISTINCT r) AS release_count
        """
        genre_cypher = """
        UNWIND $label_ids AS lid
        MATCH (l:Label {id: lid})<-[:ON]-(r:Release)-[:IS]->(g:Genre)
        WITH l, g.name AS genre, count(DISTINCT r) AS genre_count
        RETURN l.id AS label_id,
               collect({name: genre, count: genre_count}) AS genres
        """
        counts, genres = await asyncio.gather(
            run_query(driver, count_cypher, timeout=60, label_ids=batch_ids),
            run_query(driver, genre_cypher, timeout=60, label_ids=batch_ids),
        )
        # The stored count; counted live only for a label that lacks it, as before.
        unstored = [row["label_id"] for row in counts if row["release_count"] is None]
        if unstored:
            live = {r["label_id"]: r for r in await run_query(driver, live_count_cypher, timeout=60, label_ids=unstored)}
            counts = [row if row["release_count"] is not None else live.get(row["label_id"], {**row, "release_count": 0}) for row in counts]

        genre_map: dict[str, list[dict[str, Any]]] = {row["label_id"]: row["genres"] for row in genres}
        results: list[dict[str, Any]] = []
        for row in counts:
            lid = row["label_id"]
            results.append(
                {
                    "label_id": lid,
                    "label_name": row["label_name"],
                    "release_count": row["release_count"],
                    "genres": genre_map.get(lid, []),
                }
            )
        return results

    batch_results = await asyncio.gather(*[_fetch_batch(batch) for batch in batches])
    return [item for batch in batch_results for item in batch]


def compute_similar_labels(
    target_genres: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Rank candidate labels by cosine similarity to the target's genre vector."""
    target_vec = to_genre_vector(target_genres)
    if not target_vec:
        return []

    target_genre_names = set(target_vec)
    results = []
    for candidate in candidates:
        cand_vec = to_genre_vector(candidate["genres"])
        sim = cosine_similarity(target_vec, cand_vec)
        if sim > 0.0:
            shared = sorted(set(cand_vec) & target_genre_names)
            results.append(
                {
                    "label_id": candidate["label_id"],
                    "label_name": candidate["label_name"],
                    "similarity": round(sim, 4),
                    "release_count": candidate["release_count"],
                    "shared_genres": shared,
                }
            )

    results.sort(key=lambda x: x["similarity"], reverse=True)
    return results[:limit]
