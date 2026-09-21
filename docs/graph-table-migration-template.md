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

## Proving parity: the harness

Parity is the gate every family crosses on, so there is one harness for all of them rather than
one test module per family. It lives in
[`tests/test_real_databases.py`](../tests/test_real_databases.py) and **registering a family with
it is step one of migrating that family** — before the SQL exists, not after. A registration is
one line:

```python
register_parity_family("collaborators", COLLABORATORS_CALLS)
```

`COLLABORATORS_CALLS` is a tuple of `ParityCall(function, args, kwargs)` — one per question the
family should be asked. For each call the harness resolves both backends through
`api/graph_backend.py` (the same selector the router uses, so a family cannot be proven at parity
against a module the router would never call), runs the call on each, and asserts the two results
are equal in **rows, order, Python types, and column order**. It compares the two engines to each
other, never to a hand-written expectation: an expectation only proves both halves agree with
whatever the author believed.

Three things follow from registering first:

- The calls are the family's specification. A registered, failing family is a migration with a
  finish line; an unregistered one is a migration with an opinion.
- `test_every_function_of_a_registered_family_is_covered_by_a_parity_call` reads the family's
  `Protocol` from `api/graph_backend.py` and fails if a function the router can reach has no
  call. Forgetting one is otherwise silent — the suite goes green having never run it.
- A family that needs `graph.catalog` keeps the default `requires_property_graph=True`, and its
  calls then run only on the PostgreSQL 19 tier and skip elsewhere. A family whose PostgreSQL
  side is ordinary SQL over the `graph` views passes `requires_property_graph=False` and runs on
  every tier.

### Declaring an expected difference

`EXPECTED_DIFFERENCES` is a plain mapping in the same module. A `(family, function)` key
applies to every call of that function:

```python
("label_dna", "get_label_profile"): ExpectedDifference(
    reason="Neo4j returns float scores; the SQL sums numeric and rounds at 6 places",
    normalize=lambda rows: [{**row, "score": round(row["score"], 6)} for row in rows],
),
```

`normalize` is applied to *both* results and they must then agree, so a declaration says how much
is tolerated instead of switching the assertion off. The harness fails on any divergence that is
not declared — and names the mapping when it does — and it also fails on a declaration whose
difference did not materialise, so a tolerance cannot outlive the behaviour it was granted for.
The collaborators family reaches column-for-column agreement with no entry, which is the bar.
When only one fixture question differs, key the entry by `(family, str(call))` instead. The
rendered-call key is checked first, so an accepted non-empty divergence cannot accidentally
tolerate an empty or filtering call that should still agree exactly.

### The fixture

Both engines are seeded from [`tests/graph_fixture.py`](../tests/graph_fixture.py), which holds
one graph in two disconnected components. The ordering component is free of ties on
`(distance, collaboration_count)` read from its anchor: both implementations order by exactly
those two keys and neither adds a tiebreaker, so a tie would make row order legitimately
unspecified on both sides and the comparison would be testing the planners instead of the
queries. Give a new family a component with the same property under *its* ordering.

The predicate component exists for the section above: it carries a release crediting three
artists, which is what lets a walk turn around inside a release and reach somebody.

### Running it

`just test-integration-pg19` starts the digest-pinned `postgres:19beta3-alpine` image beside the
usual Neo4j container, applies the schema initializer with `SCHEMA_PROPERTY_GRAPH=enabled`, and
runs the integration suite together with
[`tests/test_graph_parity.py`](../tests/test_graph_parity.py). It is opt-in and not part of
`just check`: PostgreSQL 19 is an advisory tier, and the required tier is 18, where the property
graph is deliberately absent and the property-graph families skip.

`tests/test_graph_parity.py` is the part that is about the SQL rather than about the family: that
`graph.catalog` is really declared, what the fixture's answer actually is, and what each
no-revisit predicate is holding back. Query shape — that values are bound, that the traversal is
`GRAPH_TABLE` over `graph.catalog`, that the anti-join is a `NOT EXISTS` over a second one — is
covered without a server in
[`tests/test_network_pg_queries.py`](../tests/test_network_pg_queries.py).

The harness's own decisions — what counts as a divergence, what a declared difference buys — are
covered in [`tests/test_parity_harness.py`](../tests/test_parity_harness.py), which needs no
engine and runs in `just test`. Change `assert_parity` and that is the suite to run.

