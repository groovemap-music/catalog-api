"""Tests for the catalog-media-backfill CLI tool (api/media_backfill.py)."""

import json
from unittest.mock import MagicMock, patch

import pytest
from common.media import legacy_format_names_to_media, map_discogs_formats


def _cursor(fetchall_results: list[list[dict]] | None = None) -> MagicMock:
    """Build a mock cursor whose fetchall() returns each list in sequence, then [].

    Mirrors the batch loop's own termination condition (an empty result ends the
    while-True loop), so a test only needs to supply the non-empty batches.
    """
    mock_cur = MagicMock()
    mock_cur.__enter__ = MagicMock(return_value=mock_cur)
    mock_cur.__exit__ = MagicMock(return_value=False)
    results = list(fetchall_results or [])
    results.append([])
    mock_cur.fetchall = MagicMock(side_effect=results)
    return mock_cur


def _conn(select_cur: MagicMock, update_cur: MagicMock) -> MagicMock:
    """Build a mock connection whose cursor() alternates SELECT/UPDATE cursors.

    backfill_collection/backfill_wantlist open a fresh cursor for the SELECT and
    another for the executemany UPDATE, each iteration — this hands back
    select_cur, update_cur, select_cur, update_cur, … in that order.
    """
    mock_conn = MagicMock()
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_conn.cursor = MagicMock(side_effect=lambda *_args, **_kwargs: select_cur if mock_conn.cursor.call_count % 2 == 1 else update_cur)
    mock_conn.commit = MagicMock()
    return mock_conn


class TestBuildConninfo:
    """Tests for _build_conninfo (same contract as api/setup.py's helper)."""

    def test_builds_conninfo_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from api.media_backfill import _build_conninfo

        monkeypatch.setenv("POSTGRES_HOST", "db")
        monkeypatch.setenv("POSTGRES_USERNAME", "user")
        monkeypatch.setenv("POSTGRES_PASSWORD", "pass")
        monkeypatch.setenv("POSTGRES_DATABASE", "mydb")

        conninfo = _build_conninfo()
        assert "host=db" in conninfo
        assert "user=user" in conninfo
        assert "password=pass" in conninfo
        assert "dbname=mydb" in conninfo

    def test_missing_env_vars_exits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from api.media_backfill import _build_conninfo

        for var in ("POSTGRES_HOST", "POSTGRES_USERNAME", "POSTGRES_PASSWORD", "POSTGRES_DATABASE"):
            monkeypatch.delenv(var, raising=False)

        with pytest.raises(SystemExit) as exc_info:
            _build_conninfo()
        assert exc_info.value.code == 1


