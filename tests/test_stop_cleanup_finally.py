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

# Red de emergencia de las esperas del ciclo de vida (`wait_for` / `asyncio.wait`): ninguna ES el
# comportamiento bajo prueba (lo que tiene que vencer es `_startup_shutdown_timeout_s`), así que un tope
# corto solo hace fallar al test cuando el runner de Windows se pausa más que el tope. Espera de 1 s
# alrededor de `ctx.stop()` falló en CI (ancla: tests/test_tiempos_de_tests_invariant.py, familia 4).
_RED_DE_EMERGENCIA_S = 10.0


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
            await asyncio.wait_for(liberar.wait(), timeout=_RED_DE_EMERGENCIA_S)

    background = ctx._track_task(tarea_obstinada(), name="background-obstinado")
    try:
        await asyncio.wait_for(entro.wait(), timeout=_RED_DE_EMERGENCIA_S)

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
            await asyncio.wait_for(liberar.wait(), timeout=_RED_DE_EMERGENCIA_S)

    background = ctx._track_task(tarea_obstinada(), name="background-obstinado")
    try:
        await asyncio.wait_for(entro.wait(), timeout=_RED_DE_EMERGENCIA_S)
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


# ---------------------------------------------------------------------------
# #661 — el trabajo remoto de etapa 9 se cierra ANTES que el supervisor
# (review CodeRabbit, PR #740)
# ---------------------------------------------------------------------------
#
# El trabajo de LODs no es una task de fondo de AppContext: lo cancela
# `lod_jobs.cerrar()`. Si corría en el cleanup del AsyncExitStack, la task del
# supervisor —que SÍ es de fondo— ya había cerrado event bus, locks, journal y DB,
# y el rollback de la cancelación del pipeline corría contra recursos cerrados
# (TX PENDING, lock hasta su TTL, evento terminal perdido).


class _TrabajosDeLodsFalsos:
    def __init__(self, orden: list[str], *, colgado: bool = False) -> None:
        self._orden = orden
        self._colgado = colgado

    async def cerrar(self) -> None:
        if self._colgado:
            await asyncio.Event().wait()
        self._orden.append("lods")


async def test_stop_cierra_el_trabajo_de_lods_antes_que_las_tasks_de_fondo(
    mock_args: argparse.Namespace,
) -> None:
    ctx = AppContext(mock_args)
    orden: list[str] = []
    ctx.lod_jobs = _TrabajosDeLodsFalsos(orden)
    entro = asyncio.Event()

    async def supervisor() -> None:
        entro.set()
        try:
            await asyncio.Event().wait()
        finally:
            orden.append("supervisor")

    ctx._track_task(supervisor(), name="supervisor-daemon")
    await asyncio.wait_for(entro.wait(), timeout=_RED_DE_EMERGENCIA_S)

    await asyncio.wait_for(ctx.stop(), timeout=5.0)

    assert orden[:2] == ["lods", "supervisor"]


async def test_un_trabajo_de_lods_colgado_no_cuelga_el_stop(
    mock_args: argparse.Namespace,
) -> None:
    """Acotado por el mismo plazo de stop(): el resto del apagado corre y el timeout se informa."""
    ctx = AppContext(mock_args)
    ctx._startup_shutdown_timeout_s = 0.05
    orden: list[str] = []
    ctx.lod_jobs = _TrabajosDeLodsFalsos(orden, colgado=True)
    limpiezas: list[str] = []

    async def _cleanup() -> None:
        limpiezas.append("corrio")

    ctx._push_startup_cleanup(_cleanup)

    with pytest.raises(TimeoutError, match="etapa 9"):
        await asyncio.wait_for(ctx.stop(), timeout=5.0)
    assert limpiezas == ["corrio"]
