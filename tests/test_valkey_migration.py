"""Driver-migration contracts at catalog-owned compatibility boundaries."""

from dataclasses import replace
from unittest.mock import AsyncMock, patch

import pytest

from api.api import _create_valkey
from api.config import ApiConfig


@pytest.mark.asyncio
async def test_valkey_startup_logs_only_host_without_connection_credentials(test_api_config: ApiConfig) -> None:
    url = "valkey://:fixture%3Acredential@cache:6379/0"
    config = replace(test_api_config, valkey_url=url)
    client = AsyncMock()
    with (
        patch("api.api.aiovalkey.from_url", new_callable=AsyncMock, return_value=client) as from_url,
        patch("api.api.instrument_valkey", return_value=client),
        patch("api.api.logger.info") as info,
    ):
        assert await _create_valkey(config) is client
    from_url.assert_awaited_once_with(url, decode_responses=True)
    info.assert_called_once_with("✅ Valkey connected", host="cache:6379/0")
    assert "fixture" not in str(info.call_args)
    assert "credential" not in str(info.call_args)
