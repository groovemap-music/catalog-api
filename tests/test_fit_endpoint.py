"""GET /api/fit/release/{release_id} — the CrateFit endpoint, end to end and offline.

The graph reads and the rarity read are patched at the router's own names, so what these
tests exercise is the plumbing the bead owns: the auth, the 404, the cache-then-stamp
ordering, and the native-id gap. The scoring itself is tested on hand-built inputs in
`test_fit.py`, where it has no endpoint in the way.

The orderings asserted here are the ones that would fail silently if they regressed. A
cached body that carried an `impression_id` would hand every later viewer somebody else's
impression, and nothing about the response would look wrong; a release with no native id
that raised would turn a missing alias into a 500.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

from fastapi.testclient import TestClient

from api.app_tokens import generate_plaintext_token, hash_token


_APP_USER_ID = "99999999-9999-9999-9999-999999999999"
_APP_TOKEN_ID = "11111111-1111-1111-1111-111111111111"
_NATIVE_ID = "77777777-7777-7777-7777-777777777777"
_IMPRESSION_ID = "88888888-8888-8888-8888-888888888888"
_PATH = "/api/fit/release/555"


def _context() -> dict[str, Any]:
    """The candidate's context, as `get_release_context` returns it."""
    return {
        "id": "555",
        "title": "Blue Train",
        "year": 1957,
        "artists": [{"id": "a-coltrane", "name": "John Coltrane"}],
        "labels": [{"id": "l-blue-note", "name": "Blue Note"}],
        "genres": ["Jazz"],
        "styles": ["Hard Bop"],
        "media_families": ["grooved"],
        "master_id": "m-blue-train",
        "master_title": "Blue Train",
        "siblings": [],
    }


def _collection() -> dict[str, Any]:
    """A folded collection of three Blue Note records."""
    from api.queries.fit_queries import fold_collection

    return fold_collection(
        [
            {
                "release_id": str(index),
                "title": f"LP {index}",
                "artist_ids": [f"a-{index}"],
                "label_ids": ["l-blue-note"],
                "genres": ["Jazz"],
                "styles": ["Hard Bop"],
                "master_ids": [],
            }
            for index in range(3)
        ]
    )


def _token_row(scopes: Sequence[str], token_hash: str) -> dict[str, Any]:
    """An active `app_tokens` row shaped the way `_lookup_active_token` returns one."""
    return {
        "id": UUID(_APP_TOKEN_ID),
        "user_id": UUID(_APP_USER_ID),
        "name": "GRUVAX kiosk",
        "scope": list(scopes),
        "token_hash": token_hash,
    }


def _app_token(mock_cur: MagicMock, scopes: Sequence[str]) -> dict[str, str]:
    """Headers for a live app token carrying `scopes`."""
    plaintext = generate_plaintext_token()
    mock_cur.fetchone = AsyncMock(return_value=_token_row(scopes, hash_token(plaintext)))
    return {"Authorization": f"Bearer {plaintext}"}


def _patch_reads(context: dict[str, Any] | None = None, rarity: dict[str, Any] | None = None, native: dict[str, str] | None = None) -> list[Any]:
    """The four patches every happy-path request needs, as a list of started patchers."""
    patchers = [
        patch("api.routers.fit.get_release_context", AsyncMock(return_value=_context() if context is None else context)),
        patch("api.routers.fit.get_collection_ids", AsyncMock(return_value=_collection())),
        patch("api.routers.fit.get_release_rarity", AsyncMock(return_value=rarity)),
        patch("api.routers.fit.native_ids_for", AsyncMock(return_value={"555": _NATIVE_ID} if native is None else native)),
    ]
    for patcher in patchers:
        patcher.start()
    return patchers


def _stop(patchers: list[Any]) -> None:
    for patcher in patchers:
        patcher.stop()


# ──────────────────────────────────────────────────────────────────────────────
# The profile
# ──────────────────────────────────────────────────────────────────────────────


def test_full_profile(test_client: TestClient, auth_headers: dict[str, str]) -> None:
    """The whole decomposition, the identity, and the version that produced it."""
    patchers = _patch_reads(rarity={"score": 72.5, "tier": "scarce"})
    impressions = patch("api.activity.record_impressions", AsyncMock(return_value=[_IMPRESSION_ID]))
    impressions.start()
    try:
        response = test_client.get(_PATH, headers=auth_headers)
    finally:
        impressions.stop()
        _stop(patchers)

    assert response.status_code == 200
    body = response.json()
    assert body["release"] == {
        "id": "555",
        "gm_id": _NATIVE_ID,
        "title": "Blue Train",
        "artist": "John Coltrane",
        "year": 1957,
        "media_families": ["grooved"],
        "rarity": {"score": 72.5, "tier": "scarce"},
    }
    assert set(body["components"]) == {"affinity", "novelty", "bridge", "depth", "redundancy"}
    for component in body["components"].values():
        assert 0.0 <= component["score"] <= 1.0
        assert isinstance(component["evidence"], list)
        assert isinstance(component["evidence_items"], list)
        assert len(component["evidence_items"]) == len(component["evidence"])
    assert 0.0 <= body["fit"] <= 1.0
    assert body["confidence"] == "exact"
    assert body["policy_id"] == "cratefit_v0"
    assert body["fit_version"] == "cratefit_v0"
    assert body["impression_id"] == _IMPRESSION_ID


