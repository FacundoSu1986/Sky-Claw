"""H-11: la etapa 9 dura horas y, hasta ahora, nada distinguía «generando» de «colgado».

El latido del runner solo escribía una línea al logger. Estas pruebas anclan el contrato nuevo, en tres niveles:

1. el runner MIDE el progreso de la herramienta (tiempo, y tamaño/antigüedad de su log) y lo ENTREGA por un callback
   en cada latido;
2. el callback es un observador best-effort: su falla jamás cambia el resultado de la corrida;
3. el servicio lo convierte en un evento (`pipeline.dyndolod.progress`) y un snapshot que lee `lods_status`
   (ver `tests/test_dyndolod_progreso_servicio.py`).

El log que se mide es el de ``_ruta_del_log`` (fuente única): bajo USVFS el archivo puede aparecer en otra capa y
entonces tamaño/antigüedad quedan en ``None``; el tiempo transcurrido siempre está. No se midió en un rig.
"""

from __future__ import annotations

import asyncio
import logging
import os
import pathlib
import threading
import time
from unittest import mock
from unittest.mock import MagicMock

import pytest

import sky_claw.local.tools.dyndolod_runner as ddl
from sky_claw.local.tools.dyndolod_runner import DynDOLODRunner, ProgresoDeHerramienta, ReadinessMode


class _StreamVacio:
    async def read(self, _n: int) -> bytes:
        return b""


class _ProcesoLento:
    """Vivo ``duracion`` segundos: da tiempo a que el latido (intervalo diminuto) dispare varias veces."""

    pid = 4242

    def __init__(self, duracion: float) -> None:
        self._duracion = duracion
        self.returncode: int | None = None
        self.stdout = _StreamVacio()
        self.stderr = _StreamVacio()

    def kill(self) -> None:
        self.returncode = -9

    async def wait(self) -> int:
        await asyncio.sleep(self._duracion)
        self.returncode = 0
        return 0


def _runner(tmp_path: pathlib.Path, *, on_progress=None, intervalo: float = 0.01) -> DynDOLODRunner:
    config = MagicMock()
    config.timeout_seconds = 3600
    config.heartbeat_interval = intervalo
    config.fence_ownership = None
    config.external_work_root = None
    config.game_mode = "sse"
    config.texgen_exe = tmp_path / "TexGen" / "TexGenx64.exe"
    config.dyndolod_exe = tmp_path / "DynDOLOD" / "DynDOLODx64.exe"
    return DynDOLODRunner(config, readiness=ReadinessMode.DISABLED_FOR_TEST, on_progress=on_progress)


async def _correr(runner: DynDOLODRunner, proc: _ProcesoLento, tool: str = "TexGen"):
    with (
        mock.patch.object(ddl.asyncio, "create_subprocess_exec", mock.AsyncMock(return_value=proc)),
        # El proceso es falso (pid 4242): sin esto, en Windows el helper real asignaría al Job el proceso VIVO cuyo
        # PID coincide (OpenProcess ignora los dos bits bajos) y ``close_job`` lo mataría (review CodeRabbit #905).
        mock.patch.object(ddl, "assign_kill_on_close_job", return_value=None),
    ):
        return await runner._execute_process(  # noqa: SLF001 -- el seam bajo prueba ES privado
            executable=pathlib.Path(f"{tool}x64.exe"),
            args=["-sse"],
            tool_name=tool,
        )


def _escribir_log(runner: DynDOLODRunner, tool: str, contenido: bytes, *, antiguedad: float = 0.0) -> pathlib.Path:
    ruta = runner._ruta_del_log(tool)  # noqa: SLF001
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_bytes(contenido)
    ahora = time.time()
    os.utime(ruta, (ahora - antiguedad, ahora - antiguedad))
    return ruta


# ---------------------------------------------------------------------------
# 1. La medición
# ---------------------------------------------------------------------------


