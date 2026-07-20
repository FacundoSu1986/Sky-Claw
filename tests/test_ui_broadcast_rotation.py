"""F6 + F2 (auditoría Zero-Trust 2026-07-18) — hardening de UIBroadcastServer.

F6a: al rotar el token, los sockets ya conectados deben cerrarse (paridad con
     ``WebApp.close_all_ws_ui_clients``); hoy sobreviven con el token viejo.
F6b: ``broadcast`` itera ``_clients`` con un ``await`` interno mientras el
     handler muta el set → ``RuntimeError: Set changed size``.
F2:  ``ws_daemon`` importaba ``ast_guardian`` a nivel de módulo desde una ruta
     inexistente (sys.path hack) — cualquier import del módulo reventaba salvo
     stub. UIBroadcastServer no lo usa; solo TelegramDaemon.
"""

from __future__ import annotations

import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# F2: tras el fix, ws_daemon ya NO importa ast_guardian a nivel de módulo, así
# que UIBroadcastServer se importa sin stub. (TelegramDaemon lo importa lazy.)
from sky_claw.antigravity.comms.ws_daemon import UIBroadcastServer

_ROTATION_CLOSE_CODE = 1008  # POLICY_VIOLATION — NO 4001 (lockout de auth)


def _make_server() -> UIBroadcastServer:
    with patch("sky_claw.antigravity.comms.ws_daemon.AuthTokenManager"):
        return UIBroadcastServer()


class _FakeWS:
    """WebSocket mínimo: registra sends/closes y opcionalmente muta el set en send."""

    def __init__(self, *, on_send=None) -> None:
        self.sent: list[str] = []
        self.closed_with: tuple | None = None
        self.remote_address = ("127.0.0.1", 5555)
        self._on_send = on_send

    async def send(self, payload: str) -> None:
        if self._on_send is not None:
            self._on_send()
        self.sent.append(payload)

    async def close(self, code=None, reason: str = "") -> None:
        self.closed_with = (code, reason)


# ── F6a: rotación cierra sockets vivos ──────────────────────────────────────


async def test_start_registra_callback_de_rotacion() -> None:
    server = _make_server()
    server._auth.start_rotation = AsyncMock()
    server._auth.register_rotation_callback = MagicMock()
    with patch("sky_claw.antigravity.comms.ws_daemon.websockets.serve", new=AsyncMock()):
        await server.start()
    server._auth.register_rotation_callback.assert_called_once_with(server._close_all_clients)


async def test_rotacion_cierra_clientes_con_1008_y_vacia_el_set() -> None:
    server = _make_server()
    ws_a, ws_b = _FakeWS(), _FakeWS()
    server._clients = {ws_a, ws_b}

    await server._close_all_clients()

    assert server._clients == set()
    for ws in (ws_a, ws_b):
        assert ws.closed_with is not None
        assert ws.closed_with[0] == _ROTATION_CLOSE_CODE  # 1008, no 4001


async def test_handshake_durante_rotacion_es_rechazado() -> None:
    """Un socket que llega con la rotación en curso se rechaza con 1008 y no queda en el set."""
    server = _make_server()
    server._auth.validate.return_value = True
    server._token_rotating = True

    ws = _FakeWS()
    ws.request_headers = {"X-Auth-Token": "token-valido"}

    await server._handler(ws)

    assert ws.closed_with is not None
    assert ws.closed_with[0] == _ROTATION_CLOSE_CODE
    assert ws not in server._clients


# ── F6b: broadcast robusto ante mutación concurrente del set ────────────────


async def test_broadcast_no_revienta_si_send_muta_clients() -> None:
    """El handler puede add/discard mientras broadcast itera: no debe lanzar
    RuntimeError: Set changed size during iteration."""
    server = _make_server()
    intruso = _FakeWS()

    def _mutar() -> None:
        # Simula un handler concurrente registrando/soltando un cliente.
        server._clients.add(intruso)
        server._clients.discard(intruso)

    ws = _FakeWS(on_send=_mutar)
    server._clients = {ws}

    await server.broadcast({"type": "AGENT_RESULT", "data": "ok"})

    assert ws.sent  # el mensaje se envió sin explotar


# ── F2: import fail-closed del guardrail AST ────────────────────────────────


def test_ui_broadcast_no_depende_de_ast_guardian() -> None:
    """UIBroadcastServer se construye aunque ast_guardian no esté disponible."""
    saved = sys.modules.pop("ast_guardian", None)
    try:
        sys.modules["ast_guardian"] = None  # type: ignore[assignment]  # fuerza ImportError si se importara
        server = _make_server()
        assert isinstance(server, UIBroadcastServer)
    finally:
        if saved is not None:
            sys.modules["ast_guardian"] = saved
        else:
            sys.modules.pop("ast_guardian", None)


def test_telegram_daemon_sin_ast_guardian_falla_ruidoso() -> None:
    """Sin el guardrail AST, TelegramDaemon aborta con RuntimeError claro (fail-closed)."""
    from sky_claw.antigravity.comms.ws_daemon import TelegramDaemon

    saved = sys.modules.pop("ast_guardian", None)
    try:
        sys.modules["ast_guardian"] = None  # type: ignore[assignment]  # import ast_guardian → ImportError
        with pytest.raises(RuntimeError, match="ast_guardian"):
            TelegramDaemon(router=MagicMock(), session=MagicMock(), gateway_url="ws://localhost:0")
    finally:
        if saved is not None:
            sys.modules["ast_guardian"] = saved
        else:
            sys.modules.pop("ast_guardian", None)


def test_telegram_daemon_con_ast_guardian_construye() -> None:
    """Con el guardrail presente, TelegramDaemon se construye y expone el guardian."""
    from sky_claw.antigravity.comms.ws_daemon import TelegramDaemon

    stub = types.ModuleType("ast_guardian")
    stub.ASTGuardian = MagicMock  # type: ignore[attr-defined]
    saved = sys.modules.get("ast_guardian")
    sys.modules["ast_guardian"] = stub
    try:
        daemon = TelegramDaemon(router=MagicMock(), session=MagicMock(), gateway_url="ws://localhost:0")
        assert daemon.guardian is not None
    finally:
        if saved is not None:
            sys.modules["ast_guardian"] = saved
        else:
            sys.modules.pop("ast_guardian", None)
