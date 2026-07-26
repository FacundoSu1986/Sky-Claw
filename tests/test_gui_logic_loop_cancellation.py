"""P0-1 — el loop de lógica de la GUI debe ser cancelable de verdad.

Historia (post-mortem WinError 10048): ``_gui_logic_loop`` consumía la cola con
``await asyncio.to_thread(ctx.logic_queue.get)`` sobre un ``queue.Queue``
sincrónico. ``queue.Queue.get()`` SIN timeout bloquea un worker NO-daemon del
ThreadPoolExecutor por defecto; cancelar la task despierta la corrutina pero NO
interrumpe el hilo. Al cerrar, ``loop.shutdown_default_executor()`` hace
``thread.join()`` sin timeout → el proceso nunca termina y retiene 8080/8765.

El fix es consumir una ``asyncio.Queue`` con ``await get()`` (cancelable de
forma nativa, sin hilos). Estos tests fijan ese contrato:
- la cola es ``asyncio.Queue`` (no un ``queue.Queue`` que obligue a un hilo);
- el loop consume el ítem vía ``await get()`` y lo despacha;
- cancelar la task la deja realmente ``cancelled()`` y no deja hilos vivos.
"""

from __future__ import annotations

import argparse
import asyncio
import pathlib
import threading

import pytest

from sky_claw.app_context import AppContext


@pytest.fixture
def mock_args(tmp_path: pathlib.Path) -> argparse.Namespace:
    """Namespace mínimo tipo argparse para construir AppContext."""
    return argparse.Namespace(
        db_path=str(tmp_path / "test.db"),
        mo2_root=tmp_path / "MO2",
        staging_dir=tmp_path / "staging",
        provider="ollama",
        operator_chat_id=None,
        loot_exe=None,
        install_dir=None,
        mode="gui",
    )


def test_logic_queue_es_asyncio_queue(mock_args: argparse.Namespace) -> None:
    """La logic_queue debe ser asyncio.Queue: consumible con ``await get()``
    en el loop de eventos, sin bloquear un worker del pool por defecto."""
    ctx = AppContext(mock_args)
    assert isinstance(ctx.logic_queue, asyncio.Queue), (
        "logic_queue debe ser asyncio.Queue (cancelable), no queue.Queue "
        "(obliga a to_thread + hilo bloqueado sin salida)"
    )


async def test_logic_loop_consume_via_await_y_es_cancelable(monkeypatch) -> None:
    """El loop consume un ítem de chat con ``await get()`` y lo despacha; al
    cancelar, la task queda ``cancelled()`` sin dejar hilos del pool vivos."""
    from unittest.mock import MagicMock

    from sky_claw.app.gui import _bootloader

    ctx = MagicMock()
    ctx.router = MagicMock()  # evaluado como verdadero → el loop despacha, no avisa "no inicializado"
    ctx.logic_queue = asyncio.Queue()

    despachos: list[str] = []

    async def _fake_dispatch(_ctx: object, text: str) -> None:
        despachos.append(text)

    monkeypatch.setattr(_bootloader, "_dispatch_chat_to_router", _fake_dispatch)

    hilos_antes = set(threading.enumerate())
    task = asyncio.create_task(_bootloader._gui_logic_loop(ctx))
    ctx.logic_queue.put_nowait(("chat", "hola"))

    # El loop debe drenar el ítem vía await get() (no vía to_thread sobre una
    # cola que jamás despertaría). Con el bug, `item` sería un objeto corrutina
    # y el ítem nunca se despacharía.
    for _ in range(100):
        if despachos:
            break
        await asyncio.sleep(0.01)
    assert despachos == ["hola"], "el loop no consumió el ítem de chat vía await get()"

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled(), "la task debe quedar cancelled(), no terminar con un break silencioso"

    # Ningún worker del ThreadPoolExecutor debe quedar vivo tras la cancelación.
    await asyncio.sleep(0.05)
    hilos_nuevos = [t for t in set(threading.enumerate()) - hilos_antes if t.is_alive()]
    assert not hilos_nuevos, f"cancelar dejó hilos vivos (worker bloqueado): {hilos_nuevos}"
