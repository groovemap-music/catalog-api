"""Query-shape coverage for the SQL artist-identity lookup (PostgreSQL backend).

Row-level agreement with the Cypher is `tests/test_real_databases.py`'s
`collaborator_identity` family, which runs both engines. This module checks what the
statement is made of and how the module maps rows, without a server.
"""

from __future__ import annotations

import pytest

from api.queries import collaborator_pg_queries as pg
from tests.fake_postgres import FakePool


pytestmark = pytest.mark.asyncio


class TestStatementShape:
    async def test_selects_from_the_phase_0_artist_view(self) -> None:
        assert "FROM graph.artist" in pg.ARTIST_IDENTITY_SQL

    async def test_is_a_plain_select_not_a_graph_table_pattern(self) -> None:
        # Coverage spike rule: a single-vertex lookup is SQL-only, not GRAPH_TABLE — a
        # one-element pattern buys nothing over a plain select.
        assert "GRAPH_TABLE" not in pg.ARTIST_IDENTITY_SQL

    async def test_the_artist_id_is_bound_not_interpolated(self) -> None:
        assert "artist_id = %(artist_id)s" in pg.ARTIST_IDENTITY_SQL

    async def test_no_statement_carries_a_quoted_literal(self) -> None:
        assert "'" not in pg.ARTIST_IDENTITY_SQL


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
        assert await pg.get_artist_identity(FakePool([[]]), "missing") is None
