"""One-time backfill of the Identity Ledger's link history from the MusicBrainz full export.

`public.identity_link_events` (database-schema) records, per MusicBrainz row, when its Discogs
link changed. `musicbrainz-sql-loader` records those changes exactly from the first catalog
load onward. Everything before that is backfilled once by this job, from the MusicBrainz full
export, so the ledger starts with the past months rather than a single deploy-day burst
(design ADR 0016, 2026-10-24 amendment; spike `gm-design-6b8u`).

**What is dated.** Only releases: proxy 2 was validated on `l_release_url` alone. Each
release with a current Discogs link gets one ``link_added`` event, written with
``entity_kind = 'release'``, the release MBID as ``external_id``, and the linked Discogs
release id as ``discogs_id_after``.

**How it is dated: proxy 2, ported from the 6b8u harness** (design
``docs/spikes/gm-design-6b8u/scripts/months.py``, ``creation_bound`` and ``link_history``;
copied, not imported). ``l_release_url.id`` is a sequence, so it orders rows by creation, and
an edit moves a row's ``last_updated`` but keeps its id. The creation of row *k* is therefore
bounded by c(k), the minimum ``last_updated`` over every release-URL row of any link type
with id >= k. An unedited row's c(k) is its true creation; an edited row gets the creation of
the next unedited row, so an edit cannot push it later. A release's link time is the earliest
c(k) over its current Discogs links. c never decreases with the id, so that is the bound of
its lowest-id Discogs link, and the event names that link's Discogs id. A row whose
``last_updated`` is NULL is ignored, so one bad row cannot pull every earlier row back to
1970; a link with no timestamp at or after its row is *undated* and gets no event. The spike
scored proxy 2 out of sample at precision 0.912 and recall 0.9771: about 9% of the dates
belong to an older link whose row MusicBrainz replaced. So every event is written with
``dated_from = 'musicbrainz_fullexport'``: dated from the MusicBrainz full export, not
recorded by GrooveMap. Links removed before the export are absent from it, so the older
months are lower bounds.

A Discogs link is a ``l_release_url`` row whose link has the ``discogs`` release-URL link type
and whose URL parses as a Discogs release under `musicbrainz-ingestion`'s rule
(:func:`extract_discogs_id`). ``release_meta.date_added`` is not needed, so only
``mbdump.tar.bz2`` is read, not ``mbdump-derived.tar.bz2``.

**Streaming, never on disk.** ``mbdump.tar.bz2`` (about 7 GB) is read over HTTPS, hashed,
decompressed, and untarred in one pass (:func:`read_export`); no byte of it is written to a
file. Tables can arrive in any order, so the five it reads are kept as compact typed arrays
until the end: 24 bytes per ``l_release_url`` row, 20 per ``release`` row, 12 per Discogs
release URL, and 8 per ``link`` row. Every other table and every other URL streams through
without being kept. The creation bound is computed without sorting all release-URL rows:
each row's timestamp is folded into the bucket of the last Discogs row at or below its id,
and a suffix minimum over the buckets gives c(k).

**Checksum before any write.** The export's ``SHA256SUMS`` names the digest of
``mbdump.tar.bz2``. The bytes read are hashed as they stream, the rest of the file is drained
into the hash once the tar ends, and a mismatch raises :class:`ChecksumError`. The database is
first written only after the whole file has been read and verified.

**Dry run by default.** Without ``--apply`` the job reads and verifies the export and prints
counts, and never connects to PostgreSQL. With ``--apply`` it writes the events in batches,
``ON CONFLICT ON CONSTRAINT identity_link_events_idempotence_key DO NOTHING``, so rows already
written by an earlier run of the same export are not duplicated.

**One audit entry per applying run.** Every event names the run's ``admin_audit_log`` entry
as its ``decision_ref``, so the entry is written first, as
``identity.ledger.backfill.started``, and becomes ``identity.ledger.backfill`` with counts only
when the run finishes, or ``identity.ledger.backfill.failed``. Nobody decides when a link was
added: the dates are MusicBrainz's. So the entry is recorded against the reserved
identity-maintenance system actor (`api/reattach_trigger.py`), with
``target = 'musicbrainz-fullexport:<export>'``. A finished entry for the export is the
handled marker: a second ``--apply`` of the same export finds it before downloading anything
and writes nothing. A run that failed leaves no marker, so it can be re-run; the rows it
wrote keep its ``decision_ref``.

Run it once, after the first catalog load, with the latest full export.
"""

