"""The ledger freshness checks find the views by name, whatever their relkind (gm-catalog-api-sflm).

database-schema v0.6.3 keeps the identity_ledger_* views as materialized views; the xen5 schema
keeps the same names as plain tables. A relkind = 'm' filter made every route answer "not
refreshed" (503) after that upgrade. The behaviour is proven on real Postgres for both schemas;
this pins the SQL shape so the filter does not come back.
"""

from api import ledger_refresh
from api.queries import identity_ledger_queries


def test_the_freshness_check_does_not_filter_on_materialized_view_kind() -> None:
    assert "relkind" not in identity_ledger_queries._FRESHNESS
    assert "relname = ANY(%(views)s::text[])" in identity_ledger_queries._FRESHNESS


def test_the_stored_state_check_does_not_filter_on_materialized_view_kind() -> None:
    assert "relkind" not in ledger_refresh._STORED
    assert "relname = ANY(%(views)s::text[])" in ledger_refresh._STORED


def test_the_stored_state_check_requires_the_state_row_because_tables_are_always_populated() -> None:
    """relispopulated is always true on the v0.7.0 tables, so the state row says "refreshed yet"."""
    assert "EXISTS (SELECT 1 FROM identity_ledger_refresh_state)" in ledger_refresh._STORED