### What the harness cannot see

In the assembled statement the anti-join absorbs all four no-revisit predicates: every walk they
forbid ends at an artist who shares a release with the anchor, and the anti-join drops exactly
those. Delete one and the statement is still at parity. So the predicates are probed one level
down, on the two-hop pattern alone, against the same walk in Cypher — which is why the fixture
needs a three-credit release, and why `test_the_anti_join_absorbs_a_dropped_predicate_in_the_assembled_statement`
exists to pin the masking as a fact instead of leaving it to be rediscovered. Expect the same
shape of problem in the next family: a guard that a later clause makes redundant is still worth
writing, but it has to be tested where it acts.

## The family that is not a traversal: trigram autocomplete

The second family migrated is **autocomplete**, and it is worth reading beside the pilot
because almost none of the above applies to it. The Cypher coverage spike
(`gm-database-schema-9c8.2`) found six functions that do not traverse at all — the four
autocompletes in `api/queries/neo4j_queries.py`, the person search in
`api/queries/credits_queries.py`, and the `_autocomplete` engine they share — and classifies
them **SQL-only (no graph)**. They call Neo4j's Lucene full-text indexes. `GRAPH_TABLE` has
nothing to say about them, so the PostgreSQL side is
[`api/queries/autocomplete_pg_queries.py`](../api/queries/autocomplete_pg_queries.py): five
plain statements, one per relation, each reading one relation and ranking it.

Three consequences worth carrying to the next non-traversal family:

- **It registers `requires_property_graph=False`.** The `graph` relations are
  unconditional; only `graph.catalog` is conditional. So the family's parity calls run on
  the required PostgreSQL 18 tier as well as on 19, and `just test-integration` covers it.
- **The Neo4j side needed a module of its own.** The family spans two Cypher modules and the
  seam resolves a family to exactly one, so
  [`api/queries/autocomplete_queries.py`](../api/queries/autocomplete_queries.py) gathers
  them. It delegates by attribute at call time rather than re-exporting, because a
  `from ... import` binds a second name that a patch of the original never reaches — and
  patchability through the selector is a property the seam promises.
- **Three of its five relations had to stop being views.** `graph.genre`, `graph.style`, and
  `graph.person` are name-keyed tables carrying `GIN (name gin_trgm_ops)` from the phase 2
  schema revision, because a view cannot hold an index. `graph.artist` and `graph.label` are
  still views: the same predicate runs and the server scans.

### What Lucene was doing, and what replaces it

`_escape_lucene_query` exists because the query string reaches a **parser**. A bare `/`,
`~`, `:` or `(` in a name is Lucene syntax, and the one call site that forgot to escape
returned an unhandled 500 on names like `AC/DC`. Nothing on the PostgreSQL side has a
parser: the string is bound as a value three times over — a `LIKE` pattern, a
regular-expression pattern, and the right operand of `similarity()` — and a value cannot
become syntax. That is the bug class the move retires.

Lucene's matching rule, via `_build_autocomplete_query`, is one wildcard term per whitespace
term, ANDed: `post roc` becomes `post* AND roc*`. The analyzer has already split each name
into tokens, so it means **every query term is a prefix of some word in the name**. Two
predicates reproduce it:

```sql
WHERE candidate.name ILIKE %(contains)s
  AND candidate.name ~* ALL (%(prefixes)s::text[])
```

The second is the rule. Each array element is `\m` — the start-of-word constraint — followed
by the escaped term, ANDed exactly as Lucene ANDs the wildcards. Punctuation is a word
boundary to both engines, which is why `dc` still finds `AC/DC`.

The first is a **superset** of the second, and is there only so the trigram index can drive
the scan: a term that begins a word in the name is certainly a substring of it, so the
filter cannot drop a row. `ALL (...)` over an array is not an indexable operator; a plain
`ILIKE` is. The pattern is built from the longest term, which carries the most trigrams.

### The ordering rule, and why it is declared rather than matched

Lucene returns a relevance `score` and the Cypher orders by it. **It cannot be reproduced.**
It is a function of the index's term statistics and of how the wildcard query was rewritten,
and neither exists in PostgreSQL. So this backend publishes a different number in the same
column and orders by it:

```sql
ORDER BY similarity(name, <query>) DESC, name ASC
```

