"""Unit tests for credits query functions."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.queries import credits_queries
from api.queries.credits_queries import (
    autocomplete_person,
    get_person_connections,
    get_person_credits,
    get_person_profile,
    get_person_role_breakdown,
    get_person_timeline,
    get_release_credits,
    get_role_leaderboard,
    get_shared_credits,
)


class _AsyncRecordIterator:
    """Async iterator over a list of records for mocking Neo4j results."""

    def __init__(self, records: list[dict[str, Any]]) -> None:
        self._iter = iter(records)

    def __aiter__(self) -> _AsyncRecordIterator:
        return self

    async def __anext__(self) -> dict[str, Any]:
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration  # noqa: B904


def _make_mock_driver(
    query_returns: list[dict[str, Any]] | None = None,
    single_return: dict[str, Any] | None = None,
) -> MagicMock:
    """Create a mock Neo4j driver for testing query functions."""
    driver = MagicMock()
    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)

    records = query_returns if query_returns is not None else []
    mock_result = _AsyncRecordIterator(records)
    if single_return is not None:
        mock_result.single = AsyncMock(return_value=single_return)  # type: ignore[attr-defined]
    else:
        mock_result.single = AsyncMock(return_value=None)  # type: ignore[attr-defined]
    mock_result.consume = AsyncMock()  # type: ignore[attr-defined]

    session.run = AsyncMock(return_value=mock_result)
    driver.session = MagicMock(return_value=session)
    return driver


class TestGetPersonCredits:
    """Tests for get_person_credits."""

    @pytest.mark.asyncio
    async def test_returns_results(self) -> None:
        records = [
            {"release_id": "1", "title": "Album", "year": 1990, "role": "Producer", "category": "production", "artists": ["A"], "labels": ["L"]},
        ]
        driver = _make_mock_driver(query_returns=records)
        result = await get_person_credits(driver, "Test Person")
        assert len(result) == 1
        assert result[0]["role"] == "Producer"

    @pytest.mark.asyncio
    async def test_returns_empty(self) -> None:
        driver = _make_mock_driver(query_returns=[])
        result = await get_person_credits(driver, "Nobody")
        assert result == []


class TestGetPersonTimeline:
    """Tests for get_person_timeline."""

    @pytest.mark.asyncio
    async def test_returns_timeline(self) -> None:
        records = [
            {"year": 1990, "category": "mastering", "count": 5},
            {"year": 1991, "category": "mastering", "count": 8},
        ]
        driver = _make_mock_driver(query_returns=records)
        result = await get_person_timeline(driver, "Test")
        assert len(result) == 2
        assert result[0]["year"] == 1990


class TestGetReleaseCredits:
    """Tests for get_release_credits."""

    @pytest.mark.asyncio
    async def test_returns_credits(self) -> None:
        records = [
            {"name": "Person A", "role": "Producer", "category": "production", "artist_id": None, "artist_name": None},
        ]
        driver = _make_mock_driver(query_returns=records)
        result = await get_release_credits(driver, "123")
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_returns_empty(self) -> None:
        driver = _make_mock_driver(query_returns=[])
        result = await get_release_credits(driver, "999")
        assert result == []


class TestGetRoleLeaderboard:
    """Tests for get_role_leaderboard."""

    @pytest.fixture(autouse=True)
    def _reset_flag(self) -> None:
        credits_queries._fallback_logged = False

    @pytest.mark.asyncio
    async def test_computed_uses_indexed_property(self) -> None:
        rows = [{"name": "Top Person", "credit_count": 100}]
        with patch.object(credits_queries, "run_query", AsyncMock(side_effect=[[{"computed": True}], rows])) as rq:
            result = await get_role_leaderboard(MagicMock(), "mastering", limit=20)
        assert result == rows
        probe, main = rq.await_args_list
        assert "p.credit_count_mastering IS NOT NULL" in probe.args[1]
        assert "p.credit_count_mastering > 0" in main.args[1]
        assert "ORDER BY p.credit_count_mastering DESC, p.name" in main.args[1]
        assert "CREDITED_ON" not in main.args[1]

    @pytest.mark.asyncio
    async def test_uncomputed_falls_back_to_live_count_and_logs_once(self) -> None:
        rows = [{"name": "Live Person", "credit_count": 3}]
        with (
            patch.object(credits_queries, "run_query", AsyncMock(side_effect=[[], rows, [], rows])) as rq,
            patch.object(credits_queries, "logger") as log,
        ):
            first = await get_role_leaderboard(MagicMock(), "mastering")
            second = await get_role_leaderboard(MagicMock(), "mastering")
        assert first == second == rows
        assert "CREDITED_ON" in rq.await_args_list[1].args[1]
        log.warning.assert_called_once()

    @pytest.mark.asyncio
    async def test_unknown_category_rejected_before_any_query(self) -> None:
        with patch.object(credits_queries, "run_query", AsyncMock()) as rq, pytest.raises(ValueError, match="unknown credit category"):
            await get_role_leaderboard(MagicMock(), "x) DETACH DELETE p //")
        rq.assert_not_awaited()


class TestGetSharedCredits:
    """Tests for get_shared_credits."""

    @pytest.mark.asyncio
    async def test_returns_shared(self) -> None:
        records = [
            {"release_id": "1", "title": "Album", "year": 1990, "person1_role": "Producer", "person2_role": "Engineer", "artists": ["A"]},
        ]
        driver = _make_mock_driver(query_returns=records)
        result = await get_shared_credits(driver, "Person A", "Person B")
        assert len(result) == 1


class TestGetPersonConnections:
    """Tests for get_person_connections."""

    @pytest.mark.asyncio
    async def test_returns_connections_depth_1(self) -> None:
        records = [{"name": "Connected", "shared_count": 5}]
        driver = _make_mock_driver(query_returns=records)
        result = await get_person_connections(driver, "Test", depth=1, limit=50)
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_returns_connections_depth_2(self) -> None:
        records = [{"name": "Connected", "shared_count": 5, "second_hops": []}]
        driver = _make_mock_driver(query_returns=records)
        result = await get_person_connections(driver, "Test", depth=2, limit=50)
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_clamps_depth(self) -> None:
        driver = _make_mock_driver(query_returns=[])
        result = await get_person_connections(driver, "Test", depth=0, limit=50)
        assert result == []

        result = await get_person_connections(driver, "Test", depth=5, limit=50)
        assert result == []

    @pytest.mark.asyncio
    async def test_depth_2_cypher_is_valid_grouping_not_just_mocked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Depth>=2 Cypher must group validly, not only pass against a mock.

        The depth>=2 branch used to wrap `collect(DISTINCT {...})` in a CASE
        that referenced `hop2` OUTSIDE the aggregation, which Neo4j rejects at
        compile time ("Aggregation column contains implicit grouping
        expressions: hop2") — a real ClientError the mocked driver in the
        other tests here can never surface. Verified against a live Neo4j 5
        instance (both that the pre-fix Cypher raises exactly that error and
        that the fixed Cypher executes and returns the expected rows).

        Pin the structural fix in source form: the CASE referencing `hop2`
        must be nested INSIDE the collect(...) call's argument list, not
        wrapped around it.
        """
        captured: dict[str, str] = {}

        async def _fake_run_query(_driver: Any, cypher: str, **_params: Any) -> list[dict[str, Any]]:
            captured["cypher"] = cypher
            return []

        monkeypatch.setattr("api.queries.credits_queries.run_query", _fake_run_query)

        driver = _make_mock_driver(query_returns=[])
        await get_person_connections(driver, "Test", depth=2, limit=50)

        cypher = captured["cypher"]
        collect_start = cypher.index("collect(DISTINCT")
        collect_end = cypher.index(")", cypher.index("END", collect_start)) + 1
        case_idx = cypher.index("CASE WHEN hop2 IS NOT NULL")

        # The CASE referencing hop2 must be nested inside collect(...)'s
        # argument list, not wrapped around it — otherwise hop2 is a bare
        # grouping-key violation outside the aggregate.
        assert collect_start < case_idx < collect_end, (
            "The `CASE WHEN hop2 IS NOT NULL` branch must be INSIDE "
            "collect(DISTINCT ...)'s arguments, not wrapped around the whole "
            "collect(...) call — the latter is the exact pre-fix defect "
            "that Neo4j rejects as an implicit "
            "grouping expression."
        )


