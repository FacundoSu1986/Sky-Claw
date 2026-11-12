"""Liveness de los clientes WS con la implementación asyncio de websockets 16.

``requirements.lock`` fija ``websockets==16.0``. En esa versión
``ClientConnection`` ya no expone ``.open`` (solo ``state``): consultarlo lanza
``AttributeError`` y, como el handler de error de ``TelegramDaemon`` volvía a
consultarlo, la excepción escapaba del handler. Los tests previos usaban
``MagicMock`` —que inventa cualquier atributo— y por eso nunca lo vieron.

Acá el cliente es una conexión REAL contra un servidor loopback, sin mocks que
inventen ``.open``. El ancla AST enumera todos los accesos ``.open`` del árbol
para que un tercer hermano no vuelva a aparecer.
"""

from __future__ import annotations

import ast
import asyncio
import json
import pathlib
import sys
import types
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import websockets

# ws_daemon.py exige ``ast_guardian`` al construir TelegramDaemon; en el
# checkout estándar no existe. Mismo stub que ``test_ws_daemon_dispatch.py``.
_ast_module = sys.modules.get("ast_guardian") or types.ModuleType("ast_guardian")
if not hasattr(_ast_module, "ASTGuardian"):
    _ast_module.ASTGuardian = MagicMock  # type: ignore[attr-defined]
sys.modules["ast_guardian"] = _ast_module

from sky_claw.app.comms import frontend_bridge as fb  # noqa: E402
from sky_claw.app.comms._transport import enviar_si_conectado  # noqa: E402
from sky_claw.app.comms.ws_daemon import TelegramDaemon  # noqa: E402

_RAIZ = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture
async def gateway() -> AsyncIterator[tuple[str, asyncio.Queue[dict[str, Any]]]]:
    """Servidor WS loopback real que encola cada mensaje JSON recibido."""
    recibidos: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def handler(conn: Any) -> None:
        async for raw in conn:
            await recibidos.put(json.loads(raw))

    async with websockets.serve(handler, "127.0.0.1", 0) as servidor:
        puerto = servidor.sockets[0].getsockname()[1]
        yield f"ws://127.0.0.1:{puerto}", recibidos


@pytest.fixture
async def cliente(
    gateway: tuple[str, asyncio.Queue[dict[str, Any]]],
) -> AsyncIterator[Any]:
    """Conexión cliente real (``ClientConnection`` de websockets 16)."""
    url, _ = gateway
    async with websockets.connect(url) as conexion:
        yield conexion


def _daemon(ws: Any, *, audit_ok: bool = True) -> TelegramDaemon:
    daemon = TelegramDaemon(
        router=MagicMock(),
        session=MagicMock(),
        gateway_url="ws://127.0.0.1:0",
        ui_broadcast=None,
        token_dir=None,
    )
    daemon.guardian = MagicMock()
    daemon.guardian.execute_audit = AsyncMock(return_value=audit_ok)
    daemon.ws = ws
    return daemon


def _comando(texto: str = "hola") -> dict[str, Any]:
    return {"id": "m1", "type": "command", "payload": {"text": texto}, "metadata": {"user_id": "7"}}


async def _siguiente(cola: asyncio.Queue[dict[str, Any]]) -> dict[str, Any]:
    return await asyncio.wait_for(cola.get(), timeout=5)


# ── TelegramDaemon ──────────────────────────────────────────────────────────


async def test_daemon_la_respuesta_del_agente_llega_al_gateway(
    gateway: tuple[str, asyncio.Queue[dict[str, Any]]], cliente: Any
) -> None:
    # Arrange
    _, recibidos = gateway
    daemon = _daemon(cliente)
    daemon.router.chat = AsyncMock(return_value="respuesta del agente")
    # Act
    await daemon._inject_to_router(_comando())
    # Assert
    msg = await _siguiente(recibidos)
    assert msg["type"] == "response"
    assert msg["payload"]["text"] == "respuesta del agente"
    assert msg["metadata"]["reply_to"] == "m1"