def test_sin_log_el_progreso_informa_solo_el_tiempo(tmp_path: pathlib.Path) -> None:
    runner = _runner(tmp_path)

    progreso = runner._medir_progreso("TexGen", 12.5)  # noqa: SLF001

    assert progreso == ProgresoDeHerramienta(
        tool_name="TexGen", elapsed_seconds=12.5, log_path=None, log_bytes=None, log_idle_seconds=None
    )


@pytest.mark.parametrize("tool", ["TexGen", "DynDOLOD"])
def test_con_log_el_progreso_informa_tamano_y_segundos_sin_cambios(tmp_path: pathlib.Path, tool: str) -> None:
    runner = _runner(tmp_path)
    ruta = _escribir_log(runner, tool, b"x" * 1234, antiguedad=90.0)

    progreso = runner._medir_progreso(tool, 3.0)  # noqa: SLF001

    assert progreso.tool_name == tool
    assert progreso.log_path == str(ruta)
    assert progreso.log_bytes == 1234
    assert progreso.log_idle_seconds == pytest.approx(90.0, abs=5.0)


def test_la_antiguedad_nunca_es_negativa_si_el_reloj_del_log_va_adelantado(tmp_path: pathlib.Path) -> None:
    runner = _runner(tmp_path)
    _escribir_log(runner, "TexGen", b"ok", antiguedad=-3600.0)  # mtime en el futuro

    progreso = runner._medir_progreso("TexGen", 1.0)  # noqa: SLF001

    assert progreso.log_idle_seconds == 0.0


# ---------------------------------------------------------------------------
# 2. El latido real entrega el progreso
# ---------------------------------------------------------------------------


async def test_el_latido_real_entrega_el_progreso_en_cada_intervalo(tmp_path: pathlib.Path) -> None:
    recibidos: list[ProgresoDeHerramienta] = []

    async def alerta(progreso: ProgresoDeHerramienta) -> None:
        recibidos.append(progreso)

    runner = _runner(tmp_path, on_progress=alerta)
    _escribir_log(runner, "TexGen", b"abc")

    await _correr(runner, _ProcesoLento(0.15))

    assert len(recibidos) >= 2, "con un intervalo de 10 ms y 150 ms de proceso debe haber varios latidos"
    assert {p.tool_name for p in recibidos} == {"TexGen"}
    assert recibidos[0].log_bytes == 3
    tiempos = [p.elapsed_seconds for p in recibidos]
    # El primer latido puede medir 0.0: el reloj monotónico de Windows avanza de a ~15,6 ms y el primer intervalo es de
    # 10 ms (CI de Windows py3.11, PR #905). Lo que se exige es que no retroceda y que el tiempo corra de verdad: el
    # último latido, tras 150 ms de proceso, no puede ser 0.
    assert tiempos == sorted(tiempos)
    assert tiempos[-1] > 0


async def test_sin_callback_la_corrida_es_la_de_siempre(tmp_path: pathlib.Path) -> None:
    runner = _runner(tmp_path, on_progress=None)

    stdout, stderr, codigo, _duracion = await _correr(runner, _ProcesoLento(0.05))

    assert (stdout, stderr, codigo) == ("", "", 0)


# ---------------------------------------------------------------------------
# 3. Un observador roto no cambia el resultado de la corrida
# ---------------------------------------------------------------------------


async def test_un_callback_que_falla_no_cambia_el_resultado_y_queda_registrado(
    tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
) -> None:
    llamadas = 0

    async def roto(_progreso: ProgresoDeHerramienta) -> None:
        nonlocal llamadas
        llamadas += 1
        raise RuntimeError("el bus de eventos está caído")

    runner = _runner(tmp_path, on_progress=roto)

    with caplog.at_level(logging.WARNING, logger=ddl.logger.name):
        _stdout, _stderr, codigo, _duracion = await _correr(runner, _ProcesoLento(0.1))

    assert codigo == 0
    assert llamadas >= 2, "una falla no apaga el latido: se sigue intentando en el siguiente intervalo"
    assert any("progreso" in r.getMessage().lower() for r in caplog.records)


