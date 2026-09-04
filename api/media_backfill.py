"""One-shot CLI to backfill the canonical `media` column on existing rows.

ADR 0007 gives ``user_collections`` and ``user_wantlists`` an additive `media` JSONB
column. Rows synced before that column existed have `media IS NULL`; this tool derives
a media block for them from the data already on the row and fills it in, in batches, so
a large table is never locked or loaded into memory at once.

- ``user_collections`` still carries the raw Discogs API `formats` list (the shape
  ``common.media.map_discogs_formats`` expects directly), so the backfilled block is
  exactly what a fresh sync would compute.
- ``user_wantlists`` only ever kept `formats[0]["name"]` (the deprecated `format`
  column), so the richer per-format structure is already lost; the backfilled block is
  the best-effort recovery `common.media.legacy_format_names_to_media` derives from
  that single name.

Only rows with `media IS NULL` are ever selected, so re-running this tool is a no-op
once every row has been filled — safe to run repeatedly, including concurrently with
new syncs (which always write `media` themselves).

Run via docker exec on the API container:
    docker exec <container> catalog-media-backfill
    docker exec <container> catalog-media-backfill --batch-size 200
    docker exec <container> catalog-media-backfill --collection-only
    docker exec <container> catalog-media-backfill --wantlist-only
"""

import argparse
import json
import os
import sys

import psycopg
from common.config import get_secret, parse_postgres_host_port
from common.media import legacy_format_names_to_media, map_discogs_formats
from psycopg.rows import dict_row


DEFAULT_BATCH_SIZE = 500


def _build_conninfo() -> str:
    """Build a psycopg conninfo string from environment variables."""
    address = os.environ.get("POSTGRES_HOST", "")
    username = get_secret("POSTGRES_USERNAME") or ""
    password = get_secret("POSTGRES_PASSWORD") or ""
    database = os.environ.get("POSTGRES_DATABASE", "")

    missing = []
    if not address:
        missing.append("POSTGRES_HOST")
    if not username:
        missing.append("POSTGRES_USERNAME")
    if not password:
        missing.append("POSTGRES_PASSWORD")
    if not database:
        missing.append("POSTGRES_DATABASE")

    if missing:
        print(f"❌ Missing required environment variables: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)

    # POSTGRES_HOST may include an optional :port suffix (e.g. a pooler)
    default_port = int(os.getenv("POSTGRES_PORT", "5432") or "5432")
    host, port = parse_postgres_host_port(address, default_port)

    return f"host={host} port={port} user={username} password={password} dbname={database}"


def backfill_collection(conninfo: str, batch_size: int) -> int:
    """Fill `media` on `user_collections` rows where it is still NULL.

    Reads a batch of rows whose `media` is NULL, computes each row's media block from
    its raw `formats` column (already the Discogs API shape `map_discogs_formats`
    expects), writes the batch back, and repeats until no NULL rows remain. Every batch
    is committed on its own, so a large backfill makes steady progress rather than
    holding one long-running transaction.

    Returns:
        Total number of rows updated.
    """
    total = 0
    with psycopg.connect(conninfo) as conn:
        while True:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """
                        SELECT user_id, release_id, instance_id, formats
                        FROM user_collections
                        WHERE media IS NULL
                        LIMIT %s
                    """,
                    (batch_size,),
                )
                rows = cur.fetchall()

            if not rows:
                break

            updates = [
                (
                    json.dumps(map_discogs_formats(row["formats"])),
                    row["user_id"],
                    row["release_id"],
                    row["instance_id"],
                )
                for row in rows
            ]
            with conn.cursor() as cur:
                cur.executemany(
                    """
                        UPDATE user_collections
                        SET media = %s::jsonb
                        WHERE user_id = %s AND release_id = %s AND instance_id = %s
                    """,
                    updates,
                )
            conn.commit()
            total += len(rows)
            print(f"  collection: backfilled {total} row(s) so far…")

    return total


def backfill_wantlist(conninfo: str, batch_size: int) -> int:
    """Fill `media` on `user_wantlists` rows where it is still NULL.

    Reads a batch of rows whose `media` is NULL, derives a best-effort media block from
    the single legacy `format` name via `legacy_format_names_to_media` (the wantlist
    path never kept the full raw format objects — see the module docstring), writes the
    batch back, and repeats until no NULL rows remain.

    Returns:
        Total number of rows updated.
    """
    total = 0
    with psycopg.connect(conninfo) as conn:
        while True:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """
                        SELECT user_id, release_id, format
                        FROM user_wantlists
                        WHERE media IS NULL
                        LIMIT %s
                    """,
                    (batch_size,),
                )
                rows = cur.fetchall()

            if not rows:
                break

            updates = [
                (
                    json.dumps(legacy_format_names_to_media([row["format"]] if row["format"] else [])),
                    row["user_id"],
                    row["release_id"],
                )
                for row in rows
            ]
            with conn.cursor() as cur:
                cur.executemany(
                    """
                        UPDATE user_wantlists
                        SET media = %s::jsonb
                        WHERE user_id = %s AND release_id = %s
                    """,
                    updates,
                )
            conn.commit()
            total += len(rows)
            print(f"  wantlist: backfilled {total} row(s) so far…")

    return total


def main() -> None:
    """Entry point for the catalog-media-backfill CLI tool."""
    parser = argparse.ArgumentParser(
        prog="catalog-media-backfill",
        description=(
            "One-shot backfill of the canonical `media` column (ADR 0007) on existing "
            "user_collections and user_wantlists rows. Only rows with media IS NULL are "
            "touched, so this is safe to re-run."
        ),
        epilog=("Reads DB connection from environment variables: POSTGRES_HOST, POSTGRES_USERNAME, POSTGRES_PASSWORD, POSTGRES_DATABASE"),
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        metavar="N",
        help=f"Rows to read and update per batch (default: {DEFAULT_BATCH_SIZE})",
    )
    parser.add_argument("--collection-only", action="store_true", help="Only backfill user_collections")
    parser.add_argument("--wantlist-only", action="store_true", help="Only backfill user_wantlists")

    args = parser.parse_args()

    if args.collection_only and args.wantlist_only:
        parser.error("--collection-only and --wantlist-only are mutually exclusive")

    if args.batch_size < 1:
        parser.error("--batch-size must be at least 1")

    conninfo = _build_conninfo()

    collection_total = 0
    wantlist_total = 0

    if not args.wantlist_only:
        print("📋 Backfilling user_collections.media…")
        collection_total = backfill_collection(conninfo, args.batch_size)
        print(f"✅ user_collections: {collection_total} row(s) backfilled.")

    if not args.collection_only:
        print("📋 Backfilling user_wantlists.media…")
        wantlist_total = backfill_wantlist(conninfo, args.batch_size)
        print(f"✅ user_wantlists: {wantlist_total} row(s) backfilled.")

    print(f"✅ Done. Total rows backfilled: {collection_total + wantlist_total}.")


if __name__ == "__main__":
    main()