class TestAutocompletePerson:
    """Tests for autocomplete_person."""

    @pytest.mark.asyncio
    async def test_returns_results(self) -> None:
        records = [{"name": "Bob Ludwig", "score": 5.2}]
        driver = _make_mock_driver(query_returns=records)
        result = await autocomplete_person(driver, "Bob")
        assert len(result) == 1
        assert result[0]["name"] == "Bob Ludwig"

    @pytest.mark.asyncio
    async def test_adds_wildcard(self) -> None:
        driver = _make_mock_driver(query_returns=[])
        await autocomplete_person(driver, "Bob")
        # Verify the query was called with wildcard
        session = driver.session().__aenter__.return_value
        call_args = session.run.call_args
        assert call_args is not None

    @pytest.mark.asyncio
    async def test_no_double_wildcard(self) -> None:
        driver = _make_mock_driver(query_returns=[])
        assert await autocomplete_person(driver, "Bob*") == []
        params = driver.session().__aenter__.return_value.run.await_args.args[1]
        # Input * is literal; exactly one unescaped prefix wildcard is appended.
        assert params["query"] == r"Bob\**"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "raw",
        ["AC/DC", "C++", "Emerson, Lake (Palmer", "a:b", "foo~", "Jean-Michel"],
    )
    async def test_escapes_lucene_metacharacters(self, raw: str) -> None:
        """Crafted names with Lucene syntax must be escaped, not passed raw.

        Regression for autocomplete_person handing user input straight to
        db.index.fulltext.queryNodes, where metacharacters (/, +, :, ~, (, -)
        trigger a Lucene ParseException surfaced as an unhandled 500.
        """
        driver = _make_mock_driver(query_returns=[])
        await autocomplete_person(driver, raw)

        session = driver.session().__aenter__.return_value
        call_args = session.run.call_args
        assert call_args is not None
        # params dict is the 2nd positional arg to session.run(cypher, params, ...)
        params = call_args[0][1]
        sent = params["query"]
        # Every Lucene special char that appears in the raw input must be
        # backslash-escaped in the query actually sent to Neo4j.
        for ch in raw:
            if ch in '+-&|!(){}[]^"~*?:\\/':
                assert f"\\{ch}" in sent, f"metacharacter {ch!r} was not escaped in {sent!r}"


class TestGetPersonProfile:
    """Tests for get_person_profile."""

    @pytest.mark.asyncio
    async def test_returns_profile(self) -> None:
        record = {
            "name": "Bob Ludwig",
            "total_credits": 500,
            "categories": ["mastering"],
            "first_year": 1970,
            "last_year": 2020,
            "artist_id": None,
            "artist_name": None,
        }
        driver = _make_mock_driver(single_return=record)
        result = await get_person_profile(driver, "Bob Ludwig")
        assert result is not None
        assert result["name"] == "Bob Ludwig"

    @pytest.mark.asyncio
    async def test_returns_none(self) -> None:
        driver = _make_mock_driver(single_return=None)
        result = await get_person_profile(driver, "Nobody")
        assert result is None


class TestGetPersonRoleBreakdown:
    """Tests for get_person_role_breakdown."""

    @pytest.mark.asyncio
    async def test_returns_breakdown(self) -> None:
        records = [{"category": "mastering", "count": 500}]
        driver = _make_mock_driver(query_returns=records)
        result = await get_person_role_breakdown(driver, "Bob Ludwig")
        assert len(result) == 1
        assert result[0]["category"] == "mastering"
