"""Grooved-media rarity extension.

Pressing scarcity — how many pressings of the same master exist — is a property of a physical
grooved pressing, not of a release. Applying it to a CD, a download card, or a VHS tape scored
media that have no pressings at all as if they had exactly one, which is why every lone CD
used to look as rare as a lone LP.

ADR 0007 moves it here. This module contributes ``pressing_scarcity`` only for the three
grooved families, and owns the sibling-count query that feeds it. Everything else the ADR
names as grooved-specific — pressing plant, matrix and runout, lacquer and stamper lineage,
colour and appearance evidence — belongs in this module when it arrives, and is the payload a
future vinyl-specific service would take with it.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import Final

from api.rarity.core import ReleaseContext


# The taxonomy families whose media carry a groove. `grooved: true` in the vendored
# vocabulary; pinned explicitly here so a vocabulary bump cannot silently widen or narrow what
# this module claims.
GROOVED_FAMILIES: Final[frozenset[str]] = frozenset({"vinyl", "shellac", "grooved_other"})

# The fact name this module's query rows are keyed under in ReleaseContext.facts.
PRESSING_FACT: Final[str] = "grooved_pressing"

# Weight inherited unchanged from the pre-split SIGNAL_WEIGHTS, so a grooved release scores
# exactly as it did before. It is renormalised away for every non-grooved release.
PRESSING_WEIGHT: Final[float] = 0.25

# Sibling pressings of the same master, for one page of release ids.
#
# NOTE (groovemap-cu2.75): the master lookup and the sibling lookup are deliberately two
# separate OPTIONAL MATCHes. Combining them into one pattern makes `m` contingent on a sibling
# existing: for a release that IS linked to a master but is that master's ONLY pressing, the
# combined pattern (including its inline WHERE sibling <> r) fails entirely and `m` comes back
# null too — misclassifying the rarest pressing case (a unique pressing of a master) as "no
# master link", scoring it 90.0 (standalone) instead of 100.0 (unique pressing). The +1 must
# therefore be applied INSIDE the non-null branch, to a plain sibling_count, rather than folded
# into the aggregate.
#
# The `UNWIND $ids` page scoping is the chunking contract documented in
# api/queries/rarity_queries.py — do not reintroduce a bare `MATCH (r:Release)` here.
PRESSING_QUERY: Final[str] = """
UNWIND $ids AS rid
MATCH (r:Release {id: rid})
OPTIONAL MATCH (r)-[:DERIVED_FROM]->(m:Master)
OPTIONAL MATCH (m)<-[:DERIVED_FROM]-(sibling:Release)
WHERE sibling <> r
WITH r, m, count(DISTINCT sibling) AS sibling_count
WITH r, CASE WHEN m IS NULL THEN 0 ELSE sibling_count + 1 END AS pressing_count
RETURN r.id AS release_id, pressing_count
"""

# The same question in SQL/PGQ, for the PostgreSQL backend. ADR 0012 migrates the graph reads
# family by family and a family extension module owns its graph question in both spellings,
# because the question is the module's, not the orchestrator's — `api/queries/rarity_pg_queries.py`
# discovers this the same way `api/queries/rarity_queries.py` discovers `PRESSING_QUERY`.
#
# The `$ids` page becomes `= ANY(%(ids)s)` over the same page, which is the chunking contract
# in its PostgreSQL spelling — do not let either of these grow a scan of the whole release set.
#
# The two OPTIONAL MATCHes the note above insists on are the two LEFT JOINs here, and for the
# same reason: `mastered` answers "is this release linked to a master at all" independently of
# whether that master has any other pressing, so a unique pressing of a known master still
# reaches the `+ 1` branch and scores 100.0 rather than falling into the 90.0 standalone case.
# Folding the two into one walk would make the master link contingent on a sibling existing,
# which is exactly the groovemap-cu2.75 bug.
#
# `sibling.release_id <> r.release_id` is the walk-semantics guard. Neo4j derives it from
# relationship isomorphism (`sibling <> r`); SQL/PGQ lets the same `derived_from` edge bind to
# both edge patterns, so without it every mastered release would count itself as its own
# sibling.
PRESSING_SQL: Final[str] = """
WITH page AS (
    SELECT release_id
    FROM graph.release
    WHERE release_id = ANY(%(ids)s)
),
mastered AS (
    SELECT DISTINCT release_id
    FROM GRAPH_TABLE (graph.catalog
        MATCH (r IS release WHERE r.release_id = ANY(%(ids)s))-[IS derived_from]->(m IS master)
        COLUMNS (r.release_id AS release_id)
    ) AS linked
),
siblings AS (
    SELECT release_id, count(DISTINCT sibling_id) AS tally
    FROM GRAPH_TABLE (graph.catalog
        MATCH (r IS release WHERE r.release_id = ANY(%(ids)s))
              -[IS derived_from]->(m IS master)<-[IS derived_from]-(sibling IS release)
        WHERE sibling.release_id <> r.release_id
        COLUMNS (r.release_id AS release_id, sibling.release_id AS sibling_id)
    ) AS walk
    GROUP BY release_id
)
SELECT page.release_id AS release_id,
       (CASE WHEN mastered.release_id IS NULL THEN 0 ELSE COALESCE(siblings.tally, 0) + 1 END)::bigint AS pressing_count
FROM page
LEFT JOIN mastered ON mastered.release_id = page.release_id
LEFT JOIN siblings ON siblings.release_id = page.release_id
ORDER BY page.release_id
"""


def compute_pressing_scarcity_score(pressing_count: int) -> float:
    """Score based on number of pressings of the same master.

    Args:
        pressing_count: Pressings of this release's master, counting this one. ``0`` means the
            release has no master link at all.

    Returns:
        A 0-100 score. A unique pressing of a known master (1) outranks a standalone release
        with no master link (0), because the master link is evidence that no reissue exists.
    """
    if pressing_count <= 0:
        return 90.0  # Standalone release (no master link)
    if pressing_count == 1:
        return 100.0
    if pressing_count == 2:
        return 85.0
    if pressing_count <= 5:
        return 60.0
    if pressing_count <= 10:
        return 35.0
    return 10.0


class GroovedSignals:
    """The grooved family extension. See :class:`api.rarity.families.registry.FamilySignals`."""

    module_id: str = "grooved"
    weights: Mapping[str, float] = {"pressing_scarcity": PRESSING_WEIGHT}
    queries: Mapping[str, str] = {PRESSING_FACT: PRESSING_QUERY}
    sql_queries: Mapping[str, str] = {PRESSING_FACT: PRESSING_SQL}

    def applies_to(self, families: Collection[str]) -> bool:
        """Return whether any of ``families`` is a grooved family."""
        return any(family in GROOVED_FAMILIES for family in families)

    def signals(self, release_ctx: ReleaseContext) -> dict[str, float]:
        """Return ``pressing_scarcity`` for a grooved release.

        A release whose sibling-count query returned no row scores as a standalone release,
        which is what a missing master link means.
        """
        row = release_ctx.facts.get(PRESSING_FACT) or {}
        return {"pressing_scarcity": compute_pressing_scarcity_score(row.get("pressing_count") or 0)}
