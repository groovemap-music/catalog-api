"""Unit tests for the full-export Identity Ledger backfill (api/ledger_backfill.py).

No test here opens a network connection or a database: the export is invented and served by
`httpx.MockTransport` (tests/synthetic_fullexport.py), and PostgreSQL is the `FakePool`
double. `tests/test_ledger_backfill_integration.py` applies a synthetic export against a real
server.

The parity tests run proxy 2 twice on the same synthetic rows: once through the command, and
once through the 6b8u harness's definition, transcribed below from design
`docs/spikes/gm-design-6b8u/scripts/` (`extract_mb_history.core`, `months.creation_bound`, and
`months.link_history` with ``proxy=2``). The transcription swaps numpy for plain lists and
keeps every step: argsort, NULL as the int64 maximum, the reversed running minimum, and
searchsorted.
"""

from __future__ import annotations

import bz2
import hashlib
import io
import sys
import tarfile
import tracemalloc
from bisect import bisect_left
from datetime import UTC, datetime
from itertools import accumulate
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import httpx
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import api.ledger_backfill as backfill
from api.ledger_backfill import (
    APPLY_ACTION,
    DATED_FROM,
    FAILED_ACTION,
    NO_TIME,
    STARTED_ACTION,
    BackfillPlan,
    ChecksumError,
    ExportError,
    apply_backfill,
    creation_bounds,
    epoch,
    export_target,
    extract_discogs_id,
    is_backfilled,
    parse_sha256sums,
    plan_details,
    read_export,
    resolve_export,
    write_events,
)
from api.reattach_trigger import SYSTEM_ACTOR_EMAIL, SYSTEM_ACTOR_ID
from tests.fake_postgres import FakePool
from tests.synthetic_fullexport import BASE_URL, EXPORT, GID, TINY_MEMBERS, chunks, dump, sha256sums, tarball, transport


_INT64_MAX = 2**63 - 1


# ---------------------------------------------------------------------------
# The 6b8u harness, transcribed
# ---------------------------------------------------------------------------


def harness_core(members: dict[str, str]) -> dict[str, list[Any]]:
    """`extract_mb_history.core`, over member texts instead of a tar stream."""
    link_type_ids: set[int] = set()
    link_types: dict[int, int] = {}
    discogs_urls: dict[int, int] = {}
    l_id, l_release, l_url, l_link, l_time = [], [], [], [], []
    release_gid: dict[int, str] = {}
    rows = {name: [line.split("\t") for line in text.splitlines()] for name, text in members.items()}
    for cells in rows.get("link_type", []):
        if cells[4] == "release" and cells[5] == "url" and cells[6] == "discogs":
            link_type_ids.add(int(cells[0]))
    for cells in rows.get("link", []):
        link_types[int(cells[0])] = int(cells[1])
    for cells in rows.get("url", []):
        found = extract_discogs_id(cells[2], "release")
        if found is not None:
            discogs_urls[int(cells[0])] = found
    for cells in rows.get("l_release_url", []):
        l_id.append(int(cells[0]))
        l_link.append(int(cells[1]))
        l_release.append(int(cells[2]))
        l_url.append(int(cells[3]))
        l_time.append(epoch(cells[5]))
    for cells in rows.get("release", []):
        release_gid[int(cells[0])] = cells[1]
    keep = [i for i in range(len(l_link)) if link_types.get(l_link[i]) in link_type_ids and l_url[i] in discogs_urls]
    releases = [l_release[i] for i in keep]
    return {
        "release_id": releases,
        "row_id": [l_id[i] for i in keep],
        "all_row_id": l_id,
        "all_last_updated": l_time,
        "discogs_id": [discogs_urls[l_url[i]] for i in keep],
        "mbid": [release_gid[r] for r in releases],
    }


def harness_creation_bound(row_ids: list[int], all_row_ids: list[int], all_updated: list[int]) -> list[int]:
    """`months.creation_bound`."""
    order = sorted(range(len(all_row_ids)), key=all_row_ids.__getitem__)  # np.argsort(all_row_ids)
    ids = [all_row_ids[i] for i in order]
    updated = [all_updated[i] if all_updated[i] > 0 else _INT64_MAX for i in order]  # np.where(... > 0, ..., int64 max)
    suffix_min = list(accumulate(reversed(updated), min))[::-1]  # np.minimum.accumulate(updated[::-1])[::-1]
    return [suffix_min[bisect_left(ids, k)] for k in row_ids]  # suffix_min[np.searchsorted(ids, row_ids)]


