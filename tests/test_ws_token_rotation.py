"""Tests for WS token rotation invalidation (F3).

Verifies the AuthTokenManager rotation-callback machinery:

  1. register_rotation_callback() stores callbacks in AuthTokenManager.
  2. _rotation_loop() invokes registered callbacks after generate() succeeds
     and skips them when generate() raises.
  3. WebApp registra su callback de rotación y cierra los sockets /ws/ui
     activos al rotar el token (tanda 6 #219).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest
from aiohttp.test_utils import TestClient, TestServer

from sky_claw.antigravity.security.auth_token_manager import AuthTokenManager
from sky_claw.antigravity.web.app import WebApp

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
# WebApp — /ws/ui se registra en la rotación y cierra sockets activos (#219)
# ---------------------------------------------------------------------------


class _StubAuthRotacion:
    """Stub de AuthTokenManager: valida un token conocido y acumula callbacks.

    El PR #219 eliminó el Operations Hub y con él se perdió el registro del
    callback de rotación en ``create_app()``: los sockets /ws/ui viejos
    sobrevivían a la rotación del token. Estos tests anclan la regresión.
    """

    def __init__(self, valid_token: str = "token-bueno") -> None:
        self._valid = valid_token
        self.callbacks: list = []

    def validate(self, token: str) -> bool:
        return token == self._valid

    def register_rotation_callback(self, cb) -> None:
        if cb not in self.callbacks:
            self.callbacks.append(cb)


class TestWebAppCierraWsUiEnRotacion:
    def test_create_app_registra_callback_de_rotacion(self):
        auth = _StubAuthRotacion()
        webapp = WebApp(router=None, session=None, auth_manager=auth)

        webapp.create_app()

        assert webapp.close_all_ws_ui_clients in auth.callbacks

    def test_create_app_sin_auth_manager_no_registra_ni_rompe(self):
        # Fail-closed sin auth_manager ya está cubierto en test_ws_ui_chat;
        # acá solo importa que create_app() no explote sin manager.
        app = WebApp(router=None, session=None, auth_manager=None).create_app()
        assert app is not None

    @pytest.mark.asyncio
    async def test_rotacion_cierra_sockets_ws_ui_activos(self):
        auth = _StubAuthRotacion()
        webapp = WebApp(router=None, session=None, auth_manager=auth)
        app = webapp.create_app()
        server = TestServer(app)
        client = TestClient(server)
        await client.start_server()
        try:
            async with client.ws_connect("/ws/ui", headers={"X-Auth-Token": "token-bueno"}) as ws:
                # Simula la rotación: AuthTokenManager invoca los callbacks
                # registrados tras un generate() exitoso.
                for cb in auth.callbacks:
                    await cb()
                msg = await ws.receive(timeout=5)
                assert msg.type == aiohttp.WSMsgType.CLOSE
                assert msg.data == aiohttp.WSCloseCode.POLICY_VIOLATION  # 1008, no 4001 (lockout)
            assert webapp._ws_ui_clients == set()
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_conexion_durante_rotacion_es_rechazada(self):
        """La ventana conexión-vs-rotación: un socket que llega con la rotación
        en curso se rechaza con 1008 en vez de sobrevivir con el token viejo."""
        auth = _StubAuthRotacion()
        webapp = WebApp(router=None, session=None, auth_manager=auth)
        app = webapp.create_app()
        webapp._token_rotating = True  # rotación en curso
        server = TestServer(app)
        client = TestClient(server)
        await client.start_server()
        try:
            async with client.ws_connect("/ws/ui", headers={"X-Auth-Token": "token-bueno"}) as ws:
                msg = await ws.receive(timeout=5)
                assert msg.type == aiohttp.WSMsgType.CLOSE
                assert msg.data == aiohttp.WSCloseCode.POLICY_VIOLATION
            assert webapp._ws_ui_clients == set()
        finally:
            await client.close()
