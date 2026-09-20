"""SQL/PGQ queries for the Credits & Provenance endpoints — the PostgreSQL backend.

Coverage spike family 4 (`gm-database-schema-9c8.2`), migrated by `gm-catalog-api-dl8.1`.
It replaces the Cypher in :mod:`api.queries.credits_queries` and follows the four rules
:mod:`api.queries.network_pg_queries` established, with `docs/graph-table-migration-template.md`
as the worked example: pattern matching replaces Cypher and not SQL, one statement per
module-level constant, every value is a parameter, and parity is column-for-column.

Eight of the nine functions in the Cypher module are here. The ninth,
``autocomplete_person``, is its full-text search: the spike classifies it SQL-only and it
was migrated with the other five full-text functions into
:mod:`api.queries.autocomplete_pg_queries`, where the credits router still reaches it
through the "autocomplete" family. See ``CreditsBackend`` in :mod:`api.graph_backend`.

``category`` is ``role_category``
---------------------------------
The one rename in the family, and the spike flags it because a missed one is a silent
null rather than an error. ``graphinator`` writes ``CREDITED_ON.category``; the relational
edge publishes the same value as ``role_category``, a column generated over
``graph.credit_role_category(role)`` — the schema producer renders that function from
``common.credit_roles.ROLE_CATEGORIES``, the same taxonomy ``categorize_role`` scans, so
the two stores compute one answer from one vocabulary rather than agreeing by coincidence.

Six statements read it and each says so where it reads it. The **API** column keeps the
Cypher's name: every one of them projects ``role_category AS category``, because the
router, the response models, and the cache payloads are all spelled ``category`` and this
is a backend swap underneath an unchanged response schema, not a rename of the endpoint.

Mapping the Cypher onto the graph
---------------------------------
``:Person`` is ``graph.person`` is ``(p IS person)``, keyed on ``name`` — the same key
Neo4j uses, which is why one person credited on two releases is one vertex on both sides.
``[:CREDITED_ON]`` is ``graph.credited_on`` is ``-[IS credited_on]->``, directed person ->
release and keyed ``(person_name, release_id, role)``: one person credited twice on one
release under two roles is **two edges**, exactly as the enricher's
``MERGE (p)-[c:CREDITED_ON {role: credit.role}]->(r)`` makes it two relationships.
``[:SAME_AS]`` is ``graph.same_as``, directed person -> artist, and the ``OPTIONAL MATCH``
on it becomes a ``LEFT JOIN`` against a second ``GRAPH_TABLE``.

``graph.release.year`` restates the producer's ``releases.data ->> 'year'``, which is
``text``, while the Cypher's ``r.year`` is an integer property. Every read of it here is
guarded by ``year ~ '^[0-9]{4}$'`` before the cast, the same defensive order
:mod:`api.queries.collaborator_pg_queries` uses on the same JSONB-backed column.

Where a pattern could bind one edge twice
-----------------------------------------
Neo4j applies relationship isomorphism within a ``MATCH``: no relationship may bind to two
of its edge variables. SQL/PGQ's default is walk semantics, so each place the Cypher
relied on that rule needs the constraint written out.

``get_shared_credits`` is the one that needs a guard of its own. Its pattern is
``(p1)-[c1]->(r)<-[c2]-(p2)``, and when ``person1`` and ``person2`` name the *same* person
— which the endpoint accepts — walk semantics let ``c1`` and ``c2`` bind the same edge and
report every release that person is credited on as one they share with themselves. Neo4j
returns nothing. The guard compares the two edges on the columns their key is made of.

Every other pattern here is already closed by a predicate the Cypher carries for its own
reasons: ``get_person_connections`` filters on ``connected.name <> $name`` at depth 1 and
on both ``hop2.name <> $name`` and ``hop2.name <> hop1.name`` at depth 2, and each of the
edge identities walk semantics would otherwise admit implies one of those names is equal
to another. ``tests/test_graph_parity.py`` probes that claim rather than leaving it stated.

Orders Cypher does not define
-----------------------------
``collect(...)`` has no defined order in Cypher, and three results here are built from one:
``get_person_credits``' ``artists`` and ``labels``, ``get_person_profile``' ``categories``,
and ``get_person_connections``' ``second_hops``. This side orders each of them — by name,
or by the sort ``array_agg(DISTINCT ...)`` already performs — because a backend should be
deterministic even where its sibling is not. No parity call reaches a list with more than
one element for exactly that reason; what the two engines *can* be asked about such a list
is how many elements survive the Cypher's ``[..3]`` / ``[..1]`` / ``[..10]`` cap, and
``tests/test_graph_parity.py`` asks them that.

Text ordering is the other half of the same caution. ``ORDER BY c.category, p.name`` is
Unicode-codepoint ordering in Neo4j and database-collation ordering here, so the fixture's
ordered calls are anchored on names the two agree on.

What a binding costs
--------------------
Every vertex binding is an index probe on ``provider_aliases``: ``graph.release``,
``graph.artist`` and ``graph.label`` each publish ``gm_id`` through
``_native_identity_join``, so the join is paid whether or not the query projects it.
``get_person_credits`` binds four vertex labels (person, release, artist, label) across
its three patterns and ``get_person_connections``' two-hop statement binds five, which
makes them the two statements in this family to measure first. ``graph.person`` carries no
such column and costs nothing beyond its own primary key.
"""

