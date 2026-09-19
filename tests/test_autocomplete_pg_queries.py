"""Query-shape coverage for the trigram autocomplete backend.

These run without a server, so they assert what the module *sends*: that every value is
bound rather than interpolated, that each statement reads one `graph` vertex relation and
no property graph, that the ordering rule is the documented one, and — the part this family
exists for — that a query string carrying Lucene syntax comes out as a parameter rather
than as syntax.

Row-level agreement with the Cypher is the parity harness in `tests/test_real_databases.py`,
which runs both engines. What the fake pool can show is the half that has no engine in it:
the patterns the module builds from a caller's string.
"""

from __future__ import annotations

from typing import Any

import pytest

from api.queries import autocomplete_pg_queries as pg
from tests.fake_postgres import FakePool


pytestmark = pytest.mark.asyncio

ALL_STATEMENTS = (
    pg.ARTIST_AUTOCOMPLETE_SQL,
    pg.LABEL_AUTOCOMPLETE_SQL,
    pg.GENRE_AUTOCOMPLETE_SQL,
    pg.STYLE_AUTOCOMPLETE_SQL,
    pg.PERSON_AUTOCOMPLETE_SQL,
)

# Every public search, with the relation it reads and the columns the Cypher returns.
FUNCTIONS: tuple[tuple[Any, str, tuple[str, ...]], ...] = (
    (pg.autocomplete_artist, "graph.artist", ("id", "name", "score")),
    (pg.autocomplete_label, "graph.label", ("id", "name", "score")),
    (pg.autocomplete_genre, "graph.genre", ("id", "name", "score")),
    (pg.autocomplete_style, "graph.style", ("id", "name", "score")),
    (pg.autocomplete_person, "graph.person", ("name", "score")),
)


class TestStatementShape:
    """What the five module-level statements are made of."""

    @pytest.mark.parametrize("sql", ALL_STATEMENTS)
    async def test_no_statement_carries_a_quoted_literal(self, sql: str) -> None:
        # A single quote anywhere would mean a value — a pattern, a term — got baked in.
        assert "'" not in sql

    @pytest.mark.parametrize("sql", ALL_STATEMENTS)
    async def test_the_query_reaches_the_server_only_as_a_bound_parameter(self, sql: str) -> None:
        assert "%(query)s" in sql
        assert "%(contains)s" in sql
        assert "%(prefixes)s::text[]" in sql
        assert "%(limit)s" in sql

    @pytest.mark.parametrize("sql", ALL_STATEMENTS)
    async def test_nothing_here_touches_the_property_graph(self, sql: str) -> None:
        # The coverage spike classifies all six of these as SQL-only: they do not
        # traverse, so they neither need `graph.catalog` nor may depend on it — it is
        # declared only on PostgreSQL 19 with the switch on.
        assert "GRAPH_TABLE" not in sql
        assert "graph.catalog" not in sql

    @pytest.mark.parametrize("sql", ALL_STATEMENTS)
    async def test_the_indexable_prefilter_sits_beside_the_exact_rule(self, sql: str) -> None:
        # The ILIKE is a superset of the word-prefix rule and is what the GIN trigram
        # index can drive; `~* ALL (...)` over an array is the rule and is not indexable.
        # Dropping either one is a bug: the first alone over-matches, the second alone
        # scans.
        assert "candidate.name ILIKE %(contains)s" in sql
        assert "candidate.name ~* ALL (%(prefixes)s::text[])" in sql

    @pytest.mark.parametrize("sql", ALL_STATEMENTS)
    async def test_the_documented_ordering_rule_is_the_one_in_the_statement(self, sql: str) -> None:
        assert "similarity(candidate.name, %(query)s)::float8 AS score" in sql
        assert "ORDER BY score DESC, candidate.name ASC" in sql

    @pytest.mark.parametrize(("function", "relation", "_columns"), FUNCTIONS)
    async def test_each_search_reads_its_own_relation(self, function: Any, relation: str, _columns: tuple[str, ...]) -> None:
        pool = FakePool([[]])
        await function(pool, "roc")
        assert f"FROM {relation} AS candidate" in pool.sql


