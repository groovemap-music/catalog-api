"""Query-shape coverage for the SQL/PGQ collaborators backend.

These run without a server, so they assert what the module *sends*: that every value is
bound rather than interpolated, that the traversal is pattern matching over
`graph.catalog` with the labels the schema producer declares, that the depth-2 branch is
an explicitly chained two-hop pattern with the constraints Neo4j gets from relationship
isomorphism, and that the anti-join is a NOT EXISTS over a second GRAPH_TABLE.

Row-level agreement with the Cypher is not something a fake pool can show. That is
`tests/test_graph_parity.py`, which runs both engines.
"""

from __future__ import annotations

import re

import pytest

from api.queries import network_pg_queries as pg
from tests.fake_postgres import FakePool


pytestmark = pytest.mark.asyncio

ALL_STATEMENTS = (
    pg.ARTIST_IDENTITY_SQL,
    pg.MULTI_HOP_COLLABORATORS_SQL,
    pg.COUNT_MULTI_HOP_COLLABORATORS_SQL,
)


class TestArtistCentrality:
    async def test_uses_precomputed_degree_and_bound_artist_id(self) -> None:
        sql = pg.ARTIST_CENTRALITY_SQL
        assert "FROM graph.artist_vertex a" in sql
        assert "a.degree" in sql
        assert "a.artist_id = %(artist_id)s" in sql
        assert "count(DISTINCT peer.artist_id)" in sql
        assert "count(DISTINCT artist_id)::bigint FROM graph.alias_of" in sql
        assert "WHERE alias_artist_id = a.artist_id" in sql

    async def test_result_shape_and_missing_artist(self) -> None:
        pool = FakePool([[("1", "Artist", 6, 2, 1, 1, 0)]])
        assert await pg.get_artist_centrality(pool, "1") == {
            "artist_id": "1",
            "artist_name": "Artist",
            "degree": 6,
            "collaborator_count": 2,
            "collaboration_releases": 1,
            "group_count": 1,
            "alias_count": 0,
        }
        assert pool.params == {"artist_id": "1"}
        assert await pg.get_artist_centrality(FakePool([[]]), "missing") is None


class TestStatementShape:
    """What the module-level SQL constants are made of."""

    @pytest.mark.parametrize("sql", ALL_STATEMENTS)
    async def test_every_traversal_is_a_graph_table_over_the_declared_graph(self, sql: str) -> None:
        assert "GRAPH_TABLE (graph.catalog" in sql

    @pytest.mark.parametrize("sql", ALL_STATEMENTS)
    async def test_the_anchor_artist_is_bound_not_interpolated(self, sql: str) -> None:
        # Inside the graph pattern, which is the part a reader is most likely to assume
        # cannot take a parameter.
        assert "anchor.artist_id = %(artist_id)s" in sql

    @pytest.mark.parametrize("sql", ALL_STATEMENTS)
    async def test_no_statement_carries_a_quoted_literal(self, sql: str) -> None:
        # A single quote anywhere in these statements would mean a value got baked in.
        assert "'" not in sql

    async def test_depth_one_uses_the_by_artist_label_in_both_directions(self) -> None:
        # Release -> artist is the edge's declared direction, so reaching a collaborator
        # means traversing it backwards and then forwards.
        assert "<-[IS by_artist]-(credit IS release)-[IS by_artist]->(peer IS artist)" in pg.MULTI_HOP_COLLABORATORS_SQL

    async def test_depth_two_is_an_explicit_chained_pattern_not_a_quantifier(self) -> None:
        sql = pg.MULTI_HOP_COLLABORATORS_SQL
        assert "<-[IS by_artist]-(near IS release)-[IS by_artist]->(bridge IS artist)" in sql
        assert "<-[IS by_artist]-(far IS release)-[IS by_artist]->(peer IS artist)" in sql
        # No variable-length spelling anywhere: the hops are written out.
        assert "{1," not in sql

    async def test_depth_two_restates_the_constraints_neo4j_derives_from_edge_isomorphism(self) -> None:
        sql = pg.MULTI_HOP_COLLABORATORS_SQL
        for predicate in (
            "bridge.artist_id <> anchor.artist_id",
            "peer.artist_id <> anchor.artist_id",
            "peer.artist_id <> bridge.artist_id",
            "far.release_id <> near.release_id",
        ):
            assert predicate in sql

    @pytest.mark.parametrize("sql", (pg.MULTI_HOP_COLLABORATORS_SQL, pg.COUNT_MULTI_HOP_COLLABORATORS_SQL))
    async def test_the_anti_join_is_a_not_exists_over_a_second_graph_table(self, sql: str) -> None:
        anti_join = sql[sql.index("NOT EXISTS") :]
        assert "GRAPH_TABLE (graph.catalog" in anti_join
        assert "one_hop.collaborator_id = indirect.collaborator_id" in anti_join
        # Two GRAPH_TABLE traversals for the result and one more for the exclusion.
        assert sql.count("GRAPH_TABLE (graph.catalog") == 3

    @pytest.mark.parametrize("sql", (pg.MULTI_HOP_COLLABORATORS_SQL, pg.COUNT_MULTI_HOP_COLLABORATORS_SQL))
    async def test_the_two_hop_branch_is_gated_on_the_depth_parameter(self, sql: str) -> None:
        assert "WHERE %(depth)s >= 2" in sql

    async def test_the_result_ordering_matches_the_cypher(self) -> None:
        sql = pg.MULTI_HOP_COLLABORATORS_SQL
        assert "ORDER BY distance ASC, collaboration_count DESC" in sql
        assert sql.rstrip().endswith("LIMIT %(limit)s")

    async def test_the_projected_columns_match_the_cypher(self) -> None:
        projection = pg.MULTI_HOP_COLLABORATORS_SQL.split("FROM (")[0]
        assert re.search(r"collaborator_id AS artist_id", projection)
        assert re.search(r"collaborator_name AS artist_name", projection)
        assert "distance" in projection
        assert "collaboration_count" in projection

    async def test_counts_are_cast_to_bigint_so_the_driver_returns_int_not_decimal(self) -> None:
        # `sum()` over bigint yields numeric, which psycopg hands back as Decimal; the
        # Cypher returns a Python int and the response schema says integer.
        assert "count(DISTINCT release_id)::bigint" in pg.MULTI_HOP_COLLABORATORS_SQL
        assert "count(DISTINCT bridge_id)::bigint" in pg.MULTI_HOP_COLLABORATORS_SQL
        assert "count(*)::bigint AS total" in pg.COUNT_MULTI_HOP_COLLABORATORS_SQL


