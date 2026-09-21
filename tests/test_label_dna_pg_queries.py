"""Query-shape and assembly tests for the PostgreSQL label-DNA backend."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from api.queries import label_dna_pg_queries as pg
from tests.fake_postgres import FakePool


pytestmark = pytest.mark.asyncio

ALL_STATEMENTS = (
    pg.LABEL_IDENTITY_SQL,
    pg.LABEL_GENRE_PROFILE_SQL,
    pg.LABEL_STYLE_PROFILE_SQL,
    pg.LABEL_DECADE_PROFILE_SQL,
    pg.LABEL_ACTIVE_YEARS_SQL,
    pg.LABEL_FORMAT_PROFILE_SQL,
    pg.LABEL_MEDIA_FAMILY_COUNTS_SQL,
    pg.LABEL_MEDIUM_COUNTS_SQL,
    pg.LABEL_MEDIA_FAMILIES_FALLBACK_SQL,
    pg.CANDIDATE_LABELS_SQL,
    pg.CANDIDATE_PROFILES_SQL,
)


class TestStatementContracts:
    @pytest.mark.parametrize("sql", ALL_STATEMENTS)
    async def test_values_are_named_parameters(self, sql: str) -> None:
        assert "%s" not in sql

    async def test_identity_reads_the_counter_bearing_vertex(self) -> None:
        assert "FROM graph.label_vertex" in pg.LABEL_IDENTITY_SQL
        assert "release_count" in pg.LABEL_IDENTITY_SQL
        assert "artist_count" in pg.LABEL_IDENTITY_SQL
        assert "count(" not in pg.LABEL_IDENTITY_SQL

    @pytest.mark.parametrize("sql", [pg.LABEL_DECADE_PROFILE_SQL, pg.LABEL_ACTIVE_YEARS_SQL])
    async def test_year_reads_guard_null_nonnumeric_and_nonpositive_values(self, sql: str) -> None:
        assert "CASE" in sql
        assert "~ '^[0-9]{1,9}$'" in sql
        assert "WHERE year > 0" in sql

    async def test_format_profile_unnests_release_formats_and_deduplicates_releases(self) -> None:
        assert "unnest(release.formats)" in pg.LABEL_FORMAT_PROFILE_SQL
        assert "count(DISTINCT label.release_id)::bigint" in pg.LABEL_FORMAT_PROFILE_SQL
        assert pg.LABEL_FORMAT_PROFILE_SQL.rstrip().endswith("ORDER BY count DESC, name")

    @pytest.mark.parametrize("sql", [pg.LABEL_MEDIA_FAMILY_COUNTS_SQL, pg.LABEL_MEDIUM_COUNTS_SQL])
    async def test_media_queries_walk_and_validate_the_whole_relation_chain(self, sql: str) -> None:
        assert "GRAPH_TABLE (graph.catalog" in sql
        assert "<- [IS on_label]".replace(" ", "") in sql.replace(" ", "")
        assert "-[IS issued_on]->(medium IS medium)" in sql
        assert "-[IS in_family]->(family IS media_family)" in sql

    async def test_media_orders_are_stable_when_counts_tie(self) -> None:
        assert pg.LABEL_MEDIA_FAMILY_COUNTS_SQL.rstrip().endswith("ORDER BY count DESC, family")
        assert pg.LABEL_MEDIUM_COUNTS_SQL.rstrip().endswith("ORDER BY family, count DESC, medium_id")

    async def test_candidate_discovery_keeps_cypher_caps_and_tie_breaks(self) -> None:
        assert "LIMIT 5" in pg.CANDIDATE_LABELS_SQL
        assert "HAVING sum(shared_in_style) >= %(min_releases)s" in pg.CANDIDATE_LABELS_SQL
        assert "LIMIT 100" in pg.CANDIDATE_LABELS_SQL
        assert pg.CANDIDATE_LABELS_SQL.rstrip().endswith("ORDER BY total_shared DESC, label_id")
        assert "ORDER BY genre.count DESC, genre.name" in pg.CANDIDATE_PROFILES_SQL


class TestSimpleQueries:
    async def test_identity_returns_both_materialized_counters(self) -> None:
        pool = FakePool([[("901", "Label DNA Target", 6, 2)]])
        assert await pg.get_label_identity(pool, "901") == {
            "label_id": "901",
            "label_name": "Label DNA Target",
            "release_count": 6,
            "artist_count": 2,
        }
        assert pool.params == {"label_id": "901"}

    async def test_missing_identity_is_none(self) -> None:
        assert await pg.get_label_identity(FakePool([[]]), "missing") is None

    @pytest.mark.parametrize(
        ("function", "rows", "expected"),
        [
            (pg.get_label_genre_profile, [("Electronic", 6), ("Jazz", 2)], [{"name": "Electronic", "count": 6}, {"name": "Jazz", "count": 2}]),
            (pg.get_label_style_profile, [("Ambient", 6)], [{"name": "Ambient", "count": 6}]),
            (pg.get_label_decade_profile, [(1990, 4), (2000, 2)], [{"decade": 1990, "count": 4}, {"decade": 2000, "count": 2}]),
            (pg.get_label_format_profile, [("Vinyl", 4), ("CD", 2)], [{"name": "Vinyl", "count": 4}, {"name": "CD", "count": 2}]),
            (pg.get_label_media_family_counts, [("vinyl", 4)], [{"family": "vinyl", "count": 4}]),
            (
                pg.get_label_medium_counts,
                [("vinyl", "vinyl_12", '12" vinyl', 4)],
                [{"family": "vinyl", "medium_id": "vinyl_12", "medium_label": '12" vinyl', "count": 4}],
            ),
            (pg.get_label_media_families_fallback, [("vinyl", 1)], [{"family": "vinyl", "count": 1}]),
        ],
    )
    async def test_row_mappings(self, function: Any, rows: list[tuple[Any, ...]], expected: Any) -> None:
        assert await function(FakePool([rows]), "901") == expected

    async def test_active_years_drop_the_row_wrapper(self) -> None:
        assert await pg.get_label_active_years(FakePool([[(197,), (1999,), (2000,)]]), "901") == [197, 1999, 2000]


class TestMediaAssembly:
    async def test_nested_profile_groups_mediums_under_each_family(self) -> None:
        pool = FakePool(
            [
                [("vinyl", 4), ("optical", 2)],
                [("optical", "optical_cd", "CD", 2), ("vinyl", "vinyl_12", '12" vinyl', 4)],
            ]
        )
        assert await pg.get_label_media_profile(pool, "901") == [
            {"family": "vinyl", "count": 4, "mediums": [{"id": "vinyl_12", "label": '12" vinyl', "count": 4}]},
            {"family": "optical", "count": 2, "mediums": [{"id": "optical_cd", "label": "CD", "count": 2}]},
        ]

    async def test_profile_uses_document_fallback_only_when_traversal_is_empty(self) -> None:
        pool = FakePool([[], [], [("vinyl", 1)]])
        assert await pg.get_label_media_profile(pool, "904") == [{"family": "vinyl", "count": 1, "mediums": []}]
        assert pool.calls[2].sql == pg.LABEL_MEDIA_FAMILIES_FALLBACK_SQL


class TestFullProfile:
    async def test_missing_label_returns_none_without_profile_reads(self) -> None:
        pool = FakePool([[]])
        assert await pg.get_label_full_profile(pool, "missing") is None
        assert len(pool.calls) == 1

    async def test_low_release_label_returns_identity_without_profile_reads(self) -> None:
        pool = FakePool([[("903", "Tiny Label", 2, 1)]])
        assert await pg.get_label_full_profile(pool, "903") == {
            "label_id": "903",
            "label_name": "Tiny Label",
            "release_count": 2,
            "artist_count": 1,
            "genres": [],
            "styles": [],
            "decades": [],
        }
        assert len(pool.calls) == 1

    async def test_full_profile_assembles_all_three_parallel_reads(self) -> None:
        pool = FakePool(
            [
                [("901", "Target", 6, 2)],
                [("Electronic", 6)],
                [("Ambient", 6)],
                [(1990, 6)],
            ]
        )
        result = await pg.get_label_full_profile(pool, "901")
        assert result == {
            "label_id": "901",
            "label_name": "Target",
            "release_count": 6,
            "artist_count": 2,
            "genres": [{"name": "Electronic", "count": 6}],
            "styles": [{"name": "Ambient", "count": 6}],
            "decades": [{"decade": 1990, "count": 6}],
        }


class TestCandidateVectors:
    async def test_no_candidates_short_circuits_profile_fetch(self) -> None:
        pool = FakePool([[]])
        assert await pg.get_candidate_labels_genre_vectors(pool, "901") == []
        assert len(pool.calls) == 1

    async def test_candidate_profiles_keep_rank_order_and_empty_genres(self) -> None:
        pool = FakePool(
            [
                [("902", "Candidate A", 8), ("905", "Candidate B", 5)],
                [
                    ("902", "Candidate A", 8, [{"name": "Electronic", "count": 8}]),
                    ("905", "Candidate B", 5, []),
                ],
            ]
        )
        assert await pg.get_candidate_labels_genre_vectors(pool, "901") == [
            {
                "label_id": "902",
                "label_name": "Candidate A",
                "release_count": 8,
                "genres": [{"name": "Electronic", "count": 8}],
            },
            {"label_id": "905", "label_name": "Candidate B", "release_count": 5, "genres": []},
        ]
        assert pool.calls[0].params == {"label_id": "901", "min_releases": 5}
        assert pool.calls[1].params == {"label_ids": ["902", "905"]}

    async def test_candidates_are_batched_in_groups_of_twenty_five(self) -> None:
        candidates = [(str(index), f"Label {index}", 5) for index in range(26)]
        with patch.object(pg, "_rows", new_callable=AsyncMock) as rows:
            rows.side_effect = [candidates, [], []]
            assert await pg.get_candidate_labels_genre_vectors(object(), "901") == []
        assert rows.await_count == 3
        assert rows.await_args_list[1].args[2] == {"label_ids": [str(index) for index in range(25)]}
        assert rows.await_args_list[2].args[2] == {"label_ids": ["25"]}