class TestBackfillCollection:
    """Tests for backfill_collection."""

    def test_fills_null_media_rows_from_raw_formats(self) -> None:
        from api.media_backfill import backfill_collection

        formats_raw = [{"name": "Vinyl", "qty": "1", "descriptions": ['12"', "Album"], "text": None}]
        rows = [
            {
                "user_id": "00000000-0000-0000-0000-000000000001",
                "release_id": 123,
                "instance_id": 1123,
                "formats": formats_raw,
            }
        ]

        select_cur = _cursor([rows])
        update_cur = _cursor()
        mock_conn = _conn(select_cur, update_cur)

        with patch("psycopg.connect", return_value=mock_conn):
            total = backfill_collection("host=localhost dbname=test", batch_size=500)

        assert total == 1
        # Two SELECTs: one that returns the batch, one that finds nothing left
        # and ends the loop — the loop's own termination condition.
        assert select_cur.execute.call_count == 2
        assert "WHERE media IS NULL" in select_cur.execute.call_args_list[0].args[0]

        update_cur.executemany.assert_called_once()
        update_sql, updates = update_cur.executemany.call_args.args
        assert "SET media = %s::jsonb" in update_sql
        assert len(updates) == 1
        media_json, user_id, release_id, instance_id = updates[0]
        assert user_id == rows[0]["user_id"]
        assert release_id == 123
        assert instance_id == 1123
        assert json.loads(media_json) == map_discogs_formats(formats_raw)

        mock_conn.commit.assert_called_once()

    def test_no_null_rows_is_a_noop(self) -> None:
        from api.media_backfill import backfill_collection

        select_cur = _cursor()  # first fetchall() already returns []
        update_cur = _cursor()
        mock_conn = _conn(select_cur, update_cur)

        with patch("psycopg.connect", return_value=mock_conn):
            total = backfill_collection("host=localhost dbname=test", batch_size=500)

        assert total == 0
        update_cur.executemany.assert_not_called()
        mock_conn.commit.assert_not_called()

    def test_batches_until_exhausted(self) -> None:
        """Two non-empty batches must both be processed before the loop ends."""
        from api.media_backfill import backfill_collection

        def _row(n: int) -> dict:
            return {
                "user_id": "00000000-0000-0000-0000-000000000001",
                "release_id": n,
                "instance_id": n,
                "formats": [{"name": "CD", "qty": "1", "descriptions": [], "text": None}],
            }

        batch1 = [_row(1), _row(2)]
        batch2 = [_row(3)]

        select_cur = _cursor([batch1, batch2])
        update_cur = _cursor()
        mock_conn = _conn(select_cur, update_cur)

        with patch("psycopg.connect", return_value=mock_conn):
            total = backfill_collection("host=localhost dbname=test", batch_size=2)

        assert total == 3
        assert update_cur.executemany.call_count == 2
        assert mock_conn.commit.call_count == 2

    def test_idempotent_second_run_touches_nothing(self) -> None:
        """A row already backfilled (media NOT NULL) is never selected again —
        WHERE media IS NULL means a second run over the same table is a no-op."""
        from api.media_backfill import backfill_collection

        # Simulates the state *after* a first successful run: no NULL rows left.
        select_cur = _cursor()
        update_cur = _cursor()
        mock_conn = _conn(select_cur, update_cur)

        with patch("psycopg.connect", return_value=mock_conn):
            total = backfill_collection("host=localhost dbname=test", batch_size=500)

        assert total == 0
        update_cur.executemany.assert_not_called()


class TestBackfillWantlist:
    """Tests for backfill_wantlist."""

    def test_fills_null_media_rows_from_legacy_format_name(self) -> None:
        from api.media_backfill import backfill_wantlist

        rows = [
            {
                "user_id": "00000000-0000-0000-0000-000000000002",
                "release_id": 456,
                "format": "Vinyl",
            }
        ]

        select_cur = _cursor([rows])
        update_cur = _cursor()
        mock_conn = _conn(select_cur, update_cur)

        with patch("psycopg.connect", return_value=mock_conn):
            total = backfill_wantlist("host=localhost dbname=test", batch_size=500)

        assert total == 1
        # Two SELECTs: one that returns the batch, one that finds nothing left
        # and ends the loop — the loop's own termination condition.
        assert select_cur.execute.call_count == 2
        assert "WHERE media IS NULL" in select_cur.execute.call_args_list[0].args[0]

        update_cur.executemany.assert_called_once()
        update_sql, updates = update_cur.executemany.call_args.args
        assert "SET media = %s::jsonb" in update_sql
        assert len(updates) == 1
        media_json, user_id, release_id = updates[0]
        assert user_id == rows[0]["user_id"]
        assert release_id == 456
        assert json.loads(media_json) == legacy_format_names_to_media(["Vinyl"])

        mock_conn.commit.assert_called_once()

    def test_null_format_name_still_writes_empty_media_block(self) -> None:
        from api.media_backfill import backfill_wantlist

        rows = [{"user_id": "00000000-0000-0000-0000-000000000003", "release_id": 789, "format": None}]

        select_cur = _cursor([rows])
        update_cur = _cursor()
        mock_conn = _conn(select_cur, update_cur)

        with patch("psycopg.connect", return_value=mock_conn):
            backfill_wantlist("host=localhost dbname=test", batch_size=500)

        media_json = update_cur.executemany.call_args.args[1][0][0]
        media_block = json.loads(media_json)
        assert media_block["items"] == []
        assert media_block["families"] == []

    def test_no_null_rows_is_a_noop(self) -> None:
        from api.media_backfill import backfill_wantlist

        select_cur = _cursor()
        update_cur = _cursor()
        mock_conn = _conn(select_cur, update_cur)

        with patch("psycopg.connect", return_value=mock_conn):
            total = backfill_wantlist("host=localhost dbname=test", batch_size=500)

        assert total == 0
        update_cur.executemany.assert_not_called()


