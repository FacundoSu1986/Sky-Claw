"""Tests for sky_claw.scraper.masterlist."""

from __future__ import annotations

from unittest.mock import AsyncMock

import aiohttp
import pytest
from sky_claw.scraper.masterlist import MasterlistClient, MasterlistFetchError
from sky_claw.security.network_gateway import (
    EgressPolicy,
    EgressViolation,
    NetworkGateway,
)


@pytest.fixture()
def gw() -> NetworkGateway:
    return NetworkGateway(EgressPolicy(block_private_ips=False))


@pytest.fixture()
def client(gw: NetworkGateway) -> MasterlistClient:
    return MasterlistClient(gateway=gw, api_key="test-key")


class TestMasterlistClient:
    @pytest.mark.asyncio
    async def test_fetch_mod_info_success(self, client: MasterlistClient) -> None:
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={"mod_id": 42, "name": "TestMod"})
        mock_resp.release = AsyncMock()

        mock_session = AsyncMock(spec=aiohttp.ClientSession)
        mock_session.request = AsyncMock(return_value=mock_resp)

        # Patch gateway to skip egress check since we use fake URLs
        client._gw.authorize = AsyncMock()

        result = await client.fetch_mod_info(42, mock_session)
        assert result["mod_id"] == 42
        assert result["name"] == "TestMod"

    @pytest.mark.asyncio
    async def test_fetch_mod_info_http_error(self, client: MasterlistClient) -> None:
        mock_resp = AsyncMock()
        mock_resp.status = 404
        mock_resp.text = AsyncMock(return_value="Not Found")
        mock_resp.release = AsyncMock()

        mock_session = AsyncMock(spec=aiohttp.ClientSession)
        mock_session.request = AsyncMock(return_value=mock_resp)

        client._gw.authorize = AsyncMock()

        with pytest.raises(MasterlistFetchError, match="HTTP 404"):
            await client.fetch_mod_info(999, mock_session)


class TestNetworkGatewayRequest:
    @pytest.mark.asyncio
    async def test_request_authorizes_and_calls(self, gw: NetworkGateway) -> None:
        mock_resp = AsyncMock()
        mock_session = AsyncMock(spec=aiohttp.ClientSession)
        mock_session.request = AsyncMock(return_value=mock_resp)

        resp = await gw.request("GET", "https://www.nexusmods.com/test", mock_session)
        assert resp is mock_resp
        mock_session.request.assert_awaited_once()
        call_args = mock_session.request.call_args
        assert call_args[0] == ("GET", "https://www.nexusmods.com/test")
        assert call_args[1]["allow_redirects"] is False

    @pytest.mark.asyncio
    async def test_request_rejects_blocked_host(self, gw: NetworkGateway) -> None:
        mock_session = AsyncMock(spec=aiohttp.ClientSession)
        with pytest.raises(EgressViolation, match="not in the allow-list"):
            await gw.request("GET", "https://evil.example.com/x", mock_session)