def harness_link_history(core: dict[str, list[Any]]) -> dict[str, tuple[int, int]]:
    """`months.link_history(proxy=2)`: mbid -> (L, Discogs id of the earliest link)."""
    dated = harness_creation_bound(core["row_id"], core["all_row_id"], core["all_last_updated"])
    out: dict[str, tuple[int, int]] = {}
    for did, updated, mbid in zip(core["discogs_id"], dated, core["mbid"], strict=True):
        current = out.get(mbid)
        if current is None or updated < current[0]:
            out[mbid] = (updated, did)
    return out


def command_history(members: dict[str, str]) -> dict[str, tuple[int, int]]:
    """The command's events for the same members, streamed as `mbdump.tar.bz2`."""
    data = dump(members)
    plan = read_export(chunks(data), hashlib.sha256(data).hexdigest())
    return {mbid: (moment, discogs_id) for mbid, discogs_id, moment in plan.events()}


def harness_dated(history: dict[str, tuple[int, int]]) -> dict[str, tuple[int, int]]:
    """The harness's history, minus links it dates at the int64 maximum (undated here)."""
    return {mbid: value for mbid, value in history.items() if value[0] != _INT64_MAX}


def _at(text: str) -> int:
    return int(datetime.fromisoformat(text).replace(tzinfo=UTC).timestamp())


# ---------------------------------------------------------------------------
# Parity with the harness
# ---------------------------------------------------------------------------


def test_the_tiny_export_is_dated_exactly_as_the_harness_dates_it() -> None:
    history = command_history(TINY_MEMBERS)
    assert history == harness_dated(harness_link_history(harness_core(TINY_MEMBERS)))
    assert history == {
        GID.format(7): (_at("2026-03-10 12:00:00"), 900001),  # the harness extractor test's 1773144000
        GID.format(9): (_at("2026-04-01 00:00:00"), 900004),  # an April link edited in September stays in April
        GID.format(11): (_at("2026-05-01 00:00:00"), 900006),  # two links tie: the lower row id names it
    }
    assert history[GID.format(7)][0] == 1773144000


def test_the_tiny_export_counts() -> None:
    data = dump(TINY_MEMBERS)
    plan = read_export(chunks(data), hashlib.sha256(data).hexdigest())
    assert plan.counts == {
        "release_url_rows": 11,
        "discogs_release_links": 5,
        "releases_linked": 4,
        "undated": 1,
        "missing_release_row": 0,
        "events": 3,
        "dump_bytes": len(data),
    }
    assert plan.per_year() == {"2026": 3}


def test_creation_bound_matches_the_harness_test_case() -> None:
    # months.py's own test: row 2 was created at 200 and edited at 900; row 4 has no time.
    all_ids, all_updated = [5, 1, 2, 3, 4], [500, 100, 900, 300, 0]
    candidates = backfill.array("i", [1, 2, 5])
    assert list(creation_bounds(candidates, all_ids, all_updated)) == [100, 300, 500]
    assert harness_creation_bound([2, 1, 5], all_ids, all_updated) == [300, 100, 500]


@settings(max_examples=200, deadline=None)
@given(
    rows=st.dictionaries(st.integers(1, 400), st.one_of(st.just(0), st.integers(1, 2_000_000_000)), min_size=1, max_size=120),
    data=st.data(),
)
def test_creation_bound_parity_on_random_rows(rows: dict[int, int], data: st.DataObject) -> None:
    ids = data.draw(st.permutations(list(rows)))
    updated = [rows[i] for i in ids]
    picked = sorted(data.draw(st.sets(st.sampled_from(ids), min_size=1)))
    ours = list(creation_bounds(backfill.array("i", picked), ids, updated))
    theirs = harness_creation_bound(picked, ids, updated)
    assert ours == [NO_TIME if value == _INT64_MAX else value for value in theirs]


