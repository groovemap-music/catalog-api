"""Bounded, read-only Missing preview adapter; never a provider-universe census."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from common.query_debug import execute_sql
from psycopg.rows import dict_row

from api.pursuit_scope import AuthorizedCollection, CatalogSnapshot, IdentityState, PursuitScope, ReleaseFact, TargetKind, preview_scope
from api.queries.helpers import run_query


READ_SECONDS = 5.0
# One bounded lookup per requested alias. Two DISTINCT candidates suffice to prove
# ambiguity without collecting an arbitrarily large damaged alias relation.
ALIASES = """
SELECT requested.entity_kind, requested.external_id, candidate.native_id
FROM unnest(%s::text[], %s::text[]) requested(entity_kind, external_id)
LEFT JOIN LATERAL (
 SELECT DISTINCT COALESCE(supersession.survivor_id, alias.native_id) AS native_id
 FROM provider_aliases alias
 LEFT JOIN catalog_item_supersessions supersession
 ON supersession.superseded_id = alias.native_id AND supersession.valid_to IS NULL
 WHERE provider = 'discogs' AND entity_kind = requested.entity_kind
 AND external_id = requested.external_id AND alias.valid_to IS NULL
 ORDER BY native_id LIMIT 2
) candidate ON true
ORDER BY requested.entity_kind, requested.external_id, candidate.native_id
"""
OWNERSHIP = """
SELECT requested.item_id, COALESCE(cardinality(reverse.ids) > 200, false) AS truncated,
 CASE WHEN
   EXISTS (SELECT 1 FROM owned_copies copy
           WHERE copy.user_id = %s::uuid AND copy.item_id = requested.item_id
           AND NOT EXISTS (SELECT 1 FROM catalog_item_supersessions current
                           WHERE current.superseded_id = requested.item_id AND current.valid_to IS NULL))
   OR EXISTS (SELECT 1 FROM owned_copies copy
              WHERE copy.user_id = %s::uuid AND copy.item_id = ANY(reverse.ids[1:200]))
 THEN true WHEN cardinality(reverse.ids) > 200 THEN NULL ELSE false END AS owned
