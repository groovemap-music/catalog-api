"""Missing runtime boundaries, with no real-store reads."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import dependencies
from api.missing_preview import ALIASES, OWNERSHIP, MissingSource, missing_preview
from api.pursuit_scope import TargetKind
from api.routers import missing


class Source:
    def __init__(self, rows=None, aliases=None, ownership=None):
        self.catalog = AsyncMock(return_value=rows if rows is not None else [{"id": "1", "credits": ["9"]}])
        self.aliases = AsyncMock(
            return_value=aliases
            if aliases is not None
            else [
                {"entity_kind": "artist", "external_id": "9", "native_id": "target"},
                {"entity_kind": "release", "external_id": "1", "native_id": "edition"},
            ]
        )
        self.ownership = AsyncMock(side_effect=lambda owner, items: [{"item_id": item, "owned": owner == "owner-a"} for item in items])
        if ownership is not None:
            self.ownership = AsyncMock(return_value=ownership)


@pytest.mark.asyncio
async def test_owners_and_source_windows():
    source = Source()
    a = await missing_preview(source, "owner-a", TargetKind.ARTIST, "9", 2)
    b = await missing_preview(source, "owner-b", TargetKind.ARTIST, "9", 2)
    assert a["preview"]["denominator_evidence"]["owned"] == 1
    assert b["preview"]["denominator_evidence"]["missing"] == 1
    assert a["preview"]["denominator_evidence"]["completion_fraction"] is None
    assert not a["preview"]["denominator_evidence"]["denominator_complete"]
    assert a["sources"]["catalog"]["version"] is None
    assert a["preview"]["catalog_snapshot_id"] != a["preview"]["collection_snapshot_id"]
    source.catalog.assert_awaited_with(TargetKind.ARTIST, "9", 3)
    source.ownership.assert_awaited_with("owner-b", ["edition"])


@pytest.mark.asyncio
async def test_alias_dedup_conflict_unknown_and_co_release():
    source = Source(rows=[{"id": str(i), "credits": ["9"]} for i in range(1, 5)])
    source.aliases.return_value += [
        {"entity_kind": "release", "external_id": "1", "native_id": "edition"},
        {"entity_kind": "release", "external_id": "2", "native_id": "edition"},
        {"entity_kind": "release", "external_id": "3", "native_id": "conflict-a"},
        {"entity_kind": "release", "external_id": "3", "native_id": "conflict-b"},
        {"entity_kind": "release", "external_id": "4", "native_id": None},
    ]
    result = await missing_preview(source, "owner-a", TargetKind.ARTIST, "9", 10)
    assert result["preview"]["denominator_evidence"]["owned"] == 1
    assert result["preview"]["denominator_evidence"]["needs_review"] == 2
    source.catalog.return_value = [{"id": "1", "credits": ["1", "2", "9"]}]
    result = await missing_preview(source, "owner-a", TargetKind.ARTIST, "9", 10)
    assert result["preview"]["denominator_evidence"]["needs_review"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["ownership", "aliases", "catalog"])
async def test_failed_sources_remain_unknown(stage):
    source = Source()
    getattr(source, stage).side_effect = TimeoutError
    result = await missing_preview(source, "owner-b", TargetKind.ARTIST, "9", 1)
    assert result["sources"][stage]["status"] == "unavailable"
    assert result["preview"]["denominator_evidence"]["missing"] == 0
    if stage == "ownership":
        assert result["preview"]["denominator_evidence"]["ownership_review"] == 1


@pytest.mark.asyncio
async def test_partial_empty_label_and_bounds():
    source = Source(ownership=[])
    result = await missing_preview(source, "owner-b", TargetKind.ARTIST, "9", 1)
    assert result["preview"]["denominator_evidence"]["ownership_review"] == 1
    source = Source(ownership=[{"item_id": "edition", "owned": None, "truncated": True}])
    result = await missing_preview(source, "owner-b", TargetKind.ARTIST, "9", 1)
    assert result["preview"]["denominator_evidence"]["ownership_review"] == 1
    assert "not_in_authorized_collection" not in result["preview"]["ownership_review"][0]["reasons"]
    assert result["sources"]["ownership"]["truncated"]
    source = Source(rows=[{"id": "1", "credits": ["9"]}, {"id": "2", "credits": ["9"]}])
    result = await missing_preview(source, "owner-b", TargetKind.ARTIST, "9", 1)
    assert result["sources"]["catalog"]["truncated"]
    assert len(result["preview"]["missing"]) == 1
    source = Source(rows=[])
    result = await missing_preview(source, "owner-b", TargetKind.LABEL, "9", 200)
    assert result["preview"]["denominator_evidence"]["denominator"] == 0
    assert result["preview"]["denominator_evidence"]["completion_fraction"] is None
    for owner, limit in [("", 1), ("a", 0), ("a", 201)]:
        with pytest.raises(ValueError):
            await missing_preview(source, owner, TargetKind.ARTIST, "9", limit)


@pytest.fixture
def client(monkeypatch):
    app = FastAPI()
    app.include_router(missing.router)
    monkeypatch.setattr(missing, "_source", Source())
    app.dependency_overrides[dependencies.require_user] = lambda: {"sub": "owner-a"}
    with TestClient(app) as client:
        yield client, app


def test_route_strict_controls_and_readiness(client, monkeypatch):
    client, _ = client
    body = {"target_kind": "artist", "target_id": "9"}
    first = client.post("/api/user/missing/preview", json=body)
    assert first.status_code == 200
    assert first.json()["preview"]["denominator_evidence"]["owned"] == 1
    client.app.dependency_overrides[dependencies.require_user] = lambda: {"sub": "owner-b"}
    second = client.post("/api/user/missing/preview", json=body)
    assert second.json()["preview"]["denominator_evidence"]["owned"] == 0
    assert second.json()["preview"]["denominator_evidence"]["missing"] == 1
    for extra in ["owner_id", "catalog", "year_from", "include_sublabels", "media_any", "co_release_policy", "unit_kind"]:
        assert client.post("/api/user/missing/preview", json={**body, extra: "bad"}).status_code == 422
    assert client.post("/api/user/missing/preview?owner_id=other", json=body).status_code == 422
    for invalid in [{"limit": 201}, {"target_id": "native-uuid"}, {"target_kind": "master"}]:
        assert client.post("/api/user/missing/preview", json={**body, **invalid}).status_code == 422
    missing.configure(None, None)
    assert client.post("/api/user/missing/preview", json=body).status_code == 503
    source = Source()
    source.catalog.side_effect = TimeoutError
    monkeypatch.setattr(missing, "_source", source)
    assert client.post("/api/user/missing/preview", json=body).status_code == 503


def test_real_auth_dependency_rejects_missing_and_app_credentials(client, monkeypatch):
    client, app = client
    app.dependency_overrides.clear()
    monkeypatch.setattr(dependencies, "_jwt_secret", "test-secret-only")
    body = {"target_kind": "artist", "target_id": "9"}
    assert client.post("/api/user/missing/preview", json=body).status_code == 401
    assert client.post("/api/user/missing/preview", json=body, headers={"Authorization": "Bearer dscg_invalid"}).status_code == 401


@pytest.mark.asyncio
async def test_production_queries_are_bounded_and_owner_scoped(monkeypatch):
    import api.missing_preview as module

    query = AsyncMock(return_value=[])
    monkeypatch.setattr(module, "run_query", query)
    cur = MagicMock()
    cur.fetchall = AsyncMock(return_value=[])
    cur.__aenter__ = AsyncMock(return_value=cur)
    conn = MagicMock()
    conn.cursor.return_value = cur
    conn.__aenter__ = AsyncMock(return_value=conn)
    pool = MagicMock()
    pool.connection.return_value = conn
    execute = AsyncMock()
    monkeypatch.setattr(module, "execute_sql", execute)
    source = MissingSource("driver", pool)
    for kind, relation in [(TargetKind.ARTIST, "BY"), (TargetKind.LABEL, "ON")]:
        await source.catalog(kind, "9", 201)
        assert f"[:{relation}]" in query.call_args.args[1]
        assert "LIMIT $limit" in query.call_args.args[1]
        assert "[$target] + collect(credit_id)" in query.call_args.args[1]
        assert "credit.id <> $target" in query.call_args.args[1]
        assert "ORDER BY other LIMIT 1" in query.call_args.args[1]
        assert query.call_args.kwargs["timeout"] == 5
    await source.aliases([("release", "1")])
    execute.assert_awaited_with(cur, ALIASES, (["release"], ["1"]))
    assert "SELECT DISTINCT COALESCE" in ALIASES and "LIMIT 2" in ALIASES
    assert "supersession.survivor_id" in ALIASES
    assert "copy.item_id = requested.item_id" in OWNERSHIP
    assert "supersession.survivor_id = requested.item_id" in OWNERSHIP
    assert "copy.item_id = ANY(reverse.ids[1:200])" in OWNERSHIP
    assert "LIMIT 201" in OWNERSHIP
    assert "THEN NULL ELSE false" in OWNERSHIP
    assert "current.superseded_id = requested.item_id" in OWNERSHIP
    assert "COALESCE(supersession.survivor_id, copy.item_id)" not in OWNERSHIP
    await source.ownership("owner-a", ["edition"])
    execute.assert_awaited_with(cur, OWNERSHIP, ("owner-a", "owner-a", ["edition"]))
    assert "copy.user_id = %s::uuid" in OWNERSHIP
    missing.configure("driver", pool)
    assert isinstance(missing._source, MissingSource)
