"""Tests for the catalog re-attachment census (api/reattach.py) — ADR 0014 section 8.

The SQL itself is proved against the real schema in `tests/test_reattach_integration.py`;
these tests pin what each step sends, in what order, and what it does with the rows back.
"""

from __future__ import annotations

import re
import sys
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from api.reattach import DEPENDENT_TABLES, IDENTIFIER_PROVIDERS, KINDS, census_sql, identifier_sql, run_census
from tests.fake_postgres import FakePool


_WRITE = re.compile(r"\b(INSERT|UPDATE|DELETE|TRUNCATE|MERGE|ALTER|CREATE|DROP)\b")


def _statements(pool: FakePool) -> list[str]:
    return [call.sql for call in pool.calls]


def _is_read(sql: str) -> bool:
    """A statement that can only read: a SELECT (not locking) or the transaction's own mode."""
    stripped = sql.strip()
    if stripped in ("BEGIN", "COMMIT", "ROLLBACK") or stripped.startswith("SET TRANSACTION"):
        return True
    return stripped.startswith("SELECT") and not _WRITE.search(stripped.replace("FOR UPDATE", ""))


class TestKindMapping:
    """The four kinds, their `musicbrainz.*` tables, Discogs columns, and alias entity kinds."""

    def test_columns_match_database_schema(self) -> None:
        assert {name: (kind.table, kind.discogs_column, kind.entity_kind) for name, kind in KINDS.items()} == {
            "release": ("releases", "discogs_release_id", "release"),
            "release_group": ("release_groups", "discogs_master_id", "master"),
            "artist": ("artists", "discogs_artist_id", "artist"),
            "label": ("labels", "discogs_label_id", "label"),
        }

    @pytest.mark.parametrize("name", list(KINDS))
    def test_every_statement_reads_only_current_aliases(self, name: str) -> None:
        kind = KINDS[name]
        for sql in (census_sql(kind), identifier_sql(kind)):
            assert _is_read(sql)
            assert f"FROM musicbrainz.{kind.table} AS mb" in sql
            assert f"mb.{kind.discogs_column}::text" in sql
            # Both alias joins go through the partial unique index.
            assert "discogs_alias.valid_to IS NULL" in sql
            assert "mb_alias.valid_to IS NULL" in sql
            assert f"entity_kind = '{kind.entity_kind}'" in sql

    def test_only_releases_are_contested_against_the_discogs_document(self) -> None:
        assert "identifiers' -> 'aliases'" in identifier_sql(KINDS["release"])
        for name in ("release_group", "artist", "label"):
            assert "FILTER (WHERE FALSE)" in identifier_sql(KINDS[name])


def _census_row(**overrides: int) -> tuple[int, ...]:
    columns = [
        "linked",
        "unresolved",
        "split",
        "stale_gm_item_id",
        *DEPENDENT_TABLES,
        "dependents",
        "shared_native_id",
        "non_catalog_alias",
        "guarded",
    ]
    return tuple(overrides.get(column, 0) for column in columns)


