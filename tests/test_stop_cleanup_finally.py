"""P0-2 — el timeout de tasks de fondo no debe saltear el cleanup de ``stop()``.

``AppContext.stop()`` cancela las tasks de fondo, espera
``_startup_shutdown_timeout_s`` y, si alguna sigue pendiente, levanta
``TimeoutError``. Ese raise estaba ANTES de ``_close_cleanup_generation()`` y
sin ``try/finally``, así que UNA sola task que tarda en morir salteaba el
``AsyncExitStack`` entero: WAL sin checkpoint, locks distribuidos sin liberar,
broker / sesión HTTP / vault sin cerrar. El apagado quedaba a medias y el
proceso se llevaba los recursos puestos.

El timeout debe seguir propagándose —el apagado NO fue limpio y quien llama
tiene que enterarse— pero después de liberar lo que sí se puede liberar.

Alcance deliberado: el otro ``TimeoutError`` de ``stop()``, el del startup que
ignora su cancelación, NO se toca. Ese raise es load-bearing: ``_run_startup``
retiene ``_lifecycle_lock`` a través de ``await operation()``, así que seguir
hasta el bloque del lock colgaría ``stop()`` esperando un lock que el startup
atascado nunca suelta. Abortar temprano ahí es correcto, y
``test_stop_agota_presupuesto_si_startup_ignora_cancelacion``
(``test_app_context_arc01_arc03.py``) ya lo cubre.
"""

from __future__ import annotations

import argparse
import asyncio
import pathlib

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
        mode="cli",
    )


async def test_stop_corre_el_cleanup_aunque_una_task_de_fondo_ignore_la_cancelacion(
    mock_args: argparse.Namespace,
) -> None:
    """Una task de fondo obstinada no debe llevarse puesto el AsyncExitStack.

    El ``TimeoutError`` sigue propagándose, pero recién después de que el
    cleanup registrado haya corrido.
    """
    ctx = AppContext(mock_args)
    ctx._startup_shutdown_timeout_s = 0.01

    limpiezas: list[str] = []

    async def _cleanup() -> None:
        limpiezas.append("corrio")

    ctx._push_startup_cleanup(_cleanup)

    entro = asyncio.Event()
    liberar = asyncio.Event()

    async def tarea_obstinada() -> None:
        entro.set()
        try:
            await asyncio.wait_for(asyncio.Event().wait(), timeout=10.0)
        except asyncio.CancelledError:
            # Ignora la cancelación el tiempo suficiente para agotar el
            # presupuesto de stop(): el caso que disparaba el bug.
            await asyncio.wait_for(liberar.wait(), timeout=1.0)

    background = ctx._track_task(tarea_obstinada(), name="background-obstinado")
    try:
        await asyncio.wait_for(entro.wait(), timeout=1.0)

        # ``match`` distingue el TimeoutError de stop() del que levantaría un
        # wait_for de guarda si el fix introdujera un cuelgue.
        with pytest.raises(TimeoutError, match="background tasks did not stop"):
            await asyncio.wait_for(ctx.stop(), timeout=5.0)

        assert limpiezas == ["corrio"], (
            "el TimeoutError salteó _close_cleanup_generation(): el AsyncExitStack "
            "quedó sin desenrollar (WAL sin checkpoint, locks sin liberar)"
        )
    finally:
        liberar.set()
        await asyncio.gather(background, return_exceptions=True)


async def test_stop_no_ejecuta_el_cleanup_dos_veces_tras_el_timeout(
    mock_args: argparse.Namespace,
) -> None:
    """Correr el cleanup en el camino de timeout no debe duplicarlo.

    El segundo ``stop()`` —el que sí completa— tiene que encontrar la
    generación ya cerrada, no volver a ejecutar los callbacks.
    """
    ctx = AppContext(mock_args)
    ctx._startup_shutdown_timeout_s = 0.01

    ejecuciones = 0

    async def _cleanup() -> None:
        nonlocal ejecuciones
        ejecuciones += 1

    ctx._push_startup_cleanup(_cleanup)

    entro = asyncio.Event()
    liberar = asyncio.Event()

    async def tarea_obstinada() -> None:
        entro.set()
        try:
            await asyncio.wait_for(asyncio.Event().wait(), timeout=10.0)
        except asyncio.CancelledError:
            await asyncio.wait_for(liberar.wait(), timeout=1.0)

    background = ctx._track_task(tarea_obstinada(), name="background-obstinado")
    try:
        await asyncio.wait_for(entro.wait(), timeout=1.0)
        with pytest.raises(TimeoutError, match="background tasks did not stop"):
            await asyncio.wait_for(ctx.stop(), timeout=5.0)
        assert ejecuciones == 1

        liberar.set()
        await asyncio.gather(background, return_exceptions=True)
        await asyncio.wait_for(ctx.stop(), timeout=5.0)

        assert ejecuciones == 1, "el cleanup se ejecutó de nuevo en el segundo stop()"
    finally:
        liberar.set()
        await asyncio.gather(background, return_exceptions=True)
