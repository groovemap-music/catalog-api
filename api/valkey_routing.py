"""Send catalog caches to their own evicting Valkey, and everything else to the auth one.

``docker-compose.cache-isolation.yml`` (deployment, gm-deployment-t52v) adds a second instance that
runs ``allkeys-lru``. The auth instance stays ``noeviction`` because revoked-token and password-change
keys live only there, so the two kinds of key must never swap places: an auth key on the evicting
instance is lost silently, and a cache key on the noeviction one is what filled it.

:class:`RoutedValkey` stands in for the one client every caller already holds. Each command is sent by
the key it names:

* a key in a **cache family** (:data:`CACHE_FAMILIES`) goes to the cache instance, and a failure there
  (down, full, read-only, timing out) is a miss, never an error: reads return nothing, writes are not
  stored, and the caller carries on as it would after any cache miss;
* every other key goes to the auth instance, errors included. That is the default on purpose: a family
  nobody classified keeps today's behaviour, and ``tests/test_valkey_routing.py`` fails on a family in
  the source that is in neither list, so the decision is always made in review.

A name that does not start with the configured key prefix is a legacy key (see ``valkey_keys``). Legacy
keys exist only on the auth instance, so they stay there, which also lets erasure reach the old copies.

Set ``CATALOG_CACHE_VALKEY_HOST`` (and optionally ``CATALOG_CACHE_VALKEY_PORT``) to turn it on. Unset,
:func:`route_valkey` is never called and the service uses its single client exactly as before.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Final, cast

import structlog

from api.valkey_errors import VALKEY_UNAVAILABLE_ERRORS
from api.valkey_keys import valkey_key_prefix


if TYPE_CHECKING:
    import valkey.asyncio as aiovalkey


logger = structlog.get_logger(__name__)

ALERT_CACHE_UNAVAILABLE: Final = "valkey_cache_unavailable"

# Un-namespaced key stems, as passed to ``valkey_key``. Rebuildable data only: anything whose loss changes
# who may do what, or what a user is told, is an auth family.
CACHE_FAMILIES: Final[tuple[str, ...]] = (
    "explore:",
    "explore-hub:",
    "trends:",
    "expand:",
    "search:",
    "credits:",
    "label-dna:",
    "label-similar:",
    "network:",
    "nlq:",
    "enrichment:",
    "recommend:",
    "insights:",
    "budget:",
)

# Everything the service keeps on the noeviction instance. Listed so a new family has to be put in one of the
# two lists: the routing default is the auth instance, but the guard test refuses an unclassified stem.
AUTH_FAMILIES: Final[tuple[str, ...]] = (
    "revoked:jti:",
    "password_changed:",
    "reset:",
    "2fa_challenge:",
    "snapshot:",
    "discogs:oauth:state:",
    "sync:",
    "import_cache:",
    "legacy-keys:",
    "genre-tree:",
)

_REPORT_INTERVAL_S: Final = 60.0
_last_report = -_REPORT_INTERVAL_S


def is_cache_name(name: Any) -> bool:
    """Whether a key (or a SCAN glob) belongs on the cache instance."""
    text = name.decode("utf-8", errors="replace") if isinstance(name, bytes) else str(name)
    prefix = valkey_key_prefix()
    if prefix:
        if not text.startswith(prefix):
            return False
        text = text[len(prefix) :]
    return text.startswith(CACHE_FAMILIES)


def _report(command: str, error: Exception) -> None:
    """Say a cache command became a miss, at most once a minute, so a down cache does not flood the log."""
    global _last_report
    now = time.monotonic()
    if now - _last_report < _REPORT_INTERVAL_S:
        return
    _last_report = now
    logger.warning(
        "⚠️ Catalog cache instance unavailable: serving misses and skipping writes",
        alert=ALERT_CACHE_UNAVAILABLE,
        command=command,
        error_type=type(error).__name__,
    )


class RoutedValkey:
    """The service's Valkey client, sending each command to the instance its key belongs on."""

    def __init__(self, auth: Any, cache: Any, *, strict: bool = False) -> None:
        self._auth = auth
        self._cache = cache
        self._strict = strict

    def strict(self) -> RoutedValkey:
        """The same two connections, with a cache failure raised instead of served as a miss.

        For the callers that must not mistake an unreachable cache for an empty one: an erasure that
        has to verify the per-user keys are gone, and the import invalidation that must not record the
        import as handled while stale entries remain. Everything else wants the default.
        """
        return RoutedValkey(self._auth, self._cache, strict=True)

    @property
    def auth(self) -> Any:
        """The noeviction instance's client."""
        return self._auth

    @property
    def cache(self) -> Any:
        """The evicting instance's client."""
        return self._cache

    def _miss(self, command: str, error: Exception, miss: Any) -> Any:
        """The answer for a cache command that failed: the error in strict mode, otherwise a miss."""
        if self._strict:
            raise error
        _report(command, error)
        return miss

    async def _one(self, command: str, name: Any, args: tuple[Any, ...], kwargs: dict[str, Any], miss: Any) -> Any:
        if not is_cache_name(name):
            return await getattr(self._auth, command)(name, *args, **kwargs)
        try:
            return await getattr(self._cache, command)(name, *args, **kwargs)
        except VALKEY_UNAVAILABLE_ERRORS as error:
            return self._miss(command, error, miss)

    async def get(self, name: Any, *args: Any, **kwargs: Any) -> Any:
        return await self._one("get", name, args, kwargs, None)

    async def getdel(self, name: Any, *args: Any, **kwargs: Any) -> Any:
        return await self._one("getdel", name, args, kwargs, None)

    async def set(self, name: Any, *args: Any, **kwargs: Any) -> Any:
        return await self._one("set", name, args, kwargs, None)

    async def setex(self, name: Any, *args: Any, **kwargs: Any) -> Any:
        return await self._one("setex", name, args, kwargs, None)

    async def exists(self, name: Any, *args: Any, **kwargs: Any) -> Any:
        return await self._one("exists", name, args, kwargs, 0)

    async def expire(self, name: Any, *args: Any, **kwargs: Any) -> Any:
        return await self._one("expire", name, args, kwargs, False)

    async def pttl(self, name: Any, *args: Any, **kwargs: Any) -> Any:
        return await self._one("pttl", name, args, kwargs, -2)

    async def ttl(self, name: Any, *args: Any, **kwargs: Any) -> Any:
        return await self._one("ttl", name, args, kwargs, -2)

    async def incr(self, name: Any, *args: Any, **kwargs: Any) -> Any:
        return await self._one("incr", name, args, kwargs, 0)

    async def decr(self, name: Any, *args: Any, **kwargs: Any) -> Any:
        return await self._one("decr", name, args, kwargs, 0)

    async def zadd(self, name: Any, *args: Any, **kwargs: Any) -> Any:
        return await self._one("zadd", name, args, kwargs, 0)

    async def zrem(self, name: Any, *args: Any, **kwargs: Any) -> Any:
        return await self._one("zrem", name, args, kwargs, 0)

    async def zscore(self, name: Any, *args: Any, **kwargs: Any) -> Any:
        return await self._one("zscore", name, args, kwargs, None)

    async def zcard(self, name: Any, *args: Any, **kwargs: Any) -> Any:
        return await self._one("zcard", name, args, kwargs, 0)

    async def zrange(self, name: Any, *args: Any, **kwargs: Any) -> Any:
        return await self._one("zrange", name, args, kwargs, [])

    async def zrangebyscore(self, name: Any, *args: Any, **kwargs: Any) -> Any:
        return await self._one("zrangebyscore", name, args, kwargs, [])

    async def zremrangebyscore(self, name: Any, *args: Any, **kwargs: Any) -> Any:
        return await self._one("zremrangebyscore", name, args, kwargs, 0)

    async def delete(self, *names: Any) -> int:
        """Delete each key on the instance it lives on; the count is the sum."""
        cache_names = [name for name in names if is_cache_name(name)]
        auth_names = [name for name in names if not is_cache_name(name)]
        deleted = int(await self._auth.delete(*auth_names)) if auth_names else 0
        if cache_names:
            try:
                deleted += int(await self._cache.delete(*cache_names))
            except VALKEY_UNAVAILABLE_ERRORS as error:
                self._miss("delete", error, 0)
        return deleted

    async def scan(self, cursor: Any = 0, match: Any = None, count: Any = None, **kwargs: Any) -> Any:
        """SCAN the instance a glob's family belongs on; a glob naming no cache family scans the auth instance."""
        if match is None or not is_cache_name(match):
            return await self._auth.scan(cursor=cursor, match=match, count=count, **kwargs)
        try:
            return await self._cache.scan(cursor=cursor, match=match, count=count, **kwargs)
        except VALKEY_UNAVAILABLE_ERRORS as error:
            return self._miss("scan", error, (0, []))

    async def eval(self, script: str, numkeys: int, *keys_and_args: Any) -> Any:
        """Run a script on the instance its first key belongs on (the auth instance when it names no key)."""
        if not numkeys or not is_cache_name(keys_and_args[0]):
            return await self._auth.eval(script, numkeys, *keys_and_args)
        try:
            return await self._cache.eval(script, numkeys, *keys_and_args)
        except VALKEY_UNAVAILABLE_ERRORS as error:
            return self._miss("eval", error, 0)

    async def aclose(self) -> None:
        """Close both connections; the cache one never stops the auth one from closing."""
        try:
            await self._cache.aclose()
        finally:
            await self._auth.aclose()

    close = aclose

    def __getattr__(self, name: str) -> Any:
        """Anything not routed above (ping, info, connection pool) is the auth instance's."""
        return getattr(self._auth, name)


def strict_valkey(valkey: Any) -> Any:
    """``valkey`` with cache failures raised, when it routes to a cache instance; otherwise ``valkey`` itself.

    With no cache instance configured there is one client and it already raises, so nothing changes.
    """
    return valkey.strict() if isinstance(valkey, RoutedValkey) else valkey


def route_valkey(auth: aiovalkey.Valkey, cache: aiovalkey.Valkey) -> aiovalkey.Valkey:
    """Return the routing client, typed as the client it stands in for (the one place that is asserted)."""
    return cast("aiovalkey.Valkey", RoutedValkey(auth, cache))
