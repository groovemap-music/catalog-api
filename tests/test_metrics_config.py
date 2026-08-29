"""Regression tests for metrics environment parsing on ``ApiConfig``."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from api.config import ApiConfig


REQUIRED_ENV = {
    "POSTGRES_HOST": "localhost",
    "POSTGRES_USERNAME": "test",
    "POSTGRES_PASSWORD": "test",
    "POSTGRES_DATABASE": "test",
    "JWT_SECRET_KEY": "secret",
    "NEO4J_HOST": "localhost",
    "NEO4J_USERNAME": "neo4j",
    "NEO4J_PASSWORD": "pass",
}


def _config(**overrides: str) -> ApiConfig:
    environment = {**REQUIRED_ENV, **overrides}
    with patch.dict(os.environ, environment, clear=True):
        return ApiConfig.from_env()


@pytest.mark.parametrize(
    ("attribute", "expected"),
    [("metrics_retention_days", 366), ("metrics_collection_interval", 300)],
)
def test_metrics_defaults(attribute: str, expected: int) -> None:
    assert getattr(_config(), attribute) == expected


@pytest.mark.parametrize(
    ("environment", "attribute", "expected"),
    [
        ({"METRICS_RETENTION_DAYS": "90"}, "metrics_retention_days", 90),
        ({"METRICS_COLLECTION_INTERVAL": "60"}, "metrics_collection_interval", 60),
    ],
)
def test_metrics_environment_overrides(environment: dict[str, str], attribute: str, expected: int) -> None:
    assert getattr(_config(**environment), attribute) == expected


@pytest.mark.parametrize(
    ("environment", "attribute", "expected"),
    [
        ({"METRICS_RETENTION_DAYS": "invalid"}, "metrics_retention_days", 366),
        ({"METRICS_COLLECTION_INTERVAL": "invalid"}, "metrics_collection_interval", 300),
    ],
)
def test_invalid_metrics_values_use_defaults(environment: dict[str, str], attribute: str, expected: int) -> None:
    assert getattr(_config(**environment), attribute) == expected
