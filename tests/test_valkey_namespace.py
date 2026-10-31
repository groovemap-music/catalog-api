"""Namespaced Valkey keys and the upgrade path for keys written before namespacing."""

import time
from unittest.mock import AsyncMock

import fakeredis
import pytest

from api import valkey_keys
from api.auth import REASON_CREDENTIALS_CHANGED, REASON_REVOKED, token_revocation_reason
from api.queries.admin_queries import get_valkey_storage
from api.routers import activity as activity_router
from api.snapshot_store import SnapshotStore


PREFIX = "groovemap:"


@pytest.fixture(autouse=True)
def namespaced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VALKEY_KEY_PREFIX", PREFIX)
    monkeypatch.delenv("VALKEY_LEGACY_KEYS", raising=False)
    monkeypatch.delenv("VALKEY_LEGACY_WINDOW_SECONDS", raising=False)
    valkey_keys.reset_state()


@pytest.fixture
def valkey() -> fakeredis.FakeAsyncValkey:
    return fakeredis.FakeAsyncValkey(server=fakeredis.FakeServer(server_type="valkey"), decode_responses=True)


async def _all_keys(valkey: fakeredis.FakeAsyncValkey) -> set[str]:
    return {key async for key in valkey.scan_iter(match="*")}


def test_keys_carry_the_prefix_and_the_prefix_can_be_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    assert valkey_keys.valkey_key("revoked:jti:abc") == "groovemap:revoked:jti:abc"
    assert valkey_keys.legacy_name("groovemap:revoked:jti:abc") == "revoked:jti:abc"
    monkeypatch.setenv("VALKEY_KEY_PREFIX", "")
    assert valkey_keys.valkey_key("revoked:jti:abc") == "revoked:jti:abc"
    assert valkey_keys.legacy_name("revoked:jti:abc") is None


def test_an_unknown_legacy_mode_fails_at_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VALKEY_LEGACY_KEYS", "yes")
    with pytest.raises(ValueError, match="VALKEY_LEGACY_KEYS"):
        valkey_keys.legacy_mode()