async def test_daemon_la_telemetria_de_progreso_llega_al_gateway(
    gateway: tuple[str, asyncio.Queue[dict[str, Any]]], cliente: Any
) -> None:
    # Arrange
    _, recibidos = gateway
    daemon = _daemon(cliente)

    async def chat(*_args: Any, progress_callback: Any, **_kw: Any) -> str:
        await progress_callback("trabajando", 40)
        return "listo"

    daemon.router.chat = chat
    # Act
    await daemon._inject_to_router(_comando())
    # Assert
    telemetria = await _siguiente(recibidos)
    assert (telemetria["type"], telemetria["status"], telemetria["progress"]) == ("telemetry", "trabajando", 40)
    assert (await _siguiente(recibidos))["type"] == "response"


async def test_daemon_el_payload_bloqueado_avisa_al_gateway(
    gateway: tuple[str, asyncio.Queue[dict[str, Any]]], cliente: Any
) -> None:
    # Arrange: antes, ``getattr(ws, "open", False)`` daba False y el aviso se perdía.
    _, recibidos = gateway
    daemon = _daemon(cliente, audit_ok=False)
    daemon.router.chat = AsyncMock()
    # Act
    await daemon._inject_to_router(_comando("payload malicioso"))
    # Assert
    msg = await _siguiente(recibidos)
    assert msg["type"] == "error"
    daemon.router.chat.assert_not_awaited()


async def test_daemon_el_error_del_router_avisa_al_gateway(
    gateway: tuple[str, asyncio.Queue[dict[str, Any]]], cliente: Any
) -> None:
    # Arrange
    _, recibidos = gateway
    daemon = _daemon(cliente)
    daemon.router.chat = AsyncMock(side_effect=RuntimeError("falla del proveedor"))
    # Act
    await daemon._inject_to_router(_comando())
    # Assert
    msg = await _siguiente(recibidos)
    assert msg["type"] == "error"
    assert "falla del proveedor" in msg["payload"]["text"]


async def test_daemon_tolera_el_cierre_concurrente_del_socket(cliente: Any) -> None:
    # Arrange: el gateway cierra mientras el agente procesaba.
    daemon = _daemon(cliente)

    async def chat(*_args: Any, progress_callback: Any, **_kw: Any) -> str:
        await cliente.close()
        await progress_callback("trabajando", 10)
        return "respuesta tardía"

    daemon.router.chat = chat
    # Act / Assert: ni el progreso, ni la respuesta, ni el handler de error propagan.
    await daemon._inject_to_router(_comando())


async def test_daemon_el_error_del_router_con_el_socket_ya_cerrado_no_propaga(cliente: Any) -> None:
    # Arrange: la combinación que el handler de error debe tolerar — el router
    # falla Y el gateway ya cerró, así que avisar el error también encuentra el socket cerrado.
    daemon = _daemon(cliente)

    async def chat(*_args: Any, **_kw: Any) -> str:
        await cliente.close()
        raise RuntimeError("falla del proveedor")

    daemon.router.chat = chat
    # Act / Assert: ni el error original ni el aviso fallido escapan del handler.
    await daemon._inject_to_router(_comando())


# ── Contrato del helper aislado ─────────────────────────────────────────────


class _SocketQueFalla:
    """Socket mínimo cuyo ``send`` lanza la excepción dada (no inventa ``.open``)."""

    def __init__(self, error: BaseException) -> None:
        self._error = error

    async def send(self, _raw: str) -> None:
        raise self._error


async def test_enviar_si_conectado_devuelve_true_si_envio_y_false_si_no_pudo(
    gateway: tuple[str, asyncio.Queue[dict[str, Any]]], cliente: Any
) -> None:
    # Arrange
    _, recibidos = gateway
    # Act / Assert: abierto -> True y el frame llega; ausente -> False; cerrado -> False.
    assert await enviar_si_conectado(cliente, json.dumps({"k": 1})) is True
    assert await _siguiente(recibidos) == {"k": 1}
    assert await enviar_si_conectado(None, "x") is False
    await cliente.close()
    assert await enviar_si_conectado(cliente, "x") is False