`similarity()` is the trigram overlap of the whole query against the whole name — a number
in `[0, 1]`, not a relevance score. `name ASC` is a tiebreaker the Cypher does not have,
which makes this side's order total where Lucene's is arbitrary among equal scores.

That is the family's declared difference, and it is the reason `EXPECTED_DIFFERENCES` is no
longer empty:

```python
ExpectedDifference(
    reason="Neo4j ranks by Lucene relevance; PostgreSQL has no such number and returns "
    "pg_trgm similarity in the same column, ordered by it and then by name",
    normalize=_rank_free,
)
```

`_rank_free` sorts both results by name and replaces the score with **the name of its
type**. That tolerates exactly two things — the score's value and the order it drives — and
nothing else: the rows, the `id` and `name` values, the column set and the column order are
all still compared, and a backend returning `Decimal` where the other returns `float` still
fails. A tolerance that replaced the score with a constant would have hidden that.

Two rules follow for a family with a declared difference, and both bite:

1. **Every registered call must actually diverge.** The harness fails a declaration whose
   difference did not materialise, and two empty results agree — so a query that matches
   nothing cannot be a parity call. Cover it on one engine instead.
2. **Row sets must still agree.** A declared difference normalises; it does not excuse a
   missing row. Inputs where Lucene returns something else entirely — `AC/DC`, a quoted
   nickname — are therefore not parity calls either. They are asserted directly, in
   `test_trigram_autocomplete_answers_the_inputs_lucene_mishandled`, which fails if Neo4j
   ever starts agreeing.

An apostrophe turned out **not** to be in that set: Lucene's tokenizer keeps `O'Connor` as
one token, so both engines answer and `Sinéad O'Connor` is a parity call. It is worth one
anyway, for the character a hand-built SQL string would have broken on.

## The family that is a walk: the rarity signal batch

The third family migrated is **rarity**, and it is worth reading beside both of the above
because it is neither one question nor a set of independent ones. It is a *walk*: a keyset page
of release ids, nine page-scoped signal queries against that page, a join, a scoring pass, and
a second pass for percentile ranks over the global distributions. Eleven Cypher constants and
six driver functions, in [`api/queries/rarity_queries.py`](../api/queries/rarity_queries.py)
and [`api/rarity/families/grooved.py`](../api/rarity/families/grooved.py); the PostgreSQL side
is [`api/queries/rarity_pg_queries.py`](../api/queries/rarity_pg_queries.py).

| Function | Answers |
| --- | --- |
| `fetch_release_id_page` | Which release ids come after this cursor? |
| `fetch_page_signals` | Every core and family signal, for one page of ids. |
| `count_releases` | How many releases are there, for the walk's coverage check? |
| `fetch_all_rarity_signals` | The whole batch: every release, scored. |
| `get_rarity_by_artist` | The rarest releases credited to this artist. |
| `get_rarity_by_label` | The rarest releases on this label. |

### Only the reads are the family

Everything after the reads — the join, the composition through `api/rarity`, the percentile
pass, the coverage warning — is arithmetic over rows and is identical whichever engine produced
them. Duplicating it into the second backend would have been about 150 lines of the most
load-bearing code in the module, twice. It lives in
[`api/queries/rarity_pipeline.py`](../api/queries/rarity_pipeline.py) instead, and each backend
binds it to three reads. **Split the walk from the reads before writing the second one.** A
family whose PostgreSQL module is a transcription of its Cypher module is one that will drift.

### The chunking contract, in SQL

This is the part to get right before anything else. The signal queries once ran as eight
unbounded `MATCH (r:Release)` scans; on the production graph Neo4j killed the transaction at
`db.transaction.timeout` every time, and `release_rarity` failed on 33 consecutive daily cycles
(2026-06-22 → 2026-07-23). Every query is now bound to an explicit page, and
`UNWIND $ids AS rid / MATCH (r:Release {id: rid})` becomes:

```sql
MATCH (r IS release WHERE r.release_id = ANY(%(ids)s))
```

**Inside the element pattern.** A `GRAPH_TABLE` that matched every release and was filtered by
a surrounding `WHERE` returns exactly the same rows and traverses the whole catalog to do it,
which is the outage reintroduced silently. `tests/test_rarity_pg_queries.py` pins it: every
signal statement binds the page, no `MATCH` anchors on an unbound vertex, no read of
`graph.release` is unbounded, and no signal statement carries a `LIMIT` of its own — paging
belongs to the keyset walk, and a signal query that also paged would drop releases the walk had
already committed to scoring.