class TestUpgradeWithLegacyKeysPresent:
    """A dedicated stack that ran unprefixed, upgraded with no operator action (default mode)."""

    @pytest.mark.asyncio
    async def test_a_token_revoked_before_the_upgrade_stays_revoked(self, valkey: fakeredis.FakeAsyncValkey) -> None:
        await valkey.setex("revoked:jti:old-jti", 600, "1")
        await valkey_keys.prepare_legacy_keys(valkey)

        reason = await token_revocation_reason({"jti": "old-jti", "sub": "u1", "iat": 1}, valkey)

        assert reason == REASON_REVOKED

    @pytest.mark.asyncio
    async def test_a_password_change_before_the_upgrade_still_invalidates_older_sessions(self, valkey: fakeredis.FakeAsyncValkey) -> None:
        await valkey.setex("password_changed:u1", 600, "2000")
        await valkey_keys.prepare_legacy_keys(valkey)

        older = await token_revocation_reason({"jti": "j", "sub": "u1", "iat": 1999}, valkey)
        newer = await token_revocation_reason({"jti": "j", "sub": "u1", "iat": 2001}, valkey)

        assert older == REASON_CREDENTIALS_CHANGED
        assert newer is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("name", ["reset:tok", "snapshot:tok", "2fa_challenge:j1", "discogs:oauth:state:s1", "sync:lock:u1"])
    async def test_a_foreign_authorization_key_is_never_read_from_the_legacy_spelling(self, valkey: fakeredis.FakeAsyncValkey, name: str) -> None:
        """Another app on a shared Valkey owns ``reset:<token>`` etc.; ours must not accept it."""
        await valkey.setex(name, 600, '{"user_id": "victim"}')
        await valkey_keys.prepare_legacy_keys(valkey)

        assert await valkey_keys.get_deny_with_legacy(valkey, valkey_keys.valkey_key(name)) is None
        assert await valkey.get(valkey_keys.valkey_key(name)) is None
        assert await valkey.get(name) == '{"user_id": "victim"}'

    @pytest.mark.asyncio
    async def test_a_foreign_deny_key_can_only_deny(self, valkey: fakeredis.FakeAsyncValkey) -> None:
        await valkey.setex("revoked:jti:foreign", 600, "1")

        assert await token_revocation_reason({"jti": "foreign", "sub": "u1", "iat": 1}, valkey) == REASON_REVOKED
        assert await token_revocation_reason({"jti": "other", "sub": "u1", "iat": 1}, valkey) is None

    @pytest.mark.asyncio
    async def test_the_prefixed_key_wins_and_writes_never_create_unprefixed_keys(self, valkey: fakeredis.FakeAsyncValkey) -> None:
        await valkey.setex("password_changed:u1", 600, "1000")
        await valkey.setex(valkey_keys.valkey_key("password_changed:u1"), 600, "3000")

        assert await valkey_keys.get_deny_with_legacy(valkey, valkey_keys.valkey_key("password_changed:u1")) == "3000"

    @pytest.mark.asyncio
    async def test_the_legacy_window_closes_after_the_configured_seconds(
        self, valkey: fakeredis.FakeAsyncValkey, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await valkey.setex("revoked:jti:old-jti", 600, "1")
        await valkey.set(valkey_keys.valkey_key("legacy-keys:cutover"), str(int(time.time()) - 100))
        monkeypatch.setenv("VALKEY_LEGACY_WINDOW_SECONDS", "50")

        assert await token_revocation_reason({"jti": "old-jti", "sub": "u1", "iat": 1}, valkey) is None

    @pytest.mark.asyncio
    async def test_the_first_process_fixes_the_cutover_for_the_fleet(self, valkey: fakeredis.FakeAsyncValkey) -> None:
        await valkey_keys.prepare_legacy_keys(valkey)
        first = await valkey.get(valkey_keys.valkey_key("legacy-keys:cutover"))
        time.sleep(1.1)
        await valkey_keys.prepare_legacy_keys(valkey)

        assert await valkey.get(valkey_keys.valkey_key("legacy-keys:cutover")) == first


class TestOffAndSharedInstance:
    @pytest.mark.asyncio
    async def test_off_never_reads_or_writes_an_unprefixed_key(self, valkey: fakeredis.FakeAsyncValkey, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VALKEY_LEGACY_KEYS", "off")
        await valkey.setex("revoked:jti:foreign", 600, "1")
        await valkey_keys.prepare_legacy_keys(valkey)

        assert await token_revocation_reason({"jti": "foreign", "sub": "u1", "iat": 1}, valkey) is None
        assert await _all_keys(valkey) == {"revoked:jti:foreign"}

    @pytest.mark.asyncio
    async def test_a_disabled_prefix_has_no_legacy_spelling_to_consult(
        self, valkey: fakeredis.FakeAsyncValkey, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("VALKEY_KEY_PREFIX", "")
        await valkey_keys.prepare_legacy_keys(valkey)
        await valkey_keys.migrate_legacy_keys(valkey)

        assert await _all_keys(valkey) == set()


class TestFreshStack:
    @pytest.mark.asyncio
    async def test_a_fresh_stack_writes_only_prefixed_keys(self, valkey: fakeredis.FakeAsyncValkey) -> None:
        await valkey_keys.prepare_legacy_keys(valkey)
        await valkey.setex(valkey_keys.valkey_key("revoked:jti:j1"), 600, "1")

        assert await token_revocation_reason({"jti": "j1", "sub": "u1", "iat": 1}, valkey) == REASON_REVOKED
        assert await token_revocation_reason({"jti": "j2", "sub": "u1", "iat": 1}, valkey) is None
        assert all(key.startswith(PREFIX) for key in await _all_keys(valkey))

    def test_snapshot_keys_are_stored_under_the_prefix(self) -> None:
        assert SnapshotStore.snapshot_key("t") == "groovemap:snapshot:t"
        assert SnapshotStore.user_count_key("u1") == "groovemap:snapshot:usercount:u1"


class TestMigration:
    @pytest.mark.asyncio
    async def test_migrates_known_families_preserving_ttl_and_removes_the_legacy_copy(
        self, valkey: fakeredis.FakeAsyncValkey, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("VALKEY_LEGACY_KEYS", "migrate")
        await valkey.setex("revoked:jti:j1", 600, "1")
        await valkey.setex("password_changed:u1", 1200, "2000")
        await valkey.setex("2fa_challenge:j2", 300, "u1")
        await valkey.setex("discogs:oauth:state:s1", 500, "{}")
        await valkey.setex("reset:tok", 900, "{}")
        await valkey.setex("snapshot:abc", 5000, "payload")
        await valkey.setex("snapshot:usercount:u1", 5000, "2")

        await valkey_keys.prepare_legacy_keys(valkey)

        keys = await _all_keys(valkey)
        assert not {key for key in keys if not key.startswith(PREFIX)}
        assert await valkey.get("groovemap:revoked:jti:j1") == "1"
        assert 590 <= await valkey.ttl("groovemap:revoked:jti:j1") <= 600
        assert 1190 <= await valkey.ttl("groovemap:password_changed:u1") <= 1200
        assert await valkey.get("groovemap:snapshot:usercount:u1") == "2"
        assert await token_revocation_reason({"jti": "j1", "sub": "u9", "iat": 1}, valkey) == REASON_REVOKED

    @pytest.mark.asyncio
    async def test_is_idempotent_and_never_overwrites_a_newer_prefixed_value(self, valkey: fakeredis.FakeAsyncValkey) -> None:
        await valkey.setex("password_changed:u1", 600, "1000")
        await valkey.setex("groovemap:password_changed:u1", 600, "3000")

        first = await valkey_keys.migrate_legacy_keys(valkey)
        second = await valkey_keys.migrate_legacy_keys(valkey)

        assert first["already_present"] == 1
        assert second == {"migrated": 0, "already_present": 0, "skipped": 0, "failed": 0}
        assert await valkey.get("groovemap:password_changed:u1") == "3000"
        assert await valkey.get("password_changed:u1") is None

    @pytest.mark.asyncio
    async def test_a_foreign_applications_keys_are_left_untouched(self, valkey: fakeredis.FakeAsyncValkey, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VALKEY_LEGACY_KEYS", "migrate")
        foreign = {
            "sidekiq:queue:default": "x",
            "session:abc": "y",
            "reset:foo:bar": "shape does not match the family",
            "snapshot:a:b:c": "shape does not match the family",
            "revoked:jti:no-ttl": "permanent, not written by this service",
            "otherapp:revoked:jti:j1": "different namespace",
        }
        for key, value in foreign.items():
            if key == "revoked:jti:no-ttl":
                await valkey.set(key, value)
            else:
                await valkey.setex(key, 600, value)
        await valkey.setex("revoked:jti:ours", 600, "1")

        await valkey_keys.prepare_legacy_keys(valkey)

        for key, value in foreign.items():
            assert await valkey.get(key) == value, key
        assert await valkey.get("groovemap:revoked:jti:ours") == "1"
        assert await valkey.get("revoked:jti:ours") is None

    @pytest.mark.asyncio
    async def test_a_rejected_copy_keeps_the_legacy_key(self, valkey: fakeredis.FakeAsyncValkey) -> None:
        await valkey.setex("revoked:jti:j1", 600, "1")

        class Rejecting:
            def __getattr__(self, name: str):
                return getattr(valkey, name)

            async def set(self, *_args: object, **_kwargs: object) -> None:
                raise RuntimeError("OOM command not allowed")

        stats = await valkey_keys.migrate_legacy_keys(Rejecting())

        assert stats["failed"] == 1
        assert await valkey.get("revoked:jti:j1") == "1"

    def test_erasure_covers_the_legacy_spelling_only_when_migrating(self, monkeypatch: pytest.MonkeyPatch) -> None:
        keys = [valkey_keys.valkey_key("sync:lock:u1")]
        assert valkey_keys.with_legacy_keys(keys) == keys
        monkeypatch.setenv("VALKEY_LEGACY_KEYS", "migrate")
        assert valkey_keys.with_legacy_keys(keys) == ["groovemap:sync:lock:u1", "sync:lock:u1"]
        assert valkey_keys.with_legacy_patterns(["groovemap:recommend:enhanced:u1"]) == ["groovemap:recommend:enhanced:u1", "recommend:enhanced:u1"]


class TestSharedInstanceVisibility:
    @pytest.mark.asyncio
    async def test_admin_storage_report_only_counts_this_namespace(self, valkey: fakeredis.FakeAsyncValkey) -> None:
        await valkey.set("groovemap:search:a", "1")
        await valkey.set("groovemap:search:b", "1")
        await valkey.set("groovemap:nlq:a", "1")
        await valkey.set("otherapp:jobs:1", "1")

        valkey.info = AsyncMock(return_value={})  # fakeredis has no INFO

        report = await get_valkey_storage(valkey)

        assert report["keys_by_prefix"] == {"search:": 2, "nlq:": 1}


class TestErasure:
    """Per-user erasure removes the namespaced keys; legacy spellings only on a dedicated opt-in."""

    LEGACY_FOR_U1 = ("snapshot:usercount:u1", "sync:lock:u1", "sync:cooldown:u1", "recommend:enhanced:u1", "recommend:explore:u1:fit:collection")
    NOT_U1 = ("snapshot:usercount:u2", "recommend:enhanced:u2", "recommend:explore:u2:x", "sidekiq:queue:u1", "session:u1")

    async def _seed(self, valkey: fakeredis.FakeAsyncValkey) -> None:
        for name in self.LEGACY_FOR_U1 + self.NOT_U1:
            await valkey.setex(name, 600, "x")
        for name in self.LEGACY_FOR_U1:
            await valkey.setex(PREFIX + name, 600, "x")

    @pytest.fixture
    def wired(self, valkey: fakeredis.FakeAsyncValkey, monkeypatch: pytest.MonkeyPatch) -> fakeredis.FakeAsyncValkey:
        monkeypatch.setattr(activity_router, "_valkey", valkey)
        return valkey

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", ["off", "read"])
    async def test_without_the_dedicated_opt_in_no_unprefixed_key_is_deleted(
        self, wired: fakeredis.FakeAsyncValkey, monkeypatch: pytest.MonkeyPatch, mode: str
    ) -> None:
        monkeypatch.setenv("VALKEY_LEGACY_KEYS", mode)
        await self._seed(wired)

        assert await activity_router._erase_valkey("u1") is None

        keys = await _all_keys(wired)
        assert not {key for key in keys if key.startswith(PREFIX)}
        assert set(self.LEGACY_FOR_U1 + self.NOT_U1) <= keys

    @pytest.mark.asyncio
    async def test_the_dedicated_opt_in_also_erases_the_legacy_spellings_and_leaves_others_alone(
        self, wired: fakeredis.FakeAsyncValkey, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("VALKEY_LEGACY_KEYS", "migrate")
        await self._seed(wired)

        assert await activity_router._erase_valkey("u1") is None

        keys = await _all_keys(wired)
        assert keys == set(self.NOT_U1)