async def test_enviar_si_conectado_no_traga_la_cancelacion_ni_otros_errores_de_transporte() -> None:
    # Arrange / Act / Assert: solo ConnectionClosed se tolera; el resto propaga.
    # (CancelledError es BaseException en 3.8+: ningún ``except Exception`` del llamador la atrapa.)
    with pytest.raises(asyncio.CancelledError):
        await enviar_si_conectado(_SocketQueFalla(asyncio.CancelledError()), "x")
    with pytest.raises(RuntimeError):
        await enviar_si_conectado(_SocketQueFalla(RuntimeError("transporte")), "x")


# ── FrontendBridge (hermano: misma API, otro archivo) ───────────────────────


def _bridge(ws: Any) -> fb.FrontendBridge:
    bridge = fb.FrontendBridge(
        router=MagicMock(),
        session=MagicMock(),
        config=MagicMock(),
        app_context=MagicMock(),
        keyring_client=MagicMock(),
    )
    bridge.ws = ws
    return bridge


async def test_bridge_send_entrega_el_payload_por_un_cliente_real(
    gateway: tuple[str, asyncio.Queue[dict[str, Any]]], cliente: Any
) -> None:
    # Arrange
    _, recibidos = gateway
    bridge = _bridge(cliente)
    # Act
    await bridge._send({"type": "PING", "id": "x"})
    # Assert
    assert await _siguiente(recibidos) == {"type": "PING", "id": "x"}


async def test_bridge_send_tolera_un_socket_ya_cerrado(cliente: Any) -> None:
    # Arrange
    bridge = _bridge(cliente)
    await cliente.close()
    # Act / Assert: no lanza (antes: AttributeError por ``.open``).
    await bridge._send({"type": "PING"})


async def test_bridge_send_sin_socket_no_lanza() -> None:
    await _bridge(None)._send({"type": "PING"})


# ── Ancla: la familia se enumera por AST, no se muestrea ────────────────────


def _accesos_a_open() -> set[tuple[str, str]]:
    """``(archivo, expresión)`` de todo ``x.open`` sin llamar y ``getattr(x, "open")``."""
    hallados: set[tuple[str, str]] = set()
    for base in ("sky_claw", "scripts"):
        for ruta in sorted((_RAIZ / base).rglob("*.py")):
            arbol = ast.parse(ruta.read_text(encoding="utf-8"))
            llamadas = {id(n.func) for n in ast.walk(arbol) if isinstance(n, ast.Call)}
            rel = ruta.relative_to(_RAIZ).as_posix()
            for nodo in ast.walk(arbol):
                if isinstance(nodo, ast.Attribute) and nodo.attr == "open" and id(nodo) not in llamadas:
                    hallados.add((rel, ast.unparse(nodo)))
                if (
                    isinstance(nodo, ast.Call)
                    and isinstance(nodo.func, ast.Name)
                    and nodo.func.id in {"getattr", "hasattr"}
                    and len(nodo.args) >= 2
                    and isinstance(nodo.args[1], ast.Constant)
                    and nodo.args[1].value == "open"
                ):
                    hallados.add((rel, ast.unparse(nodo)))
    return hallados


def test_ningun_cliente_ws_consulta_el_atributo_open() -> None:
    """``.open`` no es contrato de liveness en websockets 16: enviar y tolerar ConnectionClosed.

    Conjunto congelado por igualdad literal. Un acceso nuevo rompe el test: la
    salida es ``enviar_si_conectado`` (``_transport.py``), no agregarlo acá.
    ``.closed`` queda fuera a propósito: hoy solo lo leen sesiones aiohttp y
    archivos, no sockets websockets.
    """
    assert _accesos_a_open() == set()