async def test_un_callback_que_cuelga_no_retrasa_el_fin_de_la_corrida(tmp_path: pathlib.Path) -> None:
    """El latido corre en su propia tarea: aunque el observador no vuelva, la corrida termina y lo cancela."""

    async def colgado(_progreso: ProgresoDeHerramienta) -> None:
        await asyncio.sleep(3600)

    runner = _runner(tmp_path, on_progress=colgado)

    inicio = time.monotonic()
    _stdout, _stderr, codigo, _duracion = await asyncio.wait_for(_correr(runner, _ProcesoLento(0.05)), timeout=10)

    assert codigo == 0
    assert time.monotonic() - inicio < 5


async def test_un_callback_que_cuelga_una_vez_no_apaga_los_latidos_siguientes(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review CodeRabbit #905: un observador pendiente frenaba el latido para siempre (en una corrida de horas, sin logs)."""
    monkeypatch.setattr(ddl, "_PLAZO_DEL_AVISO_DE_PROGRESO_S", 0.03)
    llamadas: list[float] = []

    async def cuelga_la_primera_vez(progreso: ProgresoDeHerramienta) -> None:
        llamadas.append(progreso.elapsed_seconds)
        if len(llamadas) == 1:
            await asyncio.sleep(3600)

    runner = _runner(tmp_path, on_progress=cuelga_la_primera_vez)

    _stdout, _stderr, codigo, _duracion = await asyncio.wait_for(_correr(runner, _ProcesoLento(0.4)), timeout=10)

    assert codigo == 0
    assert len(llamadas) >= 3, "tras el plazo del primer aviso el latido sigue con los intervalos siguientes"


async def test_la_cancelacion_externa_atraviesa_el_plazo_del_aviso(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """El plazo acota la entrega del aviso; no puede tragarse la cancelación de quien detiene la corrida."""
    monkeypatch.setattr(ddl, "_PLAZO_DEL_AVISO_DE_PROGRESO_S", 30.0)

    async def colgado(_progreso: ProgresoDeHerramienta) -> None:
        await asyncio.sleep(3600)

    runner = _runner(tmp_path, on_progress=colgado)
    tarea = asyncio.create_task(runner._reportar_progreso("TexGen", 1.0))  # noqa: SLF001
    await asyncio.sleep(0.05)

    tarea.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(tarea, timeout=5)


def _hilos_del_executor_por_defecto() -> int:
    """Tope de hilos del executor por defecto de asyncio (``min(32, cpu + 4)``): lo que un goteo de hilos colgados agota."""
    return min(32, (os.cpu_count() or 1) + 4)


async def test_un_stat_colgado_ocupa_un_solo_hilo_y_no_agota_el_executor_por_defecto(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review CodeRabbit #905: al vencer el plazo se cancela el ``await`` pero NO el hilo que ya estaba corriendo.

    Con ``to_thread`` cada latido (uno por minuto, durante horas) sumaba un hilo al executor por defecto mientras el disco
    siguiera colgado; al agotarse, TODO otro ``to_thread`` de la app quedaba esperando detrás. Ahora hay a lo sumo UNA
    medición en vuelo: mientras no vuelve, las siguientes se omiten. Y ese hilo es daemon: ``tests/conftest.py`` falla la
    sesión (código 3) con cualquier hilo no-daemon vivo al final, y un ``stat`` colgado no debe bloquear el cierre.
    """
    monkeypatch.setattr(ddl, "_PLAZO_DEL_AVISO_DE_PROGRESO_S", 0.01)
    liberar = threading.Event()
    iniciadas: list[float] = []

    def colgada(tool_name: str, elapsed_seconds: float) -> ProgresoDeHerramienta:
        iniciadas.append(elapsed_seconds)
        liberar.wait(timeout=30)
        return ProgresoDeHerramienta(tool_name=tool_name, elapsed_seconds=elapsed_seconds)

    runner = _runner(tmp_path, on_progress=lambda _p: asyncio.sleep(0))
    monkeypatch.setattr(runner, "_medir_progreso", colgada)

    try:
        for latido in range(_hilos_del_executor_por_defecto() + 8):  # más latidos que hilos tiene el executor
            await runner._reportar_progreso("TexGen", float(latido))  # noqa: SLF001

        assert len(iniciadas) == 1, "una medición colgada no puede lanzar otra mientras no vuelva"
        hilos = [h for h in threading.enumerate() if h.name == "skyclaw-progreso"]
        assert len(hilos) == 1 and hilos[0].daemon, (
            "un solo hilo de medición y daemon (no bloquea el cierre del proceso)"
        )
        # El executor por defecto sigue libre para el resto de la app (persistencia de config, post-checks, etc.).
        assert await asyncio.wait_for(asyncio.to_thread(lambda: "libre"), timeout=5) == "libre"
    finally:
        liberar.set()


async def test_la_medicion_se_reanuda_cuando_el_disco_vuelve(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Degradar es omitir muestras mientras el ``stat`` no vuelve; no es apagar el progreso para el resto de la corrida."""
    monkeypatch.setattr(ddl, "_PLAZO_DEL_AVISO_DE_PROGRESO_S", 0.05)
    liberar = threading.Event()
    llamadas = 0

    def cuelga_la_primera_vez(tool_name: str, elapsed_seconds: float) -> ProgresoDeHerramienta:
        nonlocal llamadas
        llamadas += 1
        if llamadas == 1:
            liberar.wait(timeout=30)
        return ProgresoDeHerramienta(tool_name=tool_name, elapsed_seconds=elapsed_seconds)

    entregas: list[ProgresoDeHerramienta] = []

    async def alerta(progreso: ProgresoDeHerramienta) -> None:
        entregas.append(progreso)

    runner = _runner(tmp_path, on_progress=alerta)
    monkeypatch.setattr(runner, "_medir_progreso", cuelga_la_primera_vez)

    try:
        await runner._reportar_progreso("TexGen", 1.0)  # noqa: SLF001 -- el hilo queda colgado: no hay entrega
        assert entregas == []
        liberar.set()
        for latido in range(2, 200):  # el hilo termina en milisegundos: se sondea sin depender del reloj
            await runner._reportar_progreso("TexGen", float(latido))  # noqa: SLF001
            if entregas:
                break
            await asyncio.sleep(0.02)
    finally:
        liberar.set()

    assert entregas, "tras volver el disco las muestras vuelven a llegar"


async def test_una_medicion_que_falla_libera_el_turno_de_la_siguiente(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """La bandera «una en vuelo» se limpia en el hilo aunque la medición lance: una falla no apaga el progreso para siempre."""
    llamadas = 0

    def falla_la_primera_vez(tool_name: str, elapsed_seconds: float) -> ProgresoDeHerramienta:
        nonlocal llamadas
        llamadas += 1
        if llamadas == 1:
            raise RuntimeError("el stat falló de una forma inesperada")
        return ProgresoDeHerramienta(tool_name=tool_name, elapsed_seconds=elapsed_seconds)

    entregas: list[ProgresoDeHerramienta] = []

    async def alerta(progreso: ProgresoDeHerramienta) -> None:
        entregas.append(progreso)

    runner = _runner(tmp_path, on_progress=alerta)
    monkeypatch.setattr(runner, "_medir_progreso", falla_la_primera_vez)

    with caplog.at_level(logging.WARNING, logger=ddl.logger.name):
        await runner._reportar_progreso("TexGen", 1.0)  # noqa: SLF001 -- la falla se registra y no se propaga
        await runner._reportar_progreso("TexGen", 2.0)  # noqa: SLF001

    assert llamadas == 2, "la segunda medición corre: la falla de la primera no dejó la bandera pegada"
    assert [p.elapsed_seconds for p in entregas] == [2.0]
    assert any("progreso" in r.getMessage().lower() for r in caplog.records)
