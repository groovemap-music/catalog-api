"""Trigram autocomplete over the `graph` vertex relations — the PostgreSQL backend.

The second family migrated under ADR 0012, and the first that never touches
`graph.catalog`. The coverage spike
(`gm-database-schema-9c8.2`, "Full-text search: six functions ADR 0012 does not count")
classifies all six of these as **SQL-only (no graph)**: they do not traverse, so there is
nothing for `GRAPH_TABLE` to say. They read one relation, filter it by name, and rank it.

What is being replaced is Neo4j's Lucene full-text indexes and the escaping that stands in
front of them. `_escape_lucene_query` exists because the query string reaches a *parser*:
a bare `/`, `~`, `:` or `(` in a name is Lucene syntax, and the one time the escaper was
not applied the result was an unhandled 500 on names like `AC/DC`. Nothing here has a
parser. The query string is bound as a value — three times, as a `LIKE` pattern, as a
regular-expression pattern, and as the right operand of `similarity()` — and a value
cannot become syntax. That is the bug class this module retires, not merely a bug.

The relations
-------------
`graph.genre`, `graph.style`, and `graph.person` are **tables** from the phase 2 schema
revision, keyed on `name`, each carrying `GIN (name gin_trgm_ops)`. That index is the
reason they stopped being views: a view cannot hold one. `graph.artist` and `graph.label`
are still views over `public.artists` and `public.labels`, so their name column has no
trigram index yet — the same predicate runs, and the server scans. Correctness does not
depend on the index; latency does, and closing that gap is the schema producer's to do.

`similarity()` and `gin_trgm_ops` both come from `pg_trgm`, which the initializer creates.
The index statements are guarded on the extension being present; these queries are not,
because a name search with no similarity function has no degraded mode worth serving.

The matching rule
-----------------
Lucene's spelling, via `_build_autocomplete_query`, is one wildcard term per whitespace
term, ANDed: `post roc` becomes `post* AND roc*`. The standard analyzer has already split
each name into tokens, so that means **every query term is a prefix of some word in the
name**, and it is what makes `roc` offer "Rock" and "Rockabilly" but not "Baroque".

Two predicates say the same thing here, and both are needed:

1. ``candidate.name ~* ALL (%(prefixes)s::text[])`` is the rule itself. Each element is
   ``\\m`` — the regular-expression constraint matching the start of a word — followed by
   the term, so the array is ANDed exactly as Lucene ANDs the wildcard terms. Punctuation
   is a word boundary to both engines, which is why `dc` still finds `AC/DC`.
2. ``candidate.name ILIKE %(contains)s`` is a superset of (1), and is there so the trigram
   index can drive the scan: if a term begins a word in the name then it is certainly a
   substring of the name, so adding this filter cannot drop a row. The pattern is built
   from the longest term, which carries the most trigrams and so is the most selective.
   `ALL (...)` over an array is not an indexable operator; a plain `ILIKE` is.

The ranking rule, which is where the two engines part
-----------------------------------------------------
Lucene returns a relevance `score` and the Cypher orders by it. It cannot be reproduced:
it is a function of the index's term statistics and of how the wildcard query was
rewritten, neither of which exists in PostgreSQL. So this backend publishes a different
number in the same column and orders by it:

    ORDER BY similarity(name, <query>) DESC, name ASC

`similarity()` is the trigram overlap of the whole query against the whole name — a number
in [0, 1], not a relevance score — and `name ASC` is a tiebreaker the Cypher does not have,
which makes this side's order total where Lucene's is arbitrary among equal scores. Both
halves of that divergence are declared to the parity harness under `("autocomplete", …)` in
`EXPECTED_DIFFERENCES`; see `docs/graph-table-migration-template.md`. The rows and their
order within a *displayed* result may therefore differ from Neo4j's, and the row *set* may
not: that is the whole of the difference, and it is what "the same result shape" means here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, cast

import structlog
from common.query_debug import execute_sql


logger = structlog.get_logger(__name__)


# The regular-expression constraint for "start of a word". A term is a word prefix when the
# name matches this followed by the term, which is how a Lucene `term*` behaves once the
# analyzer has tokenized the name.
_WORD_START = r"\m"

# The three characters `LIKE` reads as syntax. PostgreSQL's default escape character is the
# backslash, so it is escaped first or it would escape the escapes.
_LIKE_SPECIAL = str.maketrans({"\\": "\\\\", "%": "\\%", "_": "\\_"})


# One statement per relation, written out rather than assembled, so the SQL a reviewer
# reads is the SQL the server runs. `id` restates the key the Cypher returns: the entity id
# for an artist or a label, the name itself for a genre or a style, because that is what
# keys those nodes in Neo4j. `::float8` is the same discipline the collaborators family
# applies to its `::bigint` counts — `similarity()` returns `real`, the Cypher returns a
# double, and the column has to arrive in Python as the same type from both engines.

ARTIST_AUTOCOMPLETE_SQL = """
SELECT candidate.artist_id AS id,
       candidate.name AS name,
       similarity(candidate.name, %(query)s)::float8 AS score
FROM graph.artist AS candidate
WHERE candidate.name ILIKE %(contains)s
  AND candidate.name ~* ALL (%(prefixes)s::text[])
ORDER BY score DESC, candidate.name ASC
LIMIT %(limit)s
"""

LABEL_AUTOCOMPLETE_SQL = """
SELECT candidate.label_id AS id,
       candidate.name AS name,
       similarity(candidate.name, %(query)s)::float8 AS score
FROM graph.label AS candidate
WHERE candidate.name ILIKE %(contains)s
  AND candidate.name ~* ALL (%(prefixes)s::text[])