from __future__ import annotations

import argparse
import asyncio
import bz2
import hashlib
import io
import re
import sys
import tarfile
from array import array
from bisect import bisect_left, bisect_right
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import IO, Any, Final
from uuid import UUID, uuid4

import httpx
import structlog

from api import __version__
from api.audit_log import insert_audit_entry, update_audit_entry
from api.ledger_refresh import TRIGGER_BACKFILL, refresh_ledger
from api.reattach import _connection_params
from api.reattach_trigger import SYSTEM_ACTOR_ID, ensure_system_actor


logger = structlog.get_logger(__name__)

DEFAULT_BASE_URL: Final = "https://data.metabrainz.org/pub/musicbrainz/data/fullexport"
DUMP_FILE: Final = "mbdump.tar.bz2"
DEFAULT_BATCH_SIZE: Final = 5000

PROVIDER: Final = "musicbrainz"
ENTITY_KIND: Final = "release"
EVENT: Final = "link_added"
DATED_FROM: Final = "musicbrainz_fullexport"

STARTED_ACTION: Final = "identity.ledger.backfill.started"
APPLY_ACTION: Final = "identity.ledger.backfill"
FAILED_ACTION: Final = "identity.ledger.backfill.failed"

# A full export's directory name, e.g. 20261024-102842. Checked before it reaches a URL.
_EXPORT_NAME: Final = re.compile(r"\d{8}-\d{6}")
_SHA256_LINE: Final = re.compile(r"([0-9a-fA-F]{64})\s+\*?(\S+)")
_CHUNK: Final = 1 << 20

# No timestamp at or after a row: the harness's int64 maximum, read here as "undated".
NO_TIME: Final = 2**63 - 1

TABLES: Final = frozenset({"link_type", "link", "url", "l_release_url", "release"})


# ---------------------------------------------------------------------------
# Ported rules
# ---------------------------------------------------------------------------

# Rust's `str::parse::<i64>`: an optional `+`, then one or more ASCII digits, in range.
_I64: Final = re.compile(r"\+?[0-9]+")
_I64_MAX: Final = 2**63 - 1
_DISCOGS_RELEASE: Final = "https://www.discogs.com/release/"


def extract_discogs_id(url: str, entity: str) -> int | None:
    """`musicbrainz-ingestion`'s Discogs-id rule (`extract_discogs_id` in `jsonl_parser.rs`).

    Copied from the 6b8u harness's `extract_mb.py`, which copied it from that rule.
    """
    prefix = f"https://www.discogs.com/{entity}/"
    if not url.startswith(prefix):
        return None
    head = url[len(prefix) :].split("-", 1)[0]
    if not _I64.fullmatch(head):
        return None
    value = int(head)
    return value if value <= _I64_MAX else None


def epoch(text: str) -> int:
    """A COPY-format timestamp as whole UTC seconds, 0 for NULL (the harness's `_epoch`)."""
    if text == "\\N":
        return 0
    moment = datetime.fromisoformat(text.replace(" ", "T", 1))
    return int((moment if moment.tzinfo else moment.replace(tzinfo=UTC)).timestamp())


def creation_bounds(candidates: array, row_ids: Iterable[int], row_updated: Iterable[int]) -> array:
    """Proxy 2's c(k) for each of the sorted *candidates*, over every release-URL row.

    c(k) is the minimum non-zero ``last_updated`` over rows with id >= k, or :data:`NO_TIME`
    when there is none. Each row is folded into the bucket of the last candidate at or below
    its id, so only the candidates are sorted; a suffix minimum over the buckets then gives
    c(k) for each. The candidates must be sorted and distinct, and be among the rows.
    """
    bounds = array("q", [NO_TIME]) * len(candidates)
    for row_id, updated in zip(row_ids, row_updated, strict=True):
        if updated > 0:
            position = bisect_right(candidates, row_id)
            if position and updated < bounds[position - 1]:
                bounds[position - 1] = updated
    running = NO_TIME
    for index in range(len(bounds) - 1, -1, -1):
        running = min(running, bounds[index])
        bounds[index] = running
    return bounds


