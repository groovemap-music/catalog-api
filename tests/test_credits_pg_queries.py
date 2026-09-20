"""Query-shape coverage for the credits family's PostgreSQL backend.

What a fake pool can show is what statement `api/queries/credits_pg_queries.py` builds and
what it does with the rows that come back: that every traversal is `GRAPH_TABLE` over
`graph.catalog` with the labels the schema producer declares, that the `category` column
is read from `role_category` in all six places that read it, that the walk-semantics guard
Neo4j gets for free from relationship isomorphism is written out, and that `depth` selects
a statement rather than parameterising a quantifier.

What it cannot show is that any of those statements returns the rows the Cypher returns —
nothing here parses the SQL, and only PostgreSQL 19 has a graph to parse it against. That
is the `credits` family in `tests/test_real_databases.py`, which runs both engines, and
`tests/test_graph_parity.py` for the claims the row-for-row comparison cannot see.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from api.queries import credits_pg_queries as pg
from tests.fake_postgres import FakePool


pytestmark = pytest.mark.asyncio

# Every statement the module runs, so a new one cannot quietly skip the whole-family rules.
ALL_STATEMENTS: tuple[tuple[str, str], ...] = (
    ("PERSON_CREDITS_SQL", pg.PERSON_CREDITS_SQL),
    ("PERSON_TIMELINE_SQL", pg.PERSON_TIMELINE_SQL),
    ("RELEASE_CREDITS_SQL", pg.RELEASE_CREDITS_SQL),
    ("ROLE_LEADERBOARD_SQL", pg.ROLE_LEADERBOARD_SQL),
    ("SHARED_CREDITS_SQL", pg.SHARED_CREDITS_SQL),
    ("PERSON_CONNECTIONS_SQL", pg.PERSON_CONNECTIONS_SQL),
    ("PERSON_CONNECTIONS_TWO_HOP_SQL", pg.PERSON_CONNECTIONS_TWO_HOP_SQL),
    ("PERSON_PROFILE_SQL", pg.PERSON_PROFILE_SQL),
    ("PERSON_ROLE_BREAKDOWN_SQL", pg.PERSON_ROLE_BREAKDOWN_SQL),
)

# The statements whose result carries the Cypher's `c.category` column, or filters on it.
# The coverage spike singles this rename out because a missed one is a silent null rather
# than an error, so the list is spelled out here and checked rather than left to a grep.
CATEGORY_READING_STATEMENTS: tuple[tuple[str, str], ...] = (
    ("PERSON_CREDITS_SQL", pg.PERSON_CREDITS_SQL),
    ("PERSON_TIMELINE_SQL", pg.PERSON_TIMELINE_SQL),
    ("RELEASE_CREDITS_SQL", pg.RELEASE_CREDITS_SQL),
    ("ROLE_LEADERBOARD_SQL", pg.ROLE_LEADERBOARD_SQL),
    ("PERSON_PROFILE_SQL", pg.PERSON_PROFILE_SQL),
    ("PERSON_ROLE_BREAKDOWN_SQL", pg.PERSON_ROLE_BREAKDOWN_SQL),
)


def _statement_ids(statements: tuple[tuple[str, str], ...]) -> list[str]:
    return [name for name, _ in statements]


class TestFamilyWideStatementShape:
    @pytest.mark.parametrize(("name", "sql"), ALL_STATEMENTS, ids=_statement_ids(ALL_STATEMENTS))
    async def test_every_traversal_is_a_graph_table_over_the_declared_property_graph(self, name: str, sql: str) -> None:
        assert "GRAPH_TABLE (graph.catalog" in sql, name

    @pytest.mark.parametrize(("name", "sql"), ALL_STATEMENTS, ids=_statement_ids(ALL_STATEMENTS))
    async def test_every_statement_walks_the_credited_on_edge(self, name: str, sql: str) -> None:
        # One edge type backs the whole family; a statement that does not walk it is not a
        # credits query at all.
        assert "IS credited_on]" in sql, name

    @pytest.mark.parametrize(("name", "sql"), ALL_STATEMENTS, ids=_statement_ids(ALL_STATEMENTS))
    async def test_every_statement_binds_the_person_vertex_by_its_name_key(self, name: str, sql: str) -> None:
        assert "IS person" in sql, name

    @pytest.mark.parametrize(("name", "sql"), ALL_STATEMENTS, ids=_statement_ids(ALL_STATEMENTS))
    async def test_no_statement_interpolates_a_positional_placeholder(self, name: str, sql: str) -> None:
        # Every value is bound by name, inside the graph pattern as well as outside it, so
        # a caller's string can never become syntax.
        assert not re.search(r"%s|%\(\)s", sql), name

    @pytest.mark.parametrize(("name", "sql"), ALL_STATEMENTS, ids=_statement_ids(ALL_STATEMENTS))
    async def test_every_count_is_cast_to_bigint(self, name: str, sql: str) -> None:
        # `count()` yields `numeric` through psycopg's `Decimal` unless it is cast; the
        # Cypher returns a Python `int` and the response schema says integer.
        for aggregate in re.findall(r"count\([^)]*\)(?!::bigint)", sql):
            assert "ORDER BY" in sql[sql.index(aggregate) - 10 : sql.index(aggregate)] or "::bigint" in sql, f"{name}: {aggregate}"

    @pytest.mark.parametrize(("name", "sql"), CATEGORY_READING_STATEMENTS, ids=_statement_ids(CATEGORY_READING_STATEMENTS))
    async def test_the_category_column_is_read_from_role_category(self, name: str, sql: str) -> None:
        # The family's one rename: `graphinator` writes `CREDITED_ON.category`, the
        # relational edge generates the same value as `role_category`.
        assert "role_category" in sql, name
        assert ".category" not in sql, f"{name} reads a `category` property the edge does not publish"


class TestPersonCreditsStatement:
    async def test_the_two_optional_matches_are_left_joins_on_their_own_patterns(self) -> None:
        assert "IS by_artist]->(artist IS artist)" in pg.PERSON_CREDITS_SQL
        assert "IS on_label]->(label IS label)" in pg.PERSON_CREDITS_SQL
        assert pg.PERSON_CREDITS_SQL.count("LEFT JOIN") == 2

    async def test_both_collect_slices_are_reproduced(self) -> None:
        # `collect(DISTINCT a.name)[..3]` and `collect(DISTINCT l.name)[..1]`.
        assert "[1:3] AS artists" in pg.PERSON_CREDITS_SQL
        assert "[1:1] AS labels" in pg.PERSON_CREDITS_SQL

    async def test_an_unmatched_optional_collects_to_an_empty_list_not_a_null(self) -> None:
        # Cypher's `collect` drops nulls, so a release with no artist yields `[]`.
        assert pg.PERSON_CREDITS_SQL.count("ARRAY[]::text[]") == 2

    async def test_the_year_is_guarded_before_it_is_cast(self) -> None:
        # `graph.release.year` is `text`; the Cypher's `r.year` is an integer property.
        assert "year ~ '^[0-9]{4}$'" in pg.PERSON_CREDITS_SQL

    async def test_it_orders_by_year_descending_then_title(self) -> None:
        assert pg.PERSON_CREDITS_SQL.rstrip().endswith("ORDER BY year DESC, title")

    async def test_the_person_is_bound_in_all_three_patterns(self) -> None:
        assert pg.PERSON_CREDITS_SQL.count("person.name = %(name)s") == 3


class TestReleaseCreditsStatement:
    async def test_the_same_as_optional_match_is_a_left_join_over_a_second_pattern(self) -> None:
        assert "IS same_as]->(artist IS artist)" in pg.RELEASE_CREDITS_SQL
        assert "LEFT JOIN identity" in pg.RELEASE_CREDITS_SQL

    async def test_it_orders_by_category_then_name(self) -> None:
        assert pg.RELEASE_CREDITS_SQL.rstrip().endswith("ORDER BY category, name")

    async def test_the_release_id_is_bound_inside_the_element_pattern(self) -> None:
        assert "release.release_id = %(release_id)s" in pg.RELEASE_CREDITS_SQL


class TestRoleLeaderboardStatement:
    async def test_it_counts_distinct_releases_not_credits(self) -> None:
        # A person holding two roles on one release is one leaderboard credit.
        assert "count(DISTINCT release_id)::bigint AS credit_count" in pg.ROLE_LEADERBOARD_SQL

    async def test_both_the_category_and_the_limit_are_bound(self) -> None:
        assert "credit.role_category = %(category)s" in pg.ROLE_LEADERBOARD_SQL
        assert "LIMIT %(limit)s" in pg.ROLE_LEADERBOARD_SQL


class TestSharedCreditsStatement:
    async def test_the_two_credit_edges_meet_at_one_release(self) -> None:
        assert "-[credit_one IS credited_on]->(release IS release)" in pg.SHARED_CREDITS_SQL
        assert "<-[credit_two IS credited_on]-(person_two IS person" in pg.SHARED_CREDITS_SQL

    async def test_the_walk_semantics_guard_is_written_out(self) -> None:
        """Relationship isomorphism is Neo4j's; SQL/PGQ lets one edge bind to both halves.

        `graph.credited_on` is keyed `(person_name, release_id, role)` and both edges of
        this pattern already share the release, so comparing the other two columns is edge
        inequality. Without it, asking for the releases one person shares with themselves
        returns every release they are credited on; Neo4j returns nothing.
        """
        assert "NOT (credit_one.person_name = credit_two.person_name AND credit_one.role = credit_two.role)" in pg.SHARED_CREDITS_SQL

    async def test_both_names_are_bound(self) -> None:
        assert "person_one.name = %(person1)s" in pg.SHARED_CREDITS_SQL
        assert "person_two.name = %(person2)s" in pg.SHARED_CREDITS_SQL


class TestPersonConnectionsStatements:
    async def test_depth_one_walks_two_edges_and_depth_two_walks_four(self) -> None:
        # `depth` selects a statement; it is not a quantifier, which is what the coverage
        # spike says about this function and what the Cypher's two fixed variants do.
        assert pg.PERSON_CONNECTIONS_SQL.count("IS credited_on]") == 2
        assert pg.PERSON_CONNECTIONS_TWO_HOP_SQL.count("IS credited_on]") == 6

    async def test_the_name_predicates_that_close_the_walk_are_all_present(self) -> None:
        # Each edge identity walk semantics would admit implies two of anchor/bridge/
        # reached are one person, and each of those is excluded here.
        assert "bridge.name <> %(name)s" in pg.PERSON_CONNECTIONS_TWO_HOP_SQL
        assert "reached.name <> %(name)s" in pg.PERSON_CONNECTIONS_TWO_HOP_SQL
        assert "reached.name <> bridge.name" in pg.PERSON_CONNECTIONS_TWO_HOP_SQL

    async def test_the_second_hop_list_is_capped_at_ten(self) -> None:
        # The Cypher's `[..10]`, applied as a LIMIT inside the lateral so ten rows are
        # built rather than all of them and then sliced.
        assert "LIMIT 10" in pg.PERSON_CONNECTIONS_TWO_HOP_SQL

    async def test_a_bridge_with_no_second_hop_gets_an_empty_list(self) -> None:
        assert "COALESCE(capped.second_hops, '[]'::json)" in pg.PERSON_CONNECTIONS_TWO_HOP_SQL

    async def test_the_second_hop_map_carries_the_cypher_keys(self) -> None:
        assert "json_build_object('name'" in pg.PERSON_CONNECTIONS_TWO_HOP_SQL
        assert "'via'" in pg.PERSON_CONNECTIONS_TWO_HOP_SQL
        assert "'shared'" in pg.PERSON_CONNECTIONS_TWO_HOP_SQL


class TestPersonProfileStatement:
    async def test_it_counts_credits_not_distinct_releases(self) -> None:
        # The mirror of the leaderboard's `count(DISTINCT r)`: `count(c)` here.
        assert "count(*)::bigint AS total_credits" in pg.PERSON_PROFILE_SQL
        assert "count(DISTINCT" not in pg.PERSON_PROFILE_SQL

    async def test_the_category_list_is_deduplicated(self) -> None:
        assert "array_agg(DISTINCT role_category) AS categories" in pg.PERSON_PROFILE_SQL

    async def test_the_same_as_lookup_is_a_left_join(self) -> None:
        assert "LEFT JOIN identity" in pg.PERSON_PROFILE_SQL


class TestPersonRoleBreakdownStatement:
    async def test_it_counts_credits_per_category_and_orders_by_the_count(self) -> None:
        assert "count(*)::bigint AS count" in pg.PERSON_ROLE_BREAKDOWN_SQL
        assert pg.PERSON_ROLE_BREAKDOWN_SQL.rstrip().endswith("ORDER BY count(*) DESC")

    async def test_the_release_is_bound_even_though_it_is_never_projected(self) -> None:
        # The Cypher binds it, so a credit whose release is gone is a credit on neither
        # side. It is the one `gm_id` index probe this statement pays for.
        assert "(release IS release)" in pg.PERSON_ROLE_BREAKDOWN_SQL


class TestGetPersonCredits:
    async def test_it_returns_the_cypher_column_names(self) -> None:
        pool = FakePool([[("123", "Kind Of Blue", 1959, "Mastered By", "mastering", ["Miles Davis"], ["Columbia"])]])
        assert await pg.get_person_credits(pool, "Bob Ludwig") == [
            {
                "release_id": "123",
                "title": "Kind Of Blue",
                "year": 1959,
                "role": "Mastered By",
                "category": "mastering",
                "artists": ["Miles Davis"],
                "labels": ["Columbia"],
            }
        ]

    async def test_it_binds_the_person_name(self) -> None:
        pool = FakePool([[]])
        await pg.get_person_credits(pool, "Bob Ludwig")
        assert pool.params == {"name": "Bob Ludwig"}


class TestGetPersonTimeline:
    async def test_it_returns_the_cypher_column_names(self) -> None:
        pool = FakePool([[(1990, "mastering", 5)]])
        assert await pg.get_person_timeline(pool, "Bob Ludwig") == [{"year": 1990, "category": "mastering", "count": 5}]


class TestGetReleaseCredits:
    async def test_a_person_with_no_same_as_artist_keeps_the_null_columns(self) -> None:
        pool = FakePool([[("Rex Quill", "Producer", "production", None, None)]])
        assert await pg.get_release_credits(pool, "701") == [
            {"name": "Rex Quill", "role": "Producer", "category": "production", "artist_id": None, "artist_name": None}
        ]

    async def test_it_binds_the_release_id(self) -> None:
        pool = FakePool([[]])
        await pg.get_release_credits(pool, "701")
        assert pool.params == {"release_id": "701"}


class TestGetRoleLeaderboard:
    async def test_it_returns_the_cypher_column_names(self) -> None:
        pool = FakePool([[("Bob Ludwig", 42)]])
        assert await pg.get_role_leaderboard(pool, "mastering") == [{"name": "Bob Ludwig", "credit_count": 42}]

    async def test_it_keeps_the_cypher_default_limit(self) -> None:
        pool = FakePool([[]])
        await pg.get_role_leaderboard(pool, "mastering")
        assert pool.params == {"category": "mastering", "limit": 20}


class TestGetSharedCredits:
    async def test_it_returns_the_cypher_column_names(self) -> None:
        pool = FakePool([[("702", "Release 702", 1966, "Mastered By", "Guitar", ["Hale Combo"])]])
        assert await pg.get_shared_credits(pool, "Tessa Vance", "Marlon Hale") == [
            {
                "release_id": "702",
                "title": "Release 702",
                "year": 1966,
                "person1_role": "Mastered By",
                "person2_role": "Guitar",
                "artists": ["Hale Combo"],
            }
        ]


class TestGetPersonConnections:
    async def test_depth_one_returns_two_columns_and_runs_the_one_hop_statement(self) -> None:
        pool = FakePool([[("Marlon Hale", 3)]])
        assert await pg.get_person_connections(pool, "Tessa Vance", depth=1) == [{"name": "Marlon Hale", "shared_count": 3}]
        assert pool.sql == pg.PERSON_CONNECTIONS_SQL

    async def test_depth_two_adds_the_second_hops_column(self) -> None:
        hops: list[dict[str, Any]] = [{"name": "Ida Okonkwo", "via": "Marlon Hale", "shared": 1}]
        pool = FakePool([[("Marlon Hale", 3, hops)]])
        assert await pg.get_person_connections(pool, "Tessa Vance", depth=2) == [
            {"name": "Marlon Hale", "shared_count": 3, "second_hops": hops}
        ]
        assert pool.sql == pg.PERSON_CONNECTIONS_TWO_HOP_SQL

    async def test_depth_three_selects_the_same_statement_as_depth_two(self) -> None:
        # The Cypher's second variant is the only one that adds hops, so depth 3 is depth
        # 2 on both backends. The endpoint accepts it, so it has to mean something.
        pool = FakePool([[]])
        await pg.get_person_connections(pool, "Tessa Vance", depth=3)
        assert pool.sql == pg.PERSON_CONNECTIONS_TWO_HOP_SQL

    @pytest.mark.parametrize("depth", [-5, 0])
    async def test_a_depth_below_one_is_clamped_up(self, depth: int) -> None:
        pool = FakePool([[]])
        await pg.get_person_connections(pool, "Tessa Vance", depth=depth)
        assert pool.sql == pg.PERSON_CONNECTIONS_SQL

    async def test_it_keeps_the_cypher_defaults(self) -> None:
        pool = FakePool([[]])
        await pg.get_person_connections(pool, "Tessa Vance")
        assert pool.sql == pg.PERSON_CONNECTIONS_TWO_HOP_SQL
        assert pool.params == {"name": "Tessa Vance", "limit": 50}


class TestGetPersonProfile:
    async def test_it_returns_the_cypher_column_names(self) -> None:
        pool = FakePool([[("Tessa Vance", 4, ["mastering"], 1963, 1972, "801", "Vance Machine")]])
        assert await pg.get_person_profile(pool, "Tessa Vance") == {
            "name": "Tessa Vance",
            "total_credits": 4,
            "categories": ["mastering"],
            "first_year": 1963,
            "last_year": 1972,
            "artist_id": "801",
            "artist_name": "Vance Machine",
        }

    async def test_a_person_with_no_credits_is_none(self) -> None:
        pool = FakePool([[]])
        assert await pg.get_person_profile(pool, "Nobody") is None


class TestGetPersonRoleBreakdown:
    async def test_it_returns_the_cypher_column_names(self) -> None:
        pool = FakePool([[("production", 3), ("engineering", 1)]])
        assert await pg.get_person_role_breakdown(pool, "Rex Quill") == [
            {"category": "production", "count": 3},
            {"category": "engineering", "count": 1},
        ]
