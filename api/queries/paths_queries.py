"""The Neo4j side of the variable-length ``paths`` family."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from api.queries import neo4j_queries, recommend_queries


if TYPE_CHECKING:
    from common import AsyncResilientNeo4jDriver


async def find_shortest_path(
    driver: AsyncResilientNeo4jDriver,
    from_id: str,
    to_id: str,
    max_depth: int = neo4j_queries.DEFAULT_PATH_DEPTH,
    from_type: str = "",
    to_type: str = "",
) -> dict[str, Any] | None:
    """Delegate by module attribute so patches of the original still apply."""
    return await neo4j_queries.find_shortest_path(
        driver=driver,
        from_id=from_id,
        to_id=to_id,
        max_depth=max_depth,
        from_type=from_type,
        to_type=to_type,
    )


async def get_explore_traversal(
    driver: AsyncResilientNeo4jDriver,
    entity_type: str,
    entity_id: str,
    hops: int = 2,
    row_limit: int = 100,
) -> list[dict[str, Any]]:
    """Delegate by module attribute so patches of the original still apply."""
    return await recommend_queries.get_explore_traversal(driver, entity_type, entity_id, hops=hops, row_limit=row_limit)
