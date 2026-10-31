"""The shared Valkey TLS switch reaches the API's Valkey URL (gm-catalog-api-ihvh)."""

from __future__ import annotations

import pytest

from api.config import ApiConfig


REQUIRED_ENV = {
    "POSTGRES_HOST": "h",
    "POSTGRES_USERNAME": "u",
    "POSTGRES_PASSWORD": "p",
    "POSTGRES_DATABASE": "d",
    "JWT_SECRET_KEY": "x" * 32,
    "NEO4J_HOST": "bolt://n:7687",
    "NEO4J_USERNAME": "n",
    "NEO4J_PASSWORD": "p",
}


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in REQUIRED_ENV.items():
        monkeypatch.setenv(name, value)
    for name in (
        "VALKEY_HOST",
        "VALKEY_PORT",
        "VALKEY_DB",
        "VALKEY_PASSWORD",
        "VALKEY_PASSWORD_FILE",
        "VALKEY_TLS",
        "VALKEY_TLS_CA_FILE",
        "VALKEY_TLS_CERT_FILE",
        "VALKEY_TLS_KEY_FILE",
        "VALKEY_TLS_VERIFY_HOSTNAME",
    ):
        monkeypatch.delenv(name, raising=False)


def test_valkey_url_stays_plaintext_when_tls_is_unset() -> None:
    assert ApiConfig.from_env().valkey_url == "valkey://localhost:6379/0"


def test_valkey_url_uses_verified_tls_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VALKEY_TLS", "true")
    url = ApiConfig.from_env().valkey_url
    assert url == "valkeys://localhost:6379/0?ssl_cert_reqs=required&ssl_check_hostname=true"
