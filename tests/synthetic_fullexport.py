"""Invented MusicBrainz full exports for the ledger backfill tests, built in memory.

Nothing here is a real dump: every id, MBID, URL, and timestamp is made up. A member is
PostgreSQL COPY text, with columns in the order of MusicBrainz's `CreateTables.sql`, as the
6b8u harness's own extractor test writes them. The archive is assembled and bz2-compressed
in memory and served as chunks, the way the command reads it over HTTPS.
"""

from __future__ import annotations

import bz2
import hashlib
import io
import tarfile
from collections.abc import Iterable, Iterator

import httpx


EXPORT = "20261024-102842"
BASE_URL = "https://fullexport.invalid/fullexport"
GID = "00000000-0000-4000-9000-{:012x}"

# The harness extractor test's tiny mbdump, plus the cases proxy 2 has to get right.
TINY_MEMBERS: dict[str, str] = {
    # id, parent, child_order, gid, entity_type0, entity_type1, name, ...
    "link_type": ("76\t\\N\t0\tx\trelease\turl\tdiscogs\n77\t\\N\t0\ty\trelease\turl\tamazon asin\n78\t\\N\t0\tz\trelease_group\turl\tdiscogs\n"),
    # id, link_type, ...
    "link": "500\t76\t\\N\n501\t77\t\\N\n502\t78\t\\N\n",
    # id, gid, url, edits_pending, last_updated
    "url": (
        "1\tu1\thttps://www.discogs.com/release/900001-Some-Title\t0\t2020-01-01 00:00:00+00\n"
        "2\tu2\thttps://www.discogs.com/master/900002\t0\t2020-01-01 00:00:00+00\n"
        "3\tu3\thttps://www.amazon.com/dp/X\t0\t2020-01-01 00:00:00+00\n"
        "4\tu4\thttps://www.discogs.com/release/900004\t0\t2020-01-01 00:00:00+00\n"
        "5\tu5\thttps://www.discogs.com/release/900005\t0\t2020-01-01 00:00:00+00\n"
        "6\tu6\thttps://www.discogs.com/release/900006\t0\t2020-01-01 00:00:00+00\n"
        "7\tu7\thttps://www.discogs.com/release/900007\t0\t2020-01-01 00:00:00+00\n"
        "8\tu8\thttps://www.discogs.com/release/not-a-number\t0\t2020-01-01 00:00:00+00\n"
    ),
    # id, link, entity0 (release), entity1 (url), edits_pending, last_updated, link_order, ...
    "l_release_url": (
        # Release 7: the harness test's link, then an Amazon link a day later.
        "10\t500\t7\t1\t0\t2026-03-10 12:00:00.5+00\t0\t\t\n"
        # A master URL under the release link type is not a Discogs release link.
        "11\t500\t8\t2\t0\t2026-03-11 12:00:00+00\t0\t\t\n"
        "12\t501\t7\t3\t0\t2026-03-12 12:00:00+00\t0\t\t\n"
        # Release 9 was linked at 2026-04-01 and the link edited in 2026-09: proxy 2 keeps April.
        "20\t500\t9\t4\t0\t2026-09-15 00:00:00+00\t0\t\t\n"
        "21\t501\t8\t3\t0\t2026-04-01 00:00:00+00\t0\t\t\n"
        # Release 11 has two Discogs links whose bounds tie: the lower row id names the event.
        "30\t500\t11\t6\t0\t2026-05-02 00:00:00+00\t0\t\t\n"
        "31\t500\t11\t5\t0\t2026-05-01 00:00:00+00\t0\t\t\n"
        # A release-group link type, and an unparseable Discogs URL, are both dropped.
        "32\t502\t12\t7\t0\t2026-05-03 00:00:00+00\t0\t\t\n"
        "33\t500\t12\t8\t0\t2026-05-03 00:00:00+00\t0\t\t\n"
        # Release 13's row and every later row have NULL timestamps: undated.
        "40\t500\t13\t7\t0\t\\N\t0\t\t\n"
        "41\t501\t13\t3\t0\t\\N\t0\t\t\n"
    ),
    # id, gid, name, artist_credit, release_group, ...
    "release": "".join(f"{rid}\t{GID.format(rid)}\tR{rid}\t1\t70\n" for rid in (7, 8, 9, 11, 12, 13)),
    # A table the backfill never reads.
    "artist": "1\tsome-artist\tA\n",
}


def tarball(members: dict[str, str] | Iterable[tuple[str, str]]) -> bytes:
    """An uncompressed tar of ``mbdump/<name>`` members, in the order given."""
    buffer = io.BytesIO()
    items = members.items() if isinstance(members, dict) else members
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for name, text in items:
            data = text.encode()
            info = tarfile.TarInfo(f"mbdump/{name}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def dump(members: dict[str, str] | Iterable[tuple[str, str]]) -> bytes:
    """``mbdump.tar.bz2`` bytes for *members*."""
    return bz2.compress(tarball(members))


def sha256sums(data: bytes, name: str = "mbdump.tar.bz2") -> str:
    """A ``SHA256SUMS`` file naming *data*'s digest, beside another file's."""
    return f"{'0' * 64} *mbdump-derived.tar.bz2\n{hashlib.sha256(data).hexdigest()} *{name}\n"


def chunks(data: bytes, size: int = 4096) -> Iterator[bytes]:
    for start in range(0, len(data), size):
        yield data[start : start + size]


def transport(data: bytes, *, sums: str | None = None, latest: str = EXPORT, seen: list[str] | None = None) -> httpx.MockTransport:
    """Serve one export directory: ``LATEST``, ``SHA256SUMS``, and a streamed ``mbdump.tar.bz2``."""
    files = {
        f"{BASE_URL}/LATEST": latest.encode() + b"\n",
        f"{BASE_URL}/{EXPORT}/SHA256SUMS": (sums if sums is not None else sha256sums(data)).encode(),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if seen is not None:
            seen.append(url)
        if url == f"{BASE_URL}/{EXPORT}/mbdump.tar.bz2":
            return httpx.Response(200, stream=_Chunked(data))
        if url in files:
            return httpx.Response(200, content=files[url])
        return httpx.Response(404)

    return httpx.MockTransport(handler)


class _Chunked(httpx.SyncByteStream):
    """A response body handed out in small chunks, as a network read would."""

    def __init__(self, data: bytes) -> None:
        self._data = data

    def __iter__(self) -> Iterator[bytes]:
        return chunks(self._data)
