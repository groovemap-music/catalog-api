"""Fake Valkey that rejects writes like a full ``noeviction`` server.

Production Valkey runs ``maxmemory-policy noeviction``: once full, memory-adding
commands (SET/SETEX/SET NX/INCR/DECR) fail with ``OOM command not allowed``
while reads, GETDEL, DEL and EXPIRE keep working — verified against
valkey/valkey:9-alpine in the deployment write-site audit (gm-deployment-u4e.4),
and the exact class, args and message re-verified against the production-pinned
valkey/valkey:9.1.2-alpine by ``just integration-valkey`` (gm-catalog-api-u5i2,
docs/valkey-noeviction-verification.md).
Shared by the per-path OOM tests (gm-catalog-api-yf7b, gm-catalog-api-cmb6).
"""

from typing import Any

import fakeredis
import fakeredis as fake_store
from valkey.exceptions import OutOfMemoryError


OOM_MESSAGE = "command not allowed when used memory > 'maxmemory'."


class NoEvictionValkey:
    """FakeAsyncValkey proxy emulating a full ``noeviction`` Valkey.

    While ``full`` is set, memory-adding commands raise the server's OOM
    response error; every other command passes through. Each write (accepted or
    rejected) is appended to ``events`` so tests can assert ordering.
    """

    WRITE_COMMANDS = frozenset({"set", "setex", "incr", "decr"})

    def __init__(self, server: fakeredis.FakeServer, events: list[str] | None = None) -> None:
        self._inner = fake_store.FakeAsyncValkey(server=server, decode_responses=True)
        self.events: list[str] = events if events is not None else []
        self.full = False
        # Optional hook: flip `full` after the Nth successful write.
        self.fill_after_writes: int | None = None
        self._writes = 0

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._inner, name)
        if name not in self.WRITE_COMMANDS:
            return attr

        async def _write(key: str, *args: Any, **kwargs: Any) -> Any:
            if self.full:
                self.events.append(f"{name}-rejected:{key}")
                raise OutOfMemoryError(OOM_MESSAGE)
            result = await attr(key, *args, **kwargs)
            self.events.append(f"{name}:{key}")
            self._writes += 1
            if self.fill_after_writes is not None and self._writes >= self.fill_after_writes:
                self.full = True
            return result

        return _write