# ---------------------------------------------------------------------------
# Reading the export
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class BackfillPlan:
    """The events a backfill writes, one per dated release, ordered by release id."""

    mbids: bytearray = field(default_factory=bytearray)  # 16 bytes per event
    discogs_ids: array = field(default_factory=lambda: array("q"))
    event_at: array = field(default_factory=lambda: array("q"))
    counts: dict[str, int] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.event_at)

    def events(self) -> Iterator[tuple[str, int, int]]:
        """``(mbid, Discogs id, event_at epoch seconds)`` per event."""
        for index, (discogs_id, moment) in enumerate(zip(self.discogs_ids, self.event_at, strict=True)):
            yield str(UUID(bytes=bytes(self.mbids[16 * index : 16 * index + 16]))), discogs_id, moment

    def per_year(self) -> dict[str, int]:
        return dict(sorted(Counter(str(datetime.fromtimestamp(moment, UTC).year) for moment in self.event_at).items()))


class ExportError(RuntimeError):
    """The export cannot be backfilled from; nothing was written."""


class ChecksumError(ExportError):
    """``mbdump.tar.bz2`` does not match its ``SHA256SUMS`` digest; nothing was written."""


class ExportReader:
    """Accumulates the five tables proxy 2 needs, in compact arrays, in any member order."""

    def __init__(self) -> None:
        self.discogs_link_types: set[int] = set()
        self.link_ids, self.link_types = array("i"), array("i")
        self.url_ids, self.url_discogs = array("i"), array("q")
        self.row_ids, self.row_links, self.row_releases, self.row_urls = array("i"), array("i"), array("i"), array("i")
        self.row_updated = array("q")
        self.release_ids, self.release_gids = array("i"), bytearray()
        self.read: list[str] = []

    def read_member(self, name: str, handle: IO[bytes]) -> None:
        """Read one table in PostgreSQL COPY text format; columns as in `CreateTables.sql`."""
        if name == "link_type":
            # id, parent, child_order, gid, entity_type0, entity_type1, name, ...
            for raw in handle:
                cells = raw.decode("utf-8").rstrip("\n").split("\t")
                if cells[4] == "release" and cells[5] == "url" and cells[6] == "discogs":
                    self.discogs_link_types.add(int(cells[0]))
        elif name == "link":
            # id, link_type, ...
            for raw in handle:
                link = raw.split(b"\t", 2)
                self.link_ids.append(int(link[0]))
                self.link_types.append(int(link[1]))
        elif name == "url":
            # id, gid, url, edits_pending, last_updated. Only a Discogs release URL is kept.
            needle = f"\t{_DISCOGS_RELEASE}".encode()
            for raw in handle:
                if needle not in raw:
                    continue
                cells = raw.decode("utf-8").rstrip("\n").split("\t")
                found = extract_discogs_id(cells[2], "release")
                if found is not None:
                    self.url_ids.append(int(cells[0]))
                    self.url_discogs.append(found)
        elif name == "l_release_url":
            # id, link, entity0 (release), entity1 (url), edits_pending, last_updated, ...
            for raw in handle:
                row = raw.split(b"\t", 6)
                self.row_ids.append(int(row[0]))
                self.row_links.append(int(row[1]))
                self.row_releases.append(int(row[2]))
                self.row_urls.append(int(row[3]))
                self.row_updated.append(epoch(row[5].decode("utf-8")))
        elif name == "release":
            # id, gid, name, artist_credit, release_group, ...
            for raw in handle:
                release = raw.split(b"\t", 2)
                self.release_ids.append(int(release[0]))
                self.release_gids += UUID(release[1].decode("ascii")).bytes
        self.read.append(name)
        logger.info("📦 Read a full-export table", table=name)

    def finalize(self) -> BackfillPlan:
        """Date each release's Discogs link by proxy 2. Consumes the accumulated arrays."""
        missing = sorted(TABLES - set(self.read))
        if missing:
            raise ExportError(f"the export has no {', '.join(missing)} table")
        if not self.discogs_link_types:
            raise ExportError("the export has no discogs release-url link type")
        release_url_rows = len(self.row_ids)

        discogs_links = {link for link, link_type in zip(self.link_ids, self.link_types, strict=True) if link_type in self.discogs_link_types}
        self.link_ids, self.link_types = array("i"), array("i")
        url_order = sorted(range(len(self.url_ids)), key=self.url_ids.__getitem__)
        url_ids = array("i", (self.url_ids[i] for i in url_order))
        url_discogs = array("q", (self.url_discogs[i] for i in url_order))
        del url_order
        self.url_ids, self.url_discogs = array("i"), array("q")

        # The current Discogs release links: the discogs link type and a Discogs release URL.
        cand_ids, cand_releases, cand_discogs = array("i"), array("i"), array("q")
        for row_id, link, release, url in zip(self.row_ids, self.row_links, self.row_releases, self.row_urls, strict=True):
            if link in discogs_links:
                at = bisect_left(url_ids, url)
                if at < len(url_ids) and url_ids[at] == url:
                    cand_ids.append(row_id)
                    cand_releases.append(release)
                    cand_discogs.append(url_discogs[at])
        del url_ids, url_discogs
        self.row_links, self.row_releases, self.row_urls = array("i"), array("i"), array("i")

        order = sorted(range(len(cand_ids)), key=cand_ids.__getitem__)
        cand_ids = array("i", (cand_ids[i] for i in order))
        cand_releases = array("i", (cand_releases[i] for i in order))
        cand_discogs = array("q", (cand_discogs[i] for i in order))
        del order
        discogs_release_links = len(cand_ids)
        bounds = creation_bounds(cand_ids, self.row_ids, self.row_updated)
        self.row_ids, self.row_updated = array("i"), array("q")

        # Per release, its earliest bound. A suffix minimum never decreases with the row id, so
        # that is the bound of its lowest-id Discogs link, which the stable sort puts first.
        plan = BackfillPlan()
        releases = array("i")
        by_release = sorted(range(len(cand_ids)), key=cand_releases.__getitem__)
        linked = undated = 0
        start = 0
        while start < len(by_release):
            best = by_release[start]
            release = cand_releases[best]
            end = start + 1
            while end < len(by_release) and cand_releases[by_release[end]] == release:
                end += 1
            linked += 1
            if bounds[best] == NO_TIME:
                undated += 1
            else:
                releases.append(release)
                plan.discogs_ids.append(cand_discogs[best])
                plan.event_at.append(bounds[best])
            start = end
        del by_release, cand_ids, cand_releases, cand_discogs, bounds

        # Each dated release's MBID, from the release table; one with no row is dropped.
        gids = bytearray(16 * len(releases))
        found = bytearray(len(releases))
        for index, release in enumerate(self.release_ids):
            at = bisect_left(releases, release)
            if at < len(releases) and releases[at] == release:
                gids[16 * at : 16 * at + 16] = self.release_gids[16 * index : 16 * index + 16]
                found[at] = 1
        self.release_ids, self.release_gids = array("i"), bytearray()
        missing_release = len(releases) - sum(found)
        if missing_release:
            kept = [i for i in range(len(releases)) if found[i]]
            plan.discogs_ids = array("q", (plan.discogs_ids[i] for i in kept))
            plan.event_at = array("q", (plan.event_at[i] for i in kept))
            gids = bytearray(b"".join(gids[16 * i : 16 * i + 16] for i in kept))
        plan.mbids = gids
        plan.counts = {
            "release_url_rows": release_url_rows,
            "discogs_release_links": discogs_release_links,
            "releases_linked": linked,
            "undated": undated,
            "missing_release_row": missing_release,
            "events": len(plan),
        }
        return plan


