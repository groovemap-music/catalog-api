"""Shared connection bundle for the MusicBrainz read family."""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class MusicBrainzHandles:
    """Connections used by one MusicBrainz backend implementation.

    ``graph`` is either Neo4j or PostgreSQL. ``relational`` is always PostgreSQL,
    because external links and the source-table totals are relational on both paths.
    """

    graph: Any
    relational: Any
