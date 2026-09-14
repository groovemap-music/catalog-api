"""Tests for the gm_id projection job (api/projection.py) — ADR 0009 "Graph projection"."""

from __future__ import annotations

import sys
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest


NATIVE_ID_1 = UUID("00000000-0000-7000-8000-000000000001")
NATIVE_ID_2 = UUID("00000000-0000-7000-8000-000000000002")
NATIVE_ID_3 = UUID("00000000-0000-7000-8000-000000000003")


class TestLabelMapping:
    """The Neo4j label is always read from the fixed mapping, never from input."""

    def test_every_catalog_kind_has_a_label(self) -> None:
        from api.projection import _LABEL_BY_KIND

        assert _LABEL_BY_KIND == {
            "artist": "Artist",
            "label": "Label",
            "master": "Master",
            "release": "Release",
        }

    @pytest.mark.parametrize(
        ("label", "expected"),
        [
            ("Artist", "UNWIND $rows AS row MATCH (n:Artist {id: row.id}) SET n.gm_id = row.gm_id"),
            ("Label", "UNWIND $rows AS row MATCH (n:Label {id: row.id}) SET n.gm_id = row.gm_id"),
            ("Master", "UNWIND $rows AS row MATCH (n:Master {id: row.id}) SET n.gm_id = row.gm_id"),
            ("Release", "UNWIND $rows AS row MATCH (n:Release {id: row.id}) SET n.gm_id = row.gm_id"),
        ],
    )
    def test_exact_cypher_per_label(self, label: str, expected: str) -> None:
        from api.projection import _set_gm_id_cypher

        assert _set_gm_id_cypher(label) == expected

    def test_cypher_touches_no_other_property_or_relationship(self) -> None:
        """The statement is a bare MATCH + SET gm_id — no CREATE, MERGE, DELETE, or `-[`."""
        from api.projection import _LABEL_BY_KIND, _set_gm_id_cypher

        for label in _LABEL_BY_KIND.values():
            cypher = _set_gm_id_cypher(label)
            assert cypher.count("SET") == 1
            assert "gm_id" in cypher
            for forbidden in ("CREATE", "MERGE", "DELETE", "REMOVE", "-[", "]-"):
                assert forbidden not in cypher


