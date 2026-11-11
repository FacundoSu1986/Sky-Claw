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
    with mock.patch.object(ddl.asyncio, "create_subprocess_exec", mock.AsyncMock(return_value=proc)):
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
    assert tiempos == sorted(tiempos) and tiempos[0] > 0


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