The per-query budget comes across too, as a `statement_timeout`. `SET statement_timeout = %s`
is not available — `SET` is a utility statement and the server rejects a bind parameter in it —
so it is `SELECT set_config('statement_timeout', %(timeout_ms)s, false)`, which is an ordinary
expression and keeps rule 3. The pool hands out autocommit connections, so `SET LOCAL` would be
a no-op and the setting is reset explicitly before the connection goes back.

### Counters are read, never recomputed

Four signals read a node property the graph enricher writes in a post-import pass:
`Label.release_count`, `Genre.release_count`, and the two unbound degrees `COUNT { (a)--() }`
and `COUNT { (r)--() }`. Re-aggregating those on request is the same failure the chunking
contract exists to prevent, so they stay property reads. The phase 2 schema revision makes
`genre`, `style`, `label`, and `artist` bind a `<label>_vertex` projection joining the storage
relation to its counter relation, so `l.release_count` and `a.degree` read exactly as the Cypher
reads them, and the LEFT JOIN is free when no counter is named. A counter the loader has never
computed reads `0` and not null, because the projection coalesces it.

`graph.release_degree` is the exception and the one spelling to carry forward:

```sql
MATCH (d IS release_degree WHERE d.release_id = ANY(%(ids)s))
```

It is a label of its own because its live half is a pair of lateral counts over
`user_collections` and `user_wantlists` that no unique key makes removable; folding it onto the
`release` vertex would make every traversal that binds a release pay for them. Its value is the
loader's catalog-edge count plus those two counts, which is what `COUNT { (r)--() }` counts.
One bound follows and is worth knowing: `graph.release_degree_base` is grouped over the edge
tables, so a release with no catalog edge at all has no row in it and reads 0 here where Neo4j
would still count a `COLLECTED` edge. Every release a loader ingests has at least one edge, so
that is an empty catalog entry rather than a live one.

### The family's handle is not a connection

Every family above this one is called with its backend's own connection. This one cannot be.
The batch reads the community-counts table and the two lookups read the stored rarity table,
and those are PostgreSQL relations on **both** backends, because ADR 0012 migrates the graph
reads and not the results table. So the Neo4j backend needs two connections where the PostgreSQL
backend needs one, and a family whose two implementations took different argument counts could
not be bound to one `Protocol`. Both take a `RarityHandles` carrying the graph connection and
the results-store pool;
`api/graph_backend.py`'s `rarity_handles()` is what a router uses to build it, and
`register_parity_family(..., handle=...)` is how the harness is told.

That is also what makes the two lookups collapse. On Neo4j each is four round trips — does the
vertex exist, which releases hang off it, one page of stored rarity rows, the count —
because nothing could join across two databases. On PostgreSQL both fields are the same pool
and each is one statement. The shape worth copying is how it keeps three answers apart when the
page is empty: a one-row `summary` carrying `EXISTS (...) AS vertex_exists` and the total, with
the page `LEFT JOIN LATERAL`ed onto it, so the statement always returns at least one row.
`vertex_exists` false is the 404; true with `total` 0 is a vertex with nothing scored; true with
rows is a page. A plain `SELECT ... LIMIT` collapses the first two into zero rows.

### Two orderings, and one Cypher change, that parity forced

Neither engine promises an order for an aggregated row set, and the harness compares row order.
Three things had to become deterministic, and none of them is in the SQL:

- **The scoring loop walks the page's id list**, not the release query's row order. The ids are
  ordered by both engines; an aggregate is ordered by neither.
- **`rows_by_release_id`** is what every backend's `fetch_page_signals` returns its rows
  through. The join indexes by id and never cared, so this costs a sort per page and changes no
  result.
- **`artist_name` is `min(a.name)`**, where the Cypher was `collect(DISTINCT a.name)[0]`. The
  `[0]` took an arbitrary element of an unordered collect, so a release with more than one
  credit had no defined display name — the value depended on the order Neo4j's expand returned
  the `BY` edges in, and no SQL spelling can reproduce that. Both sides pin `COLLATE "C"` where
  they compare names or release ids, including the keyset page boundary: Neo4j orders strings by
  code point and PostgreSQL by the database collation, and Discogs ids being digit strings is
  exactly the kind of thing that holds until it does not.

