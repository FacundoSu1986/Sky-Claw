"""P1-1 + P1-4 — el teardown de la GUI debe completarse y soltar el puerto.

P1-1. ``_shutdown`` del bootloader era una cadena PLANA:
``stop_rotation()`` → ``revoke()`` → ``runner.cleanup()`` → ``ctx.stop()``.
``revoke()`` hace un ``unlink`` desnudo (``auth_token_manager.py``, sin el
``suppress(OSError)`` que sí protege a ``generate()``), así que un
``PermissionError`` —antivirus o handle abierto, cotidiano en Windows— cortaba
la cadena: ``runner.cleanup()`` y ``ctx.stop()`` nunca corrían. El puerto 8765
quedaba retenido y el proceso se llevaba DB y locks puestos. Es el mismo
WinError 10048 del post-mortem, entrando por otra puerta.

P1-4. ``create_app()`` no registraba NINGÚN ``on_shutdown``:
``close_all_ws_ui_clients`` estaba cableado solo al callback de rotación de
token. aiohttp NO cierra los WebSockets en ``cleanup()`` —espera a que los
handlers terminen— y el de ``/ws/ui`` vive en su ``async for``, así que
``runner.cleanup()`` se bloqueaba hasta agotar su timeout de apagado.

El código de cierre importa: ``POLICY_VIOLATION`` (1008) le dice al cliente que
relea el token y reconecte, que es correcto al rotar y **desastroso** al
apagar. Para el apagado va ``GOING_AWAY`` (1001).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import aiohttp
from aiohttp.test_utils import TestClient, TestServer

from sky_claw.antigravity.web.app import WebApp


class _StubAuth:
    """Stub de AuthTokenManager: valida un token conocido y acumula callbacks."""

    def __init__(self, valid_token: str = "token-bueno") -> None:
        self._valid = valid_token
        self.callbacks: list = []

    def validate(self, token: str) -> bool:
        return token == self._valid

    def register_rotation_callback(self, cb) -> None:
        if cb not in self.callbacks:
            self.callbacks.append(cb)


# ---------------------------------------------------------------------------
# P1-1 — teardown resiliente
# ---------------------------------------------------------------------------


async def test_teardown_corre_todos_los_pasos_aunque_revoke_falle() -> None:
    """Un paso que falla no puede llevarse puestos los siguientes.

    El caso real: ``revoke()`` levanta ``PermissionError`` al borrar el token
    y ``runner.cleanup()`` (que libera el 8765) jamás corre.
    """
    from sky_claw.antigravity.gui import _bootloader

    pasos: list[str] = []

    auth = MagicMock()

    async def _stop_rotation() -> None:
        pasos.append("stop_rotation")

    def _revoke() -> None:
        pasos.append("revoke")
        raise PermissionError(32, "El proceso no tiene acceso al archivo")

    auth.stop_rotation = _stop_rotation
    auth.revoke = _revoke

    runner = MagicMock()

    async def _cleanup() -> None:
        pasos.append("runner.cleanup")

    runner.cleanup = _cleanup

    ctx = MagicMock()

    async def _stop() -> None:
        pasos.append("ctx.stop")

    ctx.stop = _stop

    await _bootloader._teardown_runtime({"auth_manager": auth, "aiohttp_runner": runner, "ctx": ctx})

    assert pasos == ["stop_rotation", "revoke", "runner.cleanup", "ctx.stop"], (
        "un paso que falló abortó la cadena: el 8765 queda retenido y ctx.stop() "
        f"nunca libera DB ni locks. Pasos ejecutados: {pasos}"
    )


async def test_teardown_tolera_un_runtime_vacio() -> None:
    """Si el bootstrap falló antes de poblar el runtime, el teardown no explota."""
    from sky_claw.antigravity.gui import _bootloader

    await _bootloader._teardown_runtime({})


# ---------------------------------------------------------------------------
# P1-4 — /ws/ui se cierra al apagar
# ---------------------------------------------------------------------------


def test_create_app_registra_on_shutdown_para_cerrar_ws_ui() -> None:
    """Sin ``on_shutdown``, aiohttp espera a los handlers de ``/ws/ui``."""
    webapp = WebApp(router=None, session=None, auth_manager=_StubAuth())

    app = webapp.create_app()

    assert webapp.close_all_ws_ui_clients_on_shutdown in app.on_shutdown, (
        "create_app() no registró el cierre de /ws/ui en on_shutdown: "
        "runner.cleanup() se cuelga esperando handlers que nadie corta"
    )


async def test_apagado_cierra_sockets_ws_ui_con_going_away() -> None:
    """El apagado cierra los sockets vivos, y con ``GOING_AWAY`` (1001).

    ``POLICY_VIOLATION`` (1008) haría que el cliente relea el token y
    reconecte contra un server que se está muriendo.
    """
    webapp = WebApp(router=None, session=None, auth_manager=_StubAuth())
    app = webapp.create_app()
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        async with client.ws_connect("/ws/ui", headers={"X-Auth-Token": "token-bueno"}) as ws:
            # Round-trip antes de apagar: garantiza que el handler ya registró
            # el socket en _ws_ui_clients (llegó a su loop de mensajes).
            await ws.send_json({"type": "command", "command": "chat", "payload": {"text": "ping"}})
            await ws.receive_json(timeout=5)

            await app.shutdown()

            msg = await ws.receive(timeout=5)
            assert msg.type == aiohttp.WSMsgType.CLOSE
            # msg.data trae el código en aiohttp actual; close_code es el
            # fallback documentado si algún build no lo puebla.
            assert (msg.data or ws.close_code) == aiohttp.WSCloseCode.GOING_AWAY, (
                "el apagado no usó GOING_AWAY (1001): con 1008 el cliente "
                "reintenta contra un server que se está muriendo"
            )
        assert webapp._ws_ui_clients == set()
    finally:
        await client.close()


async def test_rotacion_sigue_usando_policy_violation() -> None:
    """Guard de no-regresión: el apagado NO debe cambiar la semántica de la rotación.

    La rotación cierra con 1008 a propósito (``4001`` cuenta para el lockout de
    auth del cliente) y debe reabrir los handshakes al terminar.
    """
    auth = _StubAuth()
    webapp = WebApp(router=None, session=None, auth_manager=auth)
    app = webapp.create_app()
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        async with client.ws_connect("/ws/ui", headers={"X-Auth-Token": "token-bueno"}) as ws:
            await ws.send_json({"type": "command", "command": "chat", "payload": {"text": "ping"}})
            await ws.receive_json(timeout=5)

            for cb in auth.callbacks:
                await cb()

            msg = await ws.receive(timeout=5)
            assert msg.type == aiohttp.WSMsgType.CLOSE
            assert (msg.data or ws.close_code) == aiohttp.WSCloseCode.POLICY_VIOLATION

        assert webapp._token_rotating is False, (
            "la rotación debe reabrir los handshakes al terminar; solo el apagado deja la puerta cerrada"
        )
    finally:
        await client.close()
