"""Engine-backed proof of the three ``admin_identity_ledger_*`` routes (ADR 0016 section 7).

Seeds the invented catalog of `tests/identity_ledger_story.py` (spike `gm-design-loj9`'s
synthetic story, with `identity_link_events` rows of both provenances) into the real schema
`create_postgres_schema` applies, through the same `postgres_pool` fixture
`tests/test_real_databases.py` uses, and reads it back through the mounted routes, so every
statement is parsed, planned, and run by PostgreSQL. The works list and density read
database-schema's precomputed views, so each test refreshes them with `identity_ledger_refresh()`
after seeding; the parity tests hold every case `gm-catalog-api-278z` measured equal to the live
statements the routes ran before (`tests/identity_ledger_live.py`). Nothing here touches a
shared database: `just test-integration` starts throwaway containers.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from common import AsyncPostgreSQLPool

import api.api as api_module
import api.routers.admin as admin_router
from api.dependencies import require_admin
from api.identity_ledger import EVENT_TYPES, KINDS
from api.queries import identity_ledger_queries
from tests import identity_ledger_live, identity_ledger_story
from tests.identity_ledger_story import BACKFILL, E1, E2, E3, E4, E5, E6, E7, EXPORT, G2, G5X, MOVED_ROW, RUN1, W1, W2, W5, X3, mbid, story
from tests.test_real_databases import TEST_USER_ID, postgres_pool


__all__ = ["postgres_pool"]

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_RESET = (
    "TRUNCATE musicbrainz.releases, public.releases, graph.derived_from, identity_link_events, "
    "catalog_item_moves, catalog_item_supersessions, admin_audit_log"
)


async def _execute(pool: AsyncPostgreSQLPool, sql: str, params: Any = None) -> None:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(sql, params)


async def _refresh(pool: AsyncPostgreSQLPool) -> datetime:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("SELECT identity_ledger_refresh()")
        row = await cur.fetchone()
    assert row is not None
    refreshed_at: datetime = row[0]
    return refreshed_at


_INSERTS = {
    "catalog_items": "INSERT INTO catalog_items (id, kind, created_at) VALUES (%(id)s, %(kind)s, %(created_at)s)",
    "provider_aliases": (
        "INSERT INTO provider_aliases (provider, entity_kind, external_id, native_id, valid_from, valid_to, source) "
        "VALUES (%(provider)s, %(entity_kind)s, %(external_id)s, %(native_id)s, %(valid_from)s, %(valid_to)s, %(source)s)"
    ),
    "catalog_item_supersessions": (
        "INSERT INTO catalog_item_supersessions (id, superseded_id, survivor_id, cause, decision_ref, valid_from, valid_to, via_id) "
        "VALUES (%(id)s, %(superseded_id)s, %(survivor_id)s, %(cause)s, %(decision_ref)s, %(valid_from)s, %(valid_to)s, %(via_id)s)"
    ),
    "catalog_item_moves": (
        "INSERT INTO catalog_item_moves (supersession_id, table_name, row_id, from_item_id, to_item_id, user_id, moved_at) "
        "VALUES (%(supersession_id)s, %(table_name)s, %(row_id)s, %(from_item_id)s, %(to_item_id)s, %(user_id)s, %(moved_at)s)"
    ),
    "discogs_releases": "INSERT INTO public.releases (data_id, hash, data) VALUES (%(data_id)s, 'h', '{}'::jsonb)",
    "musicbrainz_releases": (
        "INSERT INTO musicbrainz.releases (mbid, name, release_group_mbid, discogs_release_id) "
        "VALUES (%(mbid)s, 'r', %(release_group_mbid)s, %(discogs_release_id)s)"
    ),
    "identity_link_events": (
        "INSERT INTO identity_link_events (provider, entity_kind, external_id, event, discogs_id_before, discogs_id_after, "
        "event_at, dated_from, source_ref, decision_ref) VALUES (%(provider)s, %(entity_kind)s, %(external_id)s, %(event)s, "
        "%(discogs_id_before)s, %(discogs_id_after)s, %(event_at)s, %(dated_from)s, %(source_ref)s, %(decision_ref)s)"
    ),
}


async def _seed(pool: AsyncPostgreSQLPool) -> None:
    # The moved owned copy belongs to the fixture's real user: the payload may count it, never name it.
    tables = story(owner=str(TEST_USER_ID))
    async with pool.connection() as conn, conn.cursor() as cur:
        for table, insert in _INSERTS.items():
            await cur.executemany(insert, tables[table])
        # graph.derived_from is the relational form of each Discogs release's master.
        await cur.executemany(
            "INSERT INTO graph.derived_from (release_id, master_id) VALUES (%(data_id)s, %(master_id)s)",
            [row for row in tables["discogs_releases"] if row["master_id"] is not None],
        )


@pytest_asyncio.fixture
async def http(postgres_pool: AsyncPostgreSQLPool, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[httpx.AsyncClient]:
    await _execute(postgres_pool, _RESET)
    await _seed(postgres_pool)
    await _refresh(postgres_pool)
    monkeypatch.setattr(admin_router, "_pool", postgres_pool)
    api_module.app.dependency_overrides[require_admin] = lambda: {"sub": str(uuid4()), "type": "admin"}
    transport = httpx.ASGITransport(app=api_module.app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://catalog-api.test") as client:
            yield client
    finally:
        api_module.app.dependency_overrides.pop(require_admin, None)


def _edition(payload: dict[str, Any], gm_id: str) -> dict[str, Any]:
    return next(edition for edition in payload["editions"] if edition["gm_id"] == gm_id)


async def _work(http: httpx.AsyncClient, work_id: str, **params: Any) -> dict[str, Any]:
    response = await http.get(f"/api/admin/identity/ledger/works/{work_id}", params=params)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def test_work_lists_its_editions_in_ledger_order_from_both_catalogs(http: httpx.AsyncClient) -> None:
    payload = await _work(http, W1)

    assert payload["work"]["gm_id"] == W1
    assert "requested_id" not in payload["work"]
    assert payload["work"]["state"] == "linked"
    assert [edition["gm_id"] for edition in payload["editions"]] == [E1, E2, E4, E3, E5]
    assert payload["edition_count"] == 5
    assert payload["truncated"] is False
    assert _edition(payload, E4)["state"] == "discogs_only"
    assert _edition(payload, E5)["state"] == "musicbrainz_only"
    assert _edition(payload, E1)["provider_rows"] == [
        {"provider": "discogs", "external_id": "900001"},
        {"provider": "musicbrainz", "external_id": mbid(0x1), "names_discogs_release": "900001"},
    ]


async def test_reattached_edition_walks_unresolved_split_linked(http: httpx.AsyncClient) -> None:
    states = []
    for as_of in ("2026-08-05T00:00:00Z", "2026-09-01T12:00:00Z", "2026-09-03T00:00:00Z"):
        payload = await _work(http, W1, as_of=as_of)
        assert payload["as_of"] == as_of
        states.append(_edition(payload, E3)["state"])
    assert states == ["unresolved", "split", "linked"]


async def test_reattachment_is_one_event_with_its_audit_entry_and_counted_moves(http: httpx.AsyncClient) -> None:
    e3 = _edition(await _work(http, W1), E3)

    [reattached] = [event for event in e3["events"] if event["type"] == "reattached"]
    assert reattached["decision_ref"] == RUN1
    assert reattached["moves"] == {"owned_copies": 1}
    assert reattached["dated_from"] == "groovemap"
    assert e3["former_items"] == [{"gm_id": X3, "superseded_at": "2026-09-02T10:00:01Z", "cause": "catalog_reattachment", "decision_ref": RUN1}]
    assert {row["provider"] for row in e3["evidence"] if row["opened_by"] == "supersession"} == {"musicbrainz", "barcode"}


async def test_every_event_names_its_provenance(http: httpx.AsyncClient) -> None:
    payload = await _work(http, W1)
    events = [event for edition in payload["editions"] for event in edition["events"]] + payload["work"]["events"]

    assert all(event["dated_from"] in ("groovemap", "musicbrainz_fullexport") for event in events)
    backfilled = [event for event in events if event["dated_from"] == "musicbrainz_fullexport"]
    assert {(event["type"], event["external_id"]) for event in backfilled} == {
        ("link_added", mbid(0x1)),
        ("link_added", mbid(0x3)),
        ("link_added", mbid(0xB1)),
    }
    assert all(event["decision_ref"] == BACKFILL and event["source_ref"] == EXPORT for event in backfilled)
    [r1_link] = [event for event in _edition(payload, E1)["events"] if event["type"] == "link_added"]
    assert r1_link == {
        "at": "2025-11-14T00:00:00Z",
        "type": "link_added",
        "provider": "musicbrainz",
        "external_id": mbid(0x1),
        "discogs_id_before": None,
        "discogs_id_after": "900001",
        "source_ref": EXPORT,
        "decision_ref": BACKFILL,
        "dated_from": "musicbrainz_fullexport",
        "rule": "ADR 0016 2026-10-24 amendment: a MusicBrainz row gained its Discogs link",
    }


async def test_load_recorded_link_change_reads_as_groovemap(http: httpx.AsyncClient) -> None:
    e7 = _edition(await _work(http, W2), E7)

    [changed] = [event for event in e7["events"] if event["type"] == "link_changed"]
    assert changed["dated_from"] == "groovemap"
    assert (changed["discogs_id_before"], changed["discogs_id_after"]) == ("900099", "900006")


async def test_as_of_hides_what_was_not_yet_recorded(http: httpx.AsyncClient) -> None:
    early = await _work(http, W1, as_of="2026-08-02T00:00:00Z")

    assert [edition["gm_id"] for edition in early["editions"]] == [E1, E2, E4]
    assert early["edition_count"] == 3
    for edition in early["editions"]:
        assert all(event["at"] <= "2026-08-02" for event in edition["events"])
        assert all(row["valid_from"] <= "2026-08-02" for row in edition["evidence"])
    assert {event["type"] for event in _edition(early, E1)["events"]} == {"minted", "link_added"}


async def test_edition_limit_caps_the_editions_and_reports_truncation(http: httpx.AsyncClient) -> None:
    payload = await _work(http, W1, edition_limit=2)

    assert [edition["gm_id"] for edition in payload["editions"]] == [E1, E2]
    assert payload["edition_count"] == 5
    assert payload["truncated"] is True


async def test_two_parent_edition_has_the_discogs_master_as_primary(http: httpx.AsyncClient) -> None:
    for work in (W2, G2):
        payload = await _work(http, work)
        assert [edition["gm_id"] for edition in payload["editions"]] == [E6, E7]
        for edition in payload["editions"]:
            assert edition["primary_work"] == W2
            assert edition["works"] == [W2, G2]
            assert edition["work_disagreement"] is True
    assert _edition(await _work(http, W1), E1)["work_disagreement"] is False


async def test_superseded_work_answers_as_its_survivor_and_others_are_404(http: httpx.AsyncClient) -> None:
    payload = await _work(http, G5X)
    assert payload["work"]["gm_id"] == W5
    assert payload["work"]["requested_id"] == G5X
    assert [event["type"] for event in payload["work"]["events"] if event["type"] == "reattached"] == ["reattached"]

    for missing in (E1, str(uuid4())):
        response = await http.get(f"/api/admin/identity/ledger/works/{missing}")
        assert response.status_code == 404


async def test_no_user_owned_copy_or_admin_id_is_emitted(http: httpx.AsyncClient) -> None:
    text = json.dumps(await _work(http, W1)) + json.dumps((await http.get("/api/admin/identity/ledger/works")).json())

    assert str(TEST_USER_ID) not in text
    assert MOVED_ROW not in text


async def test_works_list_counts_evented_editions_without_a_default_window(http: httpx.AsyncClient) -> None:
    response = await http.get("/api/admin/identity/ledger/works", params={"page_size": 1000})
    assert response.status_code == 200
    body = response.json()

    assert body["page"] == 1
    assert body["page_size"] == 100
    works = {row["gm_id"]: row for row in body["works"]}
    assert set(works) == {W1, W2, G2}
    assert body["total"] == 3
    assert works[W1] == {
        "gm_id": W1,
        "providers": [{"provider": "discogs", "external_id": "900101"}, {"provider": "musicbrainz", "external_id": mbid(0xB1)}],
        "editions": 5,
        "evented_editions": 3,
        "repaired_editions": 1,
        "work_disagreements": 0,
        # The backfill dated r1's link to 2025, more than 90 days before any other event.
        "first_event": "2025-11-14T00:00:00Z",
        "last_event": "2026-09-10T05:00:00Z",
    }
    assert works[W2]["work_disagreements"] == 2
    assert works[G2]["work_disagreements"] == 2
    # Most recent event first; the three tie on 2026-09-10, so the id breaks it.
    assert [row["gm_id"] for row in body["works"]] == [W1, W2, G2]


async def test_works_list_filters_by_event_and_window_and_pages(http: httpx.AsyncClient) -> None:
    reattached = (await http.get("/api/admin/identity/ledger/works", params={"event": "reattached"})).json()
    assert [(row["gm_id"], row["evented_editions"], row["repaired_editions"]) for row in reattached["works"]] == [(W1, 1, 1)]

    window = (await http.get("/api/admin/identity/ledger/works", params={"since": "2026-09-05T00:00:00Z", "until": "2026-09-30T00:00:00Z"})).json()
    assert {row["gm_id"]: row["evented_editions"] for row in window["works"]} == {W1: 1, W2: 1, G2: 1}

    first = (await http.get("/api/admin/identity/ledger/works", params={"page_size": 2})).json()
    second = (await http.get("/api/admin/identity/ledger/works", params={"page_size": 2, "page": 2})).json()
    assert first["total"] == second["total"] == 3
    assert len(first["works"]) == 2
    assert len(second["works"]) == 1
    beyond = (await http.get("/api/admin/identity/ledger/works", params={"page": 9})).json()
    assert beyond["works"] == []
    assert beyond["total"] == 3


async def test_density_counts_events_per_month_by_type_and_provenance(http: httpx.AsyncClient) -> None:
    body = (await http.get("/api/admin/identity/ledger/density")).json()
    assert body["kind"] == "release"
    buckets = {(row["month"], row["type"], row["dated_from"]): row["events"] for row in body["buckets"]}

    assert buckets[("2025-11", "link_added", "musicbrainz_fullexport")] == 1
    assert buckets[("2026-03", "link_added", "musicbrainz_fullexport")] == 1
    assert buckets[("2026-09", "link_changed", "groovemap")] == 1
    assert buckets[("2026-09", "reattached", "groovemap")] == 1
    assert buckets[("2026-08", "minted", "groovemap")] == 7
    assert buckets[("2026-09", "minted", "groovemap")] == 1
    assert buckets[("2026-08", "linked_at_ingest", "groovemap")] == 3
    assert [row["month"] for row in body["buckets"]] == sorted(row["month"] for row in body["buckets"])

    masters = (await http.get("/api/admin/identity/ledger/density", params={"kind": "master"})).json()
    master_buckets = {(row["month"], row["type"], row["dated_from"]): row["events"] for row in masters["buckets"]}
    assert master_buckets[("2025-12", "link_added", "musicbrainz_fullexport")] == 1
    assert master_buckets[("2026-10", "reattached", "groovemap")] == 1


# ---------------------------------------------------------------------------------------------
# The precomputed views: parity with the live statements, refreshed_at, and the unrefreshed 503.
# ---------------------------------------------------------------------------------------------

# The story has no promotion, revert, later evidence, closed alias, removed link, artist, or label.
# These rows add one of each on top of it, so every parity case below has rows to compare.
E8, E9, P9, A1, L1, S9, RUN9 = (identity_ledger_story.native(n) for n in (0x8, 0x9, 0x19, 0x301, 0x401, 0x5009, 0xA009))
D1, MB2 = identity_ledger_story.D1, identity_ledger_story.MB2
PROMOTED, REVERTED = "2026-09-15T00:00:00+00:00", "2026-09-20T00:00:00+00:00"
_MORE = [
    (
        "INSERT INTO catalog_items (id, kind, created_at) VALUES (%s, %s, %s)",
        [(E8, "release", D1), (E9, "release", D1), (P9, "release", MB2), (A1, "artist", D1), (L1, "label", D1)],
    ),
    (
        "INSERT INTO provider_aliases (provider, entity_kind, external_id, native_id, valid_from, valid_to, source) VALUES (%s, %s, %s, %s, %s, %s, 'catalog')",
        [
            ("discogs", "release", "900007", E8, D1, None),
            # Evidence attached after the mint, and an alias closed other than by a supersession.
            ("barcode", "release", "synthetic-barcode-8", E8, "2026-09-12T00:00:00+00:00", None),
            ("barcode", "release", "synthetic-barcode-8b", E8, D1, "2026-09-25T00:00:00+00:00"),
            ("discogs", "release", "900008", E9, D1, None),
            # P9 is promoted into E9 and the promotion reverted: its alias moves over and back.
            ("musicbrainz", "release", mbid(0x9), P9, MB2, PROMOTED),
            ("musicbrainz", "release", mbid(0x9), E9, PROMOTED, REVERTED),
            ("musicbrainz", "release", mbid(0x9), P9, REVERTED, None),
            ("discogs", "artist", "700001", A1, D1, None),
            ("musicbrainz", "artist", mbid(0x301), A1, "2026-08-03T05:00:30+00:00", None),
            ("discogs", "label", "800001", L1, D1, "2026-09-29T00:00:00+00:00"),
        ],
    ),
    (
        "INSERT INTO catalog_item_supersessions (id, superseded_id, survivor_id, cause, decision_ref, valid_from, valid_to) "
        "VALUES (%s, %s, %s, 'edition_promotion', %s, %s, %s)",
        [(S9, P9, E9, RUN9, PROMOTED, REVERTED)],
    ),
    ("INSERT INTO public.releases (data_id, hash, data) VALUES (%s, 'h', '{}'::jsonb)", [("900007",), ("900008",)]),
    ("INSERT INTO graph.derived_from (release_id, master_id) VALUES (%s, '900102')", [("900007",), ("900008",)]),
    (
        "INSERT INTO identity_link_events (provider, entity_kind, external_id, event, discogs_id_before, discogs_id_after, event_at, "
        "dated_from, source_ref) VALUES ('musicbrainz', 'release', %s, 'link_removed', 900005, NULL, '2026-09-28T00:00:00+00:00', "
        "'groovemap_load', '20260928-001001')",
        [(mbid(0x6),)],
    ),
]


async def _add_every_event(pool: AsyncPostgreSQLPool) -> None:
    async with pool.connection() as conn, conn.cursor() as cur:
        for insert, rows in _MORE:
            await cur.executemany(insert, rows)
    await _refresh(pool)


@pytest_asyncio.fixture
async def every_event(http: httpx.AsyncClient, postgres_pool: AsyncPostgreSQLPool) -> httpx.AsyncClient:
    """The story plus `_MORE`, refreshed."""
    await _add_every_event(postgres_pool)
    return http


@pytest_asyncio.fixture(params=["story", "every_event"])
async def catalog(request: pytest.FixtureRequest, http: httpx.AsyncClient, postgres_pool: AsyncPostgreSQLPool) -> httpx.AsyncClient:
    """The story alone, then with `_MORE`: each parity case runs on both."""
    if request.param == "every_event":
        await _add_every_event(postgres_pool)
    return http


# Every case gm-catalog-api-278z measured (works by default, by each event filter, in a window;
# density for each kind), plus the window's edges, a filtered window, and a second page.
_WORKS_CASES: list[dict[str, Any]] = [
    {},
    *({"event": event} for event in EVENT_TYPES),
    {"since": "2026-09-05T00:00:00Z", "until": "2026-09-30T00:00:00Z"},
    {"since": "2026-09-01T00:00:00Z", "until": "2026-10-01T00:00:00Z", "event": "reattached"},
    {"since": "2026-09-02T10:00:01Z"},
    {"until": "2026-08-02T00:00:00Z"},
    {"page_size": 2, "page": 2},
]


async def _get(http: httpx.AsyncClient, path: str, params: dict[str, Any]) -> dict[str, Any]:
    response = await http.get(path, params=params)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def _live(http: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, path: str, params: dict[str, Any]) -> dict[str, Any]:
    """The same request answered by the live statements the routes ran before the views."""
    with monkeypatch.context() as live:
        live.setattr(identity_ledger_queries, "_WORKS", identity_ledger_live.WORKS.replace("%(kind)s", "'release'"))
        live.setattr(identity_ledger_queries, "_DENSITY", identity_ledger_live.DENSITY)
        return await _get(http, path, params)


def _case_id(params: dict[str, Any]) -> str:
    return "&".join(f"{key}={value}" for key, value in params.items()) or "default"


@pytest.mark.parametrize("params", _WORKS_CASES, ids=_case_id)
async def test_works_list_from_the_views_equals_the_live_statement(
    catalog: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, params: dict[str, Any]
) -> None:
    precomputed = await _get(catalog, "/api/admin/identity/ledger/works", params)

    assert precomputed == await _live(catalog, monkeypatch, "/api/admin/identity/ledger/works", params)


async def test_every_parity_case_has_works_to_compare(every_event: httpx.AsyncClient) -> None:
    """The parity above is not vacuous: with `_MORE`, every filter and window lists works."""
    totals = {_case_id(params): (await _get(every_event, "/api/admin/identity/ledger/works", params))["total"] for params in _WORKS_CASES}

    assert totals == {
        "default": 3,
        "event=linked_at_ingest": 3,
        "event=reattached": 1,
        "event=promoted": 1,
        "event=reverted": 1,
        "event=evidence_attached": 1,
        "event=alias_closed": 1,
        "event=link_added": 1,
        "event=link_changed": 2,
        "event=link_removed": 2,
        "since=2026-09-05T00:00:00Z&until=2026-09-30T00:00:00Z": 3,
        "since=2026-09-01T00:00:00Z&until=2026-10-01T00:00:00Z&event=reattached": 1,
        "since=2026-09-02T10:00:01Z": 3,
        "until=2026-08-02T00:00:00Z": 1,
        "page_size=2&page=2": 3,
    }


@pytest.mark.parametrize("kind", KINDS)
async def test_density_from_the_view_equals_the_live_statement(catalog: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    precomputed = await _get(catalog, "/api/admin/identity/ledger/density", {"kind": kind})

    assert precomputed == await _live(catalog, monkeypatch, "/api/admin/identity/ledger/density", {"kind": kind})


async def test_every_kind_has_density_to_compare(every_event: httpx.AsyncClient) -> None:
    """The story has release and master items only; `_MORE` adds an artist and a label."""
    for kind in KINDS:
        assert (await _get(every_event, "/api/admin/identity/ledger/density", {"kind": kind}))["buckets"], kind


async def test_every_payload_names_the_last_refresh(http: httpx.AsyncClient, postgres_pool: AsyncPostgreSQLPool) -> None:
    refreshed_at = await _refresh(postgres_pool)
    expected = refreshed_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
    assert expected.endswith("Z")

    works = await _get(http, "/api/admin/identity/ledger/works", {})
    density = await _get(http, "/api/admin/identity/ledger/density", {})
    work = await _work(http, W1)

    # The single work's planes are live; its refreshed_at names the list it was reached from.
    assert works["refreshed_at"] == density["refreshed_at"] == work["refreshed_at"] == expected


async def test_a_refresh_shows_what_was_written_since_the_last(http: httpx.AsyncClient, postgres_pool: AsyncPostgreSQLPool) -> None:
    """The views hold the last refresh's rows until the next one."""
    before = await _get(http, "/api/admin/identity/ledger/density", {})
    await _execute(
        postgres_pool,
        "UPDATE provider_aliases SET valid_to = '2026-09-20T00:00:00Z' "
        "WHERE provider = 'discogs' AND entity_kind = 'release' AND external_id = '900001' AND valid_to IS NULL",
    )

    assert await _get(http, "/api/admin/identity/ledger/density", {}) == before
    await _refresh(postgres_pool)
    after = await _get(http, "/api/admin/identity/ledger/density", {})
    assert {(row["month"], row["type"]): row["events"] for row in after["buckets"]}[("2026-09", "alias_closed")] >= 1
    assert datetime.fromisoformat(after["refreshed_at"]) > datetime.fromisoformat(before["refreshed_at"])


