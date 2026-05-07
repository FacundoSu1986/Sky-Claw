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
    @pytest.fixture(autouse=True)
    def bypass_token_dir_permissions(self):
        with patch("sky_claw.antigravity.security.auth_token_manager.restrict_to_owner"):
            yield

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

    def test_register_same_callback_twice_is_idempotent(self, tmp_path):
        """Registering the same callable twice must not duplicate it."""
        mgr = AuthTokenManager(token_dir=str(tmp_path))
        cb = AsyncMock()
        mgr.register_rotation_callback(cb)
        mgr.register_rotation_callback(cb)
        assert mgr._rotation_callbacks.count(cb) == 1

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

    @pytest.mark.asyncio
    async def test_close_all_tolerates_timeout_error(self):
        """asyncio.TimeoutError during close must not abort remaining closes."""
        import asyncio

        handler = self._make_handler()
        ws_timeout = AsyncMock(spec=aiohttp.web.WebSocketResponse)
        ws_timeout.close.side_effect = asyncio.TimeoutError
        ws_ok = AsyncMock(spec=aiohttp.web.WebSocketResponse)
        handler._clients = {ws_timeout, ws_ok}

        await handler.close_all_clients()  # must not raise

        ws_ok.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_close_all_times_out_hung_client_and_resets_rotation_flag(self):
        """A hung close must not leave token rotation stuck forever."""
        handler = self._make_handler()

        async def never_closes(**_kwargs):
            await asyncio.Event().wait()

        ws_hung = AsyncMock(spec=aiohttp.web.WebSocketResponse)
        ws_hung.close.side_effect = never_closes
        ws_ok = AsyncMock(spec=aiohttp.web.WebSocketResponse)
        handler._clients = {ws_hung, ws_ok}

        with patch("sky_claw.antigravity.web.operations_hub_ws.ROTATION_CLOSE_TIMEOUT_SECONDS", 0.01):
            await handler.close_all_clients()

        ws_ok.close.assert_awaited_once()
        assert not handler._token_rotating

    @pytest.mark.asyncio
    async def test_close_all_sets_token_rotating_flag_during_execution(self):
        """_token_rotating must be True while close_all_clients runs so concurrent
        websocket_handler calls that acquire the lock see the flag."""
        handler = self._make_handler()
        flag_during_close: list[bool] = []

        async def slow_close(**_kwargs):
            flag_during_close.append(handler._token_rotating)

        ws = AsyncMock(spec=aiohttp.web.WebSocketResponse)
        ws.close.side_effect = slow_close
        handler._clients = {ws}

        await handler.close_all_clients()

        assert flag_during_close == [True]
        assert not handler._token_rotating

    @pytest.mark.asyncio
    async def test_websocket_handler_rejects_connection_during_rotation(self):
        """A connection that arrives while _token_rotating=True is closed immediately."""
        handler = self._make_handler()
        handler._token_rotating = True

        ws_mock = AsyncMock(spec=aiohttp.web.WebSocketResponse)
        ws_mock.closed = False

        request = MagicMock()
        request.remote = "127.0.0.1"
        request.headers = {}

        with (
            patch.object(handler, "_validate_ws_auth", return_value=True),
            patch("aiohttp.web.WebSocketResponse", return_value=ws_mock),
        ):
            await handler.websocket_handler(request)

        ws_mock.close.assert_awaited_once()
        _, kwargs = ws_mock.close.call_args
        assert kwargs.get("code") == aiohttp.WSCloseCode.POLICY_VIOLATION
        assert ws_mock not in handler._clients


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

        auth_manager = MagicMock(spec=AuthTokenManager)
        auth_manager.register_rotation_callback = MagicMock()
        event_bus = MagicMock()
        event_bus.subscribe = MagicMock()
        session = MagicMock(spec=aiohttp.ClientSession)
        web_app = WebApp(router=None, session=session, auth_manager=None, event_bus=event_bus)

        with patch("sky_claw.antigravity.web.app.register_operations_hub_routes") as mock_register:
            mock_handler = MagicMock(spec=OperationsHubWSHandler)
            mock_register.return_value = mock_handler
            web_app.create_app()

        auth_manager.register_rotation_callback.assert_not_called()
