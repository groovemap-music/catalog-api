"""The data-completeness counts are what they always were, on a real PostgreSQL.

Audit H4 (``gm-catalog-api-wl47``): the filters read ``data->>'key'`` three times per field, which
detoasts each release document about twelve times. The rewrite reads the document once through
``jsonb_to_record``. The oracle below is the statement it replaced, kept verbatim, run over documents
chosen to sit on every edge of the filter: absent, JSON null, the empty string, ``[]``, ``{}``, zero,
false, whitespace, a string that spells ``[]``, a one-element array. A mock cannot tell the two apart.

Nothing here touches a shared database: ``just test-integration`` starts throwaway containers.
"""

# ruff: noqa: S608  (the oracle interpolates only constants of this module)

from __future__ import annotations

import json
from typing import Any

import pytest
import pytest_asyncio
from common import AsyncPostgreSQLPool

from api.queries.insights_pg_queries import _COMBINED_QUERIES, _COMPLETENESS_FIELDS, query_data_completeness
from tests.test_real_databases import postgres_pool


__all__ = ["postgres_pool"]

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_TABLES = ["artists", "labels", "masters", "releases"]
_KEYS = ["year", "country", "genres", "images"]

# Every value a key can hold, plus "absent" (the key is left out of the document).
_EDGE_VALUES: list[Any] = [
    "absent",
    None,
    "",
    "[]",
    [],
    {},
    0,
    False,
    True,
    " ",
    "null",
    1999,
    "US",
    ["Rock"],
    ["Rock", "Pop"],
    [[]],
    {"a": 1},
    'Ünï "quoted"',
]


def _documents() -> list[dict[str, Any]]:
    """One document per (key, value) pair, every other key set to an ordinary value, plus some mixes."""
    ordinary = {"year": 2001, "country": "UK", "genres": ["Jazz"], "images": [{"uri": "x"}]}
    documents = []
    for key in _KEYS:
        for value in _EDGE_VALUES:
            document = dict(ordinary)
            if value == "absent":
                del document[key]
            else:
                document[key] = value
            documents.append(document)
    documents.append({})
    documents.append({"year": "", "country": "", "genres": [], "images": []})
    return documents


def _oracle(table: str) -> str:
    """The statement this change replaced, verbatim."""
    parts = []
    for field_name, key in _COMPLETENESS_FIELDS[table]:
        parts.append(f"count(*) FILTER (WHERE data->>'{key}' IS NOT NULL AND data->>'{key}' != '' AND data->>'{key}' != '[]') AS {field_name}")
    return f"SELECT count(*) AS total_count, {', '.join(parts)} FROM {table}"


@pytest_asyncio.fixture
async def pool(postgres_pool: AsyncPostgreSQLPool) -> AsyncPostgreSQLPool:
    documents = _documents()
    async with postgres_pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("TRUNCATE artists, labels, masters, releases")
        for table in _TABLES:
            await cur.executemany(
                f"INSERT INTO {table} (data_id, hash, data) VALUES (%s, %s, %s::jsonb)",
                [(f"{table[0]}{i}", f"{table[0]}{i}", json.dumps(document)) for i, document in enumerate(documents)],
            )
        await cur.execute("ANALYZE artists, labels, masters, releases")
    return postgres_pool


@pytest.mark.parametrize("table", _TABLES)
async def test_counts_equal_the_statement_they_replaced(pool: AsyncPostgreSQLPool, table: str) -> None:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(_oracle(table))
        expected = await cur.fetchone()
        await cur.execute(_COMBINED_QUERIES[table])
        actual = await cur.fetchone()

    assert actual == expected
    assert expected is not None
    assert expected[0] == len(_documents())
    assert any(0 < count < expected[0] for count in expected[1:])  # the fixtures really split on the filter


async def test_the_route_query_reports_the_same_numbers(pool: AsyncPostgreSQLPool) -> None:
    results = {item["entity_type"]: item for item in await query_data_completeness(pool)}

    async with pool.connection() as conn, conn.cursor() as cur:
        for table in _TABLES:
            await cur.execute(_oracle(table))
            row = await cur.fetchone()
            assert row is not None
            assert results[table]["total_count"] == row[0]
            for index, (field_name, _) in enumerate(_COMPLETENESS_FIELDS[table]):
                assert results[table][field_name] == row[index + 1]