# Weighted toward Discogs release URLs, so most examples date something.
_URLS = [
    "https://www.discogs.com/release/{n}",
    "https://www.discogs.com/release/{n}",
    "https://www.discogs.com/release/{n}",
    "https://www.discogs.com/release/{n}-Title-With-Dashes",
    "https://www.discogs.com/master/{n}",
    "https://www.discogs.com/release/x{n}",
    "https://www.amazon.com/dp/{n}",
]


@st.composite
def exports(draw: st.DrawFn) -> dict[str, str]:
    """A random invented export: links of three types, URLs of five shapes, edits and NULLs."""
    url_count = draw(st.integers(3, 30))
    urls = "".join(f"{u}\tu\t{draw(st.sampled_from(_URLS)).format(n=900000 + u)}\t0\t\\N\n" for u in range(1, url_count + 1))
    release_ids = draw(st.lists(st.integers(1, 40), min_size=1, max_size=15, unique=True))
    row_ids = draw(st.lists(st.integers(1, 10_000), min_size=8, max_size=80, unique=True))
    lines = []
    for row_id in sorted(row_ids):
        moment = draw(st.one_of(st.none(), st.integers(1_000_000_000, 1_800_000_000), st.integers(1_000_000_000, 1_800_000_000)))
        stamp = "\\N" if moment is None else datetime.fromtimestamp(moment, UTC).strftime("%Y-%m-%d %H:%M:%S+00")
        link = draw(st.sampled_from([500, 500, 500, 501, 502]))
        lines.append(f"{row_id}\t{link}\t{draw(st.sampled_from(release_ids))}\t{draw(st.integers(1, url_count))}\t0\t{stamp}\t0\t\t\n")
    return {
        "link_type": TINY_MEMBERS["link_type"],
        "link": TINY_MEMBERS["link"],
        "url": urls,
        "l_release_url": "".join(lines),
        "release": "".join(f"{rid}\t{GID.format(rid)}\tR\t1\t1\n" for rid in release_ids),
    }


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(members=exports())
def test_proxy_2_parity_on_random_exports(members: dict[str, str]) -> None:
    assert command_history(members) == harness_dated(harness_link_history(harness_core(members)))


@settings(max_examples=50, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(members=exports(), data=st.data())
def test_row_and_member_order_do_not_change_the_dates(members: dict[str, str], data: st.DataObject) -> None:
    rows = data.draw(st.permutations(members["l_release_url"].splitlines(keepends=True)))
    order = data.draw(st.permutations(list(members.items())))
    shuffled = dict(order) | {"l_release_url": "".join(rows)}
    assert command_history(shuffled) == command_history(members)


# ---------------------------------------------------------------------------
# Ported rules
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.discogs.com/release/900001-Some-Title", 900001),
        ("https://www.discogs.com/release/+42", 42),
        ("https://www.discogs.com/release/9223372036854775807", 2**63 - 1),
        ("https://www.discogs.com/release/9223372036854775808", None),
        ("https://www.discogs.com/release/", None),
        ("https://www.discogs.com/master/1", None),
        ("http://www.discogs.com/release/1", None),
    ],
)
def test_extract_discogs_id(url: str, expected: int | None) -> None:
    assert extract_discogs_id(url, "release") == expected


def test_epoch_reads_copy_timestamps() -> None:
    assert epoch("\\N") == 0
    assert epoch("2026-03-10 12:00:00.5+00") == 1773144000
    assert epoch("2026-03-10 12:00:00") == 1773144000
    assert epoch("2026-03-10 14:00:00+02") == 1773144000


# ---------------------------------------------------------------------------
# Reading and verifying the export
# ---------------------------------------------------------------------------


def test_a_release_with_no_release_row_is_dropped_and_counted() -> None:
    members = TINY_MEMBERS | {"release": TINY_MEMBERS["release"].replace(f"9\t{GID.format(9)}\tR9\t1\t70\n", "")}
    data = dump(members)
    plan = read_export(chunks(data), hashlib.sha256(data).hexdigest())
    assert plan.counts["missing_release_row"] == 1
    assert {mbid for mbid, _, _ in plan.events()} == {GID.format(7), GID.format(11)}
    assert {mbid: (moment, did) for mbid, did, moment in plan.events()} == {
        GID.format(7): (_at("2026-03-10 12:00:00"), 900001),
        GID.format(11): (_at("2026-05-01 00:00:00"), 900006),
    }


