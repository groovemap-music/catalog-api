"""Neo4j Cypher queries for the credits & provenance feature.

All queries target Person nodes and CREDITED_ON relationships created
by the graphinator from Discogs release extraartists data.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog
from common.credit_roles import ALL_CATEGORIES

from api.queries.helpers import run_count, run_query, run_single
from api.queries.neo4j_queries import _build_autocomplete_query


if TYPE_CHECKING:
    from common import AsyncResilientNeo4jDriver


logger = structlog.get_logger(__name__)


async def get_person_credits(
    driver: AsyncResilientNeo4jDriver,
    name: str,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Return one page of the releases a person is credited on, one row per release, role and category.

    Pages follow a total order (year newest first, then title, release id, role, category) over the credit
    records, so walking ``offset`` upward reaches every credit. The page is cut before the artist and label
    lookups and before any de-duplication, so memory is the page and not the person: Bob Ludwig has 60,758
    credits and the full list took 8.2 s and 110 MB (audit H12). A credit recorded twice is one row, as before
    (collapsed inside the page).
    """
    cypher = """
    MATCH (p:Person {name: $name})-[c:CREDITED_ON]->(r:Release)
    WITH r, c.role AS role, c.category AS category
    ORDER BY r.year DESC, r.title, r.id, role, category
    SKIP $offset
    LIMIT $limit
    WITH DISTINCT r, role, category
    OPTIONAL MATCH (r)-[:BY]->(a:Artist)
    OPTIONAL MATCH (r)-[:ON]->(l:Label)
    WITH r, role, category, collect(DISTINCT a.name)[..3] AS artists, collect(DISTINCT l.name)[..1] AS labels
    RETURN r.id AS release_id,
           r.title AS title,
           r.year AS year,
           role,
           category,
           artists,
           labels
    ORDER BY r.year DESC, r.title, r.id, role, category
    """
    return await run_query(driver, cypher, name=name, limit=limit, offset=offset)


async def count_person_credits(driver: AsyncResilientNeo4jDriver, name: str) -> int:
    """Count the credit records :func:`get_person_credits` pages through (one per ``CREDITED_ON`` edge)."""
    cypher = """
    MATCH (:Person {name: $name})-[c:CREDITED_ON]->(:Release)
    RETURN count(c) AS total
    """
    return await run_count(driver, cypher, name=name)


async def get_person_timeline(
    driver: AsyncResilientNeo4jDriver,
    name: str,
) -> list[dict[str, Any]]:
    """Return year-by-year credit activity for a person."""
    cypher = """
    MATCH (p:Person {name: $name})-[c:CREDITED_ON]->(r:Release)
    WHERE r.year IS NOT NULL
    RETURN r.year AS year,
           c.category AS category,
           count(*) AS count
    ORDER BY r.year
    """
    return await run_query(driver, cypher, name=name)


async def get_release_credits(
    driver: AsyncResilientNeo4jDriver,
    release_id: str,
) -> list[dict[str, Any]]:
    """Return full credits breakdown for a release."""
    cypher = """
    MATCH (p:Person)-[c:CREDITED_ON]->(r:Release {id: $release_id})
    OPTIONAL MATCH (p)-[:SAME_AS]->(a:Artist)
    RETURN p.name AS name,
           c.role AS role,
           c.category AS category,
           a.id AS artist_id,
           a.name AS artist_name
    ORDER BY c.category, p.name
    """
    return await run_query(driver, cypher, release_id=release_id)


_LIVE_LEADERBOARD_CYPHER = """
MATCH (p:Person)-[c:CREDITED_ON]->(r:Release)
WHERE c.category = $category
WITH p, count(DISTINCT r) AS credit_count
RETURN p.name AS name, credit_count
ORDER BY credit_count DESC, p.name
LIMIT $limit
"""

_fallback_logged = False


def _credit_count_property(category: str) -> str:
    """Return the precomputed Person property for ``category``.

    Raises ValueError for anything outside ``ALL_CATEGORIES``: the result is spliced into
    Cypher (property names cannot be parameters), so it is built only from a validated value.
    """
    if category not in ALL_CATEGORIES:
        msg = f"unknown credit category {category!r}"
        raise ValueError(msg)
    return f"credit_count_{category}"


