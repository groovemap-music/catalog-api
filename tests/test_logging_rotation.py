"""Prove the API's file log sink rotates instead of growing without bound.

``api.api.main`` calls ``setup_logging("api", log_file=Path("/logs/api.log"))`` — see
python-libraries commit 11cf764 (``fix(logging): bound application log files``), vendored
here at ``groovemap-runtime`` revision 9bac022. That library now builds every ``setup_logging``
file sink through ``common.log_rotation.build_rotating_file_handler``, a size-capped
``RotatingFileHandler`` instead of an unbounded ``FileHandler``, bounded by the
``LOG_FILE_MAX_BYTES`` / ``LOG_FILE_BACKUP_COUNT`` environment variables.

``main()`` itself is excluded from coverage (it also starts Uvicorn), so this test exercises
the exact call shape it uses — service name plus a real ``log_file`` path — and asserts the
resulting root-logger file handler is bounded. The rotation mechanics themselves (rollover
behavior, invalid-override fallback) are proved by the library's own
``tests/test_log_rotation.py`` and ``tests/test_query_debug.py::TestGetProfilingLogger``,
which this test does not duplicate.
"""

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest
from common import setup_logging
from common.log_rotation import DEFAULT_LOG_FILE_BACKUP_COUNT, DEFAULT_LOG_FILE_MAX_BYTES


def test_api_log_file_handler_is_bounded_and_rotating(tmp_path: Path) -> None:
    """The handler `main()` builds for its log file is a size-capped RotatingFileHandler."""
    log_file = tmp_path / "api.log"
    root_logger = logging.getLogger()
    previous_handlers = root_logger.handlers[:]

    try:
        setup_logging("api", log_file=log_file)

        file_handlers = [handler for handler in root_logger.handlers if isinstance(handler, logging.FileHandler)]
        assert file_handlers, "setup_logging(log_file=...) must install a file handler"
        assert len(file_handlers) == 1

        handler = file_handlers[0]
        # RotatingFileHandler, not the bare, unbounded logging.FileHandler it replaced.
        assert isinstance(handler, RotatingFileHandler)
        assert handler.baseFilename == str(log_file)
        assert handler.maxBytes == DEFAULT_LOG_FILE_MAX_BYTES
        assert handler.backupCount == DEFAULT_LOG_FILE_BACKUP_COUNT
    finally:
        for installed_handler in root_logger.handlers:
            installed_handler.close()
        root_logger.handlers = previous_handlers


def test_api_log_file_handler_honors_deployment_overrides(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """LOG_FILE_MAX_BYTES / LOG_FILE_BACKUP_COUNT reach the handler this service builds."""
    monkeypatch.setenv("LOG_FILE_MAX_BYTES", "2048")
    monkeypatch.setenv("LOG_FILE_BACKUP_COUNT", "3")

    log_file = tmp_path / "api.log"
    root_logger = logging.getLogger()
    previous_handlers = root_logger.handlers[:]

    try:
        setup_logging("api", log_file=log_file)

        (handler,) = [handler for handler in root_logger.handlers if isinstance(handler, RotatingFileHandler)]
        assert handler.maxBytes == 2048
        assert handler.backupCount == 3
    finally:
        for installed_handler in root_logger.handlers:
            installed_handler.close()
        root_logger.handlers = previous_handlers
