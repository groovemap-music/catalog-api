"""Query-shape and response-shape coverage for the admin storage panel (PostgreSQL backend).

`get_neo4j_storage` is not registered with the Neo4j-versus-PostgreSQL parity harness — see
`api/queries/admin_pg_queries.py`'s module docstring for why: JMX store sizes and the full
MusicBrainz relationship vocabulary have no PostgreSQL equivalent at this phase, so a row
comparison against the Neo4j side would either be vacuous or require fixture surface this
family's phase 0 scope does not otherwise need. This module is what stands in for that: the
SQL shape, and that the response validates against `api.models.Neo4jStorage` — the same four
`store_sizes` keys the Neo4j backend returns, `nodes`/`relationships`/`strings` populated with
`None` rather than a formatted size, which is why those three fields are typed `str | None`.
"""

from __future__ import annotations

import pytest

from api.models import Neo4jStorage
from api.queries import admin_pg_queries as pg
from tests.fake_postgres import FakePool


pytestmark = pytest.mark.asyncio


class TestStatementShape:
    async def test_no_statement_is_a_graph_table_pattern(self) -> None:
        # Counting rows in a view is ordinary SQL, not a traversal — none of these three
        # statements needs `graph.catalog`.
        for sql in (pg.NODE_COUNTS_SQL, pg.EDGE_COUNTS_SQL, pg.STORE_SIZE_SQL):
            assert "GRAPH_TABLE" not in sql

    async def test_node_counts_cover_the_same_six_labels_as_get_graph_stats(self) -> None:
        for view in ("graph.artist", "graph.label", "graph.release", "graph.master", "graph.genre", "graph.style"):
            assert f"FROM {view}" in pg.NODE_COUNTS_SQL

    async def test_node_counts_are_ordered_alphabetically_by_label(self) -> None:
        # Mirrors `get_neo4j_storage`'s `sorted(record["labels"].items())`.
        assert pg.NODE_COUNTS_SQL.strip().endswith("ORDER BY label")

    async def test_edge_counts_fold_in_genre_and_in_style_into_one_is_bucket(self) -> None:
        # `graphinator` writes both patterns as a single `[:IS]` type, so `apoc.meta.stats()`
        # reports one `IS` bucket rather than two — the SQL side has to fold them the same way.
        assert pg.EDGE_COUNTS_SQL.count("'IS'") == 2
        assert "FROM graph.in_genre" in pg.EDGE_COUNTS_SQL
        assert "FROM graph.in_style" in pg.EDGE_COUNTS_SQL
        assert "GROUP BY type" in pg.EDGE_COUNTS_SQL

    async def test_edge_counts_are_ordered_alphabetically_by_type(self) -> None:
        assert pg.EDGE_COUNTS_SQL.strip().endswith("ORDER BY type")

    async def test_store_size_sums_the_four_discogs_entity_tables(self) -> None:
        for table in ("artists", "labels", "releases", "masters"):
            assert f"pg_total_relation_size('{table}')" in pg.STORE_SIZE_SQL


class TestFormatBytes:
    async def test_formats_gigabytes(self) -> None:
        assert pg._format_bytes(2 * 1_073_741_824) == "2.0 GB"

    async def test_formats_megabytes(self) -> None:
        assert pg._format_bytes(5 * 1_048_576) == "5 MB"

    async def test_formats_kilobytes(self) -> None:
        assert pg._format_bytes(2048) == "2 kB"


class TestGetNeo4jStorage:
    async def test_shapes_nodes_and_relationships_as_the_admin_queries_module_does(self) -> None:
        pool = FakePool(
            [
                [("Artist", 11), ("Genre", 0), ("Label", 1), ("Master", 1), ("Release", 15), ("Style", 0)],
                [("BY", 12), ("ON", 0)],
                [(1_048_576,)],
            ]
        )
        result = await pg.get_neo4j_storage(pool)

        assert result["status"] == "ok"
        assert result["nodes"] == [
            {"label": "Artist", "count": 11},
            {"label": "Genre", "count": 0},
            {"label": "Label", "count": 1},
            {"label": "Master", "count": 1},
            {"label": "Release", "count": 15},
            {"label": "Style", "count": 0},
        ]
        assert result["relationships"] == [{"type": "BY", "count": 12}, {"type": "ON", "count": 0}]
        assert result["store_sizes"] == {"total": "1 MB", "nodes": None, "relationships": None, "strings": None}

    async def test_response_validates_against_the_declared_neo4j_storage_contract(self) -> None:
        # api/models.py:StoreSizes types nodes/relationships/strings as str | None precisely
        # so this backend's None-filled payload is a valid Neo4jStorage rather than a
        # response that only stays green because /api/admin/storage skips model validation.
        pool = FakePool(
            [
                [("Artist", 11), ("Genre", 0), ("Label", 1), ("Master", 1), ("Release", 15), ("Style", 0)],
                [("BY", 12), ("ON", 0)],
                [(1_048_576,)],
            ]
        )
        result = await pg.get_neo4j_storage(pool)
        Neo4jStorage.model_validate(result)

    async def test_runs_three_statements_in_the_documented_order(self) -> None:
        pool = FakePool([[], [], [(0,)]])
        await pg.get_neo4j_storage(pool)
        assert [call.sql for call in pool.calls] == [pg.NODE_COUNTS_SQL, pg.EDGE_COUNTS_SQL, pg.STORE_SIZE_SQL]

    async def test_reports_zero_bytes_when_the_size_query_returns_no_row(self) -> None:
        pool = FakePool([[], [], []])
        result = await pg.get_neo4j_storage(pool)
        assert result["store_sizes"]["total"] == "0 kB"
