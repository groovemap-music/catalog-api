"""Neo4j Cypher queries for insights computations.

Each function takes an AsyncResilientNeo4jDriver (or compatible async driver),
executes a Cypher query, and returns a list of dicts.
"""

import asyncio
from typing import Any

import structlog

from api.queries.helpers import run_query


logger = structlog.get_logger(__name__)

# Server-side budget for each style-year read. The aggregation scans every release-style tag
# (about 30 s on the full graph, logged tail to 110 s on the predecessor), so it runs under a
# long internal budget rather than any public route budget. It stays well under Neo4j's
# db.transaction.timeout (600 s in production), so a pathological run fails fast with a
# retryable timeout. The internal-insights contract publishes the same figure as
# ``x-groovemap-max-processing-seconds`` on this operation.
STYLE_YEAR_QUERY_TIMEOUT_SECONDS = 300.0

# Server-side budget for the landmark read. It ranks every (style, master) pair in the graph, a
# scan of the same size as the style-year read, so it gets the same long internal budget. The
# internal-insights contract publishes this figure as ``x-groovemap-max-processing-seconds`` on
# the style-landmarks operation.
STYLE_LANDMARK_QUERY_TIMEOUT_SECONDS = 300.0

# Masters kept per style. Concept B draws the top 25 masters of each style as landmarks.
LANDMARKS_PER_STYLE = 25

# Server-side budget for the co-tag read. It expands every release into its style pairs and
# counts them, a scan at least the size of the style-year read, so it gets the same long
# internal budget. The internal-insights contract publishes this figure as
# ``x-groovemap-max-processing-seconds`` on the style-cotags operation.
STYLE_COTAG_QUERY_TIMEOUT_SECONDS = 300.0

# Co-tagged styles kept per style. Concept B draws the top 25 lateral neighbours of each style.
COTAGS_PER_STYLE = 25


async def query_artist_centrality(driver: Any, limit: int = 100) -> list[dict[str, Any]]:
    """Query top artists by total edge count (degree centrality).

    Counts all relationships connected to each Artist node:
    releases, labels, aliases, groups, and collaborations.
    """
    cypher = """
    MATCH (a:Artist)
    WITH a, size([(a)-[]-() | 1]) AS edge_count
    ORDER BY edge_count DESC
    LIMIT $limit
    RETURN a.id AS artist_id, a.name AS artist_name, edge_count
    """
    results = await run_query(driver, cypher, database="neo4j", limit=limit)
    logger.info("🔍 Artist centrality query complete", count=len(results))
    return results


async def query_genre_trends(driver: Any, genre: str | None = None) -> list[dict[str, Any]]:
    """Query release counts per genre per decade.

    Groups releases by their genres and the decade of release,
    counting how many releases fall into each genre/decade bucket.
    """
    if genre:
        cypher = """
        MATCH (r:Release)-[:IS]->(g:Genre {name: $genre})
        WHERE r.year IS NOT NULL AND r.year > 0
        WITH g.name AS genre, (r.year / 10) * 10 AS decade, count(r) AS release_count
        ORDER BY decade
        RETURN genre, decade, release_count
        """
        results = await run_query(driver, cypher, database="neo4j", genre=genre)
    else:
        cypher = """
        MATCH (r:Release)-[:IS]->(g:Genre)
        WHERE r.year IS NOT NULL AND r.year > 0
        WITH g.name AS genre, (r.year / 10) * 10 AS decade, count(r) AS release_count
        ORDER BY genre, decade
        RETURN genre, decade, release_count
        """
        results = await run_query(driver, cypher, database="neo4j")
    logger.info("🔍 Genre trends query complete", count=len(results), genre=genre)
    return results


# Release-year cells. The year is the release's own, never its master's: the lifecycle stage
# flips for 0.54% of styles between dumps on release year against 1.69% on master year
# (gm-design-4804 B4). ``DISTINCT`` keeps a release or master counted once per cell even if
# the graph carries a duplicate edge; a masterless release adds no master.
_STYLE_YEAR_CYPHER = """
MATCH (r:Release)-[:IS]->(s:Style)
WHERE r.year IS NOT NULL AND r.year > 0
OPTIONAL MATCH (r)-[:DERIVED_FROM]->(m:Master)
WITH s.name AS style, r.year AS year,
     count(DISTINCT r) AS release_count, count(DISTINCT m) AS master_count
ORDER BY style, year
RETURN style, year, release_count, master_count
"""

# Co-tag counts: how many of a style's releases also carry each genre. The denominator is
# every release tagged with the style, dated or not, because genre membership does not
# depend on when a release came out.
_STYLE_GENRE_CYPHER = """
MATCH (r:Release)-[:IS]->(s:Style)
MATCH (r)-[:IS]->(g:Genre)
WITH s.name AS style, g.name AS genre, count(DISTINCT r) AS release_count
ORDER BY style, genre
RETURN style, genre, release_count
"""