from __future__ import annotations

from typing import Any, cast

import structlog
from common.query_debug import execute_sql


logger = structlog.get_logger(__name__)


# One row per (release, role) the person is credited under, with the release's own
# columns beside it. `credit.role_category` is the rename: `c.category` in the Cypher.
#
# `artists` and `labels` are the Cypher's two `OPTIONAL MATCH`es, each walked as its own
# anchored pattern and left-joined back on the release. Anchoring them on the same person
# rather than on `graph.by_artist` at large is what keeps them index-driven: the outer
# join then discards nothing the anchor did not already reach.
PERSON_CREDITS_SQL = """
WITH credited AS (
    SELECT release_id, title, year, role, role_category
    FROM GRAPH_TABLE (graph.catalog
        MATCH (person IS person WHERE person.name = %(name)s)
              -[credit IS credited_on]->(release IS release)
        COLUMNS (
            release.release_id AS release_id,
            release.title AS title,
            release.year AS year,
            credit.role AS role,
            credit.role_category AS role_category
        )
    ) AS credit_row
),
release_artists AS (
    SELECT release_id, (array_agg(DISTINCT artist_name))[1:3] AS artists
    FROM GRAPH_TABLE (graph.catalog
        MATCH (person IS person WHERE person.name = %(name)s)
              -[IS credited_on]->(release IS release)-[IS by_artist]->(artist IS artist)
        WHERE artist.name IS NOT NULL
        COLUMNS (release.release_id AS release_id, artist.name AS artist_name)
    ) AS artist_row
    GROUP BY release_id
),
release_labels AS (
    SELECT release_id, (array_agg(DISTINCT label_name))[1:1] AS labels
    FROM GRAPH_TABLE (graph.catalog
        MATCH (person IS person WHERE person.name = %(name)s)
              -[IS credited_on]->(release IS release)-[IS on_label]->(label IS label)
        WHERE label.name IS NOT NULL
        COLUMNS (release.release_id AS release_id, label.name AS label_name)
    ) AS label_row
    GROUP BY release_id
)
SELECT credited.release_id AS release_id,
       credited.title AS title,
       CASE WHEN credited.year ~ '^[0-9]{4}$' THEN (credited.year)::int END AS year,
       credited.role AS role,
       credited.role_category AS category,
       COALESCE(release_artists.artists, ARRAY[]::text[]) AS artists,
       COALESCE(release_labels.labels, ARRAY[]::text[]) AS labels
FROM credited
LEFT JOIN release_artists ON release_artists.release_id = credited.release_id
LEFT JOIN release_labels ON release_labels.release_id = credited.release_id
ORDER BY year DESC, title
"""

# `WHERE r.year IS NOT NULL`, spelled as the four-digit guard the cast needs anyway. The
# two differ only for a release whose `year` is non-numeric text, which is a document the
# Cypher would have read as an integer and this statement declines to guess at.
#
# `c.category` is `credit.role_category`.
PERSON_TIMELINE_SQL = """
SELECT year, category, count(*)::bigint AS count
FROM (
    SELECT (year_text)::int AS year, role_category AS category
    FROM GRAPH_TABLE (graph.catalog
        MATCH (person IS person WHERE person.name = %(name)s)
              -[credit IS credited_on]->(release IS release)
        WHERE release.year ~ '^[0-9]{4}$'
        COLUMNS (release.year AS year_text, credit.role_category AS role_category)
    ) AS credit_row
) AS dated
GROUP BY year, category
ORDER BY year
"""

