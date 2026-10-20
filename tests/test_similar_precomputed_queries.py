"""The precomputed similar-artist reads and the score fusion (gm-catalog-api-2zsq.1)."""

from __future__ import annotations

import math
from typing import ClassVar

import pytest
from psycopg import errors

from api.queries.similar_precomputed_queries import (
    PrecomputedList,
    cosine,
    fuse,
    get_current_list,
    get_embedding_cosines,
    parse_vector,
)
from tests.fake_postgres import FakePool


@pytest.mark.asyncio
class TestGetCurrentList:
    async def test_returns_the_current_releases_list_in_rank_order(self) -> None:
        pool = FakePool([[(7, "fastrp-v3", ["b", "c"], [0.9, 0.5])]])
        result = await get_current_list(pool, "a")
        assert result == PrecomputedList(release_id=7, model_version="fastrp-v3", neighbours=(("b", 0.9), ("c", 0.5)))
        assert pool.calls[0].params == ("a",)
        assert "is_current" in pool.sql

    async def test_no_current_release_is_none(self) -> None:
        assert await get_current_list(FakePool([[]]), "a") is None

    async def test_artist_absent_from_the_release_is_none(self) -> None:
        assert await get_current_list(FakePool([[(7, "fastrp-v3", None, None)]]), "a") is None

    @pytest.mark.parametrize("error", [errors.UndefinedTable("no such table"), errors.OperationalError("down")])
    async def test_missing_tables_or_unreadable_database_is_none(self, error: Exception) -> None:
        assert await get_current_list(FakePool(raise_on={"": error}), "a") is None

    async def test_no_pool_is_none(self) -> None:
        assert await get_current_list(None, "a") is None


@pytest.mark.asyncio
class TestEmbeddingCosines:
    async def test_cosine_comes_from_the_stored_vectors(self) -> None:
        rows = [("a", "[1,0,0]"), ("b", "[1,1,0]"), ("c", "[0,0,1]")]
        pool = FakePool([rows])
        result = await get_embedding_cosines(pool, "fastrp-v3", "a", ["b", "c", "d"])
        assert result is not None
        assert result["b"] == pytest.approx(1 / math.sqrt(2))
        assert result["c"] == 0.0
        assert "d" not in result  # no stored vector: left for the caller to score 0.0
        assert pool.calls[0].params == ("fastrp-v3", ["a", "b", "c", "d"])

    async def test_target_without_a_vector_yields_no_cosines(self) -> None:
        assert await get_embedding_cosines(FakePool([[("b", "[1,0]")]]), "v", "a", ["b"]) == {}

    async def test_no_candidates_skips_the_query(self) -> None:
        pool = FakePool()
        assert await get_embedding_cosines(pool, "v", "a", []) == {}
        assert pool.calls == []

    async def test_unreadable_vectors_are_none_not_a_partial_result(self) -> None:
        pool = FakePool(raise_on={"": errors.UndefinedTable("artist_embeddings")})
        assert await get_embedding_cosines(pool, "v", "a", ["b"]) is None


def test_parse_vector_and_cosine_edge_cases() -> None:
    assert parse_vector("[0.5, -1,2]") == [0.5, -1.0, 2.0]
    assert parse_vector("[]") == []
    assert cosine([], []) == 0.0
    assert cosine([0.0, 0.0], [1.0, 1.0]) == 0.0
    assert cosine([1.0], [1.0, 2.0]) == 0.0
    assert cosine([2.0, 0.0], [5.0, 0.0]) == pytest.approx(1.0)


class TestFuse:
    """Synthetic lists and cosines; ``alpha`` weights the embedding."""

    neighbours: ClassVar = {"n1": 0.9, "n2": 0.4}
    heuristic: ClassVar = {"n2": {"similarity": 0.8}, "h1": {"similarity": 0.7}}
    cosines: ClassVar = {"h1": 0.5}

    def test_alpha_one_is_the_pure_precomputed_ranking(self) -> None:
        ranked = fuse(self.neighbours, {}, {}, alpha=1.0)
        assert [(a, s) for a, s, _c, _h in ranked] == [("n1", 0.9), ("n2", 0.4)]

    def test_fused_score_is_alpha_cosine_plus_complement_heuristic(self) -> None:
        ranked = {a: (s, c, h) for a, s, c, h in fuse(self.neighbours, self.heuristic, self.cosines, alpha=0.8)}
        assert ranked["n1"] == (pytest.approx(0.8 * 0.9), 0.9, 0.0)  # precomputed-only: heuristic 0
        assert ranked["n2"] == (pytest.approx(0.8 * 0.4 + 0.2 * 0.8), 0.4, 0.8)
        # heuristic-only candidate: cosine from the stored vectors, not the list
        assert ranked["h1"] == (pytest.approx(0.8 * 0.5 + 0.2 * 0.7), 0.5, 0.7)

    def test_heuristic_only_candidate_without_a_vector_scores_cosine_zero(self) -> None:
        ranked = {a: s for a, s, _c, _h in fuse({}, self.heuristic, {}, alpha=0.8)}
        assert ranked["h1"] == pytest.approx(0.2 * 0.7)

    def test_order_is_by_fused_score_then_artist_id(self) -> None:
        order = [a for a, *_ in fuse({"z": 0.5, "a": 0.5, "m": 0.9}, {}, {}, alpha=1.0)]
        assert order == ["m", "a", "z"]

    def test_alpha_zero_ranks_by_heuristic_alone(self) -> None:
        order = [a for a, *_ in fuse(self.neighbours, self.heuristic, self.cosines, alpha=0.0)]
        assert order[:2] == ["n2", "h1"]
