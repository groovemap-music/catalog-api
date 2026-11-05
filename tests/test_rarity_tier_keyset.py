"""The tier keyset page binds the score as REAL and orders by score DESC, release id ASC (gm-catalog-api-1xps).

A float8 cursor, or a descending release id, repeats or drops rows at score ties on real Postgres.
The walk itself is proven on real Postgres 18 with 66,666 tied rows; this pins the SQL shape.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from api.queries import rarity_queries


def test_the_score_cursor_is_bound_as_real_not_float8() -> None:
    sql = rarity_queries._TIER_KEYSET_PAGE
    assert "rarity_score <= %(after_score)s::real" in sql
    assert "rarity_score < %(after_score)s::real" in sql
    assert "double" not in sql


def test_the_order_matches_the_tier_index_and_the_keyset_predicate() -> None:
    sql = rarity_queries._TIER_KEYSET_PAGE
    assert "ORDER BY rarity_score DESC, release_id" in sql
    assert "release_id DESC" not in sql
    assert "release_id > %(after_id)s::bigint" in sql


@pytest.mark.asyncio
async def test_the_first_page_passes_no_cursor() -> None:
    cursor = AsyncMock()
    cursor.execute = AsyncMock()
    cursor.fetchall = AsyncMock(return_value=[{"release_id": 1}])
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=cursor)
    cm.__aexit__ = AsyncMock(return_value=False)
    conn = MagicMock()
    conn.cursor = MagicMock(return_value=cm)
    conn_cm = MagicMock()
    conn_cm.__aenter__ = AsyncMock(return_value=conn)
    conn_cm.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.connection = MagicMock(return_value=conn_cm)

    rows = await rarity_queries.get_rarity_tier_page(pool, "common", None, limit=50)

    assert rows == [{"release_id": 1}]
    params = cursor.execute.await_args.args[1]
    assert params == {"tier": "common", "after_score": None, "after_id": None, "limit": 50}


@pytest.mark.asyncio
async def test_a_later_page_binds_the_cursor_score_and_id() -> None:
    cursor = AsyncMock()
    cursor.execute = AsyncMock()
    cursor.fetchall = AsyncMock(return_value=[])
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=cursor)
    cm.__aexit__ = AsyncMock(return_value=False)
    conn = MagicMock()
    conn.cursor = MagicMock(return_value=cm)
    conn_cm = MagicMock()
    conn_cm.__aenter__ = AsyncMock(return_value=conn)
    conn_cm.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.connection = MagicMock(return_value=conn_cm)

    await rarity_queries.get_rarity_tier_page(pool, "rare", (0.3, 1639), limit=500)

    params = cursor.execute.await_args.args[1]
    assert params == {"tier": "rare", "after_score": 0.3, "after_id": 1639, "limit": 500}
