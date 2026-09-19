"""Query-shape coverage for the SQL/PGQ one-hop collaborators backend.

These run without a server, so they assert what the module *sends*: that the traversal is
pattern matching over `graph.catalog` with the labels the schema producer declares, that the
walk-semantics guard Neo4j gets for free from relationship isomorphism is written out, and
that the year filter mirrors the Cypher's `r.year > 0` over a JSONB-backed text column.

Row-level agreement with the Cypher is not something a fake pool can show. That is
`tests/test_real_databases.py`'s `one_hop_collaborators` family, which runs both engines.
"""

from __future__ import annotations

import re

import pytest

from api.queries import collaborator_pg_queries as pg
from tests.fake_postgres import FakePool


pytestmark = pytest.mark.asyncio

ALL_STATEMENTS = (
    pg.ONE_HOP_COLLABORATORS_SQL,
    pg.COUNT_ONE_HOP_COLLABORATORS_SQL,
)


class TestStatementShape:
    """What the module-level SQL constants are made of."""

    @pytest.mark.parametrize("sql", ALL_STATEMENTS)
    async def test_every_traversal_is_a_graph_table_over_the_declared_graph(self, sql: str) -> None:
        assert "GRAPH_TABLE (graph.catalog" in sql

    @pytest.mark.parametrize("sql", ALL_STATEMENTS)
    async def test_the_anchor_artist_is_bound_not_interpolated(self, sql: str) -> None:
        assert "anchor.artist_id = %(artist_id)s" in sql

    @pytest.mark.parametrize("sql", ALL_STATEMENTS)
    async def test_every_quoted_literal_is_structural_not_a_baked_in_value(self, sql: str) -> None:
        # Unlike the pilot family, this one carries quoted literals: the year-shape regex
        # guard and the two JSON key names `json_build_object` writes — both structural,
        # neither a value a caller supplied. `%(artist_id)s` and `%(limit)s` remain the
        # only caller-supplied placeholders.
        quoted = set(re.findall(r"'[^']*'", sql))
        assert quoted <= {"'^[0-9]{4}$'", "'year'", "'count'"}
        assert "'^[0-9]{4}$'" in quoted

    async def test_get_collaborators_projects_the_year_regex_and_json_keys(self) -> None:
        quoted = set(re.findall(r"'[^']*'", pg.ONE_HOP_COLLABORATORS_SQL))
        assert quoted == {"'^[0-9]{4}$'", "'year'", "'count'"}

    @pytest.mark.parametrize("sql", ALL_STATEMENTS)
    async def test_the_hop_uses_the_by_artist_label_in_both_directions(self, sql: str) -> None:
        # Release -> artist is the edge's declared direction, so reaching a collaborator
        # means traversing it backwards and then forwards.
        assert "<-[IS by_artist]-(credit IS release)-[IS by_artist]->(peer IS artist)" in sql

    @pytest.mark.parametrize("sql", ALL_STATEMENTS)
    async def test_the_walk_semantics_guard_excludes_the_anchor_as_its_own_peer(self, sql: str) -> None:
        # SQL/PGQ has no relationship isomorphism: without this, a release shared with
        # nobody else would let the same edge bind to both legs of the pattern and report
        # the anchor as its own collaborator.
        assert "peer.artist_id <> anchor.artist_id" in sql

    @pytest.mark.parametrize("sql", ALL_STATEMENTS)
    async def test_the_year_filter_guards_the_cast_before_it_runs(self, sql: str) -> None:
        # Mirrors the defensive order `api.queries.search_queries._run_decade_facets` uses
        # on the same JSONB-backed `year` column: a regex check before the numeric cast.
        assert "credit.year ~ '^[0-9]{4}$'" in sql
        assert "(credit.year)::int AS year" in sql

    async def test_the_zero_year_is_excluded_after_the_cast(self) -> None:
        # `r.year > 0` in the Cypher; the regex alone would let "0000" through.
        assert "WHERE year > 0" in pg.ONE_HOP_COLLABORATORS_SQL
        assert "WHERE year > 0" in pg.COUNT_ONE_HOP_COLLABORATORS_SQL

    async def test_the_result_ordering_matches_the_cypher(self) -> None:
        assert "ORDER BY release_count DESC" in pg.ONE_HOP_COLLABORATORS_SQL
        assert pg.ONE_HOP_COLLABORATORS_SQL.rstrip().endswith("LIMIT %(limit)s")

    async def test_the_projected_columns_match_the_cypher(self) -> None:
        projection = pg.ONE_HOP_COLLABORATORS_SQL.split("FROM by_year")[0]
        assert "collaborator_id AS artist_id" in projection
        assert "collaborator_name AS artist_name" in projection
        assert "release_count" in projection
        assert "first_year" in projection
        assert "last_year" in projection
        assert "yearly_counts" in projection

    async def test_the_yearly_breakdown_is_a_json_array_ordered_by_year(self) -> None:
        assert "json_agg(json_build_object('year', year, 'count', year_count) ORDER BY year)" in pg.ONE_HOP_COLLABORATORS_SQL

    async def test_release_count_is_cast_to_bigint_so_the_driver_returns_int_not_decimal(self) -> None:
        # `sum()` over bigint yields numeric, which psycopg hands back as Decimal; the
        # Cypher returns a Python int and the response schema says integer.
        assert "sum(year_count)::bigint AS release_count" in pg.ONE_HOP_COLLABORATORS_SQL
        assert "count(DISTINCT collaborator_id)::bigint AS total" in pg.COUNT_ONE_HOP_COLLABORATORS_SQL


class TestGetCollaborators:
    async def test_maps_rows_onto_the_cypher_column_names(self) -> None:
        yearly = [{"year": 2001, "count": 1}]
        pool = FakePool([[("456", "John Coltrane", 5, 2001, 2005, yearly)]])
        assert await pg.get_collaborators(pool, "123", limit=20) == [
            {
                "artist_id": "456",
                "artist_name": "John Coltrane",
                "release_count": 5,
                "first_year": 2001,
                "last_year": 2005,
                "yearly_counts": yearly,
            }
        ]

    async def test_binds_artist_id_and_limit(self) -> None:
        pool = FakePool([[]])
        await pg.get_collaborators(pool, "123", limit=7)
        assert pool.sql == pg.ONE_HOP_COLLABORATORS_SQL
        assert pool.params == {"artist_id": "123", "limit": 7}

    async def test_defaults_match_the_neo4j_signature(self) -> None:
        pool = FakePool([[]])
        await pg.get_collaborators(pool, "123")
        assert pool.params == {"artist_id": "123", "limit": 20}

    async def test_returns_an_empty_list_when_nothing_matches(self) -> None:
        assert await pg.get_collaborators(FakePool([[]]), "123") == []


class TestCountCollaborators:
    async def test_returns_the_total_as_an_int(self) -> None:
        pool = FakePool([[(3,)]])
        total = await pg.count_collaborators(pool, "123")
        assert total == 3
        assert isinstance(total, int)

    async def test_binds_the_artist_id(self) -> None:
        pool = FakePool([[(0,)]])
        await pg.count_collaborators(pool, "123")
        assert pool.sql == pg.COUNT_ONE_HOP_COLLABORATORS_SQL
        assert pool.params == {"artist_id": "123"}

    async def test_returns_zero_when_the_server_returns_no_row(self) -> None:
        assert await pg.count_collaborators(FakePool([[]]), "123") == 0
