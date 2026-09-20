"""Query-shape coverage for the SQL/PGQ rarity backend.

These run without a server, so they assert what the module *sends*. The first class is the
one that matters: the chunking contract is why this workload runs at all — eight unbounded
`MATCH (r:Release)` scans failed `release_rarity` on 33 consecutive daily cycles against
Neo4j's 600s transaction timeout — and a PostgreSQL statement that traversed the whole catalog
and filtered afterwards would reintroduce exactly that, silently, because it would still
return the right rows.

Row-level agreement with the Cypher is not something a fake pool can show. That is the parity
harness in `tests/test_real_databases.py`, which runs both engines over one fixture.
"""

from __future__ import annotations

import re

import pytest

from api.queries import rarity_pg_queries as pg
from api.queries import rarity_queries as cypher
from api.queries.rarity_pipeline import RARITY_PAGE_SIZE, RARITY_QUERY_TIMEOUT_SECONDS, RarityHandles
from api.rarity import family_sql_queries
from tests.fake_postgres import FakePool


# Every statement that is scoped to a page of release ids: the eight core signal statements
# plus whatever the family registry contributes, discovered the way `fetch_page_signals`
# discovers them rather than by name.
SIGNAL_STATEMENTS: dict[str, str] = {**pg._CORE_SQL, **family_sql_queries()}

# The page of ids a test binds. Small enough to read, and the values never matter: what these
# tests assert is that the page reaches the statement as a parameter at all.
PAGE = ["101", "731"]


def _signal_results() -> list[list[tuple[object, ...]]]:
    """Return one empty result set per statement `fetch_page_signals` runs, plus the two SETs."""
    return [[] for _ in range(len(SIGNAL_STATEMENTS) + 2)]


class TestChunkingContract:
    """groovemap-lx1n, in its PostgreSQL spelling.

    `UNWIND $ids AS rid / MATCH (r:Release {id: rid})` becomes `= ANY(%(ids)s)` *inside* the
    element pattern. A `GRAPH_TABLE` that matched every release and was filtered by a
    surrounding `WHERE` would answer identically and traverse the whole catalog to do it.
    """

    def test_the_page_is_the_same_size_on_both_backends(self) -> None:
        assert RARITY_PAGE_SIZE == 20_000
        assert cypher.RARITY_PAGE_SIZE is RARITY_PAGE_SIZE

    def test_the_two_backends_run_the_same_set_of_signal_statements(self) -> None:
        """A fact answered on one backend and not the other is a hole the join cannot see."""
        assert set(SIGNAL_STATEMENTS) == {*cypher._CORE_QUERIES, *cypher.family_queries()}

    @pytest.mark.parametrize("fact", sorted(SIGNAL_STATEMENTS))
    def test_every_signal_statement_binds_the_page(self, fact: str) -> None:
        assert "= ANY(%(ids)s)" in SIGNAL_STATEMENTS[fact], f"{fact} is not page-scoped"

    @pytest.mark.parametrize("fact", sorted(SIGNAL_STATEMENTS))
    def test_no_graph_pattern_starts_from_an_unbound_release(self, fact: str) -> None:
        """Every `MATCH`'s anchor element pattern carries the page predicate itself."""
        for line in SIGNAL_STATEMENTS[fact].splitlines():
            if "MATCH (" not in line:
                continue
            assert "= ANY(%(ids)s)" in line, f"{fact} anchors a traversal on an unbound vertex: {line.strip()}"

    @pytest.mark.parametrize("fact", sorted(SIGNAL_STATEMENTS))
    def test_no_read_of_the_release_view_is_unbounded(self, fact: str) -> None:
        """A plain `FROM graph.release` is a scan of every release unless the page bounds it."""
        for tail in re.split(r"FROM graph\.release\b", SIGNAL_STATEMENTS[fact])[1:]:
            assert "WHERE release_id = ANY(%(ids)s)" in tail[:120], f"{fact} reads graph.release without the page"

    @pytest.mark.parametrize("fact", sorted(SIGNAL_STATEMENTS))
    def test_no_signal_statement_pages_itself(self, fact: str) -> None:
        """Paging belongs to the keyset walk. A signal statement that also limited would
        silently drop releases the walk had already committed to scoring."""
        assert "LIMIT" not in SIGNAL_STATEMENTS[fact]

    @pytest.mark.asyncio
    async def test_the_walk_asks_for_one_page_at_a_time(self) -> None:
        pool = FakePool([[], [("101",), ("102",)], []])
        ids = await pg.fetch_release_id_page(RarityHandles(graph=pool, insights=pool), "", RARITY_PAGE_SIZE)

        assert ids == ["101", "102"]
        page_call = next(call for call in pool.calls if "graph.release" in call.sql)
        assert page_call.params == {"cursor": "", "limit": RARITY_PAGE_SIZE}

    @pytest.mark.asyncio
    async def test_every_page_statement_carries_the_server_side_budget(self) -> None:
        """The Cypher passes a per-query timeout; this is the same budget, as PostgreSQL takes
        it. It is reset rather than left on the connection, which the pool hands out with
        autocommit on and reuses for unrelated work."""
        pool = FakePool(_signal_results())
        await pg.fetch_page_signals(RarityHandles(graph=pool, insights=pool), PAGE)

        assert pool.calls[0].sql == pg._SET_STATEMENT_TIMEOUT_SQL
        assert pool.calls[0].params == {"timeout_ms": str(int(RARITY_QUERY_TIMEOUT_SECONDS * 1000))}
        assert pool.calls[-1].sql == "RESET statement_timeout"


