# Authenticated transient Missing preview

`POST /api/user/missing/preview` accepts a first-party access-session Bearer token,
using the existing `require_user` policy (including revocation and token-purpose checks).
App tokens and administrator tokens are not accepted. The owner is the authenticated
session subject; owner IDs and facts cannot be supplied in the request.

Request example:

```json
{"target_kind":"artist","target_id":"9","limit":100}
```

Targets are Discogs provider artist or label IDs, explicitly separate from native UUIDs.
`limit` is 1–200 (default 100). Extra body keys and all query parameters return 422.
Work units, year/country/media filters, label ancestry, and co-release policy overrides
are unsupported controls. The response explicitly lists unsupported fields; their facts
remain unknown. Co-releases remain in the core's needs-review bucket.

The route wraps `api.pursuit_scope.preview_scope`; it does not reuse a gap page.
A Neo4j target traversal fetches at most limit+1 distinct releases in provider-ID order.
Each release includes the matched target and at most one other distinct credit, enough
to retain conservative co-release review. The extra release detects truncation. Reads
have five-second application deadlines; the graph statement also has a five-second
server budget. Reads never scan an entire owner collection. Alias lookups probe only
this page and its target, retaining up to two distinct currently-valid native candidate
IDs per key (two prove ambiguity). A single candidate confirms identity; duplicates
collapse; conflicting and absent aliases remain review evidence. Ownership checks are
bounded native-item `EXISTS` queries with the session owner predicate on `owned_copies`.
Native identity follows the existing open one-hop supersession rule for both aliases and owned copies. Ownership uses the existing item-ID index and reverse survivor index; reverse candidates are capped at 200 with one sentinel. If that reverse fan-in is truncated and no positive ownership was observed, ownership remains unknown. Multiple physical copies still count one eligible native item.

Response `preview` contains the pure-core scope, separate owned/missing/ownership-review/
needs-review/excluded evidence, and `denominator_evidence`. `sources` separately records
catalog, aliases and ownership status, server-observed start/end times and unknown
source versions (`null`). Catalog metadata adds limit and truncation. These are
independent statement read windows, **not a shared cross-store snapshot**. Exhausting a
bounded graph projection does not prove the imported provider universe is complete;
`scope_basis` says so, `denominator_complete` is false and `completion_fraction` is
always null. Counts describe only the observed projection, including when empty.
They must never be presented as whole-artist/label percentages.

Unconfigured sources or a failed/timed-out catalog read return 503 without driver details.
Unavailable aliases retain unknown identities. Incomplete/unavailable ownership leaves
confirmed positive evidence visible and moves otherwise missing units to ownership review;
absence from a partial read is never absence of ownership. Authentication failures use
existing 401/403/503 behavior. No preview is persisted or cached; the response includes
no owner identifier, source secrets, or other owners' copies. Existing gaps/search remain
unchanged.

Offline boundary proof: `uv run pytest tests/test_missing_preview.py -q`; the default
`just check` uses fake sources and never contacts backing stores.

Optional real-store proof (only against explicitly authorized test stores): start the
existing API configured for those stores, then run
`curl --fail-with-body -X POST "$API_URL/api/user/missing/preview" -H "Authorization: Bearer $SESSION_TOKEN" -H 'Content-Type: application/json' --data '{"target_kind":"artist","target_id":"9","limit":1}'`.
Repeat with two separately authenticated test owners and a known test fixture with at
least two releases; inspect truncation, independent read windows and owner-specific units.
Do not log the token or treat this manual smoke command as an automated real-store
completeness proof. No real-store proof was run for this implementation.
