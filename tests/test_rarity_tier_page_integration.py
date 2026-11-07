"""get_rarity_tier_page on real PostgreSQL: a walk with score ties and text round-tripped cursors (gm-catalog-api-1xps).

Runs on the integration tier. The rows are a small tied dataset, so the walk takes milliseconds:
every row must come back exactly once, both when the cursor is passed at full precision and when
it is rounded through text, as a JSON or URL cursor would be.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from common import AsyncPostgreSQLPool, parse_postgres_host_port
from groovemap_schema.postgres import create_postgres_schema

from api.queries.rarity_queries import decode_tier_cursor, encode_tier_cursor, get_rarity_tier_page


pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

TIER = "rarity-walk-test"
ROWS_PER_SCORE = 67
ID_BASE = 7_000_000  # clear of the ids other integration files insert into the same table
SCORES = (0.3, 0.7, 1.1, 0.123456789, 2.5)  # 0.3 and 0.123456789 are not exact in binary


def _env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.skip(f"{name} is not set; run this through just test-integration")
    return value


@pytest_asyncio.fixture
async def pool() -> AsyncIterator[AsyncPostgreSQLPool]:
    host, port = parse_postgres_host_port(_env("POSTGRES_HOST"))
    pool = AsyncPostgreSQLPool(
        connection_params={
            "host": host,
            "port": port,
            "dbname": _env("POSTGRES_DATABASE"),
            "user": _env("POSTGRES_USERNAME"),
            "password": _env("POSTGRES_PASSWORD"),
        },
        min_connections=1,
        max_connections=2,
        max_retries=1,
    )
    await pool.initialize()
    await create_postgres_schema(pool)
    async with pool.connection() as conn:
        # This file owns the table for its tests: rows another integration file left behind change the row
        # counts and statistics the planner costs the keyset page with (gm-catalog-api-bqul).
        await conn.execute("TRUNCATE insights.release_rarity")
        await conn.execute(
            """
            INSERT INTO insights.release_rarity (release_id, title, artist_name, year, rarity_score, tier, hidden_gem_score)
            SELECT n, 'T' || n, 'A', 1990, (%(scores)s::real[])[1 + (n %% %(k)s)], %(tier)s, 0.5
            FROM generate_series(%(base)s + 1, %(base)s + %(total)s) AS n
            """,
            {"scores": list(SCORES), "k": len(SCORES), "tier": TIER, "total": ROWS_PER_SCORE * len(SCORES), "base": ID_BASE},
        )
        await conn.execute(
            "INSERT INTO insights.release_rarity (release_id, title, artist_name, year, rarity_score, tier, hidden_gem_score) VALUES (999999, 'Other', 'A', 1990, 0.3, 'other-tier', 0.5)"
        )
        await conn.commit()
    try:
        yield pool
    finally:
        async with pool.connection() as conn:
            await conn.execute("TRUNCATE insights.release_rarity")
            await conn.commit()
        await pool.close()


async def _walk(pool: AsyncPostgreSQLPool, round_trip, page: int = 17) -> list[int]:
    """Walk the tier with pages of ``page`` rows, sending the cursor through ``round_trip``."""
    seen: list[int] = []
    after = None
    while True:
        rows = await get_rarity_tier_page(pool, TIER, after, limit=page)
        if not rows:
            return seen
        seen.extend(row["release_id"] for row in rows)
        last = rows[-1]
        after = (round_trip(float(last["rarity_score"])), int(last["release_id"]))


async def test_a_full_precision_walk_returns_every_tied_row_exactly_once(pool: AsyncPostgreSQLPool) -> None:
    seen = await _walk(pool, round_trip=lambda score: score)

    assert len(seen) == ROWS_PER_SCORE * len(SCORES)
    assert len(set(seen)) == len(seen)


async def test_a_walk_with_cursors_round_tripped_through_text_returns_every_tied_row_exactly_once(pool: AsyncPostgreSQLPool) -> None:
    # The cursor's score as text. A REAL survives a text round trip exactly only at 9 significant
    # digits (FLT_DECIMAL_DIG); fewer digits move it to a neighbouring REAL and the keyset then
    # repeats or skips a tie group. The page binds the score ::real, so the 9-digit text comes back
    # as the stored value.
    seen = await _walk(pool, round_trip=lambda score: float(f"{score:.9g}"))

    assert len(seen) == ROWS_PER_SCORE * len(SCORES)
    assert len(set(seen)) == len(seen)


async def test_the_walk_order_is_score_descending_then_release_id_ascending(pool: AsyncPostgreSQLPool) -> None:
    rows: list[tuple[float, int]] = []
    after = None
    while True:
        page = await get_rarity_tier_page(pool, TIER, after, limit=23)
        if not page:
            break
        rows.extend((float(row["rarity_score"]), int(row["release_id"])) for row in page)
        last = page[-1]
        after = (float(last["rarity_score"]), int(last["release_id"]))

    assert rows == sorted(rows, key=lambda pair: (-pair[0], pair[1]))


async def test_the_table_holds_only_this_files_rows(pool: AsyncPostgreSQLPool) -> None:
    """Whatever an earlier integration file seeded, the tests here start from their own rows alone."""
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT tier, count(*) FROM insights.release_rarity GROUP BY tier")
        counts = dict(await cur.fetchall())

    assert counts == {TIER: ROWS_PER_SCORE * len(SCORES), "other-tier": 1}


async def test_other_tiers_are_never_returned(pool: AsyncPostgreSQLPool) -> None:
    seen = await _walk(pool, round_trip=lambda score: score)

    assert 999999 not in seen


async def test_a_walk_through_the_opaque_cursor_returns_every_tied_row_exactly_once(pool: AsyncPostgreSQLPool) -> None:
    """The route's own loop: page_size + 1 rows tell whether another page follows; the cursor is the server's token."""
    page_size = 17
    seen: list[int] = []
    after = None
    while True:
        rows = await get_rarity_tier_page(pool, TIER, after, page_size + 1)
        seen.extend(row["release_id"] for row in rows[:page_size])
        if len(rows) <= page_size:
            break
        last = rows[page_size - 1]
        after = decode_tier_cursor(encode_tier_cursor(TIER, float(last["rarity_score"]), int(last["release_id"])), TIER)

    assert len(seen) == ROWS_PER_SCORE * len(SCORES)
    assert len(set(seen)) == len(seen)


async def test_the_keyset_page_is_one_index_range_scan_with_no_sort(pool: AsyncPostgreSQLPool) -> None:
    """Against the index definition groovemap_schema creates: (tier, rarity_score DESC, release_id)."""
    from api.queries.rarity_queries import _TIER_KEYSET_PAGE

    async with pool.connection() as conn:
        # The index comes from the schema package (database-schema v0.7.0 and later), never from this test.
        cur = await conn.execute("SELECT indexdef FROM pg_indexes WHERE schemaname = 'insights' AND indexname = 'idx_release_rarity_tier_score'")
        definition = await cur.fetchone()
        assert definition is not None, "the pinned database-schema must create idx_release_rarity_tier_score"
        assert "(tier, rarity_score DESC, release_id)" in definition[0]
        await conn.execute("ANALYZE insights.release_rarity")
        await conn.execute("SET LOCAL enable_seqscan = off")
        cur = await conn.execute(
            "EXPLAIN (FORMAT JSON) " + _TIER_KEYSET_PAGE,
            {"tier": TIER, "after_score": 0.7, "after_id": 100, "limit": 18},
        )
        plan = str((await cur.fetchone())[0])
    assert "idx_release_rarity_tier_score" in plan
    assert "'Sort'" not in plan
