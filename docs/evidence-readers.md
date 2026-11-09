# Authority-separated evidence readers

`api/evidence_readers.py` is a pure projection over records already returned by existing
read paths. It creates no route, database query, authentication boundary, or persistent record.
The offline tests use synthetic conflicting records; they demonstrate projection behavior and
authority separation, not real-catalog evidence quality.

## Separate reader paths

| Projection | Inputs it may emit | Server context required |
| --- | --- | --- |
| `read_public_evidence` | Public release details and credits | None |
| `read_owner_evidence` | Public facts and observations attached to the authenticated owner's rows | Trusted owner id from existing auth and owner-scoped reads |
| `read_admin_identity_evidence` | Existing identity-ledger alias and event fields | Payload returned by `require_admin`; that dependency validates the admin JWT and, when a database pool is configured, checks the user's active `is_admin` row |

Each projection ignores records from the other authority paths. Login alone does not expose
identity history. The owner reader matches the row owner attached by the server-side query to
`trusted_owner_id`; it does not use a caller-declared owner field. New API/UI wiring is a
separate, gated change. The helper arguments are not credentials and must not be populated from
untrusted request claims.

The returned page holds at most 100 field entries (default 50) and includes `truncated`. These
limits bound the projection output only; a caller must bound its source query using the
existing reader's limit before invoking this helper. Extra source keys are discarded through
per-source allowlists.

## Available source fields and limits

| Existing source | Fields actually available here | Reference and time | Method / review state |
| --- | --- | --- | --- |
| Neo4j release details | `id`, `name`, `year`, `artists`, `labels`, `genres`, `styles`, `formats` | `source_ref` is the record id returned by the query. `subject_ref` is a separately supplied trusted subject context; this helper does no matching. The result does not identify its underlying provider source, fact timestamp, or original source span. | Method is the existing release-details query projection; underlying source attribution and review state are unknown |
| Neo4j release credits | `name`, `role`, `category`, `artist_id`, `artist_name` | `source_ref` is the caller-supplied release id context. Credit rows have no per-credit timestamp or original span, and do not name the underlying provider source. | Method is the existing release-credits query projection; underlying source attribution and review state are unknown |
| `observations` through the owner-scoped reader | `id`, `owned_copy_id`, `kind`, `value`, `source`, `confidence`, `observed_at`, `created_at` | `observed_at` is the owner/delegate-asserted time the evidence applied; `created_at` is when the row was stored. Neither gives a source-text span. | Method is the stored observation-row projection. `confidence` is exposed as `owner_asserted_confidence`, not a review probability; `review_confidence` is null and review state unknown |
| Identity Ledger alias/event payload | Alias: `provider`, `external_id`, `source`, `held_by`, `valid_from`, `valid_to`, `opened_by`, `closed_by`. Events include `source_ref`, `decision_ref`, `dated_from`, and existing event fields. | Alias validity comes from the ledger. Event `at` retains its ledger `dated_from` meaning. Refs are carried as plain strings. No source-text span or reviewed state is present. | Existing admin identity ledger projection; review state unknown |

Unknown timestamps and source spans stay null with an explicit time meaning. Known nulls in
admin alias/event rows (such as `valid_to: null`) remain present, distinct from omitted fields.
No extraction method, review decision, provider attribution, or provenance is inferred from a
value alone. A synthetic conflict fixture attaches two conflicting query rows to the same
trusted subject and field; the projection keeps both with their separate record references.
This is a projection test, not evidence that the existing query returns conflicting provider
provenance. Provider aliases and the Identity Ledger remain the authoritative identity paths.

Reproduce the synthetic authority and conflict cases with:

```sh
uv run pytest tests/test_evidence_readers.py -q
```

No real checked evidence corpus is bundled, so real-world completeness and quality remain
unmeasured.