class TestKeysetWalk:
    def test_the_page_query_orders_and_compares_under_one_explicit_collation(self) -> None:
        """Neo4j orders strings by code point and PostgreSQL by the database's collation, so
        the boundary the cursor lands on is only the same boundary if the alphabet is."""
        sql = pg.RELEASE_ID_PAGE_SQL
        assert 'WHERE release_id COLLATE "C" > %(cursor)s' in sql
        assert 'ORDER BY release_id COLLATE "C"' in sql
        assert "LIMIT %(limit)s" in sql

    @pytest.mark.asyncio
    async def test_the_release_count_is_one_aggregate(self) -> None:
        pool = FakePool([[], [(22,)], []])
        assert await pg.count_releases(RarityHandles(graph=pool, insights=pool)) == 22
        assert "count(*)::bigint" in pg.RELEASE_COUNT_SQL


class TestCounterReads:
    """The four signals that read a counter rather than re-aggregating it.

    Re-aggregating on request is the failure the chunking contract exists to prevent, so these
    stay property reads on the PostgreSQL side too — on the vertex projections the phase 2
    schema revision added, which is what makes `l.release_count` read as the Cypher reads it.
    """

    def test_label_catalog_reads_the_label_counter(self) -> None:
        for sql in (pg.LABEL_SQL, pg.LABEL_SIZE_SQL):
            assert "-[IS on_label]->(l IS label)" in sql
            assert "l.release_count AS release_count" in sql

    def test_genre_count_reads_the_genre_counter_and_not_the_style_one(self) -> None:
        assert "-[IS in_genre]->(g IS genre)" in pg.GENRE_COUNT_SQL
        assert "g.release_count AS release_count" in pg.GENRE_COUNT_SQL
        assert "in_style" not in pg.GENRE_COUNT_SQL

    def test_artist_degree_reads_the_artist_counter(self) -> None:
        assert "-[IS by_artist]->(a IS artist)" in pg.ARTIST_DEGREE_SQL
        assert "a.degree AS degree" in pg.ARTIST_DEGREE_SQL

    def test_release_degree_reads_the_one_counter_that_is_not_on_its_label(self) -> None:
        """`graph.release_degree` is its own label because its live half is a pair of lateral
        counts no unique key makes removable; folding it onto `release` would make every
        traversal that binds a release pay for them."""
        assert "(d IS release_degree WHERE d.release_id = ANY(%(ids)s))" in pg.DEGREE_SQL
        assert "d.degree AS degree" in pg.DEGREE_SQL

    def test_a_release_with_no_counter_row_reads_zero_rather_than_null(self) -> None:
        """Every caller of degree does arithmetic on it, so a null would propagate."""
        assert "COALESCE(counted.degree, 0)::bigint AS degree" in pg.DEGREE_SQL


