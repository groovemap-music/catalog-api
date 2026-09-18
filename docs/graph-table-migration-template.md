# The GRAPH_TABLE migration template

The collaborators family is the first Neo4j query family answered by PostgreSQL. It exists to
be copied: ADR 0012 migrates the graph reads family by family, and this page is the worked
example the next family starts from. The code is
[`api/queries/network_pg_queries.py`](../api/queries/network_pg_queries.py), beside the Cypher
it replaces in [`api/queries/network_queries.py`](../api/queries/network_queries.py).

## What is being replaced

Three functions back `GET /api/network/artist/{id}/collaborators`:

| Function | Answers |
| --- | --- |
| `get_artist_identity` | Does this artist exist, and what is its name? |
| `get_multi_hop_collaborators` | Who has this artist recorded with, directly or one artist removed? |
| `count_multi_hop_collaborators` | How many such collaborators are there? |

The router calls whichever module the graph-backend selector resolves for `GRAPH_BACKEND`, so
both implementations must return the same columns, the same types, and the same order. See
[Configuration](configuration.md#connections-and-pools).

## The graph being queried

`graph.catalog` is declared by the `groovemap-database-schema` initializer, pinned in
`pyproject.toml`. It is a SQL/PGQ declaration over the `graph` schema's views — nothing is
materialized and no base table changes — and it is **conditional**: it exists only on a
PostgreSQL 19 server whose initializer ran with `SCHEMA_PROPERTY_GRAPH` enabled. The
persistence contract tells consumers to probe rather than assume, which is what
`verify_postgres_graph_backend` does at startup.

Labels are the view names verbatim, which is the whole point of the de-reserving rule ADR 0012
records: `:Artist` is `graph.artist` is `(a IS artist)`, and the overloaded `[:BY]` out of a
release is `graph.by_artist` is `-[IS by_artist]->`. There is no second table to consult.

The one edge this family walks, exactly as the producer renders it:

```sql
graph.by_artist AS by_artist KEY (release_id, artist_id)
    SOURCE KEY (release_id) REFERENCES release (release_key)
    DESTINATION KEY (artist_id) REFERENCES artist (artist_key)
    LABEL by_artist PROPERTIES ALL COLUMNS
```

`release_key` and `artist_key` are `text` restatements appended to the four Discogs vertex
views because PostgreSQL 19 beta 3 refuses a `character varying` vertex key. They are
structural: no query names them, and `artist_id` stays the property, unified on `text` by the
vertex declaration (`artist_id::text AS artist_id`). The edge is directed release → artist, so
reaching a collaborator means traversing it backwards and then forwards.

## The four rules

1. **Pattern matching replaces Cypher, not SQL.** Every traversal is a `GRAPH_TABLE` over
   `graph.catalog`; grouping, the anti-join, ordering, and the limit stay ordinary SQL around
   it. `GRAPH_TABLE` is a table expression, so it composes with CTEs and subqueries like any
   other relation.
2. **One constant per query.** Each statement is a module-level string, as in
   `api/queries/insights_pg_queries.py`, so the SQL a reviewer reads is the SQL the server
   runs.
3. **Every value is a parameter**, including inside the graph pattern — PostgreSQL 19 accepts a
   parameter in an element pattern's `WHERE` like any other expression.
4. **Parity is column-for-column**, because the router swaps the modules underneath an unchanged
   response schema.

## Depth 1

```sql
SELECT collaborator_id, collaborator_name, release_id
FROM GRAPH_TABLE (graph.catalog
    MATCH (anchor IS artist WHERE anchor.artist_id = %(artist_id)s)
          <-[IS by_artist]-(credit IS release)-[IS by_artist]->(peer IS artist)
    WHERE peer.artist_id <> anchor.artist_id
    COLUMNS (
        peer.artist_id AS collaborator_id,
        peer.name AS collaborator_name,
        credit.release_id AS release_id
    )
) AS hop
```

One row per (collaborator, shared release), so `COUNT(DISTINCT release_id)` outside is the
Cypher's `count(DISTINCT nodes(path)[1])`.

## Depth 2, and the constraint the engines do not share

This is the part that does not transcribe mechanically. Neo4j applies **relationship
isomorphism** to a `MATCH` path: no relationship may bind twice, which silently forbids the
two-hop pattern from walking back down the release it arrived on, and forbids the bridge from
being the peer. SQL/PGQ's default is **walk semantics** — the same edge may bind to two edge
patterns — so every constraint Neo4j derives for free has to be written out:

```sql
MATCH (anchor IS artist WHERE anchor.artist_id = %(artist_id)s)
      <-[IS by_artist]-(near IS release)-[IS by_artist]->(bridge IS artist)
      <-[IS by_artist]-(far IS release)-[IS by_artist]->(peer IS artist)
WHERE bridge.artist_id <> anchor.artist_id
  AND peer.artist_id <> anchor.artist_id
  AND peer.artist_id <> bridge.artist_id
  AND far.release_id <> near.release_id
```

Those four predicates are what make the PostgreSQL result set equal to the Cypher one rather
than a superset of it. **Check them first when a migrated family disagrees with its Cypher.**

The hops are chained explicitly rather than reached with a quantifier, which is what makes each
of the four edges individually constrainable.

## The anti-join

A depth-1 collaborator must never also be reported at depth 2. The Cypher says
`NOT EXISTS { MATCH (a)<-[:BY]-(:Release)-[:BY]->(hop2) }`; the SQL says the same thing with a
second `GRAPH_TABLE` walking the depth-1 pattern again:

```sql
WHERE %(depth)s >= 2
  AND NOT EXISTS (
        SELECT 1
        FROM GRAPH_TABLE (graph.catalog
            MATCH (anchor IS artist WHERE anchor.artist_id = %(artist_id)s)
                  <-[IS by_artist]-(credit IS release)-[IS by_artist]->(peer IS artist)
            COLUMNS (peer.artist_id AS collaborator_id)
        ) AS one_hop
        WHERE one_hop.collaborator_id = indirect.collaborator_id
      )
```

It is deliberately a second pattern rather than a reference back to the depth-1 CTE: the two
express different things — reachability versus the aggregated result set — and only the
subquery form stays correct if the depth-1 projection later grows a filter the exclusion must
not inherit. Nothing inside depends on the outer row except the final equality, so the planner
hashes it once per statement.

`%(depth)s >= 2` gates the whole two-hop branch as a one-time filter: at depth 1 the subplan is
never executed. Depth 3 is accepted by the endpoint and behaves as depth 2, exactly as the
Cypher does.

## Types

`sum()` and `count()` over `bigint` yield `numeric`, which psycopg returns as `Decimal`. The
Cypher returns a Python `int` and the response schema says integer, so every aggregate is cast:
`count(DISTINCT release_id)::bigint`, `count(*)::bigint`. Forgetting this is the easiest way to
produce a response that validates locally and serializes differently in production.

## Proving parity

`just test-integration-pg19` starts the digest-pinned `postgres:19beta3-alpine` image beside the
usual Neo4j container, applies the schema initializer with `SCHEMA_PROPERTY_GRAPH=enabled`, and
runs [`tests/test_graph_parity.py`](../tests/test_graph_parity.py), which seeds one fixture into
both engines and compares their results to each other rather than to a hand-written
expectation. It is opt-in and not part of `just check`: PostgreSQL 19 is an advisory tier, and
the required tier is 18, where the property graph is deliberately absent.

The fixture is free of ties on `(distance, collaboration_count)` when read from the anchor.
Both implementations order by exactly those two keys and neither adds a tiebreaker, so a tie
would make row order legitimately unspecified on both sides and the comparison would be testing
the planners instead of the queries.

Query shape — that values are bound, that the traversal is `GRAPH_TABLE` over `graph.catalog`,
that the anti-join is a `NOT EXISTS` over a second one — is covered without a server in
[`tests/test_network_pg_queries.py`](../tests/test_network_pg_queries.py).

## Migrating the next family

1. Write `api/queries/<family>_pg_queries.py` with one module-level constant per statement.
2. Add the module to its family's mapping in `api/graph_backend.py`, and bind it to that
   family's `Protocol` beside the existing backend — that binding is where mypy checks the two
   implementations still have the same signatures.
3. Give the router the handle the new backend needs, the way `configure(..., pg_pool=...)` does
   for this one.
4. Add query-shape unit tests with the fake pool in `tests/fake_postgres.py`.
5. Extend `tests/test_graph_parity.py` with a fixture that is unambiguous under the family's
   own ordering, and run `just test-integration-pg19`.