_VIEWS = ("identity_ledger_edition_events", "identity_ledger_edition_parents", "identity_ledger_work_summary", "identity_ledger_density")


@pytest.mark.parametrize("forget_state", [True, False], ids=["never-refreshed", "view-recreated"])
async def test_unpopulated_views_answer_503_with_retry_after_never_an_empty_list(
    http: httpx.AsyncClient, postgres_pool: AsyncPostgreSQLPool, *, forget_state: bool
) -> None:
    for view in _VIEWS:
        await _execute(postgres_pool, f"REFRESH MATERIALIZED VIEW {view} WITH NO DATA")
    if forget_state:
        await _execute(postgres_pool, "DELETE FROM identity_ledger_refresh_state")

    for path in ("/api/admin/identity/ledger/works", "/api/admin/identity/ledger/density"):
        response = await http.get(path)
        assert response.status_code == 503, response.text
        assert response.json() == {"detail": "identity ledger not yet refreshed"}
        assert response.headers["Retry-After"] == "60"

    # One work reads the base tables live, so it still answers.
    work = await _work(http, W1)
    assert [edition["gm_id"] for edition in work["editions"]] == [E1, E2, E4, E3, E5]
    assert (work["refreshed_at"] is None) is forget_state

    await _refresh(postgres_pool)
    assert (await http.get("/api/admin/identity/ledger/works")).json()["total"] == 3