# The Cypher's `OPTIONAL MATCH (p)-[:SAME_AS]->(a:Artist)`, as a `LEFT JOIN` against a
# second pattern. The identity side is restricted by a semi-join on the credited people
# rather than by repeating the release pattern inside it: `graph.same_as` is keyed on
# `person_name`, so the restriction is the index lookup either spelling would produce, and
# this one keeps the statement to one pattern per thing it is matching.
#
# A person with two `SAME_AS` artists yields two rows here, exactly as the `OPTIONAL MATCH`
# does — the outer join is not a lookup, and neither backend claims it is.
#
# `c.category` is `credit.role_category`.
RELEASE_CREDITS_SQL = """
WITH credited AS (
    SELECT person_name, role, role_category
    FROM GRAPH_TABLE (graph.catalog
        MATCH (person IS person)
              -[credit IS credited_on]->(release IS release WHERE release.release_id = %(release_id)s)
        COLUMNS (person.name AS person_name, credit.role AS role, credit.role_category AS role_category)
    ) AS credit_row
),
identity AS (
    SELECT person_name, artist_id, artist_name
    FROM GRAPH_TABLE (graph.catalog
        MATCH (person IS person)-[IS same_as]->(artist IS artist)
        COLUMNS (person.name AS person_name, artist.artist_id AS artist_id, artist.name AS artist_name)
    ) AS identity_row
    WHERE person_name IN (SELECT person_name FROM credited)
)
SELECT credited.person_name AS name,
       credited.role AS role,
       credited.role_category AS category,
       identity.artist_id AS artist_id,
       identity.artist_name AS artist_name
FROM credited
LEFT JOIN identity ON identity.person_name = credited.person_name
ORDER BY category, name
"""

# `count(DISTINCT r)`, not `count(*)`: a person credited three times on two releases is a
# leaderboard entry of two. The distinction is invisible on data where nobody holds two
# roles on one release, which is why the parity fixture makes sure somebody does.
#
# `c.category = $category` is `credit.role_category = %(category)s`.
ROLE_LEADERBOARD_SQL = """
SELECT person_name AS name, count(DISTINCT release_id)::bigint AS credit_count
FROM GRAPH_TABLE (graph.catalog
    MATCH (person IS person)-[credit IS credited_on]->(release IS release)
    WHERE credit.role_category = %(category)s
    COLUMNS (person.name AS person_name, release.release_id AS release_id)
) AS credit_row
GROUP BY person_name
ORDER BY credit_count DESC
LIMIT %(limit)s
"""

# Two `credited_on` edges into one release, and the family's one walk-semantics guard.
#
# `graph.credited_on` is keyed `(person_name, release_id, role)`; both edges of this
# pattern already share a release, so they are the same edge exactly when they agree on
# the other two columns. `NOT (... AND ...)` is therefore edge inequality written out in
# properties, and it is the whole difference between Neo4j's answer and a walk's when
# `person1` and `person2` name one person: without it, every release that person is
# credited on is reported as one they share with themselves.
SHARED_CREDITS_SQL = """
WITH shared_releases AS (
    SELECT release_id, title, year, person1_role, person2_role
    FROM GRAPH_TABLE (graph.catalog
        MATCH (person_one IS person WHERE person_one.name = %(person1)s)
              -[credit_one IS credited_on]->(release IS release)
              <-[credit_two IS credited_on]-(person_two IS person WHERE person_two.name = %(person2)s)
        WHERE NOT (credit_one.person_name = credit_two.person_name AND credit_one.role = credit_two.role)
        COLUMNS (
            release.release_id AS release_id,
            release.title AS title,
            release.year AS year,
            credit_one.role AS person1_role,
            credit_two.role AS person2_role
        )
    ) AS shared_row
),
shared_artists AS (
    SELECT release_id, (array_agg(DISTINCT artist_name))[1:3] AS artists
    FROM GRAPH_TABLE (graph.catalog
        MATCH (person_one IS person WHERE person_one.name = %(person1)s)
              -[IS credited_on]->(release IS release)-[IS by_artist]->(artist IS artist)
        WHERE artist.name IS NOT NULL
        COLUMNS (release.release_id AS release_id, artist.name AS artist_name)
    ) AS artist_row
    GROUP BY release_id
)
SELECT shared_releases.release_id AS release_id,
       shared_releases.title AS title,
       CASE WHEN shared_releases.year ~ '^[0-9]{4}$' THEN (shared_releases.year)::int END AS year,
       shared_releases.person1_role AS person1_role,
       shared_releases.person2_role AS person2_role,
       COALESCE(shared_artists.artists, ARRAY[]::text[]) AS artists
FROM shared_releases
LEFT JOIN shared_artists ON shared_artists.release_id = shared_releases.release_id
ORDER BY year DESC
"""

