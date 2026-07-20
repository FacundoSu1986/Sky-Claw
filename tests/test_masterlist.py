"""Tests for sky_claw.antigravity.scraper.masterlist."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from sky_claw.antigravity.scraper.masterlist import (
    MasterlistClient,
    MasterlistFetchError,
    MasterlistHTTPError,
)
from sky_claw.antigravity.security.network_gateway import (
    EgressPolicy,
    EgressViolationError,
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
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={"mod_id": 42, "name": "TestMod"})
        mock_resp.release = MagicMock()

        mock_session = AsyncMock(spec=aiohttp.ClientSession)
        mock_session.request = AsyncMock(return_value=mock_resp)

        # Patch gateway to skip egress check since we use fake URLs
        client._gw.authorize = AsyncMock()

        result = await client.fetch_mod_info(42, mock_session)
        assert result["mod_id"] == 42
        assert result["name"] == "TestMod"
        mock_resp.release.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_fetch_mod_info_http_error(self, client: MasterlistClient) -> None:
        mock_resp = MagicMock()
        mock_resp.status = 404
        mock_resp.text = AsyncMock(return_value="Not Found")
        mock_resp.release = MagicMock()

        mock_session = AsyncMock(spec=aiohttp.ClientSession)
        mock_session.request = AsyncMock(return_value=mock_resp)

        client._gw.authorize = AsyncMock()

        with pytest.raises(MasterlistFetchError, match="HTTP 404"):
            await client.fetch_mod_info(999, mock_session)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("status", "retryable"),
        [
            (401, False),
            (403, False),
            (404, False),
            (429, True),
            (500, True),
            (503, True),
        ],
    )
    async def test_error_http_expone_status_y_retryable(
        self,
        client: MasterlistClient,
        status: int,
        retryable: bool,
    ) -> None:
        """El caller clasifica por campos, sin interpretar el mensaje."""
        mock_resp = MagicMock()
        mock_resp.status = status
        mock_resp.text = AsyncMock(return_value="respuesta Nexus")
        mock_resp.release = MagicMock()
        client._gw.request = AsyncMock(return_value=mock_resp)
        session = AsyncMock(spec=aiohttp.ClientSession)

        with pytest.raises(MasterlistHTTPError) as raised:
            await client.fetch_mod_info(999, session)

        assert isinstance(raised.value, MasterlistFetchError)
        assert raised.value.status == status
        assert raised.value.retryable is retryable
        mock_resp.release.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_set_api_key_usa_la_nueva_en_el_header(self, client: MasterlistClient) -> None:
        """set_api_key permite refrescar la Nexus key sin recrear el cliente (review Codex #228)."""
        client.set_api_key("clave-nueva")
        captured: dict[str, dict[str, str] | None] = {}

        async def _fake_request(method: str, url: str, session: object, headers: dict[str, str] | None = None):
            captured["headers"] = headers
            resp = MagicMock()
            resp.status = 200
            resp.json = AsyncMock(return_value={"mod_id": 1})
            resp.release = MagicMock()
            return resp

        client._gw.request = _fake_request  # type: ignore[method-assign]
        await client.fetch_mod_info(1, AsyncMock(spec=aiohttp.ClientSession))
        assert captured["headers"] == {"apikey": "clave-nueva"}


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
        with pytest.raises(EgressViolationError, match="not in the allow-list"):
            await gw.request("GET", "https://evil.example.com/x", mock_session)
