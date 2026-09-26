"""Engine-backed proof of the barcode GTIN-12/13/14 lookup equivalence (ADR 0011's "UPC-A and
EAN-13 are one GTIN at lookup" amendment).

Runs against the real schema through the same `postgres_pool` fixture `tests/test_real_databases.py`
uses, so the `provider_aliases` partial unique index on `(provider, entity_kind, external_id)
WHERE valid_to IS NULL` is the server's own, and `resolve_alias_native_ids`'s `= ANY(...)`
probe is planned and executed by PostgreSQL rather than assumed. This module exercises the
query layer only — `resolve_alias_native_id`/`resolve_alias_native_ids` resolve whatever
forms they're given; deriving the equivalent GTIN-12/13/14 forms from a typed value is
`api.routers.lookup._barcode_forms`'s job, covered against a mocked pool in `tests/test_lookup.py`.
Nothing here touches a shared database: `just test-integration` starts throwaway containers.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from common import AsyncPostgreSQLPool

from api.queries.lookup_queries import resolve_alias_native_id, resolve_alias_native_ids
from tests.test_real_databases import postgres_pool


__all__ = ["postgres_pool"]

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

TWELVE_DIGIT = "036000291452"
THIRTEEN_DIGIT = "0036000291452"
FOURTEEN_DIGIT = "00036000291452"


@pytest_asyncio.fixture
async def pool(postgres_pool: AsyncPostgreSQLPool) -> AsyncPostgreSQLPool:
    return postgres_pool


async def _execute(pool: AsyncPostgreSQLPool, sql: str, params: Any = None) -> list[tuple[Any, ...]]:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(sql, params)
        return await cur.fetchall() if cur.description else []


async def _item(pool: AsyncPostgreSQLPool, kind: str = "release") -> UUID:
    native_id = uuid4()
    await _execute(pool, "INSERT INTO catalog_items (id, kind) VALUES (%s, %s)", (native_id, kind))
    return native_id


async def _alias(pool: AsyncPostgreSQLPool, provider: str, external_id: str, native_id: UUID) -> None:
    await _execute(
        pool,
        "INSERT INTO provider_aliases (provider, entity_kind, external_id, native_id, source) VALUES (%s, 'release', %s, %s, 'catalog')",
        (provider, external_id, native_id),
    )


async def test_twelve_digit_request_resolves_an_alias_minted_under_the_thirteen_digit_form(
    pool: AsyncPostgreSQLPool,
) -> None:
    """A request for the 12-digit form must also find an item minted under its 13-digit
    EAN-13 spelling — the two are the same GTIN at lookup, though nothing is re-keyed."""
    native_id = await _item(pool)
    await _alias(pool, "barcode", THIRTEEN_DIGIT, native_id)

    resolved = await resolve_alias_native_ids(pool, "barcode", (TWELVE_DIGIT, THIRTEEN_DIGIT))

    assert resolved == {THIRTEEN_DIGIT: native_id}
    # The 12-digit form alone, through the single-value path, correctly finds nothing —
    # the row genuinely lives under the other spelling.
    assert await resolve_alias_native_id(pool, "barcode", TWELVE_DIGIT) is None


async def test_thirteen_digit_request_resolves_an_alias_minted_under_the_twelve_digit_form(
    pool: AsyncPostgreSQLPool,
) -> None:
    native_id = await _item(pool)
    await _alias(pool, "barcode", TWELVE_DIGIT, native_id)

    resolved = await resolve_alias_native_ids(pool, "barcode", (THIRTEEN_DIGIT, TWELVE_DIGIT))

    assert resolved == {TWELVE_DIGIT: native_id}


async def test_both_forms_minted_against_different_items_both_resolve(pool: AsyncPostgreSQLPool) -> None:
    """Two genuinely different native items, one under each form of the same GTIN: the
    batched query must return both mappings rather than only the first found."""
    item_a = await _item(pool)
    item_b = await _item(pool)
    await _alias(pool, "barcode", TWELVE_DIGIT, item_a)
    await _alias(pool, "barcode", THIRTEEN_DIGIT, item_b)

    resolved = await resolve_alias_native_ids(pool, "barcode", (TWELVE_DIGIT, THIRTEEN_DIGIT))

    assert resolved == {TWELVE_DIGIT: item_a, THIRTEEN_DIGIT: item_b}


async def test_exact_form_hit_is_unaffected_by_the_batched_probe(pool: AsyncPostgreSQLPool) -> None:
    """Both forms minted against the SAME item: still one native id, same as an exact hit."""
    native_id = await _item(pool)
    await _alias(pool, "barcode", TWELVE_DIGIT, native_id)
    await _alias(pool, "barcode", THIRTEEN_DIGIT, native_id)

    resolved = await resolve_alias_native_ids(pool, "barcode", (TWELVE_DIGIT, THIRTEEN_DIGIT))

    assert set(resolved.values()) == {native_id}


async def test_closed_alias_is_not_resolved_by_either_path(pool: AsyncPostgreSQLPool) -> None:
    """The partial unique index's `WHERE valid_to IS NULL` predicate applies to the batched
    probe exactly as it does to the single-value one."""
    native_id = await _item(pool)
    await _alias(pool, "barcode", TWELVE_DIGIT, native_id)
    await _execute(pool, "UPDATE provider_aliases SET valid_to = now() WHERE external_id = %s", (TWELVE_DIGIT,))

    assert await resolve_alias_native_id(pool, "barcode", TWELVE_DIGIT) is None
    assert await resolve_alias_native_ids(pool, "barcode", (TWELVE_DIGIT, THIRTEEN_DIGIT)) == {}


async def test_fourteen_digit_double_padded_request_resolves_an_alias_minted_under_the_bare_twelve_digit_form(
    pool: AsyncPostgreSQLPool,
) -> None:
    """A GTIN-14 stored as `00` + the 12-digit UPC-A is the same GTIN: a request for the
    14-digit form must find an item minted under the bare 12-digit spelling."""
    native_id = await _item(pool)
    await _alias(pool, "barcode", TWELVE_DIGIT, native_id)

    resolved = await resolve_alias_native_ids(pool, "barcode", (TWELVE_DIGIT, THIRTEEN_DIGIT, FOURTEEN_DIGIT))

    assert resolved == {TWELVE_DIGIT: native_id}


async def test_thirteen_digit_nonzero_leading_request_resolves_an_alias_minted_under_its_fourteen_digit_form(
    pool: AsyncPostgreSQLPool,
) -> None:
    """A 13-digit EAN-13 that is not a zero-prefixed UPC-A still shares a GTIN with its own
    single-zero-padded 14-digit spelling, even though it has no 12-digit equivalent."""
    thirteen_nonzero = "5012394144777"
    fourteen_padded = f"0{thirteen_nonzero}"
    native_id = await _item(pool)
    await _alias(pool, "barcode", fourteen_padded, native_id)

    resolved = await resolve_alias_native_ids(pool, "barcode", (thirteen_nonzero, fourteen_padded))

    assert resolved == {fourteen_padded: native_id}
    # The bare 13-digit form alone finds nothing — the row genuinely lives under the padded
    # spelling, as `resolve_alias_native_id`'s single-value path shows on its own.
    assert await resolve_alias_native_id(pool, "barcode", thirteen_nonzero) is None