# Depth 1: who shares a release with the anchor, and how many releases they share.
#
# `connected.name <> %(name)s` is the Cypher's own filter, and it is also this pattern's
# walk-semantics guard: the only way one `credited_on` edge could bind to both halves is
# if `connected` were the anchor, which the filter already forbids.
PERSON_CONNECTIONS_SQL = """
SELECT person_name AS name, count(DISTINCT release_id)::bigint AS shared_count
FROM GRAPH_TABLE (graph.catalog
    MATCH (anchor IS person WHERE anchor.name = %(name)s)
          -[IS credited_on]->(release IS release)
          <-[IS credited_on]-(connected IS person)
    WHERE connected.name <> %(name)s
    COLUMNS (connected.name AS person_name, release.release_id AS release_id)
) AS hop
GROUP BY person_name
ORDER BY shared_count DESC
LIMIT %(limit)s
"""

# Depth 2 and up. `depth` selects this statement rather than parameterising a quantifier —
# the Cypher has two fixed variants and `get_person_connections` picks one, so depth 3 is
# depth 2 on both backends.
#
# The Cypher reaches the second hop with a separate `OPTIONAL MATCH` from `hop1`, which is
# why it needs no constraint tying the far release to the near one: relationship
# isomorphism applies within a `MATCH`, not across two, so the far walk may legitimately
# leave by the same edge it arrived on. This side chains all four edges into one pattern,
# which is the same walk, and the three name predicates the Cypher carries are what close
# every edge identity walk semantics would otherwise admit — each of them implies two of
# `anchor`, `bridge`, `reached` are one person, and each of those is already excluded.
#
# `second_hops` is the Cypher's `collect(DISTINCT CASE WHEN hop2 IS NOT NULL THEN ... END)`
# with its nulls dropped and its `[..10]` cap applied. The cap is a `LIMIT` inside the
# lateral rather than a slice outside it, so ten rows are built instead of all of them;
# the order it caps by is this side's own, because the Cypher's `collect` has none.
PERSON_CONNECTIONS_TWO_HOP_SQL = """
WITH direct AS (
    SELECT bridge_name, count(DISTINCT release_id)::bigint AS direct_shared
    FROM GRAPH_TABLE (graph.catalog
        MATCH (anchor IS person WHERE anchor.name = %(name)s)
              -[IS credited_on]->(near IS release)
              <-[IS credited_on]-(bridge IS person)
        WHERE bridge.name <> %(name)s
        COLUMNS (bridge.name AS bridge_name, near.release_id AS release_id)
    ) AS near_hop
    GROUP BY bridge_name
    ORDER BY direct_shared DESC
    LIMIT %(limit)s
),
second_hop AS (
    SELECT bridge_name, person_name, count(DISTINCT release_id)::bigint AS hop2_shared
    FROM GRAPH_TABLE (graph.catalog
        MATCH (anchor IS person WHERE anchor.name = %(name)s)
              -[IS credited_on]->(near IS release)
              <-[IS credited_on]-(bridge IS person)
              -[IS credited_on]->(far IS release)
              <-[IS credited_on]-(reached IS person)
        WHERE bridge.name <> %(name)s
          AND reached.name <> %(name)s
          AND reached.name <> bridge.name
        COLUMNS (bridge.name AS bridge_name, reached.name AS person_name, far.release_id AS release_id)
    ) AS far_hop
    GROUP BY bridge_name, person_name
)
SELECT direct.bridge_name AS name,
       direct.direct_shared AS shared_count,
       COALESCE(capped.second_hops, '[]'::json) AS second_hops
FROM direct
LEFT JOIN LATERAL (
    SELECT json_agg(
               json_build_object('name', reached_rows.person_name, 'via', reached_rows.bridge_name, 'shared', reached_rows.hop2_shared)
               ORDER BY reached_rows.person_name
           ) AS second_hops
    FROM (
        SELECT second_hop.bridge_name, second_hop.person_name, second_hop.hop2_shared
        FROM second_hop
        WHERE second_hop.bridge_name = direct.bridge_name
        ORDER BY second_hop.person_name
        LIMIT 10
    ) AS reached_rows
) AS capped ON TRUE
ORDER BY direct.direct_shared DESC
"""

