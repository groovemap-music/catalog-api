"""Query-shape regressions for the PostgreSQL Explore family."""

from __future__ import annotations

from typing import Any

import pytest

from api.graph_backend import get_backend, get_explore_backend, get_genre_tree_backend
from api.queries import explore_pg_queries as pg
from api.queries.genre_tree_pg_queries import GENRE_TREE_SQL, get_genre_tree
from tests.fake_postgres import FakePool


pytestmark = pytest.mark.asyncio


async def test_alias_expansion_follows_outgoing_alias_to_primary_edge() -> None:
    assert "SELECT artist_id FROM graph.alias_of WHERE alias_artist_id = %(artist_id)s" in pg._ALIASES
    assert "SELECT alias_artist_id AS artist_id" not in pg._ALIASES


async def test_counter_centers_read_vertex_properties(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    async def one(_pool: Any, sql: str, _params: dict[str, Any]) -> dict[str, Any]:
        seen.append(sql)
        return {"id": "x"}

    monkeypatch.setattr(pg, "_one", one)
    for function in (pg.explore_genre, pg.explore_style, pg.explore_label):
        await function(None, "x")
    assert "FROM graph.genre" in seen[0]
    assert "FROM graph.style" in seen[1]
    assert "FROM graph.label" in seen[2]
    assert all("release_count" in sql and "JOIN graph.release" not in sql for sql in seen)


async def test_every_expand_and_count_uses_fixed_edge_joins(monkeypatch: pytest.MonkeyPatch) -> None:
    statements: list[tuple[str, dict[str, Any]]] = []

    async def rows(_pool: Any, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        statements.append((sql, params))
        return []

    async def number(_pool: Any, sql: str, params: dict[str, Any]) -> int:
        statements.append((sql, params))
        return 0

    monkeypatch.setattr(pg, "_rows", rows)
    monkeypatch.setattr(pg, "_number", number)
    for center, children in pg._EXPANSIONS.items():
        for child in children:
            await getattr(pg, f"expand_{center}_{child}")(None, "fixture", before_year=2000)
            await getattr(pg, f"count_{center}_{child}")(None, "fixture", before_year=2000)
    assert len(statements) == 26
    assert all("JOIN graph.release r" in sql for sql, _ in statements)
    assert all("%(before_year)s" in sql and params["before_year"] == 2000 for sql, params in statements)


async def test_genre_tree_counts_distinct_release_and_omits_empty_styles() -> None:
    assert "count(DISTINCT e.release_id)" in GENRE_TREE_SQL
    assert "count(DISTINCT g.release_id)" in GENRE_TREE_SQL
    assert "FILTER (WHERE styles.style_name IS NOT NULL)" in GENRE_TREE_SQL


async def test_genre_tree_maps_nested_rows() -> None:
    pool = FakePool([[("Rock", 2, [{"name": "House", "release_count": 1}])]])
    assert await get_genre_tree(pool) == [{"name": "Rock", "release_count": 2, "styles": [{"name": "House", "release_count": 1}]}]
    assert pool.sql == GENRE_TREE_SQL


async def test_both_families_are_registered() -> None:
    assert get_backend("explore", "postgres") is pg
    assert get_backend("genre_tree", "postgres").__name__.endswith("genre_tree_pg_queries")
    assert get_explore_backend("postgres") is pg
    assert get_genre_tree_backend("postgres") is get_backend("genre_tree", "postgres")


async def test_centers_details_trends_and_emergence_route_to_parameterized_sql(monkeypatch: pytest.MonkeyPatch) -> None:
    statements: list[tuple[str, dict[str, Any]]] = []

    async def one(_pool: Any, sql: str, params: dict[str, Any]) -> dict[str, Any]:
        statements.append((sql, params))
        return {"artist_id": "1", "id": "1"}

    async def rows(_pool: Any, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        statements.append((sql, params))
        return []

    async def number(_pool: Any, sql: str, params: dict[str, Any]) -> int:
        statements.append((sql, params))
        return 0

    monkeypatch.setattr(pg, "_one", one)
    monkeypatch.setattr(pg, "_rows", rows)
    monkeypatch.setattr(pg, "_number", number)
    for center in ("artist", "genre", "label", "style"):
        await getattr(pg, f"explore_{center}")(None, "fixture")
        await getattr(pg, f"get_{center}_details")(None, "1")
        await getattr(pg, f"trends_{center}")(None, "fixture")
    await pg.get_release_details(None, "1")
    await pg.get_genre_emergence(None, 2000)
    await pg.expand_artist_aliases(None, "fixture")
    await pg.count_artist_aliases(None, "fixture")
    assert len(statements) == 20
    assert all("%s" not in sql for sql, _ in statements)
    assert statements[-3][1]["artist_id"] == "1"
    assert statements[-1][1]["artist_id"] == "1"
