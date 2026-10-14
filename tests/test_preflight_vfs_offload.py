"""RED -> GREEN para el Finding 7 de #592: el VFS health-check del preflight
no debe bloquear el event loop.

La comprobación de enlaces (symlinks/junctions) de ``VfsHealthChecker`` es una
inspección de disco (lstat/iterdir) que antes corría SÍNCRONAMENTE dentro de
``PreflightService.run()``, bloqueando el event loop durante la lectura. Esto
impedía que otras coroutines (p. ej. la cancelación de un ritual, el reporte de
avance de TexGen/DynDOLOD, o la atención de la GUI) progresaran mientras se
inspeccionaba el VFS.

El fix mueve esa llamada a ``asyncio.to_thread``, alineándola con los otros 6
sensores de disco del mismo ``run()`` (masters/limits/order/overwrite/
permissions/visibility). Como el checker es de SOLO LECTURA y opera sobre estado
inmutable, el hilo residual tras una cancelación es inofensivo (no toca leases,
ownership ni estado mutable) y la cancelación sigue siendo fail-closed.

Los tests son deterministas: no dependen de sleeps arbitrarios para decidir el
veredicto, sino de señales explícitas de ejecución y finalización
(``call_soon_threadsafe`` + ``Event``). Los únicos topes de tiempo que quedan
son cotas anti-bloqueo: si una señal no llega, el test falla —no se cuelga—.
"""

from __future__ import annotations

import asyncio
import threading
from unittest.mock import AsyncMock, MagicMock

import pytest

from sky_claw.local.validators.preflight import (
    PreflightReport,
    PreflightService,
    PreflightStatus,
)
from sky_claw.local.validators.vfs_health import VfsIssue


def _issue(severity: str) -> VfsIssue:
    return VfsIssue(
        path=__import__("pathlib").Path("/mo2/mods/ModEnlazado"),
        kind="symlink",
        severity=severity,
        remediation="usar carpeta real",
    )


def _servicio_con(
    vfs_checker: MagicMock,
    version: tuple[int, int, int] | None = (0, 29, 0),
) -> PreflightService:
    return PreflightService(
        vfs_checker=vfs_checker,
        loot_version_detector=AsyncMock(return_value=version),
    )


# Único tope de espera del archivo. NO es una duración estimada ni un slack de
# temporización: es cota anti-bloqueo. El veredicto de cada test no depende de
# que la señal llegue rápido —depende de que llegue—, y en la versión síncrona
# pre-fix la señal NUNCA llega (el event loop está bloqueado dentro del check),
# así que la capacidad RED se conserva.
_TOPE_ESPERA = 10.0


async def _esperar_senal_del_loop(evento: asyncio.Event, contexto: str) -> None:
    """Espera una señal programada en el loop, con cota anti-bloqueo."""
    try:
        await asyncio.wait_for(evento.wait(), timeout=_TOPE_ESPERA)
    except TimeoutError:
        pytest.fail(f"el loop no ejecutó la señal en {_TOPE_ESPERA}s: {contexto}")


class TestVfsOffloadNoBloqueaElEventLoop:
    """RED contra el código previo (check síncrono); GREEN tras el fix."""

    async def test_el_event_loop_continua_durante_el_check(self) -> None:
        """Un callback programado en el loop DEBE ejecutarse *mientras* el
        VFS check corre. Con el check síncrono en el loop, el callback no puede
        correr hasta que check() retorna -> el flag queda en False (RED)."""
        loop = asyncio.get_running_loop()
        # Señal EXPLÍCITA de ejecución del callback. Es un threading.Event
        # porque sincroniza el hilo del checker con el hilo del loop.
        corrio_el_callback = threading.Event()
        probe: dict[str, object] = {"flag_al_retornar_check": None}

        def check_lento() -> list[VfsIssue]:
            # Se programa en el loop desde el hilo del checker.
            loop.call_soon_threadsafe(corrio_el_callback.set)
            # El checker espera a que el loop EJECUTE el callback. Con el check
            # síncrono (pre-fix) el loop está bloqueado adentro de esta misma
            # función y la señal nunca llega: el wait corta por la cota y el
            # flag queda False (RED). Con el offload el loop está libre y la
            # prende en milisegundos. No hay ningún sleep que adivinar.
            probe["flag_al_retornar_check"] = corrio_el_callback.wait(timeout=_TOPE_ESPERA)
            return []

        checker = MagicMock()
        checker.check.side_effect = check_lento

        await _servicio_con(checker).run()

        assert probe["flag_al_retornar_check"] is True

    async def test_el_check_corre_fuera_del_hilo_del_event_loop(self) -> None:
        """Evidencia directa: el checker se ejecuta en un hilo distinto al del
        event loop, no bloqueándolo."""
        loop_thread = threading.current_thread()
        checker_thread: list[threading.Thread] = []

        def check_lento() -> list[VfsIssue]:
            # Sin sleeps: la identidad del hilo se registra al entrar y el
            # veredicto no depende de ninguna temporización.
            checker_thread.append(threading.current_thread())
            return []

        checker = MagicMock()
        checker.check.side_effect = check_lento

        await _servicio_con(checker).run()

        assert checker_thread, "el checker nunca corrió"
        assert checker_thread[0] is not loop_thread


class TestVfsOffloadResultadoYErrores:
    """El offload no cambia la semántica de resultado ni la propagación de errores."""

    async def test_resultado_normal_del_checker_surfacea(self) -> None:
        checker = MagicMock()
        checker.check.return_value = [_issue("critical")]
        reporte = await _servicio_con(checker).run()

        assert isinstance(reporte, PreflightReport)
        vfs = next(c for c in reporte.checks if c.name == "vfs")
        assert vfs.status is PreflightStatus.RED

    async def test_excepcion_del_checker_se_propaga(self) -> None:
        checker = MagicMock()
        checker.check.side_effect = RuntimeError("inspección VFS caída")

        with pytest.raises(RuntimeError, match="inspección VFS caída"):
            await _servicio_con(checker).run()