FROM unnest(%s::uuid[]) requested(item_id)
LEFT JOIN LATERAL (
 SELECT array_agg(candidate.superseded_id) AS ids FROM (
   SELECT supersession.superseded_id FROM catalog_item_supersessions supersession
   WHERE supersession.survivor_id = requested.item_id AND supersession.valid_to IS NULL
   ORDER BY supersession.superseded_id LIMIT 201
 ) candidate
) reverse ON true
ORDER BY requested.item_id
"""


def now() -> str:
    return datetime.now(UTC).isoformat()


class MissingSource:
    """Production source: scoped Neo4j page, raw alias evidence, native owner predicates."""

    def __init__(self, driver: Any, pool: Any) -> None:
        self.driver = driver
        self.pool = pool

    async def catalog(self, kind: TargetKind, target: str, limit: int) -> list[dict[str, Any]]:
        label, relation = ("Artist", "BY") if kind is TargetKind.ARTIST else ("Label", "ON")
        # Static, enum-selected identifiers; all request values remain parameters.
        query = f"""
        MATCH (target:{label} {{id: $target}})<-[:{relation}]-(r:Release)
        WITH DISTINCT r ORDER BY r.id LIMIT $limit
        CALL (r) {{
          MATCH (r)-[:{relation}]->(credit:{label}) WHERE credit.id <> $target
          WITH DISTINCT credit.id AS other ORDER BY other LIMIT 1
          RETURN head(collect(other)) AS credit_id
        }}
        RETURN r.id AS id, [$target] + collect(credit_id) AS credits ORDER BY id
        """
        return await run_query(self.driver, query, target=target, limit=limit, timeout=READ_SECONDS)

    async def aliases(self, keys: list[tuple[str, str]]) -> list[dict[str, Any]]:
        async with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await execute_sql(cur, ALIASES, ([kind for kind, _ in keys], [value for _, value in keys]))
            return list(await cur.fetchall())

    async def ownership(self, owner: str, items: list[str]) -> list[dict[str, Any]]:
        async with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await execute_sql(cur, OWNERSHIP, (owner, owner, items))
            return list(await cur.fetchall())


async def _read(source: Any, name: str, *args: Any) -> tuple[Any, dict[str, Any]]:
    started = now()
    try:
        async with asyncio.timeout(READ_SECONDS):
            rows = await getattr(source, name)(*args)
        return rows, {"status": "observed", "started_at": started, "finished_at": now(), "version": None}
    except Exception:
        # No exception text: drivers can carry credentials or query parameters.
        return [], {"status": "unavailable", "started_at": started, "finished_at": now(), "version": None}


async def missing_preview(source: Any, owner: str, kind: TargetKind, target: str, limit: int) -> dict[str, Any]:
    """Project scope facts and explicitly separate read exhaustion from completeness."""
    if not owner or not 1 <= limit <= 200:
        raise ValueError("owner and bounded limit required")
    rows, catalog = await _read(source, "catalog", kind, target, limit + 1)
    catalog["truncated"] = len(rows) > limit
    catalog["limit"] = limit
    rows = rows[:limit]
    keys = sorted({(kind.value, target), *(("release", str(row["id"])) for row in rows)})
    alias_rows, aliases = await _read(source, "aliases", keys)
    candidates: dict[tuple[str, str], set[str]] = {}
    for row in alias_rows:
        if row.get("native_id") is not None:
            candidates.setdefault((row["entity_kind"], row["external_id"]), set()).add(str(row["native_id"]))
    target_candidates = candidates.get((kind.value, target), set())
    target_state = IdentityState.CONFIRMED if len(target_candidates) == 1 else IdentityState.AMBIGUOUS if target_candidates else IdentityState.UNKNOWN
    facts = []
    for row in rows:
        ids = candidates.get(("release", str(row["id"])), set())
        state = IdentityState.CONFIRMED if len(ids) == 1 else IdentityState.AMBIGUOUS if ids else IdentityState.UNKNOWN
        # Two credit IDs are sufficient for the core's conservative co-release rule.
        credits = tuple(str(value) for value in row["credits"])
        facts.append(
            ReleaseFact(
                str(row["id"]),
                next(iter(ids)) if len(ids) == 1 else None,
                credits if kind is TargetKind.ARTIST else None,
                credits if kind is TargetKind.LABEL else None,
                None,
                None,
                None,
                release_identity=state,
            )
        )
    item_ids = sorted({fact.edition_id for fact in facts if fact.edition_id is not None})
    ownership_rows, ownership = await _read(source, "ownership", owner, item_ids)
    ownership["truncated"] = any(row.get("truncated", False) for row in ownership_rows)
    ownership_by_item = {str(row["item_id"]): row["owned"] for row in ownership_rows}
    ownership_complete = (
        ownership["status"] == "observed"
        and set(ownership_by_item) == set(item_ids)
        and all(isinstance(value, bool) for value in ownership_by_item.values())
    )
    mapping = {fact.release_id: (fact.edition_id,) for fact in facts if fact.edition_id and ownership_by_item.get(fact.edition_id) is True}
    collection = AuthorizedCollection("postgresql-window:" + ownership["started_at"], "discogs", tuple(mapping), mapping)
    snapshot = CatalogSnapshot("neo4j-window:" + catalog["started_at"], tuple(facts))
    result = asdict(preview_scope(snapshot, PursuitScope(kind, target, target_identity=target_state), collection))
    if not ownership_complete:
        for unit in result["missing"]:
            unit["reasons"] = (*(reason for reason in unit["reasons"] if reason != "not_in_authorized_collection"), "ownership_source_incomplete")
        result["ownership_review"] = (*result["ownership_review"], *result["missing"])
        result["missing"] = ()
    counts = result["denominator_evidence"]
    counts.update(
        missing=len(result["missing"]),
        ownership_review=len(result["ownership_review"]),
        ownership_complete=ownership_complete and counts["ownership_complete"],
        denominator_complete=False,
        completion_fraction=None,
    )
    return {
        "preview": result,
        "sources": {"catalog": catalog, "aliases": aliases, "ownership": ownership},
        "consistency": "independent_read_windows_no_shared_snapshot",
        "scope_basis": "bounded_neo4j_projection_not_provider_universe",
        "unsupported_fields": ["label_ancestry", "year", "country", "media", "work_units"],
        "co_release_policy": "needs_review",
    }
