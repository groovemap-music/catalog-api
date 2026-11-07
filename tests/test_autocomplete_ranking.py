"""Artist autocomplete ranks established artists first among equal-scoring matches.

These tests read back the Cypher and parameters the query helper sends. The ordering itself
is checked against lux (read-only, see the bead); the tests pin the shape the ordering needs:
the release-count tiebreak sits after the exact-name check and before the length, the candidate
pool is wider than the returned limit for artists, and the other entity types are unchanged.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from api.queries import neo4j_queries
from tests.cypher_text import cypher_text


def _driver() -> MagicMock:
    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.run = AsyncMock(return_value=AsyncMock(data=AsyncMock(return_value=[])))
    driver = MagicMock()
    driver.session = MagicMock(return_value=session)
    return driver


async def _sent(call: Any, limit: int) -> tuple[str, dict[str, Any]]:
    driver = _driver()
    await call(driver, "joh", limit=limit)
    args, _ = driver.session.return_value.run.call_args
    # run_query calls session.run(query, params): the parameter map is the second positional argument.
    return cypher_text(args[0]), args[1]


class TestArtistRanking:
    @pytest.mark.asyncio
    async def test_release_count_orders_equal_scores_after_the_exact_name(self) -> None:
        text, _ = await _sent(neo4j_queries.autocomplete_artist, 10)

        order = text[text.index("ORDER BY") :]
        assert "toLower(node.name) = $typed DESC" in order
        assert "coalesce(node.release_count, 0) DESC" in order
        assert order.index("toLower(node.name) = $typed DESC") < order.index("coalesce(node.release_count, 0) DESC")
        assert order.index("coalesce(node.release_count, 0) DESC") < order.index("size(node.name)")

    @pytest.mark.asyncio
    async def test_the_candidate_pool_is_wider_than_the_limit(self) -> None:
        _, params = await _sent(neo4j_queries.autocomplete_artist, 10)

        assert params["pool"] == 50
        assert params["limit"] == 10

    @pytest.mark.asyncio
    async def test_a_limit_above_the_pool_is_never_cut_below_the_limit(self) -> None:
        _, params = await _sent(neo4j_queries.autocomplete_artist, 60)

        assert params["pool"] == 60
        assert params["limit"] == 60


class TestOtherEntitiesAreUnchanged:
    @pytest.mark.asyncio
    async def test_label_has_no_release_tiebreak_and_pool_equals_limit(self) -> None:
        text, params = await _sent(neo4j_queries.autocomplete_label, 10)

        assert "release_count" not in text
        assert params["pool"] == 10

    @pytest.mark.asyncio
    async def test_genre_keeps_the_original_order(self) -> None:
        text, params = await _sent(neo4j_queries.autocomplete_genre, 10)

        assert "ORDER BY score DESC, toLower(node.name) = $typed DESC, size(node.name), id" in text
        assert params["pool"] == 10


def test_the_artist_spec_declares_the_rank_and_pool() -> None:
    spec = neo4j_queries._ARTIST_AUTOCOMPLETE
    assert spec.rank_expression == "coalesce(node.release_count, 0)"
    assert spec.candidate_pool == 50