# `count(c)`, not `count(DISTINCT r)`: the profile counts credits, so a person holding
# three credits across two releases has three. The leaderboard above counts the releases.
#
# `collect(DISTINCT c.category)` is `array_agg(DISTINCT role_category)`, which sorts as
# part of the de-duplication. The Cypher's order is undefined, so this is not a claim that
# the two agree on it — see the module docstring.
#
# No `ORDER BY` and no `LIMIT`: `run_single` takes the first record of an unordered result
# when a person has two `SAME_AS` artists, and this statement leaves the same thing
# unordered rather than quietly deciding it.
#
# `c.category` is `credit.role_category`.
PERSON_PROFILE_SQL = """
WITH credited AS (
    SELECT person_name,
           role_category,
           CASE WHEN year_text ~ '^[0-9]{4}$' THEN (year_text)::int END AS year
    FROM GRAPH_TABLE (graph.catalog
        MATCH (person IS person WHERE person.name = %(name)s)
              -[credit IS credited_on]->(release IS release)
        COLUMNS (
            person.name AS person_name,
            credit.role_category AS role_category,
            release.year AS year_text
        )
    ) AS credit_row
),
summary AS (
    SELECT person_name,
           count(*)::bigint AS total_credits,
           array_agg(DISTINCT role_category) AS categories,
           min(year) AS first_year,
           max(year) AS last_year
    FROM credited
    GROUP BY person_name
),
identity AS (
    SELECT person_name, artist_id, artist_name
    FROM GRAPH_TABLE (graph.catalog
        MATCH (person IS person WHERE person.name = %(name)s)-[IS same_as]->(artist IS artist)
        COLUMNS (person.name AS person_name, artist.artist_id AS artist_id, artist.name AS artist_name)
    ) AS identity_row
)
SELECT summary.person_name AS name,
       summary.total_credits AS total_credits,
       summary.categories AS categories,
       summary.first_year AS first_year,
       summary.last_year AS last_year,
       identity.artist_id AS artist_id,
       identity.artist_name AS artist_name
FROM summary
LEFT JOIN identity ON identity.person_name = summary.person_name
"""

# `count(*)` per category — credits, not releases, which is what makes this the breakdown
# of the profile's `total_credits` rather than of the leaderboard's `credit_count`.
#
# The release is bound and never projected because the Cypher binds it: a `CREDITED_ON`
# edge whose release has been deleted is not a credit on either side. It costs the one
# `gm_id` probe the module docstring describes.
#
# `c.category` is `credit.role_category`.
PERSON_ROLE_BREAKDOWN_SQL = """
SELECT role_category AS category, count(*)::bigint AS count
FROM GRAPH_TABLE (graph.catalog
    MATCH (person IS person WHERE person.name = %(name)s)
          -[credit IS credited_on]->(release IS release)
    COLUMNS (credit.role_category AS role_category)
) AS credit_row
GROUP BY role_category
ORDER BY count(*) DESC
"""


async def _rows(pool: Any, sql: str, params: dict[str, Any]) -> list[tuple[Any, ...]]:
    """Run one statement and return its rows."""
    async with pool.connection() as conn, conn.cursor() as cursor_cm:
        cursor = cast("Any", cursor_cm)
        await execute_sql(cursor, sql, params)
        return await cursor.fetchall()


async def get_person_credits(pool: Any, name: str) -> list[dict[str, Any]]:
    """Return all releases a person is credited on, grouped by role.

    Mirrors :func:`api.queries.credits_queries.get_person_credits` column for column,
    including its ``category`` column, which reads ``graph.credited_on.role_category``.
    """
    rows = await _rows(pool, PERSON_CREDITS_SQL, {"name": name})
    credits = [
        {
            "release_id": row[0],
            "title": row[1],
            "year": row[2],
            "role": row[3],
            "category": row[4],
            "artists": row[5],
            "labels": row[6],
        }
        for row in rows
    ]
    logger.debug("🔍 Person credits resolved", person=name, count=len(credits))
    return credits