ORDER BY score DESC, candidate.name ASC
LIMIT %(limit)s
"""

GENRE_AUTOCOMPLETE_SQL = """
SELECT candidate.name AS id,
       candidate.name AS name,
       similarity(candidate.name, %(query)s)::float8 AS score
FROM graph.genre AS candidate
WHERE candidate.name ILIKE %(contains)s
  AND candidate.name ~* ALL (%(prefixes)s::text[])
ORDER BY score DESC, candidate.name ASC
LIMIT %(limit)s
"""

STYLE_AUTOCOMPLETE_SQL = """
SELECT candidate.name AS id,
       candidate.name AS name,
       similarity(candidate.name, %(query)s)::float8 AS score
FROM graph.style AS candidate
WHERE candidate.name ILIKE %(contains)s
  AND candidate.name ~* ALL (%(prefixes)s::text[])
ORDER BY score DESC, candidate.name ASC
LIMIT %(limit)s
"""

# The person search returns no id: `:Person` is keyed on the verbatim credit name, so the
# name is the identity and the Cypher returns that column alone.
PERSON_AUTOCOMPLETE_SQL = """
SELECT candidate.name AS name,
       similarity(candidate.name, %(query)s)::float8 AS score
FROM graph.person AS candidate
WHERE candidate.name ILIKE %(contains)s
  AND candidate.name ~* ALL (%(prefixes)s::text[])
ORDER BY score DESC, candidate.name ASC
LIMIT %(limit)s
"""


@dataclass(frozen=True, slots=True)
class _AutocompleteSpec:
    """One relation's statement and the columns it projects, in the Cypher's order."""

    sql: str
    columns: tuple[str, ...]


_ARTIST_AUTOCOMPLETE = _AutocompleteSpec(ARTIST_AUTOCOMPLETE_SQL, ("id", "name", "score"))
_LABEL_AUTOCOMPLETE = _AutocompleteSpec(LABEL_AUTOCOMPLETE_SQL, ("id", "name", "score"))
_GENRE_AUTOCOMPLETE = _AutocompleteSpec(GENRE_AUTOCOMPLETE_SQL, ("id", "name", "score"))
_STYLE_AUTOCOMPLETE = _AutocompleteSpec(STYLE_AUTOCOMPLETE_SQL, ("id", "name", "score"))
_PERSON_AUTOCOMPLETE = _AutocompleteSpec(PERSON_AUTOCOMPLETE_SQL, ("name", "score"))


def _escape_like(term: str) -> str:
    """Return *term* with its `LIKE` metacharacters escaped.

    The mirror image of `_escape_lucene_query`, and the reason it is a footnote rather
    than a hazard: three characters, a fixed translation, and the result is still a bound
    value. A missed character here widens or narrows one prefilter; a missed character
    there was a parse error the caller saw as a 500.
    """
    return term.translate(_LIKE_SPECIAL)


def build_match_parameters(query: str, limit: int) -> dict[str, Any] | None:
    """Return the parameters every statement in this module binds, or `None` for no query.

    `None` means the query had no terms — it was empty or all whitespace. There is no
    sensible statement for that (`ILIKE '%%'` would return the whole relation ranked by a
    similarity of zero), so the caller returns no rows instead. The HTTP layer already
    rejects anything under three characters; this is the backstop for everything else.
    """
    terms = query.split()
    if not terms:
        return None
    longest = max(terms, key=len)
    return {
        "query": query,
        "contains": f"%{_escape_like(longest)}%",
        "prefixes": [_WORD_START + re.escape(term) for term in terms],
        "limit": limit,
    }


async def _autocomplete(pool: Any, spec: _AutocompleteSpec, query: str, limit: int) -> list[dict[str, Any]]:
    """Run one relation's search and project it into the Cypher's columns, in its order."""
    parameters = build_match_parameters(query, limit)
    if parameters is None:
        return []

    async with pool.connection() as conn, conn.cursor() as cursor_cm:
        cursor = cast("Any", cursor_cm)
        await execute_sql(cursor, spec.sql, parameters)
        rows = await cursor.fetchall()

    matches = [dict(zip(spec.columns, row, strict=True)) for row in rows]
    logger.debug("🔍 Autocomplete resolved", query=query, count=len(matches))
    return matches


async def autocomplete_artist(pool: Any, query: str, limit: int = 10) -> list[dict[str, Any]]:
    """Search artists by name over `graph.artist`."""
    return await _autocomplete(pool, _ARTIST_AUTOCOMPLETE, query, limit)


async def autocomplete_label(pool: Any, query: str, limit: int = 10) -> list[dict[str, Any]]:
    """Search labels by name over `graph.label`."""
    return await _autocomplete(pool, _LABEL_AUTOCOMPLETE, query, limit)


async def autocomplete_genre(pool: Any, query: str, limit: int = 10) -> list[dict[str, Any]]:
    """Search genres by name over `graph.genre` and its trigram index."""
    return await _autocomplete(pool, _GENRE_AUTOCOMPLETE, query, limit)


async def autocomplete_style(pool: Any, query: str, limit: int = 10) -> list[dict[str, Any]]:
    """Search styles by name over `graph.style` and its trigram index."""
    return await _autocomplete(pool, _STYLE_AUTOCOMPLETE, query, limit)


async def autocomplete_person(pool: Any, query: str, limit: int = 10) -> list[dict[str, Any]]:
    """Search credited people by name over `graph.person` and its trigram index."""
    return await _autocomplete(pool, _PERSON_AUTOCOMPLETE, query, limit)