class _DigestingStream:
    """A read-only file object over byte chunks that hashes every byte it is handed."""

    def __init__(self, chunks: Iterable[bytes]) -> None:
        self._chunks = iter(chunks)
        self._buffer = bytearray()
        self._digest = hashlib.sha256()
        self.size = 0

    def _pull(self) -> bool:
        chunk = next(self._chunks, None)
        if chunk is None:
            return False
        self._digest.update(chunk)
        self.size += len(chunk)
        self._buffer += chunk
        return True

    def seekable(self) -> bool:
        return False

    def seek(self, _offset: int, /) -> int:
        raise io.UnsupportedOperation("a streamed export cannot seek")

    def read(self, size: int = -1, /) -> bytes:
        while (size < 0 or len(self._buffer) < size) and self._pull():
            pass
        if size < 0:
            size = len(self._buffer)
        out = bytes(self._buffer[:size])
        del self._buffer[:size]
        return out

    def drain(self) -> str:
        """Hash whatever the reader did not consume, and return the file's SHA-256."""
        self._buffer.clear()
        while self._pull():
            self._buffer.clear()
        return self._digest.hexdigest()


def read_export(chunks: Iterable[bytes], expected_sha256: str) -> BackfillPlan:
    """Stream ``mbdump.tar.bz2`` from *chunks*, verify it, and plan the backfill.

    Raises:
        ChecksumError: The file's SHA-256 is not *expected_sha256*, including when it is too
            corrupt to decompress.
        ExportError: The file verified but is not a readable export, or lacks a table or the
            Discogs link type.
    """
    stream = _DigestingStream(chunks)
    reader = ExportReader()
    try:
        with bz2.BZ2File(stream) as decompressed, tarfile.open(fileobj=decompressed, mode="r|") as tar:
            for member in tar:
                name = member.name.rsplit("/", 1)[-1]
                if member.isfile() and name in TABLES:
                    handle = tar.extractfile(member)
                    assert handle is not None
                    reader.read_member(name, handle)
    except (OSError, EOFError, IndexError, ValueError, tarfile.TarError) as exc:
        digest = stream.drain()
        if digest != expected_sha256.lower():
            raise ChecksumError(f"{DUMP_FILE} SHA-256 {digest} does not match SHA256SUMS {expected_sha256}") from exc
        raise ExportError(f"{DUMP_FILE} verified but could not be read: {exc}") from exc
    digest = stream.drain()
    if digest != expected_sha256.lower():
        raise ChecksumError(f"{DUMP_FILE} SHA-256 {digest} does not match SHA256SUMS {expected_sha256}")
    logger.info("🔐 Full export verified against SHA256SUMS", file=DUMP_FILE, bytes=stream.size)
    plan = reader.finalize()
    plan.counts["dump_bytes"] = stream.size
    return plan