_STYLE_TOTAL_CYPHER = """
MATCH (r:Release)-[:IS]->(s:Style)
WITH s.name AS style, count(DISTINCT r) AS style_release_count
RETURN style, style_release_count
"""


# Genre release-year cells, bucketed on the release's own year exactly as the style cells are,
# with undated releases excluded the same way. The consumer derives genre emergence years from
# these. ``DISTINCT`` keeps a release counted once per cell even if the graph carries a
# duplicate edge.
_GENRE_YEAR_CYPHER = """
MATCH (r:Release)-[:IS]->(g:Genre)
WHERE r.year IS NOT NULL AND r.year > 0
WITH g.name AS genre, r.year AS year, count(DISTINCT r) AS release_count
ORDER BY genre, year
RETURN genre, year, release_count
"""


async def query_style_year_counts(driver: Any) -> dict[str, list[dict[str, Any]]]:
    """Query release and master counts per style and release year, plus each style's genres.

    Returns ``style_years`` (one row per style and release year with ``release_count`` and
    ``master_count``), ``style_parents`` (one row per style and genre it co-occurs with, with
    the co-tagged ``release_count``, the style's total ``style_release_count`` and the raw
    ``share`` of the style's releases that carry the genre) and ``genre_years`` (one row per
    genre and release year with ``release_count``).

    Shares are not floored: the 0.20 parent rule is the consumer's to apply. The four reads
    run concurrently, each under :data:`STYLE_YEAR_QUERY_TIMEOUT_SECONDS`.
    """
    # A task group, not gather: when one read fails the others are cancelled rather than left
    # scanning the graph for a response nobody will read.
    async with asyncio.TaskGroup() as group:
        years_read = group.create_task(run_query(driver, _STYLE_YEAR_CYPHER, database="neo4j", timeout=STYLE_YEAR_QUERY_TIMEOUT_SECONDS))
        genre_read = group.create_task(run_query(driver, _STYLE_GENRE_CYPHER, database="neo4j", timeout=STYLE_YEAR_QUERY_TIMEOUT_SECONDS))
        totals_read = group.create_task(run_query(driver, _STYLE_TOTAL_CYPHER, database="neo4j", timeout=STYLE_YEAR_QUERY_TIMEOUT_SECONDS))
        genre_years_read = group.create_task(run_query(driver, _GENRE_YEAR_CYPHER, database="neo4j", timeout=STYLE_YEAR_QUERY_TIMEOUT_SECONDS))
    years, genre_cells, totals = years_read.result(), genre_read.result(), totals_read.result()
    genre_years = genre_years_read.result()
    style_totals = {row["style"]: row["style_release_count"] for row in totals}
    parents = [
        {
            "style": row["style"],
            "genre": row["genre"],
            "release_count": row["release_count"],
            "style_release_count": style_totals[row["style"]],
            "share": row["release_count"] / style_totals[row["style"]],
        }
        for row in genre_cells
    ]
    logger.info("🔍 Style-year counts query complete", cells=len(years), parent_pairs=len(parents), genre_cells=len(genre_years))
    return {"style_years": years, "style_parents": parents, "genre_years": genre_years}


# Landmark masters: per style, the masters with the most releases. A release counts toward a
# master only through DERIVED_FROM, so masterless releases add nothing, and ``DISTINCT`` keeps a
# release counted once per (style, master) even if the graph carries a duplicate edge. Ranking is
# release_count descending, then master id ascending (string order), which makes the cut at the
# cap deterministic when masters tie. The slice happens per style inside the query so only the
# kept rows leave Neo4j. The year is the master's own and is null when the master has none.
_STYLE_LANDMARK_CYPHER = """
MATCH (r:Release)-[:IS]->(s:Style)
MATCH (r)-[:DERIVED_FROM]->(m:Master)
WITH s.name AS style, m, count(DISTINCT r) AS release_count
ORDER BY style, release_count DESC, m.id
WITH style, collect({master_id: m.id, title: m.title, year: m.year, release_count: release_count}) AS ranked
UNWIND ranked[0..$limit] AS landmark
RETURN style, landmark.master_id AS master_id, landmark.title AS title,
       CASE WHEN landmark.year > 0 THEN landmark.year END AS year,
       landmark.release_count AS release_count
ORDER BY style, release_count DESC, master_id
"""


async def query_style_landmarks(driver: Any) -> dict[str, list[dict[str, Any]]]:
    """Query the landmark masters of every style.

    Returns ``landmarks``: up to :data:`LANDMARKS_PER_STYLE` rows per style, each with the
    ``master_id``, ``title`` and ``year`` of the master and the ``release_count`` of releases
    that carry the style and derive from it. Rows are ordered by style, then ``release_count``
    descending, then ``master_id``. Runs under :data:`STYLE_LANDMARK_QUERY_TIMEOUT_SECONDS`.
    """
    landmarks = await run_query(
        driver,
        _STYLE_LANDMARK_CYPHER,
        database="neo4j",
        timeout=STYLE_LANDMARK_QUERY_TIMEOUT_SECONDS,
        limit=LANDMARKS_PER_STYLE,
    )
    logger.info("🔍 Style landmarks query complete", landmarks=len(landmarks))
    return {"landmarks": landmarks}


