"""Tests for the circuit breaker in sky_claw.scraper.masterlist."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
from sky_claw.scraper.masterlist import (
    CircuitOpenError,
    MasterlistClient,
    MasterlistFetchError,
    _CircuitBreaker,
)
from sky_claw.security.network_gateway import NetworkGateway


class TestCircuitBreaker:
    def test_starts_closed(self) -> None:
        cb = _CircuitBreaker()
        assert cb.state == "closed"
        assert cb.allow_request() is True

    def test_trips_after_threshold(self) -> None:
        cb = _CircuitBreaker(failure_threshold=3, recovery_timeout=60)
        cb.record_failure()
        cb.record_failure()
        assert cb.state == "closed"
        cb.record_failure()
        assert cb.state == "open"
        assert cb.allow_request() is False

    def test_resets_on_success(self) -> None:
        cb = _CircuitBreaker(failure_threshold=2, recovery_timeout=60)
        cb.record_failure()
        cb.record_success()
        assert cb.state == "closed"
        # Should need 2 more failures to trip again.
        cb.record_failure()
        assert cb.state == "closed"

    def test_half_open_after_recovery(self) -> None:
        cb = _CircuitBreaker(failure_threshold=1, recovery_timeout=0)
        cb.record_failure()
        # With 0-second recovery, the very first .state access after tripping
        # will promote open → half-open immediately.
        assert cb.state == "half-open"
        assert cb.allow_request() is True


class TestMasterlistClientCircuitBreaker:
    @pytest.mark.asyncio
    async def test_circuit_open_raises_error(self) -> None:
        gw = MagicMock(spec=NetworkGateway)
        client = MasterlistClient(gw, "fake-key", failure_threshold=1)

        # Simulate a failure to trip the breaker.
        mock_resp = AsyncMock()
        mock_resp.status = 500
        mock_resp.text = AsyncMock(return_value="Server Error")
        mock_resp.release = AsyncMock()
        gw.request = AsyncMock(return_value=mock_resp)

        session = AsyncMock(spec=aiohttp.ClientSession)

        with pytest.raises(MasterlistFetchError):
            await client.fetch_mod_info(1234, session)

        assert client.circuit_state == "open"

        with pytest.raises(CircuitOpenError):
            await client.fetch_mod_info(5678, session)

    @pytest.mark.asyncio
    async def test_success_resets_circuit(self) -> None:
        gw = MagicMock(spec=NetworkGateway)
        client = MasterlistClient(gw, "fake-key", failure_threshold=3)

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={"mod_id": 42, "name": "TestMod"})
        mock_resp.release = AsyncMock()
        gw.request = AsyncMock(return_value=mock_resp)

        session = AsyncMock(spec=aiohttp.ClientSession)

        result = await client.fetch_mod_info(42, session)
        assert result["mod_id"] == 42
        assert client.circuit_state == "closed"
