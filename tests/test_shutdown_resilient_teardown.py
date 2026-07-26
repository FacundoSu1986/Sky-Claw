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

import asyncio
import inspect
import logging
import time
from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import aiohttp
from aiohttp.test_utils import TestClient, TestServer

import sky_claw.app.web.app as app_module
from sky_claw.app.web.app import WebApp


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
    from sky_claw.app.gui import _bootloader

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
    from sky_claw.app.gui import _bootloader

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
            # Intercambio previo antes de apagar: garantiza que el handler ya
            # registró el socket en _ws_ui_clients (llegó a su loop de mensajes).
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


# ---------------------------------------------------------------------------
# Hallazgos de revisión (Codex) sobre P1-4
# ---------------------------------------------------------------------------


async def test_cierre_de_multiples_sockets_ws_ui_es_concurrente_no_secuencial(
    monkeypatch: object,
) -> None:
    """N sockets colgados no deben multiplicar el tiempo total de cierre.

    El loop cerraba cada socket con su propio ``wait_for`` SECUENCIAL: N
    clientes que no ACKean el close frame significan
    N × ``ROTATION_CLOSE_TIMEOUT_SECONDS`` de ``runner.cleanup()`` bloqueado —
    el apagado seguía siendo efectivamente ilimitado con varios clientes
    colgados a la vez, aunque cada uno individualmente esté acotado.
    """
    monkeypatch.setattr(app_module, "ROTATION_CLOSE_TIMEOUT_SECONDS", 0.1)
    webapp = WebApp(router=None, session=None, auth_manager=_StubAuth())

    class _SocketColgado:
        async def close(self, *, code: int, message: bytes) -> None:
            await asyncio.Event().wait()  # nunca ACKea el close frame

    webapp._ws_ui_clients = {_SocketColgado() for _ in range(5)}  # type: ignore[misc]

    inicio = time.monotonic()
    await webapp.close_all_ws_ui_clients_on_shutdown()
    transcurrido = time.monotonic() - inicio

    assert transcurrido < 0.1 * 2, (
        f"cerrar 5 sockets colgados tardó {transcurrido:.2f}s con un timeout de "
        "0.1s: sigue siendo secuencial (5x el timeout) en vez de concurrente (~1x)"
    )


async def test_conexion_que_llega_durante_el_apagado_recibe_going_away() -> None:
    """La ventana handshake-vs-apagado: quien llega durante el apagado recibe
    ``GOING_AWAY``, no el ``POLICY_VIOLATION`` de la rotación.

    ``_handle_ws_ui`` usa ``_token_rotating`` para rechazar un handshake que
    llega mientras se está vaciando el set de clientes, pero el código de
    cierre de esa rama estaba hardcodeado a ``POLICY_VIOLATION``. Un handshake
    que corre la carrera contra un APAGADO (no una rotación) recibía igual
    "reconectá pronto" en vez de "adiós" — exactamente el comportamiento que
    P1-4 buscaba eliminar, colándose por la rama de carrera.
    """
    webapp = WebApp(router=None, session=None, auth_manager=_StubAuth())
    app = webapp.create_app()
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        # Deja el apagado "en curso" sin depender de ganar la carrera real:
        # mismo patrón que test_ws_token_rotation.py usa para simular la
        # rotación (fijar el estado, no correr la carrera de verdad).
        await webapp._close_ws_ui_clients(
            code=aiohttp.WSCloseCode.GOING_AWAY,
            message=b"Server shutting down",
            motivo="apagado del server",
            reabrir=False,
        )

        async with client.ws_connect("/ws/ui", headers={"X-Auth-Token": "token-bueno"}) as ws:
            msg = await ws.receive(timeout=5)
            assert msg.type == aiohttp.WSMsgType.CLOSE
            assert (msg.data or ws.close_code) == aiohttp.WSCloseCode.GOING_AWAY, (
                "el handshake que corrió contra el apagado recibió POLICY_VIOLATION "
                "(el código de la rotación) en vez de GOING_AWAY"
            )
    finally:
        await client.close()


# ---------------------------------------------------------------------------
# La cadena de ``app.on_shutdown`` no puede truncarse
# ---------------------------------------------------------------------------


async def test_teardown_cierra_el_database_agent_de_la_gui_al_final(monkeypatch) -> None:
    """El cierre de la DB de la GUI va DESPUÉS de ``ctx.stop()``, nunca antes.

    La GUI y el ``SupervisorAgent`` abren dos ``DatabaseAgent`` independientes
    sobre el mismo ``sky_claw_state.db``; el del supervisor recién cierra cuando
    ``ctx.stop()`` cancela ``supervisor-daemon``. SQLite solo borra ``-wal``/
    ``-shm`` al cerrar la última conexión, así que invertir el orden deja los
    sidecars en disco y hace que ``shutdown_all`` avise "checkpoint incompleto".
    """
    import sky_claw.app.gui.sky_claw_gui as gui
    from sky_claw.app.gui import _bootloader

    pasos: list[str] = []

    ctx = MagicMock()
    ctx.stop = AsyncMock(side_effect=lambda: pasos.append("ctx.stop"))

    async def _close_db_agent() -> None:
        pasos.append("close_db_agent")

    monkeypatch.setattr(gui, "close_db_agent", _close_db_agent)

    await _bootloader._teardown_runtime({"ctx": ctx})

    assert pasos == ["ctx.stop", "close_db_agent"]