# ---------------------------------------------------------------------------
# Fetching the export
# ---------------------------------------------------------------------------


def parse_sha256sums(text: str, filename: str = DUMP_FILE) -> str:
    """The digest ``SHA256SUMS`` gives *filename* (``<hex>  <name>`` or ``<hex> *<name>``)."""
    for line in text.splitlines():
        match = _SHA256_LINE.fullmatch(line.strip())
        if match and match.group(2) == filename:
            return match.group(1).lower()
    raise ExportError(f"SHA256SUMS has no entry for {filename}")


def _fetch_text(client: httpx.Client, url: str) -> str:
    response = client.get(url)
    response.raise_for_status()
    return response.text


def resolve_export(client: httpx.Client, base_url: str, export: str | None) -> str:
    """The export named, or the one the base directory's ``LATEST`` file names."""
    name = export if export is not None else _fetch_text(client, f"{base_url}/LATEST").strip()
    if not _EXPORT_NAME.fullmatch(name):
        raise ExportError(f"{name!r} is not a full-export name like 20261024-102842")
    return name


def stream_export(client: httpx.Client, base_url: str, export: str) -> BackfillPlan:
    """Fetch the export's ``SHA256SUMS``, then stream and verify its ``mbdump.tar.bz2``."""
    expected = parse_sha256sums(_fetch_text(client, f"{base_url}/{export}/SHA256SUMS"))
    with client.stream("GET", f"{base_url}/{export}/{DUMP_FILE}") as response:
        response.raise_for_status()
        return read_export(response.iter_bytes(_CHUNK), expected)


def _http_client() -> httpx.Client:
    return httpx.Client(
        follow_redirects=True,
        timeout=httpx.Timeout(60.0),
        headers={"User-Agent": f"groovemap-catalog-api/{__version__} (identity ledger backfill)"},
    )


# ---------------------------------------------------------------------------
# Writing the events
# ---------------------------------------------------------------------------

_HANDLED: Final = "SELECT EXISTS (SELECT 1 FROM admin_audit_log WHERE admin_id = %s::uuid AND action = %s AND target = %s)"

_INSERT_EVENTS: Final = """WITH inserted AS (
        INSERT INTO identity_link_events
            (provider, entity_kind, external_id, event, discogs_id_after, event_at, dated_from, source_ref, decision_ref)
        SELECT 'musicbrainz', 'release', e.external_id, 'link_added', e.discogs_id, e.event_at, 'musicbrainz_fullexport', %s, %s::uuid
        FROM unnest(%s::text[], %s::bigint[], %s::timestamptz[]) AS e (external_id, discogs_id, event_at)
        ON CONFLICT ON CONSTRAINT identity_link_events_idempotence_key DO NOTHING
        RETURNING 1
    )
    SELECT count(*) FROM inserted
"""


