"""A minimal async PostgreSQL pool double for query-shape tests.

It records the statements and parameter maps a query module sends and replays canned
rows back, which is enough to assert what SQL a function builds and what it does with
the result without a server. Anything that needs the server to *parse* the SQL — and
every SQL/PGQ statement does, because only PostgreSQL 19 has a graph to match against —
belongs in the opt-in integration tests instead.
"""

from __future__ import annotations

from types import TracebackType
from typing import Any, Self


class RecordedCall:
    """One `cursor.execute` — the statement and the parameters bound to it."""

    def __init__(self, sql: str, params: Any) -> None:
        self.sql = sql
        self.params = params

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"RecordedCall(sql={self.sql!r}, params={self.params!r})"


class FakeCursor:
    """An async cursor that records statements and replays queued result sets."""

    def __init__(self, results: list[list[tuple[Any, ...]]], calls: list[RecordedCall], raise_on: dict[str, Exception] | None = None) -> None:
        self._results = results
        self._calls = calls
        self._raise_on = raise_on or {}
        self._rows: list[tuple[Any, ...]] = []

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def execute(self, sql: Any, params: Any = None) -> None:
        self._calls.append(RecordedCall(str(sql), params))
        for prefix, error in self._raise_on.items():
            if str(sql).startswith(prefix):
                raise error
        self._rows = self._results.pop(0) if self._results else []

    async def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None

    async def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)


class FakeTransaction:
    """`conn.transaction()` — records BEGIN, then COMMIT or ROLLBACK, among the statements."""

    def __init__(self, calls: list[RecordedCall]) -> None:
        self._calls = calls

    async def __aenter__(self) -> Self:
        self._calls.append(RecordedCall("BEGIN", None))
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        self._calls.append(RecordedCall("ROLLBACK" if exc_type else "COMMIT", None))


class FakeConnection:
    def __init__(self, results: list[list[tuple[Any, ...]]], calls: list[RecordedCall], raise_on: dict[str, Exception] | None = None) -> None:
        self._results = results
        self._calls = calls
        self._raise_on = raise_on

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        return None

    def cursor(self) -> FakeCursor:
        return FakeCursor(self._results, self._calls, self._raise_on)

    def transaction(self) -> FakeTransaction:
        return FakeTransaction(self._calls)


class FakePool:
    """Stands in for `common.AsyncPostgreSQLPool` in unit tests.

    Args:
        results: One list of rows per `execute`, handed out in order.
        raise_on: A statement starting with one of these prefixes is recorded, then raises
            the mapped error instead of consuming a result set.
    """

    def __init__(self, results: list[list[tuple[Any, ...]]] | None = None, *, raise_on: dict[str, Exception] | None = None) -> None:
        self._results = list(results or [])
        self._raise_on = raise_on
        self.calls: list[RecordedCall] = []

    def connection(self) -> FakeConnection:
        return FakeConnection(self._results, self.calls, self._raise_on)

    @property
    def sql(self) -> str:
        """The single statement this pool was asked to run."""
        assert len(self.calls) == 1, f"expected exactly one statement, got {len(self.calls)}"
        return self.calls[0].sql

    @property
    def params(self) -> Any:
        """The parameters bound to the single statement this pool was asked to run."""
        assert len(self.calls) == 1, f"expected exactly one statement, got {len(self.calls)}"
        return self.calls[0].params