async def query_label_longevity(driver: Any, limit: int = 50) -> list[dict[str, Any]]:
    """Query labels ranked by years of active operation.

    For each label, finds the earliest and latest release year,
    calculates years active, total releases, and peak decade.
    """
    cypher = """
    MATCH (r:Release)-[:ON]->(l:Label)
    WHERE r.year IS NOT NULL AND r.year > 0
    WITH l,
         min(r.year) AS first_year,
         max(r.year) AS last_year,
         count(r) AS total_releases,
         collect(r.year) AS years
    WITH l, first_year, last_year, total_releases, years,
         last_year - first_year + 1 AS years_active
    UNWIND years AS y
    WITH l, first_year, last_year, total_releases, years_active,
         (y / 10) * 10 AS decade, count(*) AS decade_count
    ORDER BY decade_count DESC
    WITH l, first_year, last_year, total_releases, years_active,
         collect({decade: decade, count: decade_count})[0].decade AS peak_decade
    ORDER BY years_active DESC
    LIMIT $limit
    RETURN l.id AS label_id, l.name AS label_name,
           first_year, last_year, years_active,
           total_releases, peak_decade
    """
    results = await run_query(driver, cypher, database="neo4j", limit=limit)
    logger.info("🔍 Label longevity query complete", count=len(results))
    return results


async def query_monthly_anniversaries(
    driver: Any,
    current_year: int,
    current_month: int,
    milestone_years: list[int] | None = None,
) -> list[dict[str, Any]]:
    """Query Master releases with milestone anniversaries in the current year.

    Returns all Masters whose release year is a milestone number of years
    ago (e.g. 25, 30, 40, 50, 75, 100 years).  The ``current_month``
    parameter is logged for observability but **not** used as a filter
    because Master nodes only store a release year, not a month.
    """
    if milestone_years is None:
        milestone_years = [25, 30, 40, 50, 75, 100]

    target_years = [current_year - m for m in milestone_years]

    cypher = """
    UNWIND $target_years AS target_year
    MATCH (m:Master {year: target_year})
    OPTIONAL MATCH (m)-[:BY]->(a:Artist)
    WITH m, collect(DISTINCT a.name)[0] AS artist_name
    RETURN m.id AS master_id, m.title AS title, artist_name,
           m.year AS release_year
    ORDER BY m.year ASC
    """
    results = await run_query(driver, cypher, database="neo4j", target_years=target_years)
    logger.info(
        "🔍 Monthly anniversaries query complete",
        count=len(results),
        month=current_month,
        year=current_year,
    )
    return results


# Style co-tags: per style, the other styles that most releases carry alongside it. A pair counts
# a release once (``DISTINCT``) however many duplicate edges the graph carries, and a style never
# pairs with itself. Counts are symmetric, so each pair appears under both of its styles, and each
# style keeps its own top ``$limit`` by release_count descending, then the other style's name
# ascending (string order), which makes the cut at the cap deterministic when styles tie. The
# slice happens per style inside the query so only the kept rows leave Neo4j.
_STYLE_COTAG_CYPHER = """
MATCH (r:Release)-[:IS]->(a:Style)
MATCH (r)-[:IS]->(b:Style)
WHERE a.name <> b.name
WITH a.name AS style, b.name AS other, count(DISTINCT r) AS release_count
ORDER BY style, release_count DESC, other
WITH style, collect({other: other, release_count: release_count}) AS ranked
UNWIND ranked[0..$limit] AS cotag
RETURN style, cotag.other AS other, cotag.release_count AS release_count
ORDER BY style, release_count DESC, other
"""


async def query_style_cotags(driver: Any) -> dict[str, list[dict[str, Any]]]:
    """Query the co-tagged styles of every style.

    Returns ``cotags``: up to :data:`COTAGS_PER_STYLE` rows per style, each with the ``other``
    style and the ``release_count`` of releases tagged with both. Rows are ordered by style, then
    ``release_count`` descending, then ``other``. Runs under
    :data:`STYLE_COTAG_QUERY_TIMEOUT_SECONDS`.
    """
    cotags = await run_query(
        driver,
        _STYLE_COTAG_CYPHER,
        database="neo4j",
        timeout=STYLE_COTAG_QUERY_TIMEOUT_SECONDS,
        limit=COTAGS_PER_STYLE,
    )
    logger.info("🔍 Style co-tags query complete", cotags=len(cotags))
    return {"cotags": cotags}
