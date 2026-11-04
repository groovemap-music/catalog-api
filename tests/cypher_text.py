"""Read the Cypher text out of whatever a helper handed to ``session.run``.

The query helpers now always send a ``neo4j.Query`` (it carries the server timeout), where they used to
send a bare string when no timeout was set.
"""

from __future__ import annotations

from typing import Any

from neo4j import Query


def cypher_text(sent: Any) -> str:
    return sent.text if isinstance(sent, Query) else str(sent)