@pytest.mark.parametrize("table", sorted(backfill.TABLES))
def test_a_missing_table_is_refused(table: str) -> None:
    members = {name: text for name, text in TINY_MEMBERS.items() if name != table}
    data = dump(members)
    with pytest.raises(ExportError, match=f"no {table} table"):
        read_export(chunks(data), hashlib.sha256(data).hexdigest())


def test_an_export_without_the_discogs_link_type_is_refused() -> None:
    members = TINY_MEMBERS | {"link_type": "77\t\\N\t0\ty\trelease\turl\tamazon asin\n"}
    data = dump(members)
    with pytest.raises(ExportError, match="no discogs release-url link type"):
        read_export(chunks(data), hashlib.sha256(data).hexdigest())


def test_a_checksum_mismatch_is_refused_after_the_whole_file_is_hashed() -> None:
    data = dump(TINY_MEMBERS)
    with pytest.raises(ChecksumError, match="does not match SHA256SUMS"):
        read_export(chunks(data), "f" * 64)


@pytest.mark.parametrize("damage", ["flip", "truncate"])
def test_a_corrupt_file_is_a_checksum_error(damage: str) -> None:
    data = bytearray(dump(TINY_MEMBERS))
    expected = hashlib.sha256(data).hexdigest()
    if damage == "flip":
        data[len(data) // 2] ^= 0xFF
    else:
        del data[len(data) // 2 :]
    with pytest.raises(ChecksumError):
        read_export(chunks(bytes(data)), expected)


def test_an_unreadable_file_that_matches_its_checksum_is_an_export_error() -> None:
    data = b"not a bzip2 stream"
    with pytest.raises(ExportError, match="verified but could not be read") as raised:
        read_export(chunks(data), hashlib.sha256(data).hexdigest())
    assert not isinstance(raised.value, ChecksumError)


def test_a_malformed_row_in_a_verified_file_is_an_export_error() -> None:
    data = dump(TINY_MEMBERS | {"l_release_url": "1\t2\n"})
    with pytest.raises(ExportError, match="verified but could not be read"):
        read_export(chunks(data), hashlib.sha256(data).hexdigest())


def test_bytes_after_the_tar_end_are_hashed() -> None:
    tar = tarball(TINY_MEMBERS) + b"\0" * 20480
    data = bz2.compress(tar)
    plan = read_export(chunks(data, 7), hashlib.sha256(data).hexdigest())
    assert plan.counts["dump_bytes"] == len(data)


def test_the_digesting_stream_reads_whole_and_cannot_seek() -> None:
    stream = backfill._DigestingStream([b"ab", b"cd"])
    assert stream.seekable() is False
    with pytest.raises(io.UnsupportedOperation):
        stream.seek(0)
    assert stream.read() == b"abcd"
    assert stream.read(3) == b""
    assert stream.drain() == hashlib.sha256(b"abcd").hexdigest()
    unread = backfill._DigestingStream([b"ab", b"cd", b"ef"])
    assert unread.read(1) == b"a"
    assert unread.drain() == hashlib.sha256(b"abcdef").hexdigest()
    assert unread.size == 6


@pytest.mark.parametrize(
    "text",
    [
        f"{'a' * 64} *mbdump.tar.bz2\n",
        f"{'A' * 64}  mbdump.tar.bz2\n",
        f"{'b' * 64} *mbdump-derived.tar.bz2\n{'a' * 64} *mbdump.tar.bz2\n",
    ],
)
def test_parse_sha256sums(text: str) -> None:
    assert parse_sha256sums(text) == "a" * 64


def test_parse_sha256sums_without_the_file_is_refused() -> None:
    with pytest.raises(ExportError, match=r"no entry for mbdump\.tar\.bz2"):
        parse_sha256sums(f"{'b' * 64} *mbdump-derived.tar.bz2\n")


def test_resolve_export_reads_latest_and_checks_the_name() -> None:
    with httpx.Client(transport=transport(b"")) as client:
        assert resolve_export(client, BASE_URL, None) == EXPORT
        assert resolve_export(client, BASE_URL, "20200101-000000") == "20200101-000000"
        with pytest.raises(ExportError, match="is not a full-export name"):
            resolve_export(client, BASE_URL, "../../etc")
    with httpx.Client(transport=transport(b"", latest="garbage")) as client, pytest.raises(ExportError):
        resolve_export(client, BASE_URL, None)


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------

# The stated bound: peak traced memory is at most 2 MiB, plus 96 bytes for each release-URL
# row, release row, and Discogs release URL the export holds. Bytes of any other table, and
# URLs that are not Discogs release URLs, cost nothing that stays.
_FIXED_BOUND = 2 << 20
_PER_ROW_BOUND = 96


class _Lines(io.RawIOBase):
    """A tar member body produced line by line, never held whole."""

    def __init__(self, lines: Any) -> None:
        self._lines = iter(lines)
        self._buffer = b""

    def readable(self) -> bool:
        return True

    def read(self, size: int = -1) -> bytes:
        while len(self._buffer) < size:
            line = next(self._lines, None)
            if line is None:
                break
            self._buffer += line
        out, self._buffer = self._buffer[:size], self._buffer[size:]
        return out


class _Compressing:
    """A write-only sink that keeps only bz2-compressed bytes."""

    def __init__(self) -> None:
        self._compressor = bz2.BZ2Compressor(9)
        self.parts: list[bytes] = []

    def write(self, data: bytes) -> int:
        self.parts.append(self._compressor.compress(data))
        return len(data)

    def finish(self) -> bytes:
        self.parts.append(self._compressor.flush())
        return b"".join(self.parts)


def _large_export(rows: int, padding: int) -> bytes:
    """*rows* release-URL rows, half of them Discogs links, plus *padding* bytes of another table."""
    sink = _Compressing()
    tar = tarfile.open(fileobj=sink, mode="w|")  # noqa: SIM115 - closed below, before finish()

    def member(name: str, lines: Any, size: int) -> None:
        info = tarfile.TarInfo(f"mbdump/{name}")
        info.size = size
        tar.addfile(info, _Lines(lines))

    def table(name: str, texts: list[str]) -> None:
        encoded = [text.encode() for text in texts]
        member(name, encoded, sum(map(len, encoded)))

    table("link_type", TINY_MEMBERS["link_type"].splitlines(keepends=True))
    table("link", TINY_MEMBERS["link"].splitlines(keepends=True))
    pad = b"1\tan artist row the backfill never keeps\t" + b"x" * 85 + b"\n"
    member("artist", (pad for _ in range(padding // len(pad))), padding // len(pad) * len(pad))
    table(
        "l_release_url",
        [f"{i}\t{500 if i % 2 else 501}\t{i // 2}\t{i}\t0\t2020-01-01 00:{i // 60 % 60:02d}:{i % 60:02d}+00\t0\t\t\n" for i in range(1, rows + 1)],
    )
    table("release", [f"{r}\t{GID.format(r)}\tR\t1\t1\n" for r in range(rows // 2 + 1)])
    table(
        "url",
        [f"{i}\tg\t" + (f"https://www.discogs.com/release/{i}" if i % 2 else f"https://example.com/{i}") + "\t0\t\\N\n" for i in range(1, rows + 1)],
    )
    tar.close()
    return sink.finish()


def _peak(data: bytes) -> tuple[int, BackfillPlan]:
    digest = hashlib.sha256(data).hexdigest()
    tracemalloc.start()
    try:
        plan = read_export(chunks(data, 1 << 16), digest)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return peak, plan


def test_memory_stays_within_the_stated_bound_on_a_large_stream() -> None:
    rows = 60_000
    padding = 24 << 20
    peak, plan = _peak(_large_export(rows, padding))
    kept_rows = rows + (rows // 2 + 1) + rows // 2  # release-URL rows, release rows, Discogs release URLs
    assert len(plan) == rows // 2
    assert plan.counts["release_url_rows"] == rows
    assert peak <= _FIXED_BOUND + _PER_ROW_BOUND * kept_rows, f"peak {peak} bytes"
    # The decompressed stream is several times the peak: it was never held.
    assert peak * 3 < padding


def test_memory_does_not_grow_with_other_tables_bytes() -> None:
    small, _ = _peak(_large_export(5_000, 0))
    padded, _ = _peak(_large_export(5_000, 24 << 20))
    assert padded - small < 1 << 20, f"{small} -> {padded} bytes"


# ---------------------------------------------------------------------------
# Writing the events
# ---------------------------------------------------------------------------


def _plan() -> BackfillPlan:
    data = dump(TINY_MEMBERS)
    return read_export(chunks(data), hashlib.sha256(data).hexdigest())


def _sql(pool: FakePool) -> list[str]:
    return [" ".join(call.sql.split()) for call in pool.calls]


@pytest.mark.asyncio
async def test_write_events_batches_and_counts_inserted_rows() -> None:
    plan = _plan()
    decision = uuid4()
    pool = FakePool([[(2,)], [(0,)]])
    assert await write_events(pool, plan, source_ref=EXPORT, decision_ref=decision, batch_size=2) == 2
    assert len(pool.calls) == 2
    first, second = pool.calls
    sql = " ".join(first.sql.split())
    assert "INSERT INTO identity_link_events" in sql
    assert "'musicbrainz', 'release', e.external_id, 'link_added', e.discogs_id, e.event_at, 'musicbrainz_fullexport'" in sql
    assert "ON CONFLICT ON CONSTRAINT identity_link_events_idempotence_key DO NOTHING" in sql
    assert first.params[:2] == (EXPORT, str(decision))
    assert first.params[2] == [GID.format(7), GID.format(9)]
    assert first.params[3] == [900001, 900004]
    assert first.params[4] == [datetime(2026, 3, 10, 12, tzinfo=UTC), datetime(2026, 4, 1, tzinfo=UTC)]
    assert second.params[2:4] == ([GID.format(11)], [900006])


@pytest.mark.asyncio
async def test_write_events_of_an_empty_plan_writes_nothing() -> None:
    pool = FakePool()
    assert await write_events(pool, BackfillPlan(), source_ref=EXPORT, decision_ref=uuid4()) == 0
    assert pool.calls == []


@pytest.mark.asyncio
async def test_write_events_counts_nothing_without_a_row() -> None:
    pool = FakePool([[]])
    assert await write_events(pool, _plan(), source_ref=EXPORT, decision_ref=uuid4()) == 0


@pytest.mark.asyncio
async def test_is_backfilled_reads_the_marker() -> None:
    pool = FakePool([[(True,)]])
    assert await is_backfilled(pool, EXPORT) is True
    assert pool.params == (str(SYSTEM_ACTOR_ID), APPLY_ACTION, export_target(EXPORT))
    assert await is_backfilled(FakePool([]), EXPORT) is False


_ACTOR = [[], [(SYSTEM_ACTOR_EMAIL, False, False)]]


@pytest.mark.asyncio
async def test_apply_writes_the_started_entry_then_the_events_then_the_marker() -> None:
    plan = _plan()
    job_id = str(uuid4())
    pool = FakePool([*_ACTOR, [(False,)], [], [(3,)], []])
    details = await apply_backfill(pool, plan, export=EXPORT, job_id=job_id)
    sql = _sql(pool)
    assert sql[0].startswith("INSERT INTO users")
    assert pool.calls[1].params == (str(SYSTEM_ACTOR_ID),)
    assert pool.calls[2].params == (str(SYSTEM_ACTOR_ID), APPLY_ACTION, export_target(EXPORT))
    started = pool.calls[3].params
    assert started[:4] == (job_id, str(SYSTEM_ACTOR_ID), STARTED_ACTION, "musicbrainz-fullexport:20261024-102842")
    assert started[4].obj == {"job_id": job_id, "apply": True, "export": EXPORT}
    assert pool.calls[4].params[1] == job_id  # every event names the entry
    assert sql[5].startswith("UPDATE admin_audit_log")
    action, final, entry = pool.calls[5].params
    assert (action, entry) == (APPLY_ACTION, job_id)
    assert final.obj == details
    assert details == {
        **plan_details(plan, job_id=job_id, export=EXPORT),
        "inserted": 3,
        "already_present": 0,
    }
    assert details["dated_from"] == DATED_FROM
    assert details["per_year"] == {"2026": 3}
    assert not any(GID.format(7) in str(value) for value in details.values())  # counts only


@pytest.mark.asyncio
async def test_apply_of_an_export_already_backfilled_writes_nothing() -> None:
    pool = FakePool([*_ACTOR, [(True,)]])
    assert await apply_backfill(pool, _plan(), export=EXPORT, job_id=str(uuid4())) is None
    assert not any(sql.startswith(("INSERT INTO admin_audit_log", "WITH inserted")) for sql in _sql(pool))


@pytest.mark.asyncio
async def test_apply_refuses_a_users_row_that_is_not_the_system_actor() -> None:
    pool = FakePool([[], [(SYSTEM_ACTOR_EMAIL, True, False)]])
    with pytest.raises(RuntimeError, match="identity-maintenance system actor"):
        await apply_backfill(pool, _plan(), export=EXPORT, job_id=str(uuid4()))
    assert len(pool.calls) == 2


@pytest.mark.asyncio
async def test_a_failed_write_marks_the_entry_failed_and_raises() -> None:
    job_id = str(uuid4())
    pool = FakePool([*_ACTOR, [(False,)], []], raise_on={"WITH inserted": RuntimeError("boom")})
    with pytest.raises(RuntimeError, match="boom"):
        await apply_backfill(pool, _plan(), export=EXPORT, job_id=job_id)
    action, details, entry = pool.calls[-1].params
    assert (action, entry) == (FAILED_ACTION, job_id)
    assert details.obj["error"] == "RuntimeError"
    assert "inserted" not in details.obj


@pytest.mark.asyncio
async def test_a_failed_failure_mark_is_logged_not_raised() -> None:
    pool = FakePool(
        [*_ACTOR, [(False,)], []],
        raise_on={"WITH inserted": RuntimeError("boom"), "UPDATE admin_audit_log": RuntimeError("down")},
    )
    with pytest.raises(RuntimeError, match="boom"):
        await apply_backfill(pool, _plan(), export=EXPORT, job_id=str(uuid4()))
    assert _sql(pool)[-1].startswith("UPDATE admin_audit_log")


@pytest.mark.asyncio
async def test_with_pool_closes_the_pool() -> None:
    pool = MagicMock(initialize=AsyncMock(), close=AsyncMock())
    with patch("common.AsyncPostgreSQLPool", return_value=pool) as factory:
        assert await backfill._with_pool(AsyncMock(return_value=7)) == 7
    assert factory.call_args.kwargs["connection_params"]["dbname"] == "test"
    pool.close.assert_awaited_once()


# ---------------------------------------------------------------------------
# The CLI
# ---------------------------------------------------------------------------


@pytest.fixture
def served(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Serve the tiny export to the CLI; collect requested URLs; refuse any database."""
    state: dict[str, Any] = {"data": dump(TINY_MEMBERS), "sums": None, "seen": [], "pool": None}

    def client() -> httpx.Client:
        return httpx.Client(transport=transport(state["data"], sums=state["sums"], seen=state["seen"]))

    async def with_pool(work: Any) -> Any:
        if state["pool"] is None:
            raise AssertionError("the dry run must not connect to PostgreSQL")
        return await work(state["pool"])

    monkeypatch.setattr(backfill, "_http_client", client)
    monkeypatch.setattr(backfill, "_with_pool", with_pool)
    return state


def _main(monkeypatch: pytest.MonkeyPatch, *argv: str) -> None:
    monkeypatch.setattr(sys, "argv", ["catalog-identity-ledger-backfill", "--base-url", BASE_URL + "/", *argv])
    backfill.main()


def test_the_dry_run_prints_counts_and_connects_to_no_database(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], served: dict[str, Any]
) -> None:
    _main(monkeypatch)
    out = capsys.readouterr().out
    assert f"full export {EXPORT} (dry run)" in out
    assert "mbdump.tar.bz2 matches SHA256SUMS" in out
    assert "11 release-URL rows, 5 Discogs release links on 4 releases" in out
    assert "3 link_added events dated, 1 undated, 0 with no release row" in out
    assert "dated 2026-03-10 to 2026-05-01; per year: {'2026': 3}" in out
    assert "Dry run only; nothing was written" in out
    assert served["seen"] == [f"{BASE_URL}/LATEST", f"{BASE_URL}/{EXPORT}/SHA256SUMS", f"{BASE_URL}/{EXPORT}/mbdump.tar.bz2"]


def test_an_empty_plan_prints_no_date_range(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], served: dict[str, Any]) -> None:
    served["data"] = dump(TINY_MEMBERS | {"l_release_url": ""})
    _main(monkeypatch, "--export", EXPORT)
    out = capsys.readouterr().out
    assert "0 link_added events dated" in out
    assert "per year" not in out


_OPENED: list[tuple[str, tuple[Any, ...]]] = []
_WATCHING: list[bool] = []


def _audit(event: str, args: tuple[Any, ...]) -> None:
    if _WATCHING and (event == "open" or event.startswith(("tempfile.", "os.mkdir"))):
        _OPENED.append((event, args))


sys.addaudithook(_audit)


@pytest.mark.usefixtures("served")
def test_the_command_opens_no_file_for_the_dump(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    _main(monkeypatch, "--export", EXPORT)  # warm: anything imported lazily is imported now
    _OPENED.clear()
    _WATCHING.append(True)
    try:
        _main(monkeypatch, "--export", EXPORT)
    finally:
        _WATCHING.clear()
    assert "matches SHA256SUMS" in capsys.readouterr().out
    assert _OPENED == []


def test_a_checksum_mismatch_aborts_before_any_write(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], served: dict[str, Any]
) -> None:
    served["sums"] = sha256sums(b"some other file")
    served["pool"] = FakePool([[(False,)]])
    with pytest.raises(SystemExit) as exited:
        _main(monkeypatch, "--apply")
    assert exited.value.code == 1
    assert "does not match SHA256SUMS" in capsys.readouterr().err
    assert _sql(served["pool"]) == [" ".join(backfill._HANDLED.split())]  # only the marker was read


@pytest.mark.usefixtures("served")
def test_an_http_error_exits(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exited:
        _main(monkeypatch, "--export", "20000101-000000")
    assert exited.value.code == 1
    assert "404" in capsys.readouterr().err


def test_apply_writes_and_reports(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], served: dict[str, Any]) -> None:
    served["pool"] = FakePool([[(False,)], *_ACTOR, [(False,)], [], [(3,)], []])
    _main(monkeypatch, "--apply", "--batch-size", "10")
    out = capsys.readouterr().out
    assert "(apply)" in out
    assert "Done. 3 events inserted, 0 already present; audit entry" in out
    job_id = out.split("job ")[1].split("…")[0]
    assert served["pool"].calls[4].params[0] == job_id
    UUID(job_id)


def test_apply_of_an_export_already_backfilled_downloads_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], served: dict[str, Any]
) -> None:
    served["pool"] = FakePool([[(True,)]])
    _main(monkeypatch, "--apply")
    assert f"Full export {EXPORT} is already backfilled; nothing was written." in capsys.readouterr().out
    assert served["seen"] == [f"{BASE_URL}/LATEST"]


def test_apply_finding_another_run_finished_meanwhile(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], served: dict[str, Any]
) -> None:
    served["pool"] = FakePool([[(False,)], *_ACTOR, [(True,)]])
    _main(monkeypatch, "--apply")
    assert "was backfilled by another run meanwhile; nothing was written." in capsys.readouterr().out


@pytest.mark.parametrize("argv", [["--batch-size", "0"]])
def test_argument_errors(monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> None:
    monkeypatch.setattr(sys, "argv", ["catalog-identity-ledger-backfill", *argv])
    with pytest.raises(SystemExit) as exited:
        backfill.main()
    assert exited.value.code == 2


def test_apply_needs_the_database_environment_before_downloading(monkeypatch: pytest.MonkeyPatch, served: dict[str, Any]) -> None:
    monkeypatch.delenv("POSTGRES_HOST")
    with pytest.raises(SystemExit) as exited:
        _main(monkeypatch, "--apply")
    assert exited.value.code == 1
    assert served["seen"] == []


def test_the_http_client_names_the_command() -> None:
    with backfill._http_client() as client:
        assert client.headers["User-Agent"].startswith("groovemap-catalog-api/")
        assert client.follow_redirects is True