class TestRunGmIdProjection:
    """Tests for run_gm_id_projection's paging and per-page Cypher writes."""

    @pytest.mark.asyncio
    async def test_pages_two_pages_for_one_kind_and_nothing_for_the_rest(
        self,
        mock_pool: MagicMock,
        mock_cur: AsyncMock,
        mock_neo4j: MagicMock,
        mock_neo4j_session: MagicMock,
    ) -> None:
        from api.projection import run_gm_id_projection

        release_page_1 = [
            {"external_id": "100", "native_id": NATIVE_ID_1},
            {"external_id": "101", "native_id": NATIVE_ID_2},
        ]
        release_page_2 = [
            {"external_id": "102", "native_id": NATIVE_ID_3},
        ]

        calls: list[tuple[str, str, int]] = []

        async def execute_side_effect(_query: str, params: tuple[str, str, int]) -> None:
            calls.append(params)

        async def fetchall_side_effect() -> list[dict[str, Any]]:
            kind, cursor, _batch_size = calls[-1]
            if kind != "release":
                return []
            if cursor == "":
                return release_page_1
            if cursor == "101":
                return release_page_2
            return []

        mock_cur.execute = AsyncMock(side_effect=execute_side_effect)
        mock_cur.fetchall = AsyncMock(side_effect=fetchall_side_effect)

        counts = await run_gm_id_projection(mock_pool, mock_neo4j, batch_size=2)

        assert counts == {"Artist": 0, "Label": 0, "Master": 0, "Release": 3}

        # One SELECT per page attempted, plus one empty terminating page per kind:
        # artist, label, master each make exactly one (empty) call; release makes three
        # (two non-empty pages, then the empty page that ends the loop).
        assert calls.count(("artist", "", 2)) == 1
        assert calls.count(("label", "", 2)) == 1
        assert calls.count(("master", "", 2)) == 1
        assert ("release", "", 2) in calls
        assert ("release", "101", 2) in calls
        assert ("release", "102", 2) in calls

        # Exactly two Cypher writes — one per non-empty release page. Nothing is written
        # for artist/label/master, since they had no rows at all.
        assert mock_neo4j_session.run.call_count == 2

        first_cypher, first_params = mock_neo4j_session.run.call_args_list[0].args
        assert first_cypher == "UNWIND $rows AS row MATCH (n:Release {id: row.id}) SET n.gm_id = row.gm_id"
        assert first_params == {
            "rows": [
                {"id": "100", "gm_id": str(NATIVE_ID_1)},
                {"id": "101", "gm_id": str(NATIVE_ID_2)},
            ]
        }

        second_cypher, second_params = mock_neo4j_session.run.call_args_list[1].args
        assert second_cypher == "UNWIND $rows AS row MATCH (n:Release {id: row.id}) SET n.gm_id = row.gm_id"
        assert second_params == {"rows": [{"id": "102", "gm_id": str(NATIVE_ID_3)}]}

    @pytest.mark.asyncio
    async def test_no_rows_for_any_kind_writes_nothing(
        self,
        mock_pool: MagicMock,
        mock_cur: AsyncMock,
        mock_neo4j: MagicMock,
        mock_neo4j_session: MagicMock,
    ) -> None:
        from api.projection import run_gm_id_projection

        mock_cur.fetchall = AsyncMock(return_value=[])

        counts = await run_gm_id_projection(mock_pool, mock_neo4j)

        assert counts == {"Artist": 0, "Label": 0, "Master": 0, "Release": 0}
        mock_neo4j_session.run.assert_not_called()

    @pytest.mark.asyncio
    async def test_default_batch_size(self, mock_pool: MagicMock, mock_cur: AsyncMock, mock_neo4j: MagicMock) -> None:
        from api.projection import DEFAULT_BATCH_SIZE, run_gm_id_projection

        mock_cur.fetchall = AsyncMock(return_value=[])

        await run_gm_id_projection(mock_pool, mock_neo4j)

        for call in mock_cur.execute.call_args_list:
            _query, params = call.args
            assert params[2] == DEFAULT_BATCH_SIZE