async def get_role_leaderboard(
    driver: AsyncResilientNeo4jDriver,
    category: str,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Return most prolific people in a given role category.

    Reads the indexed ``Person.credit_count_<category>`` property that
    discogs-graph-enricher precomputes after each import. If that property has never been
    written (counts not computed yet) it falls back to counting CREDITED_ON live, and logs
    the fallback once per process.
    """
    global _fallback_logged
    prop = _credit_count_property(category)

    # IS NOT NULL is answered by the property index; a computed graph writes 0 rather than
    # leaving the property absent, so absence everywhere means "never computed".
    computed = await run_query(
        driver,
        f"MATCH (p:Person) WHERE p.{prop} IS NOT NULL RETURN true AS computed LIMIT 1",
    )
    if not computed:
        if not _fallback_logged:
            _fallback_logged = True
            logger.warning("credit counts not computed, serving role leaderboard from live count", property=prop)
        return await run_query(driver, _LIVE_LEADERBOARD_CYPHER, category=category, limit=limit)

    # The range predicate lets the planner use the index for both filter and ORDER BY.
    # > 0 matches the live query, which only returns people with a credit in the category.
    cypher = f"""
    MATCH (p:Person)
    WHERE p.{prop} > 0
    RETURN p.name AS name, p.{prop} AS credit_count
    ORDER BY p.{prop} DESC, p.name
    LIMIT $limit
    """
    return await run_query(driver, cypher, limit=limit)


async def get_shared_credits(
    driver: AsyncResilientNeo4jDriver,
    person1: str,
    person2: str,
) -> list[dict[str, Any]]:
    """Find releases where two people are both credited."""
    cypher = """
    MATCH (p1:Person {name: $person1})-[c1:CREDITED_ON]->(r:Release)<-[c2:CREDITED_ON]-(p2:Person {name: $person2})
    OPTIONAL MATCH (r)-[:BY]->(a:Artist)
    RETURN r.id AS release_id,
           r.title AS title,
           r.year AS year,
           c1.role AS person1_role,
           c2.role AS person2_role,
           collect(DISTINCT a.name)[..3] AS artists
    ORDER BY r.year DESC
    """
    return await run_query(driver, cypher, person1=person1, person2=person2)


MAX_CONNECTION_DEPTH = 2
SECOND_HOPS_PER_CONNECTION = 10


async def get_person_connections(
    driver: AsyncResilientNeo4jDriver,
    name: str,
    depth: int = 2,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Find people connected through shared releases (collaboration graph).

    Returns one row per connected person with the count of shared releases. At ``depth=2`` each row also
    carries ``second_hops``: the people that connection shares releases with, ranked by shared releases
    (then name) *before* the top :data:`SECOND_HOPS_PER_CONNECTION` are kept. The old statement kept an
    arbitrary ten (audit H12). Only one or two hops exist: ``depth=3`` is rejected by the route.
    """
    if not 1 <= depth <= MAX_CONNECTION_DEPTH:
        msg = f"depth must be between 1 and {MAX_CONNECTION_DEPTH}"
        raise ValueError(msg)

    cypher = """
    MATCH (p:Person {name: $name})-[:CREDITED_ON]->(r:Release)<-[:CREDITED_ON]-(connected:Person)
    WHERE connected <> p
    WITH connected, count(DISTINCT r) AS shared_count
    ORDER BY shared_count DESC, connected.name
    LIMIT $limit
    RETURN connected.name AS name, shared_count
    """
    if depth >= 2:
        cypher = """
        MATCH (p:Person {name: $name})-[:CREDITED_ON]->(r:Release)<-[:CREDITED_ON]-(hop1:Person)
        WHERE hop1 <> p
        WITH p, hop1, count(DISTINCT r) AS direct_shared
        ORDER BY direct_shared DESC, hop1.name
        LIMIT $limit
        CALL {
            WITH p, hop1
            MATCH (hop1)-[:CREDITED_ON]->(r2:Release)<-[:CREDITED_ON]-(hop2:Person)
            WHERE hop2 <> hop1 AND hop2 <> p
            WITH hop1, hop2, count(DISTINCT r2) AS shared
            ORDER BY shared DESC, hop2.name
            LIMIT $second_hops
            RETURN collect({name: hop2.name, via: hop1.name, shared: shared}) AS second_hops
        }
        RETURN hop1.name AS name, direct_shared AS shared_count, second_hops
        ORDER BY shared_count DESC, name
        """
    return await run_query(driver, cypher, name=name, limit=limit, second_hops=SECOND_HOPS_PER_CONNECTION)


async def autocomplete_person(
    driver: AsyncResilientNeo4jDriver,
    query: str,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Search credited people by name using fulltext index."""
    # Build the Lucene query with the shared escaper so metacharacters in names
    # (AC/DC, C++, "Emerson, Lake (Palmer", bare ':' '/' '~' '(' ...) are escaped
    # exactly as the artist/label/genre/style autocompletes do — otherwise a raw
    # string with Lucene syntax triggers a ParseException and an unhandled 500.
    search_query = _build_autocomplete_query(query)

    cypher = """
    CALL db.index.fulltext.queryNodes('person_name_fulltext', $query)
    YIELD node, score
    RETURN node.name AS name, score
    ORDER BY score DESC
    LIMIT $limit
    """
    return await run_query(driver, cypher, query=search_query, limit=limit)


async def get_person_profile(
    driver: AsyncResilientNeo4jDriver,
    name: str,
) -> dict[str, Any] | None:
    """Get summary profile for a person: total credits, role breakdown, active years."""
    cypher = """
    MATCH (p:Person {name: $name})-[c:CREDITED_ON]->(r:Release)
    WITH p, count(c) AS total_credits,
         collect(DISTINCT c.category) AS categories,
         min(r.year) AS first_year,
         max(r.year) AS last_year
    OPTIONAL MATCH (p)-[:SAME_AS]->(a:Artist)
    RETURN p.name AS name,
           total_credits,
           categories,
           first_year,
           last_year,
           a.id AS artist_id,
           a.name AS artist_name
    """
    return await run_single(driver, cypher, name=name)


async def get_person_role_breakdown(
    driver: AsyncResilientNeo4jDriver,
    name: str,
) -> list[dict[str, Any]]:
    """Get count of credits per role category for a person."""
    cypher = """
    MATCH (p:Person {name: $name})-[c:CREDITED_ON]->(r:Release)
    RETURN c.category AS category, count(*) AS count
    ORDER BY count DESC
    """
    return await run_query(driver, cypher, name=name)