def export_target(export: str) -> str:
    """The `admin_audit_log.target` of a backfill from *export*, and its handled marker."""
    return f"musicbrainz-fullexport:{export}"


async def is_backfilled(pool: Any, export: str) -> bool:
    """Whether a finished backfill of *export* is already recorded."""
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(_HANDLED, (str(SYSTEM_ACTOR_ID), APPLY_ACTION, export_target(export)))
        row = await cur.fetchone()
    return bool(row and row[0])


async def write_events(pool: Any, plan: BackfillPlan, *, source_ref: str, decision_ref: UUID, batch_size: int = DEFAULT_BATCH_SIZE) -> int:
    """Insert the plan's events in batches, each committed on its own. Returns the rows inserted.

    An event already present under the idempotence key is skipped, not duplicated.
    """
    inserted = 0
    events = plan.events()
    while batch := _take(events, batch_size):
        mbids = [mbid for mbid, _, _ in batch]
        discogs_ids = [discogs_id for _, discogs_id, _ in batch]
        moments = [datetime.fromtimestamp(moment, UTC) for _, _, moment in batch]
        async with pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_INSERT_EVENTS, (source_ref, str(decision_ref), mbids, discogs_ids, moments))
            row = await cur.fetchone()
        inserted += int(row[0]) if row else 0
        logger.debug("📝 Wrote a batch of ledger events", batch=len(batch), inserted=inserted)
    return inserted


def _take(events: Iterator[tuple[str, int, int]], size: int) -> list[tuple[str, int, int]]:
    batch = []
    for event in events:
        batch.append(event)
        if len(batch) == size:
            break
    return batch


def plan_details(plan: BackfillPlan, *, job_id: str, export: str) -> dict[str, Any]:
    """The `admin_audit_log` details of a run: the export, the counts, and events per year. No MBIDs."""
    return {"job_id": job_id, "apply": True, "export": export, "dated_from": DATED_FROM, **plan.counts, "per_year": plan.per_year()}


async def _finalize_failed(pool: Any, *, job_id: str, details: dict[str, Any]) -> None:
    """Mark a started entry failed. Never raises: a failure leaves the started entry and logs an error."""
    try:
        async with pool.connection() as conn, conn.cursor() as cur:
            await update_audit_entry(cur, entry_id=job_id, action=FAILED_ACTION, details=details)
    except Exception:
        logger.error("❌ Could not mark the ledger backfill's audit entry failed; it stays as started", job_id=job_id, exc_info=True)


async def apply_backfill(pool: Any, plan: BackfillPlan, *, export: str, job_id: str, batch_size: int = DEFAULT_BATCH_SIZE) -> dict[str, Any] | None:
    """Write the plan under one audit entry; None when *export* was already backfilled.

    The entry (row id: *job_id*) is written as :data:`STARTED_ACTION` before any event, since
    every event names it as ``decision_ref``. It becomes :data:`APPLY_ACTION`, the handled
    marker, with the counts, or :data:`FAILED_ACTION` with the error, which is re-raised.
    """
    target = export_target(export)
    async with pool.connection() as conn, conn.cursor() as cur:
        await ensure_system_actor(cur)
        await cur.execute(_HANDLED, (str(SYSTEM_ACTOR_ID), APPLY_ACTION, target))
        row = await cur.fetchone()
        if row and row[0]:
            return None
        await insert_audit_entry(
            cur,
            entry_id=job_id,
            admin_id=str(SYSTEM_ACTOR_ID),
            action=STARTED_ACTION,
            target=target,
            details={"job_id": job_id, "apply": True, "export": export},
        )
    logger.info("🚀 Identity ledger backfill started", export=export, job_id=job_id, events=len(plan))
    details = plan_details(plan, job_id=job_id, export=export)
    try:
        inserted = await write_events(pool, plan, source_ref=export, decision_ref=UUID(job_id), batch_size=batch_size)
    except Exception as exc:
        await _finalize_failed(pool, job_id=job_id, details={**details, "error": type(exc).__name__})
        # Batches commit one by one, so a failed run may still have written some.
        await refresh_ledger(pool, trigger=TRIGGER_BACKFILL, job_id=job_id)
        raise
    details |= {"inserted": inserted, "already_present": len(plan) - inserted}
    async with pool.connection() as conn, conn.cursor() as cur:
        await update_audit_entry(cur, entry_id=job_id, action=APPLY_ACTION, details=details)
    logger.info("✅ Identity ledger backfill finished", export=export, job_id=job_id, inserted=inserted)
    # Inline, so the dated months are in the works list and density when the run reports done.
    await refresh_ledger(pool, trigger=TRIGGER_BACKFILL, job_id=job_id)
    return details


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