def test_profile_evidence_cites_the_callers_own_collection(test_client: TestClient, auth_headers: dict[str, str]) -> None:
    """The point of the surface: a fact the collector can check against their shelves."""
    patchers = _patch_reads()
    impressions = patch("api.activity.record_impressions", AsyncMock(return_value=[_IMPRESSION_ID]))
    impressions.start()
    try:
        body = test_client.get(_PATH, headers=auth_headers).json()
    finally:
        impressions.stop()
        _stop(patchers)

    assert "shares label Blue Note with 3 releases you hold" in body["components"]["affinity"]["evidence"]
    assert body["components"]["depth"]["evidence"] == ["deepens label Blue Note (3 held)"]
    assert body["components"]["depth"]["evidence_items"] == [{"dimension": "label", "entity": "Blue Note", "kind": "thread", "count": 3}]
    assert {"dimension": "label", "entity": "Blue Note", "kind": "shared", "count": 3} in body["components"]["affinity"]["evidence_items"]


def test_unknown_release_is_a_404(test_client: TestClient, auth_headers: dict[str, str]) -> None:
    """No release in the graph, no profile — and no collection read paid for."""
    context = patch("api.routers.fit.get_release_context", AsyncMock(return_value=None))
    collection = patch("api.routers.fit.get_collection_ids", AsyncMock(return_value=_collection()))
    context.start()
    collection_mock = collection.start()
    try:
        response = test_client.get("/api/fit/release/nope", headers=auth_headers)
    finally:
        collection.stop()
        context.stop()

    assert response.status_code == 404
    assert "not found" in response.json()["error"]
    collection_mock.assert_not_awaited()


def test_service_not_ready_is_a_503(test_client: TestClient, auth_headers: dict[str, str]) -> None:
    """An unwired graph is a 503, not a profile computed against nothing."""
    import api.routers.fit as mod

    original = mod._neo4j_driver
    mod._neo4j_driver = None
    try:
        response = test_client.get(_PATH, headers=auth_headers)
    finally:
        mod._neo4j_driver = original

    assert response.status_code == 503


# ──────────────────────────────────────────────────────────────────────────────
# Cache, then stamp
# ──────────────────────────────────────────────────────────────────────────────


def test_cached_body_never_carries_an_impression_id(test_client: TestClient, auth_headers: dict[str, str], mock_redis: AsyncMock) -> None:
    """What goes into Redis is the profile, not the record of one person seeing it.

    Asserted against the bytes the cache actually wrote rather than against the dictionary
    it was handed, because the handed dictionary is stamped a moment later and a reference
    held by a mock would show the stamp that the serialised value never carries.
    """
    patchers = _patch_reads()
    impressions = patch("api.activity.record_impressions", AsyncMock(return_value=[_IMPRESSION_ID]))
    impressions.start()
    try:
        response = test_client.get(_PATH, headers=auth_headers)
    finally:
        impressions.stop()
        _stop(patchers)

    assert response.json()["impression_id"] == _IMPRESSION_ID
    written = [json.loads(call.args[1]) for call in mock_redis.set.await_args_list if str(call.args[0]).endswith("fit:release:555")]
    assert written and written[0]["impression_id"] is None
    assert written[0]["fit"] == response.json()["fit"]
    assert written[0]["components"]["depth"]["evidence_items"] == response.json()["components"]["depth"]["evidence_items"]


def test_cache_hit_still_stamps_a_fresh_impression(test_client: TestClient, auth_headers: dict[str, str]) -> None:
    """An impression records a *showing*; the request that filled the cache is not it."""
    stored = {"release": {"id": "555", "gm_id": _NATIVE_ID}, "fit": 0.75, "impression_id": None}

    async def _get(key: str) -> dict[str, Any] | None:
        return dict(stored) if key.endswith("fit:release:555") else None

    context = patch("api.routers.fit.get_release_context", AsyncMock(return_value=_context()))
    cache_get = patch("api.cache.RecommendCache.get", AsyncMock(side_effect=_get))
    impressions = patch("api.activity.record_impressions", AsyncMock(return_value=[_IMPRESSION_ID]))
    context_mock = context.start()
    cache_get.start()
    impression_mock = impressions.start()
    try:
        response = test_client.get(_PATH, headers=auth_headers)
    finally:
        impressions.stop()
        cache_get.stop()
        context.stop()

    assert response.status_code == 200
    assert response.json()["impression_id"] == _IMPRESSION_ID
    # Served from the cache: no graph read, and still a fresh row.
    context_mock.assert_not_awaited()
    impression_mock.assert_awaited_once()
    surface, policy_id = impression_mock.await_args.args[1], impression_mock.await_args.args[2]
    items = impression_mock.await_args.args[4]
    assert policy_id == "cratefit_v0"
    assert items == [(1, _NATIVE_ID, 0.75, 1.0)]
    from common.events import surfaces

    assert surface in surfaces()


