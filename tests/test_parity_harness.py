"""What the parity harness does with two results, without a database in the way.

The harness itself lives in `tests/test_real_databases.py` and only ever runs with two
engines behind it, which means its own decisions — what counts as a divergence, and what
`EXPECTED_DIFFERENCES` does and does not buy — are never exercised by a run that can be
made on a laptop. They are here instead. Two backends agreeing is the case the
integration suite covers; every other case is covered here.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.test_real_databases import (
    EXPECTED_DIFFERENCES,
    ExpectedDifference,
    ParityCall,
    ParityFamily,
    assert_parity,
    register_parity_family,
)


CALL = ParityCall("get_multi_hop_collaborators", ("1",), {"depth": 2, "limit": 50})

ROWS: list[dict[str, Any]] = [{"artist_id": "2", "distance": 1, "collaboration_count": 3}]


def _declare(monkeypatch: pytest.MonkeyPatch, difference: ExpectedDifference) -> None:
    monkeypatch.setitem(EXPECTED_DIFFERENCES, ("collaborators", CALL.function), difference)


class TestAnUndeclaredDifference:
    """Nothing is tolerated that has not been written down first."""

    def test_different_rows_fail_and_the_failure_names_the_registry(self) -> None:
        with pytest.raises(pytest.fail.Exception, match="EXPECTED_DIFFERENCES") as failure:
            assert_parity("collaborators", CALL, neo4j_result=ROWS, postgres_result=[])

        assert "diverged between the two backends" in str(failure.value)
        assert "'get_multi_hop_collaborators'" in str(failure.value)

    def test_a_different_row_order_fails(self) -> None:
        reversed_rows = [{"artist_id": "3"}, {"artist_id": "2"}]
        with pytest.raises(pytest.fail.Exception):
            assert_parity("collaborators", CALL, neo4j_result=list(reversed(reversed_rows)), postgres_result=reversed_rows)

    def test_equal_values_of_different_types_fail(self) -> None:
        """`1 == True` in Python, and a response that says integer does not agree."""
        with pytest.raises(pytest.fail.Exception) as failure:
            assert_parity("collaborators", CALL, neo4j_result=[{"distance": 1}], postgres_result=[{"distance": True}])

        assert "'bool'" in str(failure.value)

    def test_a_different_column_order_fails(self) -> None:
        """Dicts compare equal whatever order their keys came back in; the response does not."""
        with pytest.raises(pytest.fail.Exception):
            assert_parity(
                "collaborators",
                CALL,
                neo4j_result=[{"artist_id": "2", "distance": 1}],
                postgres_result=[{"distance": 1, "artist_id": "2"}],
            )

    def test_two_agreeing_backends_pass(self) -> None:
        assert_parity("collaborators", CALL, neo4j_result=ROWS, postgres_result=[dict(row) for row in ROWS])


class TestADeclaredDifference:
    """A declaration says how much is tolerated, not that the assertion is off."""

    def test_a_difference_the_normalizer_closes_is_tolerated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _declare(
            monkeypatch,
            ExpectedDifference(
                reason="the SQL sums numeric and keeps more places than the Cypher",
                normalize=lambda rows: [{**row, "score": round(row["score"], 2)} for row in rows],
            ),
        )

        assert_parity(
            "collaborators",
            CALL,
            neo4j_result=[{"score": 0.33}],
            postgres_result=[{"score": 0.333333}],
        )

    def test_a_difference_wider_than_the_normalizer_still_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _declare(
            monkeypatch,
            ExpectedDifference(
                reason="rounding only",
                normalize=lambda rows: [{**row, "score": round(row["score"], 2)} for row in rows],
            ),
        )

        with pytest.raises(pytest.fail.Exception, match="diverged by more than the declared difference"):
            assert_parity("collaborators", CALL, neo4j_result=[{"score": 0.33}], postgres_result=[{"score": 0.91}])

    def test_a_declaration_whose_difference_did_not_materialise_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A tolerance must not outlive the behaviour it was granted for."""
        _declare(monkeypatch, ExpectedDifference(reason="rounding only", normalize=lambda rows: rows))

        with pytest.raises(pytest.fail.Exception, match="Delete the entry"):
            assert_parity("collaborators", CALL, neo4j_result=ROWS, postgres_result=[dict(row) for row in ROWS])

    def test_a_declaration_is_keyed_by_family_and_function_together(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Tolerating a difference in one family's function must not tolerate another's."""
        _declare(monkeypatch, ExpectedDifference(reason="rounding only", normalize=lambda rows: rows))
        other = ParityCall("count_multi_hop_collaborators", ("1",), {"depth": 2})

        with pytest.raises(pytest.fail.Exception, match="EXPECTED_DIFFERENCES"):
            assert_parity("collaborators", other, neo4j_result=6, postgres_result=5)


class TestRegistration:
    def test_registering_a_family_records_its_calls_and_the_functions_they_cover(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from tests.test_real_databases import PARITY_FAMILIES

        monkeypatch.setitem(PARITY_FAMILIES, "vertex_lookup", ParityFamily("vertex_lookup", ()))
        register_parity_family("vertex_lookup", [ParityCall("get_artist"), ParityCall("get_artist", ("1",))])

        family = PARITY_FAMILIES["vertex_lookup"]
        assert family.functions == {"get_artist"}
        assert family.requires_property_graph is True

    def test_a_family_whose_postgres_side_needs_no_property_graph_says_so(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from tests.test_real_databases import PARITY_FAMILIES

        monkeypatch.setitem(PARITY_FAMILIES, "autocomplete", ParityFamily("autocomplete", ()))
        register_parity_family("autocomplete", [ParityCall("suggest", ("mile",))], requires_property_graph=False)

        assert PARITY_FAMILIES["autocomplete"].requires_property_graph is False

    def test_a_call_renders_as_the_call_it_makes(self) -> None:
        assert str(CALL) == "get_multi_hop_collaborators('1', depth=2, limit=50)"
