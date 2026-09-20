"""Query-shape coverage for the three gap-metadata lookups (PostgreSQL backend).

Row-level agreement with the Cypher is `tests/test_real_databases.py`'s `gap_metadata`
family, which runs both engines. This module checks what each statement is made of and how
the module maps rows, without a server.
"""

from __future__ import annotations

import pytest

from api.queries import gap_pg_queries as pg
from tests.fake_postgres import FakePool


pytestmark = pytest.mark.asyncio

ALL_STATEMENTS = (pg.LABEL_METADATA_SQL, pg.ARTIST_METADATA_SQL, pg.MASTER_METADATA_SQL)


class TestStatementShape:
    @pytest.mark.parametrize("sql", ALL_STATEMENTS)
    async def test_is_a_plain_select_not_a_graph_table_pattern(self, sql: str) -> None:
        assert "GRAPH_TABLE" not in sql

    @pytest.mark.parametrize("sql", ALL_STATEMENTS)
    async def test_no_statement_carries_a_quoted_literal(self, sql: str) -> None:
        assert "'" not in sql

    async def test_label_metadata_selects_from_the_phase_0_label_view(self) -> None:
        assert "FROM graph.label" in pg.LABEL_METADATA_SQL
        assert "label_id = %(label_id)s" in pg.LABEL_METADATA_SQL

    async def test_artist_metadata_selects_from_the_phase_0_artist_view(self) -> None:
        assert "FROM graph.artist" in pg.ARTIST_METADATA_SQL
        assert "artist_id = %(artist_id)s" in pg.ARTIST_METADATA_SQL

    async def test_master_metadata_selects_the_title_column_from_the_phase_0_master_view(self) -> None:
        assert "FROM graph.master" in pg.MASTER_METADATA_SQL
        assert "SELECT master_id, title" in pg.MASTER_METADATA_SQL
        assert "master_id = %(master_id)s" in pg.MASTER_METADATA_SQL


class TestGetLabelMetadata:
    async def test_returns_id_and_name(self) -> None:
        pool = FakePool([[("301", "Blue Note")]])
        assert await pg.get_label_metadata(pool, "301") == {"id": "301", "name": "Blue Note"}

    async def test_binds_the_label_id(self) -> None:
        pool = FakePool([[("301", "Blue Note")]])
        await pg.get_label_metadata(pool, "301")
        assert pool.sql == pg.LABEL_METADATA_SQL
        assert pool.params == {"label_id": "301"}

    async def test_returns_none_for_an_unknown_label(self) -> None:
        assert await pg.get_label_metadata(FakePool([[]]), "missing") is None


class TestGetArtistMetadata:
    async def test_returns_id_and_name(self) -> None:
        pool = FakePool([[("1", "Anchor")]])
        assert await pg.get_artist_metadata(pool, "1") == {"id": "1", "name": "Anchor"}

    async def test_binds_the_artist_id(self) -> None:
        pool = FakePool([[("1", "Anchor")]])
        await pg.get_artist_metadata(pool, "1")
        assert pool.sql == pg.ARTIST_METADATA_SQL
        assert pool.params == {"artist_id": "1"}

    async def test_returns_none_for_an_unknown_artist(self) -> None:
        assert await pg.get_artist_metadata(FakePool([[]]), "missing") is None


class TestGetMasterMetadata:
    async def test_returns_id_and_title_as_name(self) -> None:
        pool = FakePool([[("401", "Fixture Master")]])
        assert await pg.get_master_metadata(pool, "401") == {"id": "401", "name": "Fixture Master"}

    async def test_binds_the_master_id(self) -> None:
        pool = FakePool([[("401", "Fixture Master")]])
        await pg.get_master_metadata(pool, "401")
        assert pool.sql == pg.MASTER_METADATA_SQL
        assert pool.params == {"master_id": "401"}

    async def test_returns_none_for_an_unknown_master(self) -> None:
        assert await pg.get_master_metadata(FakePool([[]]), "missing") is None