async def _with_pool[T](work: Callable[[Any], Awaitable[T]]) -> T:
    """Build a pool from the environment, run ``work(pool)``, and close it."""
    from common import AsyncPostgreSQLPool  # noqa: PLC0415

    pool = AsyncPostgreSQLPool(connection_params=_connection_params(), max_connections=2, min_connections=1)
    await pool.initialize()
    try:
        return await work(pool)
    finally:
        await pool.close()


def _print_plan(plan: BackfillPlan) -> None:
    counts = plan.counts
    print(
        f"  {counts['release_url_rows']} release-URL rows, {counts['discogs_release_links']} Discogs release links "
        f"on {counts['releases_linked']} releases"
    )
    print(f"  {counts['events']} link_added events dated, {counts['undated']} undated, {counts['missing_release_row']} with no release row")
    if len(plan):
        first, last = (datetime.fromtimestamp(moment, UTC).date() for moment in (min(plan.event_at), max(plan.event_at)))
        print(f"  dated {first} to {last}; per year: {plan.per_year()}")


def main() -> None:
    """Entry point for the catalog-identity-ledger-backfill CLI tool."""
    parser = argparse.ArgumentParser(
        prog="catalog-identity-ledger-backfill",
        description=(
            "One-time backfill of the Identity Ledger's release-to-Discogs link history from the "
            "MusicBrainz full export (proxy 2, spike gm-design-6b8u). It streams mbdump.tar.bz2, "
            "verifies it against SHA256SUMS, and dates each release's Discogs link. Without --apply "
            "it prints counts and connects to no database. With --apply it writes one link_added "
            "event per release, labelled dated_from 'musicbrainz_fullexport', and one admin audit entry."
        ),
        epilog="--apply reads DB connection from environment variables: POSTGRES_HOST, POSTGRES_USERNAME, POSTGRES_PASSWORD, POSTGRES_DATABASE",
    )
    parser.add_argument("--export", metavar="NAME", help="Full-export name, e.g. 20261024-102842 (default: the one LATEST names)")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, metavar="URL", help=f"Full-export directory (default: {DEFAULT_BASE_URL})")
    parser.add_argument("--apply", action="store_true", help="Write the events (default: dry run, counts only)")
    parser.add_argument(
        "--batch-size", type=int, default=DEFAULT_BATCH_SIZE, metavar="N", help=f"Events inserted per statement (default: {DEFAULT_BATCH_SIZE})"
    )
    args = parser.parse_args()
    if args.batch_size < 1:
        parser.error("--batch-size must be at least 1")
    base_url = args.base_url.rstrip("/")
    if args.apply:
        _connection_params()

    job_id = str(uuid4())
    try:
        with _http_client() as client:
            export = resolve_export(client, base_url, args.export)
            print(f"📋 Identity ledger backfill from MusicBrainz full export {export} ({'apply' if args.apply else 'dry run'}), job {job_id}…")
            if args.apply and asyncio.run(_with_pool(lambda pool: is_backfilled(pool, export))):
                print(f"✅ Full export {export} is already backfilled; nothing was written.")
                return
            plan = stream_export(client, base_url, export)
    except (ExportError, httpx.HTTPError) as exc:
        print(f"❌ {exc}; nothing was written.", file=sys.stderr)
        sys.exit(1)
    print(f"🔐 {DUMP_FILE} matches SHA256SUMS ({plan.counts['dump_bytes']} bytes, never written to disk)")
    _print_plan(plan)
    if not args.apply:
        print("✅ Dry run only; nothing was written. Re-run with --apply to write the events.")
        return
    details = asyncio.run(_with_pool(lambda pool: apply_backfill(pool, plan, export=export, job_id=job_id, batch_size=args.batch_size)))
    if details is None:
        print(f"✅ Full export {export} was backfilled by another run meanwhile; nothing was written.")
        return
    print(f"✅ Done. {details['inserted']} events inserted, {details['already_present']} already present; audit entry {job_id}.")


if __name__ == "__main__":
    main()