class TestReleaseAndMediaShape:
    def test_the_display_name_is_the_alphabetically_first_credit(self) -> None:
        """The Cypher's `min(a.name)`. Both sides pin the collation, because two engines
        cannot agree on "first" unless they agree on the alphabet."""
        assert 'min(name COLLATE "C") AS artist_name' in pg.RELEASE_SQL
        assert "min(a.name) AS artist_name" in cypher._RELEASE_QUERY

    @pytest.mark.parametrize("sql", [pg.RELEASE_SQL, pg.TEMPORAL_SQL], ids=["release", "temporal"])
    def test_year_is_guarded_before_it_is_cast(self, sql: str) -> None:
        """`graph.release.year` is text off the Discogs document, unlike Neo4j's integer
        property. An unguarded cast fails the whole aggregate on the first bad value."""
        assert "~ '^[0-9]{4}$'" in sql

    def test_media_rows_carry_the_same_keys_the_cypher_collects(self) -> None:
        assert "jsonb_build_object('id', medium_id, 'family', family)" in pg.MEDIA_SQL

    def test_a_release_with_no_medium_gets_an_empty_list_not_null(self) -> None:
        """The Cypher's list comprehension drops the null row an OPTIONAL MATCH leaves."""
        assert "COALESCE(issued.mediums, '[]'::jsonb)" in pg.MEDIA_SQL

    def test_the_sibling_walk_restates_what_neo4j_gets_from_edge_isomorphism(self) -> None:
        """Walk semantics let the same `derived_from` edge bind to both edge patterns, so
        without this every mastered release is its own latest sibling."""
        assert "WHERE sibling.release_id <> r.release_id" in pg.TEMPORAL_SQL


class TestGroovedPressingStatement:
    """groovemap-cu2.75, carried across to SQL.

    The master lookup and the sibling lookup are two separate optional lookups on both
    backends. Combining them makes the master link contingent on a sibling existing, which
    scores the rarest case — a unique pressing of a known master — as if it had no master.
    """

    def test_the_family_module_owns_both_spellings_of_its_question(self) -> None:
        from api.rarity.families.grooved import PRESSING_FACT, GroovedSignals

        module = GroovedSignals()
        assert set(module.queries) == set(module.sql_queries) == {PRESSING_FACT}

    def test_the_master_link_and_the_sibling_count_are_separate_lookups(self) -> None:
        from api.rarity.families.grooved import PRESSING_SQL

        assert "mastered AS (" in PRESSING_SQL
        assert "siblings AS (" in PRESSING_SQL
        assert "LEFT JOIN mastered ON mastered.release_id = page.release_id" in PRESSING_SQL
        assert "LEFT JOIN siblings ON siblings.release_id = page.release_id" in PRESSING_SQL

    def test_the_plus_one_lives_inside_the_master_linked_branch(self) -> None:
        from api.rarity.families.grooved import PRESSING_SQL

        assert "CASE WHEN mastered.release_id IS NULL THEN 0 ELSE COALESCE(siblings.tally, 0) + 1 END" in PRESSING_SQL

    def test_the_sibling_walk_excludes_the_release_itself(self) -> None:
        from api.rarity.families.grooved import PRESSING_SQL

        assert "WHERE sibling.release_id <> r.release_id" in PRESSING_SQL


class TestPageSignals:
    @pytest.mark.asyncio
    async def test_every_registered_fact_is_answered_and_keyed_by_release_id(self) -> None:
        results: list[list[tuple[object, ...]]] = [[]]
        for fact in (*pg._CORE_SQL, *family_sql_queries()):
            width = len(pg._FACT_COLUMNS[fact])
            results.append([("731", *[None] * (width - 1)), ("101", *[None] * (width - 1))])
        results.append([])

        signals = await pg.fetch_page_signals(RarityHandles(graph=FakePool(results), insights=None), PAGE)

        assert set(signals) == set(SIGNAL_STATEMENTS)
        for fact, rows in signals.items():
            assert [row["release_id"] for row in rows] == ["101", "731"], f"{fact} rows are not ordered by release id"
            assert set(rows[0]) == set(pg._FACT_COLUMNS[fact])

    @pytest.mark.asyncio
    async def test_the_page_is_bound_once_per_statement(self) -> None:
        pool = FakePool(_signal_results())
        await pg.fetch_page_signals(RarityHandles(graph=pool, insights=pool), PAGE)

        signal_calls = [call for call in pool.calls if "statement_timeout" not in call.sql]
        assert len(signal_calls) == len(SIGNAL_STATEMENTS)
        assert all(call.params == {"ids": PAGE} for call in signal_calls)