def test_impression_is_stamped_at_position_one_with_the_fit_as_its_score(test_client: TestClient, auth_headers: dict[str, str]) -> None:
    """CrateFit ranks nothing and samples nothing: one item, rank one, propensity one."""
    patchers = _patch_reads()
    impressions = patch("api.activity.record_impressions", AsyncMock(return_value=[_IMPRESSION_ID]))
    impression_mock = impressions.start()
    try:
        body = test_client.get(_PATH, headers=auth_headers).json()
    finally:
        impressions.stop()
        _stop(patchers)

    position, item_id, score, propensity = impression_mock.await_args.args[4][0]
    assert (position, item_id, propensity) == (1, _NATIVE_ID, 1.0)
    assert score == body["fit"]


def test_missing_native_id_is_counted_not_raised(test_client: TestClient, auth_headers: dict[str, str]) -> None:
    """A release the alias table does not carry still gets a profile, and the gap is counted."""
    patchers = _patch_reads(native={})
    counter = patch("api.activity.count_unidentified_candidate")
    impressions = patch("api.activity.record_impressions", AsyncMock())
    counter_mock = counter.start()
    impression_mock = impressions.start()
    try:
        response = test_client.get(_PATH, headers=auth_headers)
    finally:
        impressions.stop()
        counter.stop()
        _stop(patchers)

    assert response.status_code == 200
    assert response.json()["impression_id"] is None
    assert response.json()["release"]["gm_id"] is None
    counter_mock.assert_called_once()
    impression_mock.assert_not_awaited()


def test_a_dropped_impression_leaves_a_null_id(test_client: TestClient, auth_headers: dict[str, str]) -> None:
    """A client can never report an outcome against a row that was not written."""
    patchers = _patch_reads()
    impressions = patch("api.activity.record_impressions", AsyncMock(return_value=[None]))
    impressions.start()
    try:
        response = test_client.get(_PATH, headers=auth_headers)
    finally:
        impressions.stop()
        _stop(patchers)

    assert response.json()["impression_id"] is None


# ──────────────────────────────────────────────────────────────────────────────
# Delegated access
# ──────────────────────────────────────────────────────────────────────────────


def test_app_token_with_the_fit_scope_is_accepted(test_client: TestClient, mock_cur: MagicMock) -> None:
    """A kiosk scoring a record in a shop has no session, and does not need one."""
    patchers = _patch_reads()
    impressions = patch("api.activity.record_impressions", AsyncMock(return_value=[_IMPRESSION_ID]))
    impressions.start()
    try:
        response = test_client.get(_PATH, headers=_app_token(mock_cur, ["fit:read"]))
    finally:
        impressions.stop()
        _stop(patchers)

    assert response.status_code == 200
    assert response.json()["policy_id"] == "cratefit_v0"


def test_app_token_with_the_wrong_scope_is_a_403(test_client: TestClient, mock_cur: MagicMock) -> None:
    """`collection:read` lists a collection; it does not authorise scoring against it."""
    response = test_client.get(_PATH, headers=_app_token(mock_cur, ["collection:read"]))

    assert response.status_code == 403
    assert "fit:read" in response.json()["detail"]


def test_an_unauthenticated_caller_is_a_401(test_client: TestClient) -> None:
    """A fit answer is about somebody's collection, so there is no anonymous one."""
    assert test_client.get(_PATH).status_code == 401


def test_the_fit_scope_is_in_the_public_vocabulary() -> None:
    """A scope the mint endpoint rejects is a scope no delegate could ever hold."""
    from api.routers.app_tokens import ALLOWED_SCOPES

    assert "fit:read" in ALLOWED_SCOPES


# ──────────────────────────────────────────────────────────────────────────────
# The constants the stored rows are keyed on
# ──────────────────────────────────────────────────────────────────────────────


def test_the_policy_id_is_the_fit_version() -> None:
    """One string identifies the decision procedure in the response and in the row."""
    import api.activity as activity
    from api.fit import FIT_VERSION

    assert activity.POLICY_CRATEFIT == FIT_VERSION


def test_the_fit_surface_is_one_the_vocabulary_accepts() -> None:
    """The real invariant behind SURFACE_FIT, and what will keep holding when it changes.

    ADR 0010's vendored vocabulary closes the surface set and `validate_impression`
    rejects anything outside it, so a surface constant that is not a member would drop
    every fit impression silently. This is the assertion that catches that, whether the
    constant stays aliased to `recommendation` or becomes a literal `fit` the day the
    vocabulary carries one.
    """
    from common.events import surfaces

    import api.activity as activity

    assert activity.SURFACE_FIT in surfaces()