class TestMatchParameters:
    """The patterns built from a caller's string — the half with no engine in it."""

    async def test_one_term_becomes_one_word_prefix_and_one_substring_prefilter(self) -> None:
        assert pg.build_match_parameters("roc", 10) == {
            "query": "roc",
            "contains": "%roc%",
            "prefixes": [r"\mroc"],
            "limit": 10,
        }

    async def test_every_term_must_begin_a_word_which_is_what_lucene_anded(self) -> None:
        # `post roc` was `post* AND roc*`; one array element per term, ANDed by `ALL`.
        parameters = pg.build_match_parameters("post roc", 10)
        assert parameters is not None
        assert parameters["prefixes"] == [r"\mpost", r"\mroc"]

    async def test_the_prefilter_is_built_from_the_longest_term(self) -> None:
        # It only has to be a superset, so any term would be correct; the longest carries
        # the most trigrams and so is the most selective one to hand the index.
        parameters = pg.build_match_parameters("a ambient", 10)
        assert parameters is not None
        assert parameters["contains"] == "%ambient%"

    async def test_a_query_with_no_terms_has_no_statement(self) -> None:
        # `ILIKE '%%'` would return the whole relation ranked by a similarity of zero.
        assert pg.build_match_parameters("", 10) is None
        assert pg.build_match_parameters("   ", 10) is None

    @pytest.mark.parametrize(
        ("query", "prefix", "contains"),
        [
            # The name the Lucene path returned a 500 on. Nothing is escaped, because
            # nothing needs to be: a slash is not syntax to a bound parameter.
            ("AC/DC", r"\mAC/DC", "%AC/DC%"),
            # The other Lucene metacharacters from `_escape_lucene_query`, likewise inert.
            ('Chuck"', r'\mChuck"', '%Chuck"%'),
            ("O'Connor", r"\mO'Connor", "%O'Connor%"),
            ("Emerson,", r"\mEmerson,", "%Emerson,%"),
            ("rock:", r"\mrock:", "%rock:%"),
            # Regular-expression metacharacters are escaped, because the prefix pattern
            # *is* a regular expression. An unescaped `+` here would be a syntax error the
            # caller saw as a 500 — the same failure, one layer over.
            ("C++", r"\mC\+\+", "%C++%"),
            ("(Palmer", r"\m\(Palmer", "%(Palmer%"),
            # `LIKE` metacharacters are escaped in the prefilter, because the prefilter
            # *is* a pattern. Unescaped, `%` would widen it and `_` would match anything.
            ("50%", r"\m50%", r"%50\%%"),
            ("a_b", r"\ma_b", r"%a\_b%"),
        ],
    )
    async def test_characters_lucene_read_as_syntax_are_only_ever_data(self, query: str, prefix: str, contains: str) -> None:
        parameters = pg.build_match_parameters(query, 10)
        assert parameters is not None
        assert parameters["prefixes"] == [prefix]
        assert parameters["contains"] == contains
        # The similarity operand is the caller's string, untouched: it is compared, not
        # parsed.
        assert parameters["query"] == query


class TestResults:
    """What the module does with the rows it gets back."""

    @pytest.mark.parametrize(("function", "_relation", "columns"), FUNCTIONS)
    async def test_rows_are_projected_into_the_cyphers_columns_in_its_order(self, function: Any, _relation: str, columns: tuple[str, ...]) -> None:
        row = tuple(range(len(columns)))
        pool = FakePool([[row]])

        results = await function(pool, "roc", limit=5)

        assert results == [dict(zip(columns, row, strict=True))]
        # Column order is part of the response, so it is part of the assertion.
        assert list(results[0]) == list(columns)
        assert pool.params["limit"] == 5

    @pytest.mark.parametrize(("function", "_relation", "_columns"), FUNCTIONS)
    async def test_a_query_with_no_terms_never_reaches_the_server(self, function: Any, _relation: str, _columns: tuple[str, ...]) -> None:
        pool = FakePool([[("x", "y", 1.0)]])

        assert await function(pool, "  ") == []
        assert pool.calls == []
