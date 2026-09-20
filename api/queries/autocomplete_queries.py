"""The Neo4j side of the "autocomplete" family, gathered behind one module.

Every other family in `api/graph_backend.py` maps to a single Cypher module. This one does
not: four of its five functions are in :mod:`api.queries.neo4j_queries` and the fifth, the
person search, is in :mod:`api.queries.credits_queries`, because credits arrived as its own
feature. The seam resolves a family to *one* module, so the family needs one.

Splitting the difference by moving `autocomplete_person` would rename a function two
routers and three test modules already import. Re-exporting it with `from ... import` would
be worse: the seam's contract is that resolving a family returns the module itself, so a
`unittest.mock.patch` of a query function still takes effect for a caller that reached it
through the selector — and a re-exported name is a second binding that a patch of the
original never reaches.

So each function here delegates, by attribute, at call time. That keeps both properties:
the family resolves to one module, and patching either underlying module still works.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from api.queries import credits_queries, neo4j_queries


if TYPE_CHECKING:
    from common import AsyncResilientNeo4jDriver


async def autocomplete_artist(driver: AsyncResilientNeo4jDriver, query: str, limit: int = 10) -> list[dict[str, Any]]:
    """Search artists by name using the `artist_name_fulltext` index."""
    return await neo4j_queries.autocomplete_artist(driver, query, limit)


async def autocomplete_label(driver: AsyncResilientNeo4jDriver, query: str, limit: int = 10) -> list[dict[str, Any]]:
    """Search labels by name using the `label_name_fulltext` index."""
    return await neo4j_queries.autocomplete_label(driver, query, limit)


async def autocomplete_genre(driver: AsyncResilientNeo4jDriver, query: str, limit: int = 10) -> list[dict[str, Any]]:
    """Search genres by name using the `genre_name_fulltext` index."""
    return await neo4j_queries.autocomplete_genre(driver, query, limit)


async def autocomplete_style(driver: AsyncResilientNeo4jDriver, query: str, limit: int = 10) -> list[dict[str, Any]]:
    """Search styles by name using the `style_name_fulltext` index."""
    return await neo4j_queries.autocomplete_style(driver, query, limit)


async def autocomplete_person(driver: AsyncResilientNeo4jDriver, query: str, limit: int = 10) -> list[dict[str, Any]]:
    """Search credited people by name using the `person_name_fulltext` index."""
    return await credits_queries.autocomplete_person(driver, query, limit)