class TestGetArtistIdentity:
    async def test_returns_the_cypher_column_names(self) -> None:
        pool = FakePool([[("123", "Miles Davis")]])
        assert await pg.get_artist_identity(pool, "123") == {"artist_id": "123", "artist_name": "Miles Davis"}

    async def test_binds_the_artist_id(self) -> None:
        pool = FakePool([[("123", "Miles Davis")]])
        await pg.get_artist_identity(pool, "123")
        assert pool.sql == pg.ARTIST_IDENTITY_SQL
        assert pool.params == {"artist_id": "123"}

    async def test_returns_none_for_an_unknown_artist(self) -> None:
        pool = FakePool([[]])
        assert await pg.get_artist_identity(pool, "missing") is None


class TestGetMultiHopCollaborators:
    async def test_maps_rows_onto_the_cypher_column_names(self) -> None:
        pool = FakePool([[("456", "John Coltrane", 1, 5), ("789", "Herbie Hancock", 2, 3)]])
        assert await pg.get_multi_hop_collaborators(pool, "123", depth=2, limit=50) == [
            {"artist_id": "456", "artist_name": "John Coltrane", "distance": 1, "collaboration_count": 5},
            {"artist_id": "789", "artist_name": "Herbie Hancock", "distance": 2, "collaboration_count": 3},
        ]

    async def test_binds_artist_id_depth_and_limit(self) -> None:
        pool = FakePool([[]])
        await pg.get_multi_hop_collaborators(pool, "123", depth=1, limit=7)
        assert pool.sql == pg.MULTI_HOP_COLLABORATORS_SQL
        assert pool.params == {"artist_id": "123", "depth": 1, "limit": 7}

    async def test_defaults_match_the_neo4j_signature(self) -> None:
        pool = FakePool([[]])
        await pg.get_multi_hop_collaborators(pool, "123")
        assert pool.params == {"artist_id": "123", "depth": 2, "limit": 50}

    async def test_returns_an_empty_list_when_nothing_matches(self) -> None:
        assert await pg.get_multi_hop_collaborators(FakePool([[]]), "123") == []


class TestCountMultiHopCollaborators:
    async def test_returns_the_total_as_an_int(self) -> None:
        pool = FakePool([[(15,)]])
        total = await pg.count_multi_hop_collaborators(pool, "123", depth=2)
        assert total == 15
        assert isinstance(total, int)

    async def test_binds_artist_id_and_depth_but_not_limit(self) -> None:
        pool = FakePool([[(0,)]])
        await pg.count_multi_hop_collaborators(pool, "123", depth=1)
        assert pool.sql == pg.COUNT_MULTI_HOP_COLLABORATORS_SQL
        assert pool.params == {"artist_id": "123", "depth": 1}

    async def test_returns_zero_when_the_server_returns_no_row(self) -> None:
        assert await pg.count_multi_hop_collaborators(FakePool([[]]), "123") == 0