class TestMain:
    """Tests for the main() entry point."""

    def test_help_exits_cleanly(self) -> None:
        from api.media_backfill import main

        with pytest.raises(SystemExit) as exc_info, patch("sys.argv", ["catalog-media-backfill", "--help"]):
            main()
        assert exc_info.value.code == 0

    def test_mutually_exclusive_flags_exit_with_error(self) -> None:
        from api.media_backfill import main

        with (
            pytest.raises(SystemExit) as exc_info,
            patch("sys.argv", ["catalog-media-backfill", "--collection-only", "--wantlist-only"]),
        ):
            main()
        assert exc_info.value.code != 0

    def test_invalid_batch_size_exits_with_error(self) -> None:
        from api.media_backfill import main

        with (
            pytest.raises(SystemExit) as exc_info,
            patch("sys.argv", ["catalog-media-backfill", "--batch-size", "0"]),
        ):
            main()
        assert exc_info.value.code != 0

    def test_runs_both_backfills_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from api.media_backfill import main

        monkeypatch.setenv("POSTGRES_HOST", "db")
        monkeypatch.setenv("POSTGRES_USERNAME", "u")
        monkeypatch.setenv("POSTGRES_PASSWORD", "p")
        monkeypatch.setenv("POSTGRES_DATABASE", "d")

        with (
            patch("sys.argv", ["catalog-media-backfill"]),
            patch("api.media_backfill.backfill_collection", return_value=2) as mock_collection,
            patch("api.media_backfill.backfill_wantlist", return_value=3) as mock_wantlist,
        ):
            main()

        mock_collection.assert_called_once()
        mock_wantlist.assert_called_once()

    def test_collection_only_skips_wantlist(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from api.media_backfill import main

        monkeypatch.setenv("POSTGRES_HOST", "db")
        monkeypatch.setenv("POSTGRES_USERNAME", "u")
        monkeypatch.setenv("POSTGRES_PASSWORD", "p")
        monkeypatch.setenv("POSTGRES_DATABASE", "d")

        with (
            patch("sys.argv", ["catalog-media-backfill", "--collection-only"]),
            patch("api.media_backfill.backfill_collection", return_value=0) as mock_collection,
            patch("api.media_backfill.backfill_wantlist", return_value=0) as mock_wantlist,
        ):
            main()

        mock_collection.assert_called_once()
        mock_wantlist.assert_not_called()

    def test_wantlist_only_skips_collection(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from api.media_backfill import main

        monkeypatch.setenv("POSTGRES_HOST", "db")
        monkeypatch.setenv("POSTGRES_USERNAME", "u")
        monkeypatch.setenv("POSTGRES_PASSWORD", "p")
        monkeypatch.setenv("POSTGRES_DATABASE", "d")

        with (
            patch("sys.argv", ["catalog-media-backfill", "--wantlist-only"]),
            patch("api.media_backfill.backfill_collection", return_value=0) as mock_collection,
            patch("api.media_backfill.backfill_wantlist", return_value=0) as mock_wantlist,
        ):
            main()

        mock_collection.assert_not_called()
        mock_wantlist.assert_called_once()
