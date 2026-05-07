"""Tests for WS token rotation invalidation (F3).

Verifies three behaviours introduced by the hardening sprint:

  1. register_rotation_callback() stores callbacks in AuthTokenManager.
  2. close_all_clients() sends POLICY_VIOLATION to every connected socket
     without touching the bus subscription.
  3. _rotation_loop() invokes registered callbacks after generate() succeeds
     and skips them when generate() raises.
  4. WebApp.create_app() wires ops_hub_handler.close_all_clients as a
     rotation callback when both auth_manager and event_bus are provided.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from sky_claw.antigravity.security.auth_token_manager import AuthTokenManager
from sky_claw.antigravity.web.operations_hub_ws import OperationsHubWSHandler

# ---------------------------------------------------------------------------
# AuthTokenManager — callback registry
# ---------------------------------------------------------------------------


class TestRotationCallbackRegistry:
    def test_register_single_callback(self, tmp_path):
        mgr = AuthTokenManager(token_dir=str(tmp_path))
        cb = AsyncMock()
        mgr.register_rotation_callback(cb)
        assert cb in mgr._rotation_callbacks

    def test_register_multiple_callbacks(self, tmp_path):
        mgr = AuthTokenManager(token_dir=str(tmp_path))
        cb1, cb2 = AsyncMock(), AsyncMock()
        mgr.register_rotation_callback(cb1)
        mgr.register_rotation_callback(cb2)
        assert mgr._rotation_callbacks == [cb1, cb2]

    @pytest.mark.asyncio
    async def test_rotation_loop_calls_callbacks_on_success(self, tmp_path):
        """After generate() succeeds, all registered callbacks are awaited."""
        mgr = AuthTokenManager(token_dir=str(tmp_path))
        cb = AsyncMock()
        mgr.register_rotation_callback(cb)

        # First sleep completes normally → iteration runs → callbacks fire.
        # Second sleep raises CancelledError → loop exits.
        sleep_calls = 0

        async def sleep_once_then_cancel(_):
            nonlocal sleep_calls
            sleep_calls += 1
            if sleep_calls >= 2:
                raise asyncio.CancelledError

        with (
            patch.object(mgr, "generate", return_value="tok"),
            patch("asyncio.sleep", side_effect=sleep_once_then_cancel),
            pytest.raises(asyncio.CancelledError),
        ):
            await mgr._rotation_loop()

        cb.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_rotation_loop_skips_callbacks_on_generate_failure(self, tmp_path):
        """When generate() raises, callbacks must NOT be called."""
        mgr = AuthTokenManager(token_dir=str(tmp_path))
        cb = AsyncMock()
        mgr.register_rotation_callback(cb)

        sleep_calls = 0

        async def sleep_once_then_cancel(_):
            nonlocal sleep_calls
            sleep_calls += 1
            if sleep_calls >= 2:
                raise asyncio.CancelledError

        with (
            patch.object(mgr, "generate", side_effect=RuntimeError("disk full")),
            patch("asyncio.sleep", side_effect=sleep_once_then_cancel),
            pytest.raises(asyncio.CancelledError),
        ):
            await mgr._rotation_loop()

        cb.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_rotation_loop_continues_if_callback_raises(self, tmp_path):
        """A callback that raises must not break the rotation loop; subsequent callbacks still run."""
        mgr = AuthTokenManager(token_dir=str(tmp_path))
        bad_cb = AsyncMock(side_effect=RuntimeError("cb failed"))
        good_cb = AsyncMock()
        mgr.register_rotation_callback(bad_cb)
        mgr.register_rotation_callback(good_cb)

        sleep_calls = 0

        async def sleep_once_then_cancel(_):
            nonlocal sleep_calls
            sleep_calls += 1
            if sleep_calls >= 2:
                raise asyncio.CancelledError

        with (
            patch.object(mgr, "generate", return_value="tok"),
            patch("asyncio.sleep", side_effect=sleep_once_then_cancel),
            pytest.raises(asyncio.CancelledError),
        ):
            await mgr._rotation_loop()

        good_cb.assert_awaited_once()


# ---------------------------------------------------------------------------
# OperationsHubWSHandler — close_all_clients
# ---------------------------------------------------------------------------


class TestCloseAllClients:
    def _make_handler(self) -> OperationsHubWSHandler:
        bus = MagicMock()
        bus.subscribe = MagicMock()
        return OperationsHubWSHandler(bus)

    @pytest.mark.asyncio
    async def test_close_all_sends_policy_violation(self):
        handler = self._make_handler()
        ws1 = AsyncMock(spec=aiohttp.web.WebSocketResponse)
        ws2 = AsyncMock(spec=aiohttp.web.WebSocketResponse)
        handler._clients = {ws1, ws2}

        await handler.close_all_clients()

        for ws in (ws1, ws2):
            ws.close.assert_awaited_once()
            _, kwargs = ws.close.call_args
            assert kwargs.get("code") == aiohttp.WSCloseCode.POLICY_VIOLATION

    @pytest.mark.asyncio
    async def test_close_all_empties_clients_set(self):
        handler = self._make_handler()
        handler._clients = {AsyncMock(spec=aiohttp.web.WebSocketResponse)}

        await handler.close_all_clients()

        assert len(handler._clients) == 0

    @pytest.mark.asyncio
    async def test_close_all_tolerates_connection_reset(self):
        """ConnectionResetError during close must not propagate."""
        handler = self._make_handler()
        ws = AsyncMock(spec=aiohttp.web.WebSocketResponse)
        ws.close.side_effect = ConnectionResetError
        handler._clients = {ws}

        await handler.close_all_clients()  # must not raise

    @pytest.mark.asyncio
    async def test_close_all_with_no_clients(self):
        """No clients → function completes without error."""
        handler = self._make_handler()
        await handler.close_all_clients()  # must not raise


# ---------------------------------------------------------------------------
# WebApp — wiring integration
# ---------------------------------------------------------------------------


class TestWebAppRotationWiring:
    @pytest.mark.asyncio
    async def test_create_app_registers_callback(self):
        """create_app() registers close_all_clients as a rotation callback
        when both auth_manager and event_bus are provided."""
        from sky_claw.antigravity.web.app import WebApp

        auth_manager = MagicMock(spec=AuthTokenManager)
        auth_manager.register_rotation_callback = MagicMock()
        event_bus = MagicMock()
        event_bus.subscribe = MagicMock()

        session = MagicMock(spec=aiohttp.ClientSession)
        web_app = WebApp(router=None, session=session, auth_manager=auth_manager, event_bus=event_bus)

        with patch("sky_claw.antigravity.web.app.register_operations_hub_routes") as mock_register:
            mock_handler = MagicMock(spec=OperationsHubWSHandler)
            mock_handler.close_all_clients = AsyncMock()
            mock_register.return_value = mock_handler
            web_app.create_app()

        auth_manager.register_rotation_callback.assert_called_once_with(mock_handler.close_all_clients)

    @pytest.mark.asyncio
    async def test_create_app_no_callback_without_auth_manager(self):
        """No auth_manager → register_rotation_callback must NOT be called."""
        from sky_claw.antigravity.web.app import WebApp

        event_bus = MagicMock()
        event_bus.subscribe = MagicMock()
        session = MagicMock(spec=aiohttp.ClientSession)
        web_app = WebApp(router=None, session=session, auth_manager=None, event_bus=event_bus)

        with patch("sky_claw.antigravity.web.app.register_operations_hub_routes") as mock_register:
            mock_handler = MagicMock(spec=OperationsHubWSHandler)
            mock_register.return_value = mock_handler
            web_app.create_app()

        # auth_manager is None so no callback registration should happen
        # (would raise AttributeError if attempted on None)