async def test_cadena_de_on_shutdown_sobrevive_a_un_handler_que_falla(monkeypatch, caplog) -> None:
    """Si ``_cleanup`` lanza, ``_teardown_runtime`` nunca corre y el 8765 queda retenido.

    NiceGUI 3.12.1 despacha los ``on_shutdown`` en un bucle secuencial **sin**
    ``try/except`` (``nicegui/app/app.py``, ``App.stop``), y
    ``normalize_lifecycle_handler`` devuelve el callable tal cual. Este test
    reproduce ese bucle con los handlers reales: ``_cleanup`` (registrado por
    ``setup_app``) primero, el de ``_teardown_runtime`` después — el mismo orden
    que arma ``run_nicegui`` (``setup_app()`` y luego ``app.on_shutdown``).
    """
    import sky_claw.app.gui.sky_claw_gui as gui
    from sky_claw.app.gui import _bootloader

    handlers: list[Callable[[], Any]] = []
    fake_app = MagicMock(name="nicegui.app")
    fake_app.on_shutdown = MagicMock(side_effect=handlers.append)

    client = MagicMock(name="AgentClient")
    client.stop = AsyncMock(side_effect=RuntimeError("fallo explosivo del cliente"))

    monkeypatch.setattr(gui, "app", fake_app)
    monkeypatch.setattr(gui, "event_bus", MagicMock(name="event_bus"))
    monkeypatch.setattr(gui, "agent_client", client)
    monkeypatch.setattr(gui, "get_app_state_instance", lambda: MagicMock(name="AppState"))
    monkeypatch.setattr(gui, "get_store", lambda: MagicMock(name="ReactiveStore"))

    gui.setup_app()
    assert len(handlers) == 1, "setup_app debe registrar _cleanup como handler #1"

    pasos: list[str] = []
    runner = MagicMock()
    runner.cleanup = AsyncMock(side_effect=lambda: pasos.append("runner.cleanup"))
    ctx = MagicMock()
    ctx.stop = AsyncMock(side_effect=lambda: pasos.append("ctx.stop"))

    async def _close_db_agent() -> None:
        pasos.append("close_db_agent")

    monkeypatch.setattr(gui, "close_db_agent", _close_db_agent)

    async def _shutdown() -> None:
        await _bootloader._teardown_runtime({"aiohttp_runner": runner, "ctx": ctx})

    handlers.append(_shutdown)

    # El mismo bucle que nicegui/app/app.py: secuencial y sin try/except.
    with caplog.at_level(logging.ERROR):
        for handler in handlers:
            resultado = handler()
            if inspect.isawaitable(resultado):
                await resultado

    client.stop.assert_awaited_once()
    assert pasos == ["runner.cleanup", "ctx.stop", "close_db_agent"], (
        "_cleanup propagó y truncó la cadena: runner.cleanup() no libera el 8765 "
        f"y ctx.stop() no suelta DB ni locks. Pasos ejecutados: {pasos}"
    )


async def test_escritura_en_vuelo_aterriza_antes_de_cerrar_la_db(tmp_path, monkeypatch) -> None:
    """Una task de ``create_tracked_task`` en vuelo no puede perder su escritura.

    Antes, ``_cleanup`` cerraba y reseteaba ``_db_agent`` sin drenar nada: la
    task retomaba, ``get_db_agent()`` devolvía un agente nuevo SIN ``init_db()``
    y el ``INSERT`` moría con ``DatabaseAgent not initialized``, tragado por el
    ``except Exception`` del handler que la lanzó. Cero filas, en silencio.
    """
    import sky_claw.app.gui.sky_claw_gui as gui
    from sky_claw.app.core.database import DatabaseAgent
    from sky_claw.app.gui.task_tracking import create_tracked_task

    db_path = str(tmp_path / "escritura_en_vuelo.db")
    db = DatabaseAgent(db_path=db_path)
    await db.init_db()

    handlers: list[Callable[[], Any]] = []
    fake_app = MagicMock(name="nicegui.app")
    fake_app.on_shutdown = MagicMock(side_effect=handlers.append)

    monkeypatch.setattr(gui, "app", fake_app)
    monkeypatch.setattr(gui, "event_bus", MagicMock(name="event_bus"))
    monkeypatch.setattr(gui, "agent_client", None)
    monkeypatch.setattr(gui, "get_app_state_instance", lambda: MagicMock(name="AppState"))
    monkeypatch.setattr(gui, "get_store", lambda: MagicMock(name="ReactiveStore"))
    monkeypatch.setattr(gui, "_db_agent", db)

    gui.setup_app()

    async def _escritura_lenta() -> None:
        await asyncio.sleep(0.05)
        # Usa el getter real: si _cleanup reseteó el global, esto agarra un
        # DatabaseAgent nuevo sin init_db() y la escritura se pierde.
        await gui.get_db_agent().set_memory("clave_test", "valor_guardado", 12345.0)

    create_tracked_task(_escritura_lenta(), name="gui-test-escritura-lenta")

    await handlers[0]()  # _cleanup: drena y deja la DB abierta
    await gui.close_db_agent()  # el paso real de _teardown_runtime

    verificador = DatabaseAgent(db_path=db_path)
    await verificador.init_db()
    try:
        assert await verificador.get_memory("clave_test") == "valor_guardado"
    finally:
        await verificador.close()
