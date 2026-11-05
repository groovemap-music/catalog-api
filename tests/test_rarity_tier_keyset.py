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

    await rarity_queries.get_rarity_tier_page(pool, "rare", (0.3, 1639), limit=50)

    params = cursor.execute.await_args.args[1]
    assert params == {"tier": "rare", "after_score": 0.3, "after_id": 1639, "limit": 50}


@pytest.mark.asyncio
@pytest.mark.parametrize(("asked", "sent"), [(500, 101), (0, 1), (-5, 1), (101, 101), (20, 20)])
async def test_the_page_size_is_clamped_to_what_the_route_allows(asked: int, sent: int) -> None:
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

    await rarity_queries.get_rarity_tier_page(pool, "rare", None, limit=asked)

    assert cursor.execute.await_args.args[1]["limit"] == sent


def _forged(tier: object, score: object, release_id: object) -> str:
    import base64
    import json

    return base64.urlsafe_b64encode(json.dumps([tier, score, release_id]).encode()).decode().rstrip("=")


@pytest.mark.parametrize("score", [0.0, 0.3, 0.30000001192092896, 41.5, 100.0, 1e-07])
def test_a_cursor_round_trips_the_exact_score_and_id(score: float) -> None:
    token = rarity_queries.encode_tier_cursor("rare", score, 1639)
    assert rarity_queries.decode_tier_cursor(token, "rare") == (score, 1639)
    assert "=" not in token


def test_a_cursor_from_one_tier_is_refused_on_another() -> None:
    token = rarity_queries.encode_tier_cursor("rare", 41.5, 11)
    with pytest.raises(ValueError, match="invalid cursor"):
        rarity_queries.decode_tier_cursor(token, "common")


@pytest.mark.parametrize(
    "token",
    [
        "",
        "not-base64!!",
        "e30",  # {}
        "WzEsIDJd",  # [1, 2]
        _forged("rare", 0.3, 1),  # the score must be text
        _forged("rare", "nan", 1),
        _forged("rare", "inf", 1),
        _forged("rare", "x", 1),
        _forged("rare", "0.3", True),
        _forged("rare", "0.3", "1"),
        _forged("rare", "0.3", 1.5),
        _forged(7, "0.3", 1),
        _forged("rare", "0.3", -1),  # a negative id
        _forged("rare", "0.3", 10**30),  # past BIGINT: Postgres would raise, a 500
        _forged("rare", "0.3", 2**63),
        _forged("rare", "1e39", 1),  # overflows REAL
        _forged("rare", "-1e39", 1),
        _forged("rare", "1e-60", 1),  # underflows REAL
    ],
)
def test_anything_but_a_server_made_cursor_is_rejected(token: str) -> None:
    with pytest.raises(ValueError, match="invalid cursor"):
        rarity_queries.decode_tier_cursor(token, "rare")


def test_the_largest_values_postgres_accepts_still_decode() -> None:
    assert rarity_queries.decode_tier_cursor(_forged("rare", "3.4028234663852886e+38", 2**63 - 1), "rare")[1] == 2**63 - 1