`graph.release.year` is one more asymmetry, and the same one
`api/queries/neo4j_pg_queries.py` already documents: it is text off the Discogs document where
Neo4j's is an integer property, so every read of it filters `btrim(year) ~ '^[0-9]{4}$'` before
the cast rather than failing the whole aggregate on the first bad value.

**The family has no `EXPECTED_DIFFERENCES` entry.** All twenty-two parity calls agree
column-for-column, which is the bar the collaborators pilot set.

### What the fixture had to become

This is the first family that needs the fixture to be *complete* rather than sufficient for one
traversal, because the batch reads every release there is and asks for its degree. A genre,
style, or credit edge that a document implied and only PostgreSQL ever projected is a
divergence, whatever family seeded the document. So `tests/graph_fixture.py` now projects both
engines from one `release_documents()` builder, and the Neo4j side gained the `IS` and
`CREDITED_ON` edges, the titles, and the `formats` and `media_families` list properties it never
had. The counters the graph enricher writes as node properties are computed from the same
documents
and set on the Neo4j nodes, because that is what `graph.bootstrap_fill()` derives on the other
side; the degrees are not, because Neo4j counts those live and matching edges are what makes the
two agree.

The rarity component itself (ids 701+) is sized by what the signals need: a label on four
releases, a genre on three, a master with three pressings and a second master with exactly one —
the `groovemap-cu2.75` case, where a unique pressing must score 100.0 and not the 90.0 of a
release with no master link — a standalone release with no master, a canonical `vinyl_12` medium
so the grooved family actually applies, one release credited to two artists so
`artist_name` has something to choose between, and one collection row and one wantlist row,
which is the only way `graph.release_degree`'s live half is exercised at all. Names are chosen
clear of every autocomplete query prefix: a row matching `radio`, `warp`, `roc`, `elec`, `ambi`,
`tech`, `bob`, or `chuck` would change that family's answer on both engines, and the harness
would be proving the fixture rather than the SQL.

### Timing on the fixture

Measured over `tests/graph_fixture.py` on the `just test-integration-pg19` containers —
`postgres:19beta3-alpine` and `neo4j:2026-community`, both on one laptop — with seven
alternating runs per backend and the median reported. Both backends score the same 22 releases.

| Page size | Pages | Neo4j median | PostgreSQL median |
| --- | --- | --- | --- |
| 20,000 (production) | 1 | 88.8 ms | 42.7 ms |
| 4 | 6 | 283.2 ms | 92.5 ms |

Read the second row, not the first. A 22-release fixture in a single page measures almost
nothing but connection overhead, and neither number says anything about a catalog-scale run:
the PostgreSQL signal statements read `graph.release`, which is still a view over
`public.releases`, so the keyset walk there is a sequential scan per page where Neo4j's is an
index seek. What the six-page row does show is the per-page cost, which is the axis the outage
was on — the batch's whole shape is a fixed number of round trips per page, and PostgreSQL is
issuing its eleven statements over three pooled connections against Neo4j's eleven sessions.
The ratio between the two rows is the number worth watching when the catalog-scale run is
timed: Neo4j's batch cost rose 3.2x going from one page to six, PostgreSQL's 2.2x.

## Migrating the next family

1. Register the family with the parity harness in `tests/test_real_databases.py` — one
   `register_parity_family` line and the calls it should be asked — and extend
   `tests/graph_fixture.py` with a component that is unambiguous under the family's own ordering.
   The calls fail until step 2 lands; that is the point.
2. Write `api/queries/<family>_pg_queries.py` with one module-level constant per statement.
3. Add the module to its family's mapping in `api/graph_backend.py`, and bind it to that
   family's `Protocol` beside the existing backend — that binding is where mypy checks the two
   implementations still have the same signatures. Add the protocol to `FAMILY_PROTOCOLS` in the
   harness so the coverage check can read it.
4. Give the router the handle the new backend needs, the way `configure(..., pg_pool=...)` does
   for this one.
5. Add query-shape unit tests with the fake pool in `tests/fake_postgres.py`.
6. Run `just test-integration-pg19`.

If the family's two backends need different connections — as rarity's do, because its results
table stayed in PostgreSQL — give it one handle object carrying both rather than two arguments,
and register it with `register_parity_family(..., handle=...)`. A `Protocol` cannot bind two
implementations whose functions take different argument counts, and that binding is the whole
signature check.