class TestRunOnce:
    """Tests for _run_once, the CLI's real pool+driver wiring around run_gm_id_projection."""

    @pytest.mark.asyncio
    async def test_builds_pool_and_driver_runs_and_closes_both(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import api.projection as projection_module

        mock_pool = AsyncMock()
        mock_pool_cls = MagicMock(return_value=mock_pool)

        mock_driver = AsyncMock()
        mock_driver_cls = MagicMock(return_value=mock_driver)

        monkeypatch.setattr("common.AsyncPostgreSQLPool", mock_pool_cls)
        monkeypatch.setattr("common.AsyncResilientNeo4jDriver", mock_driver_cls)
        monkeypatch.setattr("common.neo4j_security_kwargs", lambda: {})

        seen: dict[str, Any] = {}

        async def fake_projection(pool: Any, driver: Any, *, batch_size: int) -> dict[str, int]:
            seen["pool"] = pool
            seen["driver"] = driver
            seen["batch_size"] = batch_size
            return {"Artist": 1, "Label": 0, "Master": 0, "Release": 0}

        monkeypatch.setattr(projection_module, "run_gm_id_projection", fake_projection)

        result = await projection_module._run_once(500)

        assert result == {"Artist": 1, "Label": 0, "Master": 0, "Release": 0}
        assert seen == {"pool": mock_pool, "driver": mock_driver, "batch_size": 500}
        mock_pool.initialize.assert_awaited_once()
        mock_pool.close.assert_awaited_once()
        mock_driver.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_closes_pool_and_driver_even_when_the_run_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import api.projection as projection_module

        mock_pool = AsyncMock()
        mock_pool_cls = MagicMock(return_value=mock_pool)

        mock_driver = AsyncMock()
        mock_driver_cls = MagicMock(return_value=mock_driver)

        monkeypatch.setattr("common.AsyncPostgreSQLPool", mock_pool_cls)
        monkeypatch.setattr("common.AsyncResilientNeo4jDriver", mock_driver_cls)
        monkeypatch.setattr("common.neo4j_security_kwargs", lambda: {})

        async def failing_projection(_pool: Any, _driver: Any, *, batch_size: int) -> dict[str, int]:  # noqa: ARG001
            raise RuntimeError("neo4j unreachable")

        monkeypatch.setattr(projection_module, "run_gm_id_projection", failing_projection)

        with pytest.raises(RuntimeError, match="neo4j unreachable"):
            await projection_module._run_once(500)

        mock_pool.close.assert_awaited_once()
        mock_driver.close.assert_awaited_once()


class TestBuildConninfoAndNeo4jKwargs:
    """Tests for the CLI's connection-info builders (same contract as media_backfill's)."""

    def test_builds_conninfo_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from api.projection import _build_conninfo

        monkeypatch.setenv("POSTGRES_HOST", "db")
        monkeypatch.setenv("POSTGRES_USERNAME", "user")
        monkeypatch.setenv("POSTGRES_PASSWORD", "pass")
        monkeypatch.setenv("POSTGRES_DATABASE", "mydb")

        conninfo = _build_conninfo()
        assert "host=db" in conninfo
        assert "user=user" in conninfo
        assert "password=pass" in conninfo
        assert "dbname=mydb" in conninfo

    def test_missing_postgres_env_vars_exits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from api.projection import _build_conninfo

        for var in ("POSTGRES_HOST", "POSTGRES_USERNAME", "POSTGRES_PASSWORD", "POSTGRES_DATABASE"):
            monkeypatch.delenv(var, raising=False)

        with pytest.raises(SystemExit) as exc_info:
            _build_conninfo()
        assert exc_info.value.code == 1

    def test_builds_neo4j_kwargs_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from api.projection import _build_neo4j_kwargs

        monkeypatch.setenv("NEO4J_HOST", "bolt://neo4j:7687")
        monkeypatch.setenv("NEO4J_USERNAME", "neo4j")
        monkeypatch.setenv("NEO4J_PASSWORD", "secret")

        kwargs = _build_neo4j_kwargs()
        assert kwargs == {"uri": "bolt://neo4j:7687", "auth": ("neo4j", "secret")}

    def test_missing_neo4j_env_vars_exits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from api.projection import _build_neo4j_kwargs

        for var in ("NEO4J_HOST", "NEO4J_USERNAME", "NEO4J_PASSWORD"):
            monkeypatch.delenv(var, raising=False)

        with pytest.raises(SystemExit) as exc_info:
            _build_neo4j_kwargs()
        assert exc_info.value.code == 1


class TestCliMain:
    """Tests for the catalog-identity-projection CLI entry point."""

    def test_main_runs_once_and_prints_counts(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        import api.projection as projection_module

        counts = {"Artist": 1, "Label": 2, "Master": 0, "Release": 5}

        def fake_run(coro: Any) -> dict[str, int]:
            coro.close()  # never actually driven — avoids a real pool/driver
            return counts

        monkeypatch.setattr(projection_module.asyncio, "run", fake_run)
        monkeypatch.setattr(sys, "argv", ["catalog-identity-projection"])

        projection_module.main()

        out = capsys.readouterr().out
        assert "Artist: 1 node(s) projected" in out
        assert "Label: 2 node(s) projected" in out
        assert "Master: 0 node(s) projected" in out
        assert "Release: 5 node(s) projected" in out
        assert "Total nodes projected: 8" in out

    def test_main_rejects_batch_size_below_one(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import api.projection as projection_module

        monkeypatch.setattr(sys, "argv", ["catalog-identity-projection", "--batch-size", "0"])

        with pytest.raises(SystemExit) as exc_info:
            projection_module.main()
        assert exc_info.value.code == 2

    def test_main_exits_when_env_incomplete(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import api.projection as projection_module

        monkeypatch.delenv("NEO4J_HOST", raising=False)
        monkeypatch.setattr(sys, "argv", ["catalog-identity-projection"])

        with pytest.raises(SystemExit) as exc_info:
            projection_module.main()
        assert exc_info.value.code == 1
