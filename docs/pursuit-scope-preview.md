# Transient pursuit scope preview core

`api.pursuit_scope.preview_scope` is an offline, deterministic evaluator for a Missing-style
preview over a caller-materialized catalog snapshot and an already-authorized folded
collection. It has no database, network, identity-resolution, persistence, or scheduler
behavior. The authenticated [Missing adapter](missing-preview.md) now wraps it in the API with conservative source-completeness limits.

## Identity inputs

Catalog rows retain their provider/source release ID and separately carry a canonical native
`edition_id`. The existing fit fold's `release_ids` are Discogs/provider IDs; callers must use
`AuthorizedCollection.from_folded` with an explicit provider name and a supplied
provider-to-native-edition candidate mapping. Exactly one candidate confirms the native
edition. Missing or multiple mappings remain unresolved; multiple provider aliases mapped to
one native edition deduplicate in the denominator. No alias is inferred or looked up here.

`work_id` must likewise be a resolved canonical work identity when `UnitKind.WORK` is used.
Unresolved source rows are retained as per-source review evidence, without asserting a unit.

## Scope and counts

Artist/label target membership is combined with optional media, year, and country constraints.
Known false filters exclude a row even if another field is unknown. Unknown fields follow the
explicit `UnknownPolicy`; target identity ambiguity and `CoReleasePolicy.NEEDS_REVIEW` always
remain review holds. Label ancestry is supplied by the snapshot and unknown ancestry is explicit.

A known eligible native unit appears once as owned, missing, or ownership-review. Ownership
uncertainty does not shrink the known eligible denominator. Unknown eligibility and unresolved
unit identities are reported separately and make `denominator_complete` false. Unknown
collection mappings do not close a gap. For work units, only a confirmed owned edition that
qualifies for the scope closes the work; owning an out-of-scope edition does not.

`completion_fraction` is available only when denominator and ownership evidence are complete
and the denominator is nonzero. Snapshot IDs and a stable `scope_key` accompany the evidence.
The preview is transient: this module does not persist pursuits, authorize collection access,
resolve identities, or claim production usefulness from its synthetic unit tests.