class TestRunCensus:
    """run_census: read-only, bounded, and the report shape."""

    @pytest.mark.asyncio
    async def test_issues_only_selects_in_one_read_only_transaction(self) -> None:
        pool = FakePool()

        await run_census(pool)

        statements = _statements(pool)
        assert statements[0] == "BEGIN"
        assert statements[1] == "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
        assert statements[-1] == "COMMIT"
        assert all(_is_read(sql) for sql in statements)
        assert not any("FOR UPDATE" in sql for sql in statements)
        # Two set-based statements per kind, whatever the data: no per-row round trips.
        assert len(statements) == 3 + 2 * len(KINDS)

    @pytest.mark.asyncio
    async def test_report_shape_and_eligible_count(self) -> None:
        release_counts = _census_row(
            linked=10,
            unresolved=3,
            split=4,
            stale_gm_item_id=1,
            artifacts=1,
            user_collections=1,
            dependents=2,
            shared_native_id=1,
            guarded=3,
        )
        pool = FakePool(
            [
                [],  # SET TRANSACTION
                [release_counts],
                [("barcode", 2, 1)],
            ]
        )

        census = await run_census(pool)

        assert list(census) == list(KINDS)
        assert census["release"] == {
            "linked": 10,
            "unresolved": 3,
            "split": 4,
            "stale_gm_item_id": 1,
            "eligible": 2,
            "guarded": 3,
            "guard_reasons": {"dependents": 2, "shared_native_id": 1, "non_catalog_alias": 0},
            "dependents": {"artifacts": 1, "owned_copies": 0, "observations": 0, "user_collections": 1, "user_wantlists": 0},
            "identifier_aliases": {
                "barcode": {"held": 2, "contested": 1},
                "catalog_number": {"held": 0, "contested": 0},
                "isrc": {"held": 0, "contested": 0},
                "matrix": {"held": 0, "contested": 0},
            },
        }
        # A kind with no rows reports zeros rather than failing.
        assert census["label"]["split"] == 0
        assert census["label"]["eligible"] == 0

    @pytest.mark.asyncio
    @pytest.mark.parametrize("table", DEPENDENT_TABLES)
    async def test_each_dependent_table_is_reported_under_its_own_name(self, table: str) -> None:
        pool = FakePool([[], [_census_row(split=1, dependents=1, guarded=1, **{table: 1})]])

        census = await run_census(pool)

        assert census["release"]["dependents"] == {name: int(name == table) for name in DEPENDENT_TABLES}
        assert census["release"]["guard_reasons"]["dependents"] == 1
        assert census["release"]["eligible"] == 0


def test_identifier_providers_are_the_alias_namespaces() -> None:
    assert IDENTIFIER_PROVIDERS == ("barcode", "catalog_number", "isrc", "matrix")


class TestCli:
    """The catalog-identity-reattach entry point: a read-only census."""

    @pytest.fixture
    def postgres_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("POSTGRES_HOST", "db:5433")
        monkeypatch.setenv("POSTGRES_USERNAME", "user")
        monkeypatch.setenv("POSTGRES_PASSWORD", "pass")
        monkeypatch.setenv("POSTGRES_DATABASE", "mydb")

    @pytest.mark.usefixtures("postgres_env")
    def test_connection_params_from_env(self) -> None:
        from api.reattach import _connection_params

        assert _connection_params() == {"host": "db", "port": 5433, "dbname": "mydb", "user": "user", "password": "pass"}

    def test_missing_env_exits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from api.reattach import _connection_params

        for var in ("POSTGRES_HOST", "POSTGRES_USERNAME", "POSTGRES_PASSWORD", "POSTGRES_DATABASE"):
            monkeypatch.delenv(var, raising=False)

        with pytest.raises(SystemExit) as exc_info:
            _connection_params()
        assert exc_info.value.code == 1

    @pytest.mark.usefixtures("postgres_env")
    def test_main_prints_the_census(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        import api.reattach as reattach_module

        census = {
            "release": {
                "split": 2,
                "stale_gm_item_id": 0,
                "eligible": 1,
                "guarded": 1,
                "guard_reasons": {},
                "dependents": {},
                "unresolved": 5,
                "identifier_aliases": {},
            }
        }

        def fake_run(coro: Any) -> dict[str, Any]:
            coro.close()
            return census

        monkeypatch.setattr(reattach_module.asyncio, "run", fake_run)
        monkeypatch.setattr(sys, "argv", ["catalog-identity-reattach"])

        reattach_module.main()

        out = capsys.readouterr().out
        assert "release: 2 split, 0 stale, 1 eligible, 1 guarded" in out
        assert "nothing was written" in out

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("postgres_env")
    async def test_run_once_builds_and_closes_the_pool(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import api.reattach as reattach_module

        pool = AsyncMock()
        monkeypatch.setattr("common.AsyncPostgreSQLPool", MagicMock(return_value=pool))
        monkeypatch.setattr(reattach_module, "run_census", AsyncMock(return_value={"release": {}}))

        assert await reattach_module._run_once() == {"release": {}}
        pool.initialize.assert_awaited_once()
        pool.close.assert_awaited_once()
