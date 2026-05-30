"""QA-1 — TelegramDaemon dispatch task tracking (T1-01, T1-07).

Verifica que:
(a) ``_listen_loop`` mantiene una referencia fuerte a las tasks de dispatch
    en ``_pending_dispatch`` para que el GC no las recolecte mientras
    estan pendientes.
(b) ``_on_dispatch_done`` libera la referencia y loggea excepciones del
    handler con ``exc_info``.
(c) ``stop()`` cancela y drena las tasks pendientes antes de cerrar el WS.
(d) ``asyncio.CancelledError`` en el loop principal propaga (no se loggea
    como "Error procesando flujo").
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import types
from unittest.mock import AsyncMock, MagicMock

import pytest

# ws_daemon.py importa ast_guardian via sys.path hack a un agent skills dir
# que no existe en el checkout estandar. Stub el modulo y la clase
# ASTGuardian para poder importar TelegramDaemon. Otro test
# (test_ws_auth_close_code.py) ya hace ``setdefault`` con un ModuleType vacio
# sin ASTGuardian — si ese test corre primero, nuestro ``setdefault`` no
# aplica. Forzamos la inyeccion del atributo en el modulo existente.
_ast_module = sys.modules.get("ast_guardian") or types.ModuleType("ast_guardian")
if not hasattr(_ast_module, "ASTGuardian"):
    _ast_module.ASTGuardian = MagicMock  # type: ignore[attr-defined]
sys.modules["ast_guardian"] = _ast_module

from sky_claw.antigravity.comms.ws_daemon import TelegramDaemon  # noqa: E402


def _make_daemon() -> TelegramDaemon:
    """Crea un TelegramDaemon con dependencias mockeadas (sin red real)."""
    router = MagicMock()
    session = MagicMock()
    daemon = TelegramDaemon(
        router=router,
        session=session,
        gateway_url="ws://localhost:0",
        ui_broadcast=None,
        token_dir=None,
    )
    # _inject_to_router se mockea por test.
    daemon.ws = MagicMock()
    daemon.ws.send = AsyncMock()
    daemon.ws.close = AsyncMock()
    return daemon


@pytest.mark.asyncio
async def test_pending_dispatch_set_initialized() -> None:
    """El set _pending_dispatch existe y es vacio al inicio."""
    daemon = _make_daemon()
    assert daemon._pending_dispatch == set()


@pytest.mark.asyncio
async def test_on_dispatch_done_discards_task_and_logs_exception(caplog) -> None:
    """_on_dispatch_done remueve el task del set y loggea la excepcion."""
    daemon = _make_daemon()

    async def boom() -> None:
        raise ValueError("dispatch handler crashed")

    task = asyncio.create_task(boom())
    daemon._pending_dispatch.add(task)
    # Esperar a que termine.
    with caplog.at_level(logging.ERROR, logger="SkyClaw.TelegramDaemon"):
        with pytest.raises(ValueError):
            await task
        # Invocar manualmente el callback.
        daemon._on_dispatch_done(task)

    assert task not in daemon._pending_dispatch
    assert any(
        "Error no manejado en _inject_to_router" in rec.message and "dispatch handler crashed" in rec.message
        for rec in caplog.records
    )


@pytest.mark.asyncio
async def test_on_dispatch_done_handles_cancelled_silently(caplog) -> None:
    """Si la task fue cancelada, no se loggea como error."""
    daemon = _make_daemon()

    async def slow() -> None:
        await asyncio.sleep(10)

    task = asyncio.create_task(slow())
    daemon._pending_dispatch.add(task)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    with caplog.at_level(logging.ERROR, logger="SkyClaw.TelegramDaemon"):
        daemon._on_dispatch_done(task)

    assert task not in daemon._pending_dispatch
    # No debe haber log de error por cancelacion.
    assert not any("Error no manejado" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_stop_cancels_and_drains_pending_dispatch() -> None:
    """stop() cancela todas las tasks pendientes y las awaitea antes de cerrar."""
    daemon = _make_daemon()

    started: list[int] = []
    finished: list[int] = []

    async def long_handler(idx: int) -> None:
        started.append(idx)
        try:
            await asyncio.sleep(10)
        finally:
            finished.append(idx)

    for i in range(5):
        t = asyncio.create_task(long_handler(i))
        daemon._pending_dispatch.add(t)
        t.add_done_callback(daemon._on_dispatch_done)

    # Dar oportunidad a las tasks de empezar.
    await asyncio.sleep(0)
    assert len(started) == 5

    # stop() debe cancelar las 5 tasks y esperar su finalizacion.
    await daemon.stop()

    assert len(finished) == 5
    assert daemon._pending_dispatch == set()
    daemon.ws.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_listen_loop_propagates_cancelled_error() -> None:
    """El loop principal debe re-lanzar CancelledError, no tragarlo como Exception."""
    daemon = _make_daemon()

    # ws.aiter devuelve un mensaje de comando y luego es cancelado.
    sent: list[dict] = []

    async def fake_iter():
        # Un solo mensaje, luego cancellation.
        yield json.dumps({"id": "m1", "type": "command", "payload": {"text": "noop"}})
        # En la 2da iteracion provocar cancel del task externamente.
        await asyncio.sleep(10)
        # Nunca llega aqui.
        yield ""

    daemon.ws = MagicMock()
    daemon.ws.__aiter__ = lambda self: fake_iter()
    daemon.ws.send = AsyncMock(side_effect=lambda raw: sent.append(json.loads(raw)))

    async def fake_inject(data: dict) -> None:
        await asyncio.sleep(0)

    daemon._inject_to_router = fake_inject  # type: ignore[assignment]

    task = asyncio.create_task(daemon._listen_loop())
    # Dejar correr para que procese el primer mensaje y empiece a esperar el 2do.
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # ACK del primer mensaje fue enviado.
    assert any(m.get("type") == "ack" for m in sent)
