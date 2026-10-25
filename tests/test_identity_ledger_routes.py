"""The three ``admin_identity_ledger_*`` routes: admin guard, readiness, paging, and validation."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import pytest

from api.identity_ledger import DEFAULT_EDITION_LIMIT, MAX_EDITION_LIMIT
from api.queries.identity_ledger_queries import LedgerNotRefreshed
from tests.test_admin_endpoints import _admin_auth_headers


if TYPE_CHECKING:
    from fastapi.testclient import TestClient


WORKS = "/api/admin/identity/ledger/works"
WORK_ID = "00000000-0000-4000-8000-000000000101"
WORK = f"{WORKS}/{WORK_ID}"
DENSITY = "/api/admin/identity/ledger/density"

_EMPTY_WORKS = {"works": [], "total": 0, "page": 1, "page_size": 50, "refreshed_at": "2026-10-25T09:30:00Z"}


@pytest.mark.parametrize("path", [WORKS, WORK, DENSITY])
def test_routes_require_an_admin(test_client: TestClient, path: str) -> None:
    assert test_client.get(path).status_code in (401, 403)


@pytest.mark.parametrize("path", [WORKS, WORK, DENSITY])
def test_routes_are_503_without_a_pool(test_client: TestClient, path: str) -> None:
    with patch("api.routers.admin._pool", None):
        assert test_client.get(path, headers=_admin_auth_headers()).status_code == 503


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
def test_routes_are_read_only(test_client: TestClient, method: str) -> None:
    for path in (WORKS, WORK, DENSITY):
        assert getattr(test_client, method)(path, headers=_admin_auth_headers()).status_code == 405


def test_works_list_clamps_paging_and_has_no_default_window(test_client: TestClient) -> None:
    with patch("api.routers.admin.get_ledger_works", new=AsyncMock(return_value=_EMPTY_WORKS)) as query:
        resp = test_client.get(WORKS, params={"page": 0, "page_size": 1000}, headers=_admin_auth_headers())
        assert resp.status_code == 200
        assert resp.json() == _EMPTY_WORKS
        assert query.call_args.kwargs == {"event": None, "since": None, "until": None, "page": 1, "page_size": 100}

        test_client.get(WORKS, params={"page_size": 0}, headers=_admin_auth_headers())
        assert query.call_args.kwargs["page_size"] == 1


def test_works_list_passes_its_filters(test_client: TestClient) -> None:
    with patch("api.routers.admin.get_ledger_works", new=AsyncMock(return_value=_EMPTY_WORKS)) as query:
        resp = test_client.get(
            WORKS,
            params={"event": "link_added", "since": "2026-09-01T00:00:00Z", "until": "2026-10-01T02:00:00+02:00", "page": 3, "page_size": 25},
            headers=_admin_auth_headers(),
        )
    assert resp.status_code == 200
    kwargs = query.call_args.kwargs
    assert kwargs["event"] == "link_added"
    assert kwargs["since"] == datetime(2026, 9, 1, tzinfo=UTC)
    assert kwargs["until"] == datetime(2026, 10, 1, tzinfo=UTC)
    assert (kwargs["page"], kwargs["page_size"]) == (3, 25)


@pytest.mark.parametrize(
    "params",
    [{"event": "minted"}, {"event": "deleted"}, {"since": "2026-09-01T00:00:00"}, {"until": "yesterday"}],
)
def test_works_list_rejects_unknown_events_and_naive_times(test_client: TestClient, params: dict[str, str]) -> None:
    with patch("api.routers.admin.get_ledger_works", new=AsyncMock(return_value=_EMPTY_WORKS)) as query:
        assert test_client.get(WORKS, params=params, headers=_admin_auth_headers()).status_code == 422
    query.assert_not_awaited()


def test_work_passes_as_of_and_edition_limit(test_client: TestClient) -> None:
    payload: dict[str, Any] = {"work": {"gm_id": WORK_ID}, "as_of": "2026-09-01T12:00:00Z", "editions": [], "edition_count": 0, "truncated": False}
    with patch("api.routers.admin.get_ledger_work", new=AsyncMock(return_value=payload)) as query:
        resp = test_client.get(WORK, params={"as_of": "2026-09-01T12:00:00Z", "edition_limit": 7}, headers=_admin_auth_headers())
        assert resp.status_code == 200
        assert resp.json() == payload
        assert query.call_args.args[1] == WORK_ID
        assert query.call_args.kwargs == {"as_of": datetime(2026, 9, 1, 12, tzinfo=UTC), "edition_limit": 7}

        test_client.get(WORK, headers=_admin_auth_headers())
        assert query.call_args.kwargs == {"as_of": None, "edition_limit": DEFAULT_EDITION_LIMIT}


def test_work_that_does_not_resolve_is_404(test_client: TestClient) -> None:
    with patch("api.routers.admin.get_ledger_work", new=AsyncMock(return_value=None)):
        resp = test_client.get(WORK, headers=_admin_auth_headers())
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Work not found"


@pytest.mark.parametrize(
    ("path", "params"),
    [
        (f"{WORKS}/not-a-uuid", {}),
        (WORK, {"edition_limit": 0}),
        (WORK, {"edition_limit": MAX_EDITION_LIMIT + 1}),
        (WORK, {"as_of": "2026-09-01T12:00:00"}),
    ],
)
def test_work_rejects_bad_ids_limits_and_naive_times(test_client: TestClient, path: str, params: dict[str, Any]) -> None:
    with patch("api.routers.admin.get_ledger_work", new=AsyncMock(return_value=None)) as query:
        assert test_client.get(path, params=params, headers=_admin_auth_headers()).status_code == 422
    query.assert_not_awaited()


def test_density_defaults_to_releases_and_takes_a_kind(test_client: TestClient) -> None:
    with patch("api.routers.admin.get_ledger_density", new=AsyncMock(return_value={"kind": "release", "buckets": []})) as query:
        resp = test_client.get(DENSITY, headers=_admin_auth_headers())
        assert resp.status_code == 200
        assert resp.json() == {"kind": "release", "buckets": []}
        assert query.call_args.args[1] == "release"

        test_client.get(DENSITY, params={"kind": "master"}, headers=_admin_auth_headers())
        assert query.call_args.args[1] == "master"

        assert test_client.get(DENSITY, params={"kind": "user"}, headers=_admin_auth_headers()).status_code == 422
        assert query.await_count == 2


@pytest.mark.parametrize(("path", "query"), [(WORKS, "get_ledger_works"), (DENSITY, "get_ledger_density")])
def test_unrefreshed_list_and_density_are_503_with_retry_after(test_client: TestClient, path: str, query: str) -> None:
    """Before the views' first refresh there is nothing true to list: 503, never an empty list."""
    with patch(f"api.routers.admin.{query}", new=AsyncMock(side_effect=LedgerNotRefreshed)):
        resp = test_client.get(path, headers=_admin_auth_headers())
    assert resp.status_code == 503
    assert resp.json()["detail"] == "identity ledger not yet refreshed"
    assert resp.headers["Retry-After"] == "60"