class TestCollapsedLookups:
    """The two round trips per lookup that become one statement.

    The Cypher path asks Neo4j whether the vertex exists, asks it for the release ids, then
    asks PostgreSQL for a page and for a total: four trips, because the graph and the stored
    scores were in different databases. Here they are not.
    """

    @pytest.mark.parametrize("sql", [pg.RARITY_BY_ARTIST_SQL, pg.RARITY_BY_LABEL_SQL], ids=["artist", "label"])
    def test_the_lookup_joins_the_graph_to_the_stored_scores(self, sql: str) -> None:
        assert "GRAPH_TABLE (graph.catalog" in sql
        assert "FROM insights.release_rarity AS rarity" in sql

    @pytest.mark.parametrize("sql", [pg.RARITY_BY_ARTIST_SQL, pg.RARITY_BY_LABEL_SQL], ids=["artist", "label"])
    def test_an_empty_page_still_reports_whether_the_vertex_exists(self, sql: str) -> None:
        """Three answers have to stay apart: no such vertex, nothing scored, and a page. A
        plain SELECT with a LIMIT collapses the first two into zero rows."""
        assert "LEFT JOIN LATERAL (" in sql
        assert "EXISTS (SELECT 1 FROM anchor) AS vertex_exists" in sql

    @pytest.mark.parametrize("sql", [pg.RARITY_BY_ARTIST_SQL, pg.RARITY_BY_LABEL_SQL], ids=["artist", "label"])
    def test_a_non_numeric_graph_id_never_reaches_the_cast(self, sql: str) -> None:
        """`graph` keys releases on text and `insights.release_rarity` on bigint — the Cypher
        path's `isdigit()` filter, in SQL."""
        assert "WHERE release_id ~ '^[0-9]+$'" in sql

    def test_the_artist_lookup_walks_the_by_artist_edge_into_the_anchor(self) -> None:
        assert "MATCH (v IS artist WHERE v.artist_id = %(vertex_id)s)<-[IS by_artist]-(r IS release)" in pg.RARITY_BY_ARTIST_SQL

    def test_the_label_lookup_walks_the_on_label_edge_into_the_anchor(self) -> None:
        assert "MATCH (v IS label WHERE v.label_id = %(vertex_id)s)<-[IS on_label]-(r IS release)" in pg.RARITY_BY_LABEL_SQL

    @pytest.mark.asyncio
    async def test_a_missing_vertex_is_one_statement_and_no_rows(self) -> None:
        pool = FakePool([[(False, 0, None, None, None, None, None, None, None)]])
        assert await pg.get_rarity_by_artist(RarityHandles(graph=pool, insights=pool), "nope") is None
        assert pool.params == {"vertex_id": "nope", "limit": 20, "offset": 0}

    @pytest.mark.asyncio
    async def test_a_vertex_with_nothing_scored_is_an_empty_page_not_a_miss(self) -> None:
        pool = FakePool([[(True, 0, None, None, None, None, None, None, None)]])
        assert await pg.get_rarity_by_label(RarityHandles(graph=pool, insights=pool), "501") == ([], 0)

    @pytest.mark.asyncio
    async def test_a_page_carries_the_stored_columns_in_the_cypher_path_order(self) -> None:
        row = (True, 2, 731, "Release 731", "Sole Presser", 1972, 91.5, "ultra-rare", 44.0)
        pool = FakePool([[row]])

        items, total = await pg.get_rarity_by_artist(RarityHandles(graph=pool, insights=pool), "701", 2, 5)

        assert total == 2
        assert items == [
            {
                "release_id": 731,
                "title": "Release 731",
                "artist_name": "Sole Presser",
                "year": 1972,
                "rarity_score": 91.5,
                "tier": "ultra-rare",
                "hidden_gem_score": 44.0,
            }
        ]
        assert pool.params == {"vertex_id": "701", "limit": 5, "offset": 5}