async def get_person_timeline(pool: Any, name: str) -> list[dict[str, Any]]:
    """Return year-by-year credit activity for a person.

    Mirrors :func:`api.queries.credits_queries.get_person_timeline` column for column.
    """
    rows = await _rows(pool, PERSON_TIMELINE_SQL, {"name": name})
    return [{"year": row[0], "category": row[1], "count": row[2]} for row in rows]


async def get_release_credits(pool: Any, release_id: str) -> list[dict[str, Any]]:
    """Return the full credits breakdown for a release.

    Mirrors :func:`api.queries.credits_queries.get_release_credits` column for column,
    including the null ``artist_id``/``artist_name`` its ``OPTIONAL MATCH`` produces for a
    person with no ``SAME_AS`` artist.
    """
    rows = await _rows(pool, RELEASE_CREDITS_SQL, {"release_id": release_id})
    return [{"name": row[0], "role": row[1], "category": row[2], "artist_id": row[3], "artist_name": row[4]} for row in rows]


async def get_role_leaderboard(pool: Any, category: str, limit: int = 20) -> list[dict[str, Any]]:
    """Return the most prolific people in a given role category.

    Mirrors :func:`api.queries.credits_queries.get_role_leaderboard` column for column,
    including its default ``limit``. ``category`` is matched against
    ``graph.credited_on.role_category``.
    """
    rows = await _rows(pool, ROLE_LEADERBOARD_SQL, {"category": category, "limit": limit})
    return [{"name": row[0], "credit_count": row[1]} for row in rows]


async def get_shared_credits(pool: Any, person1: str, person2: str) -> list[dict[str, Any]]:
    """Find releases where two people are both credited.

    Mirrors :func:`api.queries.credits_queries.get_shared_credits` column for column,
    including the empty result Neo4j's relationship isomorphism produces when the two
    names are the same person.
    """
    rows = await _rows(pool, SHARED_CREDITS_SQL, {"person1": person1, "person2": person2})
    return [
        {
            "release_id": row[0],
            "title": row[1],
            "year": row[2],
            "person1_role": row[3],
            "person2_role": row[4],
            "artists": row[5],
        }
        for row in rows
    ]


async def get_person_connections(pool: Any, name: str, depth: int = 2, limit: int = 50) -> list[dict[str, Any]]:
    """Find people connected through shared releases.

    Mirrors :func:`api.queries.credits_queries.get_person_connections`, including its
    clamping of ``depth`` into ``[1, 3]`` and its two fixed variants: depth 1 returns
    ``name``/``shared_count``, and depth 2 — which depth 3 also selects — adds
    ``second_hops``.
    """
    if depth < 1:
        depth = 1
    if depth > 3:
        depth = 3

    if depth == 1:
        rows = await _rows(pool, PERSON_CONNECTIONS_SQL, {"name": name, "limit": limit})
        return [{"name": row[0], "shared_count": row[1]} for row in rows]

    rows = await _rows(pool, PERSON_CONNECTIONS_TWO_HOP_SQL, {"name": name, "limit": limit})
    return [{"name": row[0], "shared_count": row[1], "second_hops": row[2]} for row in rows]


async def get_person_profile(pool: Any, name: str) -> dict[str, Any] | None:
    """Return a summary profile for a person, or ``None`` when they hold no credits.

    Mirrors :func:`api.queries.credits_queries.get_person_profile` column for column. A
    person with no ``CREDITED_ON`` edge matches nothing on either backend, which is the
    ``None`` the router turns into a 404 — the profile is of a person's credits, and a
    ``:Person`` vertex without any is not something either store holds.
    """
    rows = await _rows(pool, PERSON_PROFILE_SQL, {"name": name})
    if not rows:
        return None
    row = rows[0]
    return {
        "name": row[0],
        "total_credits": row[1],
        "categories": row[2],
        "first_year": row[3],
        "last_year": row[4],
        "artist_id": row[5],
        "artist_name": row[6],
    }


async def get_person_role_breakdown(pool: Any, name: str) -> list[dict[str, Any]]:
    """Return the count of credits per role category for a person.

    Mirrors :func:`api.queries.credits_queries.get_person_role_breakdown` column for
    column; its ``category`` column reads ``graph.credited_on.role_category``.
    """
    rows = await _rows(pool, PERSON_ROLE_BREAKDOWN_SQL, {"name": name})
    return [{"category": row[0], "count": row[1]} for row in rows]
