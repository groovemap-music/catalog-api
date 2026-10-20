"""Throwaway real Valkey + PostgreSQL containers for the opt-in noeviction tests.

Used only by ``tests/test_valkey_noeviction_real.py`` through ``just
integration-valkey`` (gm-catalog-api-u5i2). Nothing here runs in the default
checks: the tests are marked ``integration`` + ``valkey_noeviction`` and skip
unless ``CATALOG_VALKEY_INTEGRATION=1`` is set and a Docker daemon answers.

Every container gets a unique ``gm-catalog-api-valkey-it-<hex>`` name and the
``gm-catalog-api-valkey-it`` label, publishes its port only on 127.0.0.1 at a
random host port, and is removed (with its anonymous volumes) by the fixture
finalizer whatever the test outcome.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import valkey.asyncio as aiovalkey
from valkey.exceptions import OutOfMemoryError


OPT_IN_ENV = "CATALOG_VALKEY_INTEGRATION"
CONTAINER_LABEL = "gm-catalog-api-valkey-it"
NAME_PREFIX = "gm-catalog-api-valkey-it-"

# The exact production pin from groovemap-music/deployment docker-compose.yml.
VALKEY_IMAGE = os.environ.get(
    "VALKEY_INTEGRATION_IMAGE",
    "valkey/valkey:9.1.2-alpine@sha256:48332870af354a799964c0012ae1194a0bf2bf894eb508f945810596dc2d8d11",
)
# The image the schema's required integration tier and `just test-integration` target.
POSTGRES_IMAGE = os.environ.get(
    "POSTGRES_INTEGRATION_IMAGE",
    "postgres:18-alpine@sha256:d3e1620b530c944afa6e887d22eb899824da68e19c52024bf98f5220c88a65b2",
)
# Tiny limit so the fill is fast; production uses 512mb with the same policy.
VALKEY_MAXMEMORY_BYTES = 4 * 1024 * 1024
POSTGRES_PASSWORD = "valkey-integration-password"
POSTGRES_USER = "groovemap"
POSTGRES_DB = "groovemap"


class DockerUnavailableError(RuntimeError):
    """No usable Docker CLI/daemon: the opt-in tests skip instead of failing."""


def _docker(*args: str, timeout: float = 120) -> str:
    result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout, check=False)  # noqa: S603, S607
    if result.returncode != 0:
        raise RuntimeError(f"docker {args[0]} failed ({result.returncode}): {result.stderr.strip()}")
    return result.stdout.strip()


def require_docker() -> None:
    if shutil.which("docker") is None:
        raise DockerUnavailableError("docker CLI not found")
    try:
        _docker("info", "--format", "{{.ServerVersion}}", timeout=20)
    except (RuntimeError, subprocess.TimeoutExpired) as exc:
        raise DockerUnavailableError(f"docker daemon unavailable: {exc}") from exc


@dataclass
class Container:
    name: str
    image: str
    host: str
    port: int

    def remove(self) -> None:
        subprocess.run(["docker", "rm", "--force", "--volumes", self.name], capture_output=True, timeout=60, check=False)  # noqa: S603, S607

    def exec(self, *args: str) -> str:
        return _docker("exec", self.name, *args)

    def image_digest(self) -> str:
        """``RepoDigests`` of the image the container actually runs."""
        image_id = _docker("inspect", "--format", "{{.Image}}", self.name)
        return _docker("image", "inspect", "--format", '{{join .RepoDigests ","}}', image_id)


def _run(image: str, container_port: int, *args: str, env: dict[str, str] | None = None) -> Container:
    name = f"{NAME_PREFIX}{uuid.uuid4().hex[:12]}"
    env_args: list[str] = []
    for key, value in (env or {}).items():
        env_args += ["--env", f"{key}={value}"]
    _docker(
        "run",
        "--detach",
        "--rm",
        "--name",
        name,
        "--label",
        f"{CONTAINER_LABEL}=1",
        "--publish",
        f"127.0.0.1::{container_port}",
        *env_args,
        image,
        *args,
        timeout=300,
    )
    container = Container(name=name, image=image, host="127.0.0.1", port=0)
    try:
        published = _docker("port", name, f"{container_port}/tcp").splitlines()[0]
        container.port = int(published.rsplit(":", 1)[1])
    except Exception:
        container.remove()
        raise
    return container


def _wait(check: Callable[[], bool], what: str, container: Container, attempts: int = 60) -> None:
    for _ in range(attempts):
        try:
            if check():
                return
        except RuntimeError:
            pass
        time.sleep(1)
    logs = subprocess.run(["docker", "logs", container.name], capture_output=True, text=True, check=False).stderr  # noqa: S603, S607
    container.remove()
    raise RuntimeError(f"{what} did not become ready within {attempts}s:\n{logs[-2000:]}")


def start_valkey() -> Container:
    container = _run(
        VALKEY_IMAGE,
        6379,
        "valkey-server",
        "--maxmemory",
        str(VALKEY_MAXMEMORY_BYTES),
        "--maxmemory-policy",
        "noeviction",
        "--save",
        "",
        "--appendonly",
        "no",
    )
    _wait(lambda: container.exec("valkey-cli", "ping") == "PONG", "Valkey", container)
    return container


def start_postgres() -> Container:
    container = _run(
        POSTGRES_IMAGE,
        5432,
        env={"POSTGRES_USER": POSTGRES_USER, "POSTGRES_PASSWORD": POSTGRES_PASSWORD, "POSTGRES_DB": POSTGRES_DB},
    )
    # TCP probe: during initdb the entrypoint's temporary server listens on the
    # unix socket only, so this cannot report ready before the real server.
    _wait(
        lambda: bool(container.exec("pg_isready", "-h", "127.0.0.1", "-U", POSTGRES_USER, "-d", POSTGRES_DB)),
        "PostgreSQL",
        container,
    )
    return container


def leftover_containers() -> list[str]:
    """Names of any labelled containers still present (running or not)."""
    out = _docker("ps", "--all", "--filter", f"label={CONTAINER_LABEL}", "--format", "{{.Names}}")
    return [line for line in out.splitlines() if line]


class ValkeyFiller:
    """Drive a real noeviction Valkey to (and back from) ``used_memory > maxmemory``.

    ``fill`` writes ``filler:*`` keys from its OWN connection, stepping the value
    size down until even a one-byte SET is refused — so the first rejection is
    the server's own, reached by filling, not configured. One subtlety that the
    fake does not need: a rejected command's query buffer is freed right after
    the rejection, so a server just past the limit can briefly dip back under it
    and accept a small write. To keep "full" stable for the scenario under test,
    ``fill`` then pins ``maxmemory`` 256 KiB below the observed ``used_memory``
    (still ``noeviction``; connection churn can no longer flip it). ``drain``
    restores the configured limit and deletes only the fillers.
    """

    _SIZES = (32 * 1024, 1024, 64, 1)
    _MARGIN = 256 * 1024

    def __init__(self, client: aiovalkey.Valkey, maxmemory: int = VALKEY_MAXMEMORY_BYTES) -> None:
        self._client = client
        self._maxmemory = maxmemory
        self._seq = 0
        self.first_rejection: OutOfMemoryError | None = None

    async def used_memory(self) -> int:
        info = await self._client.info("memory")
        return int(info["used_memory"])

    async def fill(self) -> None:
        for size in self._SIZES:
            value = "x" * size
            while True:
                try:
                    await self._client.set(f"filler:{self._seq}", value)
                except OutOfMemoryError as exc:
                    self.first_rejection = self.first_rejection or exc
                    break
                self._seq += 1
        used = await self.used_memory()
        await self._client.config_set("maxmemory", str(max(used - self._MARGIN, 1)))
        try:
            await self._client.set("filler:probe", "1")
        except OutOfMemoryError:
            return
        raise AssertionError("Valkey still accepted a write after being filled")

    async def drain(self) -> None:
        """Make room again, keeping every non-filler key (tokens, counters) intact."""
        await self._client.config_set("maxmemory", str(self._maxmemory))
        batch: list[str] = []
        async for key in self._client.scan_iter(match="filler:*", count=500):
            batch.append(key)
            if len(batch) >= 500:
                await self._client.delete(*batch)
                batch.clear()
        if batch:
            await self._client.delete(*batch)
        await self._client.set("filler:probe", "1")  # proves writes are accepted again
        await self._client.delete("filler:probe")


class FillAfterWrites:
    """Real-client proxy: let N memory-adding writes through, then fill Valkey.

    Models "Valkey filled up between two writes of one request" (another writer
    took the remaining memory). The rejection of the next write is the real
    server's; this proxy only chooses *when* the server becomes full. Writes are
    recorded in ``events`` the same way ``NoEvictionValkey`` records them.
    """

    WRITE_COMMANDS = frozenset({"set", "setex", "incr", "decr"})

    def __init__(self, client: Any, filler: ValkeyFiller, after: int) -> None:
        self._client = client
        self._filler = filler
        self._remaining = after
        self.events: list[str] = []

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._client, name)
        if name not in self.WRITE_COMMANDS:
            return attr

        async def _write(key: str, *args: Any, **kwargs: Any) -> Any:
            try:
                result = await attr(key, *args, **kwargs)
            except OutOfMemoryError:
                self.events.append(f"{name}-rejected:{key}")
                raise
            self.events.append(f"{name}:{key}")
            self._remaining -= 1
            if self._remaining == 0:
                await self._filler.fill()
            return result

        return _write


async def capture_rejection(call: Callable[[], Awaitable[Any]]) -> BaseException:
    """Await *call* and return the exception it raised (fail if it succeeded)."""
    try:
        await call()
    except Exception as exc:
        return exc
    raise AssertionError("expected the write to be rejected")