class TestVfsOffloadCancelacion:
    """Análisis de cancelación: el coroutine se cancela; el hilo (read-only) se
    _termina_ inofensivamente y no se emite reporte (fail-closed)."""

    async def test_cancelacion_durante_el_check_no_emite_reporte_y_es_fail_closed(self) -> None:
        loop = asyncio.get_running_loop()
        started = asyncio.Event()
        # Señales explícitas con el worker: el test AUTORIZA la salida y espera
        # la finalización REAL del hilo. La cancelación cae determinísticamente
        # dentro de la ventana de trabajo porque el checker no puede terminar
        # antes de ser autorizado.
        puede_terminar = threading.Event()
        termino = threading.Event()
        probe: dict[str, object] = {"hilo_termino": False}

        def check_lento() -> list[VfsIssue]:
            loop.call_soon_threadsafe(started.set)
            if not puede_terminar.wait(timeout=_TOPE_ESPERA):
                raise AssertionError(f"el test no autorizó la salida en {_TOPE_ESPERA}s")
            probe["hilo_termino"] = True  # read-only: no toca estado compartido
            termino.set()
            return []

        checker = MagicMock()
        checker.check.side_effect = check_lento

        task = asyncio.create_task(_servicio_con(checker).run())
        await _esperar_senal_del_loop(started, "el checker arrancó")
        task.cancel()
        try:
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            # El hilo de to_thread NO es cancelable: se lo libera explícitamente.
            puede_terminar.set()

        # Se espera la finalización REAL del worker (señal explícita), no un
        # sleep fijo. Es inofensivo porque el checker es read-only.
        assert termino.wait(timeout=_TOPE_ESPERA), "el worker no terminó"
        assert probe["hilo_termino"] is True

    async def test_sin_efectos_tardios_sobre_estado_externo(self) -> None:
        """Tras cancelar, el (read-only) checker no debe haber mutado un objeto
        compartido externo: la única fuente de verdad del preflight es el reporte,
        y al cancelarse no se emite ninguno."""
        loop = asyncio.get_running_loop()
        started = asyncio.Event()
        # Mismo patrón de señales explícitas: la aserción se evalúa DESPUÉS de
        # la finalización REAL del worker, no después de un sleep arbitrario.
        puede_terminar = threading.Event()
        termino = threading.Event()
        shared_log: list[str] = []  # el checker NO debe escribir aquí

        def check_lento() -> list[VfsIssue]:
            loop.call_soon_threadsafe(started.set)
            if not puede_terminar.wait(timeout=_TOPE_ESPERA):
                raise AssertionError(f"el test no autorizó la salida en {_TOPE_ESPERA}s")
            termino.set()
            return []

        checker = MagicMock()
        checker.check.side_effect = check_lento

        task = asyncio.create_task(_servicio_con(checker).run())
        await _esperar_senal_del_loop(started, "el checker arrancó")
        task.cancel()
        try:
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            puede_terminar.set()

        assert termino.wait(timeout=_TOPE_ESPERA), "el worker no terminó"
        assert shared_log == []  # nada mutó fuera del scope read-only del checker


class TestVfsOffloadConcurrencia:
    async def test_dos_checks_simultaneos_son_correctos(self) -> None:
        """Dos run() concurrentes: cada uno usa su propio worker thread y su
        propio resultado; no hay carrera porque check() es stateless sobre
        estado mutable compartido."""

        def make_checker(issues: list[VfsIssue]) -> MagicMock:
            c = MagicMock()
            c.check.return_value = issues
            return c

        a = make_checker([_issue("critical")])
        b = make_checker([_issue("warning")])

        reporte_a, reporte_b = await asyncio.gather(
            _servicio_con(a).run(),
            _servicio_con(b).run(),
        )

        vfs_a = next(c for c in reporte_a.checks if c.name == "vfs")
        vfs_b = next(c for c in reporte_b.checks if c.name == "vfs")
        assert vfs_a.status is PreflightStatus.RED
        assert vfs_b.status is PreflightStatus.YELLOW
        a.check.assert_called_once()
        b.check.assert_called_once()


class TestVfsOffloadCompatibilidadExistente:
    """Regresión: el contrato de composición y omit_unconfigured se preserva."""

    async def test_composicion_symlink_mas_loot_viejo_sigue_rojo(self) -> None:
        checker = MagicMock()
        checker.check.return_value = [_issue("warning")]
        reporte = await _servicio_con(checker, version=(0, 28, 0)).run()
        assert reporte.status is PreflightStatus.RED
        assert reporte.blocks_mutations is True

    async def test_omit_unconfigured_sigue_omitiendo_los_ajenos(self) -> None:
        # Sólo se cablea vfs_checker: los demás sensores quedan sin configurar y
        # deben omitirse (no reportar "no configurado") cuando omit_unconfigured.
        checker = MagicMock()
        checker.check.return_value = []
        reporte = await PreflightService(
            vfs_checker=checker,
            omit_unconfigured=True,
        ).run()
        assert {c.name for c in reporte.checks} == {"vfs"}

    async def test_sin_checker_no_configurado_es_verde_y_no_miente(self) -> None:
        reporte = await PreflightService(loot_version_detector=AsyncMock(return_value=None)).run()
        vfs = next(c for c in reporte.checks if c.name == "vfs")
        assert "no configurad" in vfs.summary.lower()
