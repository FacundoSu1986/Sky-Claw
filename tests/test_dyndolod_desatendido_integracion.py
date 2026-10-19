"""Etapa 9 desatendida integrada al runner, al servicio y al composition root (#661 P9).

Cubre lo que los tests del driver no pueden ver porque viven debajo:

* el modo viaja EXPLÍCITO de ``run_full_pipeline`` a los dos lanzadores y a
  ``_execute_process`` (ancla enumerante: ningún spawn sin ``interaccion=``);
* antes del spawn: contrato por SHA, preset aprobado, snapshot durable; un corte
  ahí no crea proceso;
* entre spawn y ``proc.wait()``: el driver reemplaza al protocolo asistido, y un
  corte mata el proceso por la MISMA rama de cleanup;
* después: el preset vuelve a sus bytes aunque la herramienta lo reescribió, y
  un restore fallido NO se declara éxito;
* ASISTIDO sigue intacto (criterio CI 20).
"""

from __future__ import annotations

import ast
import asyncio
import pathlib
from unittest import mock
from unittest.mock import MagicMock

import pytest

import sky_claw.local.tools.dyndolod_runner as ddl
from sky_claw.local.tools.dyndolod_presets import GuardaDePresets, PresetError, huella_de_configuracion
from sky_claw.local.tools.dyndolod_runner import (
    DynDOLODDesatendidoError,
    DynDOLODExecutionError,
    DynDOLODPipelineResult,
    DynDOLODRunner,
    HerramientaAprobada,
    InteraccionDeCorrida,
    ReadinessMode,
)
from sky_claw.local.tools.dyndolod_uia_contrato import CodigoDesatendido, ModoDeInteraccion
from sky_claw.local.tools.dyndolod_uia_driver import CapacidadDesatendidaUIA, SolicitudAprobacionDesatendida
from sky_claw.local.tools.dyndolod_uia_driver_ejecutor import EjecutorDePasoEnProceso
from sky_claw.local.tools.dyndolod_uia_gate import ResultadoConfirmacion
from sky_claw.local.tools.output_targets import derivar_layout_de_dyndolod
from tests._dyndolod_gui_simulada import CONTRATO_DYNDOLOD, CONTRATO_TEXGEN, GuiSimulada

RAIZ = pathlib.Path(__file__).resolve().parents[1]
RUNNER = RAIZ / "sky_claw" / "local" / "tools" / "dyndolod_runner.py"
SERVICIO = RAIZ / "sky_claw" / "local" / "tools" / "dyndolod_service.py"
COMPOSICION = RAIZ / "sky_claw" / "app" / "orchestrator" / "orchestration_composition.py"

RAIZ_WINDOWS = pathlib.Path(r"E:\Sky-Claw T5 Rig")
#: El ejecutable LANZADO lleva sintaxis Win32: la identidad del proceso se prueba
#: con ``canonicalizar_ruta_windows`` (semántica del producto, que es Windows), y
#: una ruta POSIX de ``tmp_path`` no canonicaliza. Presets y logs sí son reales.
EXE_LANZADO = {
    "TexGen": pathlib.Path(r"C:\Modding\DynDOLOD\TexGenx64.exe"),
    "DynDOLOD": pathlib.Path(r"C:\Modding\DynDOLOD\DynDOLODx64.exe"),
}
PRESET_TEXGEN = b"[TexGen]\r\nOutputPath=E:\\Sky-Claw T5 Rig\\PR2 Work\\DynDOLOD\\TexGen\\\r\nStitchObjects=1\r\n"
PRESET_DYNDOLOD = (
    b"[DynDOLOD]\r\nOutputPath=E:\\Sky-Claw T5 Rig\\PR2 Work\\DynDOLOD\\DynDOLOD\r\nPreset=Medium\r\nTamriel=1\r\n"
)


# =============================================================================
# Dobles
# =============================================================================


class _StreamVacio:
    async def read(self, _n: int) -> bytes:
        return b""


class ProcesoDeGui:
    """Proceso cuyo ciclo de vida ES el de la GUI simulada (vive hasta que la GUI sale)."""

    def __init__(self, gui: GuiSimulada) -> None:
        self._gui = gui
        self.pid = gui.pid
        self.stdout = _StreamVacio()
        self.stderr = _StreamVacio()
        self.kill_llamado = False

    @property
    def returncode(self) -> int | None:
        return None if self._gui.proceso_vivo() else (-9 if self.kill_llamado else 0)

    def kill(self) -> None:
        self.kill_llamado = True
        self._gui.vivo = False

    def terminate(self) -> None:
        self.kill()

    async def wait(self) -> int:
        while self._gui.proceso_vivo():
            await asyncio.sleep(0.005)
        return self.returncode or 0


class AprobadorFalso:
    def __init__(
        self,
        *,
        configuracion: ResultadoConfirmacion = ResultadoConfirmacion.APROBADA,
        modal: ResultadoConfirmacion = ResultadoConfirmacion.APROBADA,
        modal_pendiente: asyncio.Event | None = None,
    ) -> None:
        self._configuracion = configuracion
        self._modal = modal
        #: Si está, el operador "no contesta" el modal hasta que el test lo suelte.
        self._modal_pendiente = modal_pendiente
        self.esperando_modal = asyncio.Event()
        self.configuraciones: list[SolicitudAprobacionDesatendida] = []
        self.modales: list[object] = []
        self.avisos: list[str] = []

    async def aprobar_configuracion(self, solicitud: SolicitudAprobacionDesatendida) -> ResultadoConfirmacion:
        self.configuraciones.append(solicitud)
        return self._configuracion

    async def aprobar_modal(self, solicitud: object) -> ResultadoConfirmacion:
        self.modales.append(solicitud)
        if self._modal_pendiente is not None:
            self.esperando_modal.set()
            await self._modal_pendiente.wait()
        return self._modal

    async def informar(self, *, tool: str, mensaje: str) -> None:
        self.avisos.append(mensaje)


def _instalacion(tmp_path: pathlib.Path) -> dict[str, pathlib.Path]:
    exe_dir = tmp_path / "DynDOLOD"
    presets = exe_dir / "Edit Scripts" / "DynDOLOD" / "Presets"
    presets.mkdir(parents=True)
    texgen = exe_dir / "TexGenx64.exe"
    dyndolod = exe_dir / "DynDOLODx64.exe"
    texgen.write_bytes(b"MZ texgen")
    dyndolod.write_bytes(b"MZ dyndolod")
    preset_texgen = presets / "DynDOLOD_SSE_TexGen.ini"
    preset_dyndolod = presets / "DynDOLOD_SSE_Default.ini"
    preset_texgen.write_bytes(PRESET_TEXGEN)
    preset_dyndolod.write_bytes(PRESET_DYNDOLOD)
    return {
        "texgen": texgen,
        "dyndolod": dyndolod,
        "preset_texgen": preset_texgen,
        "preset_dyndolod": preset_dyndolod,
        "log_texgen": exe_dir / "Logs" / "TexGen_SSE_log.txt",
        "log_dyndolod": exe_dir / "Logs" / "DynDOLOD_SSE_log.txt",
    }


@pytest.fixture
def sha_de_alpha209(monkeypatch):
    """El binario de test no es Alpha-209: el hash se fija por nombre (el contrato sí es el real)."""
    por_nombre = {
        "TexGenx64.exe": CONTRATO_TEXGEN.exe_sha256,
        "DynDOLODx64.exe": CONTRATO_DYNDOLOD.exe_sha256,
    }

    def _sha(ruta: pathlib.Path) -> str:
        for nombre, sha in por_nombre.items():
            if str(ruta).endswith(nombre):
                return sha
        return "0" * 64

    monkeypatch.setattr(ddl, "_sha256_de_archivo", _sha)
    return por_nombre


def _capacidad(
    gui: GuiSimulada | None,
    estado: pathlib.Path,
    aprobador: AprobadorFalso,
) -> CapacidadDesatendidaUIA:
    ejecutor = EjecutorDePasoEnProceso(
        fabrica_backend=lambda: gui,
        localizador=gui,
        dormir=lambda s: __import__("time").sleep(min(s, 0.002)),
        gracia_externa_segundos=2.0,
    )
    return CapacidadDesatendidaUIA(
        ejecutor=ejecutor,
        aprobador=aprobador,
        directorio_de_presets=estado,
        arranque_timeout_segundos=1.0,
        observacion_intervalo_segundos=0.005,
        ventana_de_generacion_segundos=0.5,
        generacion_intervalo_segundos=0.005,
        confirmacion_de_inicio_segundos=0.5,
        confirmacion_intervalo_segundos=0.01,
        cierre_timeout_segundos=0.5,
        aprobacion_timeout_segundos=1.0,
        gracia_externa_segundos=1.0,
    )


def _runner(instalacion, capacidad: CapacidadDesatendidaUIA | None) -> DynDOLODRunner:
    config = MagicMock()
    config.timeout_seconds = 60
    config.heartbeat_interval = 60
    config.fence_ownership = None
    config.output_layout = derivar_layout_de_dyndolod(external_work_root=RAIZ_WINDOWS)
    config.external_work_root = None
    config.game_mode = "sse"
    config.texgen_exe = instalacion["texgen"]
    config.dyndolod_exe = instalacion["dyndolod"]
    return DynDOLODRunner(config, readiness=ReadinessMode.DISABLED_FOR_TEST, desatendido=capacidad)


def _interaccion(instalacion, *, tool: str = "TexGen") -> InteraccionDeCorrida:
    contrato = CONTRATO_TEXGEN if tool == "TexGen" else CONTRATO_DYNDOLOD
    ruta = instalacion["preset_texgen"] if tool == "TexGen" else instalacion["preset_dyndolod"]
    return InteraccionDeCorrida(
        modo=ModoDeInteraccion.DESATENDIDO,
        aprobadas={
            tool: HerramientaAprobada(
                contrato=contrato, ruta_preset=ruta, huella_preset=huella_de_configuracion(ruta.read_bytes())
            )
        },
    )


async def _ejecutar(runner: DynDOLODRunner, gui: GuiSimulada, instalacion, interaccion, *, tool: str = "TexGen"):
    exe = EXE_LANZADO[tool]
    proceso = ProcesoDeGui(gui)
    lanzar = mock.AsyncMock(return_value=proceso)
    with mock.patch.object(ddl.asyncio, "create_subprocess_exec", lanzar):
        salida = await runner._execute_process(  # noqa: SLF001 -- el seam bajo prueba ES privado
            executable=exe,
            args=["-sse"],
            tool_name=tool,
            interaccion=interaccion,
        )
    return salida, lanzar, proceso


def _gui(instalacion, contrato=CONTRATO_TEXGEN, **campos) -> GuiSimulada:
    tool = contrato.tool
    base = {
        "exe": str(EXE_LANZADO[tool]),
        "ruta_log": instalacion["log_texgen"] if tool == "TexGen" else instalacion["log_dyndolod"],
        "ruta_preset": instalacion["preset_texgen"] if tool == "TexGen" else instalacion["preset_dyndolod"],
    }
    base.update(campos)
    return GuiSimulada(contrato, **base)  # type: ignore[arg-type]


# =============================================================================
# _execute_process: spawn → driver → proc.wait() → restore
# =============================================================================


async def test_la_corrida_desatendida_completa_restaura_el_preset_que_la_herramienta_reescribio(
    tmp_path, sha_de_alpha209
):
    instalacion = _instalacion(tmp_path)
    gui = _gui(instalacion)
    aprobador = AprobadorFalso()
    runner = _runner(instalacion, _capacidad(gui, tmp_path / "estado", aprobador))

    (_out, _err, rc, _dur), lanzar, proceso = await _ejecutar(runner, gui, instalacion, _interaccion(instalacion))

    assert rc == 0
    lanzar.assert_awaited_once()
    assert not proceso.kill_llamado
    assert gui.invocaciones == ["Start", "Exit TexGen"]
    # La herramienta reescribió su preset al iniciar; Sky-Claw lo devolvió exacto.
    assert instalacion["preset_texgen"].read_bytes() == PRESET_TEXGEN
    assert GuardaDePresets(tmp_path / "estado").pendientes() == []
    assert any("generación iniciada" in aviso for aviso in aprobador.avisos)


async def test_dyndolod_desatendido_advanced_reconvergencia_ok_save_and_exit(tmp_path, sha_de_alpha209):
    instalacion = _instalacion(tmp_path)
    gui = _gui(instalacion, CONTRATO_DYNDOLOD, output_tras_advanced=r"E:\Otro\DynDOLOD")
    runner = _runner(instalacion, _capacidad(gui, tmp_path / "estado", AprobadorFalso()))

    (_o, _e, rc, _d), _lanzar, _proc = await _ejecutar(
        runner, gui, instalacion, _interaccion(instalacion, tool="DynDOLOD"), tool="DynDOLOD"
    )

    assert rc == 0
    assert gui.invocaciones == ["Advanced >>>", "OK", "Save and Exit"]
    assert len(gui.escrituras) == 2  # Output rancio inicial + reconvergencia tras Advanced
    assert instalacion["preset_dyndolod"].read_bytes() == PRESET_DYNDOLOD


async def test_ci_01_build_que_cambio_desde_la_aprobacion_no_spawnea(tmp_path, sha_de_alpha209):
    instalacion = _instalacion(tmp_path)
    gui = _gui(instalacion)
    runner = _runner(instalacion, _capacidad(gui, tmp_path / "estado", AprobadorFalso()))
    sha_de_alpha209["TexGenx64.exe"] = "1" * 64
    interaccion = _interaccion(instalacion)

    with pytest.raises(DynDOLODDesatendidoError) as error:
        await _ejecutar(runner, gui, instalacion, interaccion)

    assert error.value.codigo is CodigoDesatendido.UNATTENDED_BUILD_UNSUPPORTED
    assert interaccion.codigos == [CodigoDesatendido.UNATTENDED_BUILD_UNSUPPORTED]
    assert gui.invocaciones == [] and gui.escrituras == []
    assert GuardaDePresets(tmp_path / "estado").pendientes() == []


async def test_ci_06_preset_que_cambio_desde_la_aprobacion_no_spawnea(tmp_path, sha_de_alpha209):
    instalacion = _instalacion(tmp_path)
    gui = _gui(instalacion)
    runner = _runner(instalacion, _capacidad(gui, tmp_path / "estado", AprobadorFalso()))
    interaccion = _interaccion(instalacion)
    instalacion["preset_texgen"].write_bytes(PRESET_TEXGEN.replace(b"StitchObjects=1", b"StitchObjects=0"))

    with pytest.raises(DynDOLODDesatendidoError) as error:
        _salida, lanzar, _p = await _ejecutar(runner, gui, instalacion, interaccion)

    assert error.value.codigo is CodigoDesatendido.PRESET_MISMATCH
    assert gui.invocaciones == []


async def test_un_cambio_solo_del_output_path_no_invalida_la_aprobacion(tmp_path, sha_de_alpha209):
    instalacion = _instalacion(tmp_path)
    gui = _gui(instalacion)
    runner = _runner(instalacion, _capacidad(gui, tmp_path / "estado", AprobadorFalso()))
    interaccion = _interaccion(instalacion)
    reescrito = PRESET_TEXGEN.replace(b"PR2 Work", b"Otra Corrida")
    instalacion["preset_texgen"].write_bytes(reescrito)

    (_o, _e, rc, _d), _lanzar, _p = await _ejecutar(runner, gui, instalacion, interaccion)

    assert rc == 0
    # Se restaura el estado PREVIO a la corrida (el que había al tomar el snapshot).
    assert instalacion["preset_texgen"].read_bytes() == reescrito


async def test_un_corte_del_driver_mata_el_proceso_y_restaura_el_preset(tmp_path, sha_de_alpha209):
    instalacion = _instalacion(tmp_path)
    gui = _gui(instalacion, modal_inicial=True)
    aprobador = AprobadorFalso(modal=ResultadoConfirmacion.DENEGADA)
    runner = _runner(instalacion, _capacidad(gui, tmp_path / "estado", aprobador))
    interaccion = _interaccion(instalacion)

    with pytest.raises(DynDOLODDesatendidoError) as error:
        await _ejecutar(runner, gui, instalacion, interaccion)

    assert error.value.codigo is CodigoDesatendido.KNOWN_MODAL_NOT_APPROVED
    assert interaccion.codigos == [CodigoDesatendido.KNOWN_MODAL_NOT_APPROVED]
    assert not gui.proceso_vivo()
    assert gui.invocaciones == []
    assert instalacion["preset_texgen"].read_bytes() == PRESET_TEXGEN
    assert GuardaDePresets(tmp_path / "estado").pendientes() == []


async def test_un_spawn_que_falla_devuelve_el_snapshot_en_el_acto(tmp_path, sha_de_alpha209):
    instalacion = _instalacion(tmp_path)
    gui = _gui(instalacion)
    runner = _runner(instalacion, _capacidad(gui, tmp_path / "estado", AprobadorFalso()))
    with (
        mock.patch.object(ddl.asyncio, "create_subprocess_exec", mock.AsyncMock(side_effect=OSError("acceso"))),
        pytest.raises(DynDOLODExecutionError),
    ):
        await runner._execute_process(  # noqa: SLF001
            executable=EXE_LANZADO["TexGen"],
            args=["-sse"],
            tool_name="TexGen",
            interaccion=_interaccion(instalacion),
        )
    assert GuardaDePresets(tmp_path / "estado").pendientes() == []


async def test_un_restore_fallido_no_se_declara_exito(tmp_path, sha_de_alpha209, monkeypatch):
    instalacion = _instalacion(tmp_path)
    gui = _gui(instalacion)
    runner = _runner(instalacion, _capacidad(gui, tmp_path / "estado", AprobadorFalso()))
    interaccion = _interaccion(instalacion)

    def _restaurar_roto(self, registro):
        raise PresetError("disco de solo lectura")

    monkeypatch.setattr(GuardaDePresets, "restaurar", _restaurar_roto)
    with pytest.raises(DynDOLODDesatendidoError) as error:
        await _ejecutar(runner, gui, instalacion, interaccion)

    assert error.value.codigo is CodigoDesatendido.PRESET_RESTORE_FAILED
    # La herramienta corrió y cerró; lo que falla es devolver el estado del usuario.
    assert gui.invocaciones == ["Start", "Exit TexGen"]
    # El registro durable queda para la reconciliación de la próxima corrida.
    assert len(GuardaDePresets(tmp_path / "estado").pendientes()) == 1


# -----------------------------------------------------------------------------
# Un restore fallido nunca queda sólo en el log (review Codex, PR #740)
# -----------------------------------------------------------------------------
#
# La propiedad: *en TODA salida de `_execute_process` donde el preset no volvió a
# su snapshot, el resultado de la corrida nombra PRESET_RESTORE_FAILED*. Las
# salidas son tres: normal (arriba), por excepción después del spawn y por spawn
# fallido; la cancelación manda sobre el veredicto, pero el código igual queda
# registrado en la corrida.


def _restore_roto(monkeypatch) -> None:
    def _restaurar_roto(self, registro):
        raise PresetError("disco de solo lectura")

    monkeypatch.setattr(GuardaDePresets, "restaurar", _restaurar_roto)


async def test_un_restore_fallido_tras_un_corte_del_driver_viaja_en_el_corte(tmp_path, sha_de_alpha209, monkeypatch):
    instalacion = _instalacion(tmp_path)
    gui = _gui(instalacion, modal_inicial=True)
    runner = _runner(
        instalacion, _capacidad(gui, tmp_path / "estado", AprobadorFalso(modal=ResultadoConfirmacion.DENEGADA))
    )
    interaccion = _interaccion(instalacion)
    _restore_roto(monkeypatch)

    with pytest.raises(DynDOLODDesatendidoError) as error:
        await _ejecutar(runner, gui, instalacion, interaccion)

    mensaje = str(error.value)
    # Los DOS hechos llegan al operador: por qué cortó y que su preset quedó alterado.
    assert "PRESET_RESTORE_FAILED" in mensaje and "KNOWN_MODAL_NOT_APPROVED" in mensaje
    assert interaccion.codigos == [CodigoDesatendido.KNOWN_MODAL_NOT_APPROVED, CodigoDesatendido.PRESET_RESTORE_FAILED]
    # El corte original queda como causa: la cadena que recorre el veto de rollback no cambia.
    assert isinstance(error.value.__cause__, DynDOLODDesatendidoError)
    assert error.value.__cause__.codigo is CodigoDesatendido.KNOWN_MODAL_NOT_APPROVED
    assert len(GuardaDePresets(tmp_path / "estado").pendientes()) == 1


async def test_un_restore_fallido_tras_un_spawn_fallido_viaja_en_el_corte(tmp_path, sha_de_alpha209, monkeypatch):
    instalacion = _instalacion(tmp_path)
    gui = _gui(instalacion)
    runner = _runner(instalacion, _capacidad(gui, tmp_path / "estado", AprobadorFalso()))
    interaccion = _interaccion(instalacion)
    _restore_roto(monkeypatch)

    with (
        mock.patch.object(ddl.asyncio, "create_subprocess_exec", mock.AsyncMock(side_effect=OSError("acceso"))),
        pytest.raises(DynDOLODDesatendidoError) as error,
    ):
        await runner._execute_process(  # noqa: SLF001
            executable=EXE_LANZADO["TexGen"],
            args=["-sse"],
            tool_name="TexGen",
            interaccion=interaccion,
        )

    assert error.value.codigo is CodigoDesatendido.PRESET_RESTORE_FAILED
    assert "acceso" in str(error.value)
    assert isinstance(error.value.__cause__, DynDOLODExecutionError)
    assert interaccion.codigos == [CodigoDesatendido.PRESET_RESTORE_FAILED]


async def test_un_restore_fallido_durante_una_cancelacion_no_tapa_la_cancelacion(
    tmp_path, sha_de_alpha209, monkeypatch
):
    instalacion = _instalacion(tmp_path)
    gui = _gui(instalacion, modal_inicial=True)
    aprobador = AprobadorFalso(modal_pendiente=asyncio.Event())
    runner = _runner(instalacion, _capacidad(gui, tmp_path / "estado", aprobador))
    interaccion = _interaccion(instalacion)
    _restore_roto(monkeypatch)

    tarea = asyncio.create_task(_ejecutar(runner, gui, instalacion, interaccion))
    await asyncio.wait_for(aprobador.esperando_modal.wait(), timeout=5)
    tarea.cancel()
    with pytest.raises(asyncio.CancelledError):
        await tarea

    assert CodigoDesatendido.PRESET_RESTORE_FAILED in interaccion.codigos
    assert not gui.proceso_vivo()


def test_toda_salida_de_execute_process_pasa_por_la_politica_de_restauracion():
    """Ancla enumerante: el restore crudo sólo lo llama la política, y la política
    corre en las DOS salidas de `_execute_process` (spawn fallido y `finally`)."""
    arbol = _arbol(RUNNER)
    crudos = {
        metodo.name
        for metodo in ast.walk(arbol)
        if isinstance(metodo, ast.FunctionDef | ast.AsyncFunctionDef)
        and _llamadas_a(metodo, "_restaurar_preset_sin_gatear")
    }
    assert crudos == {"_restaurar_preset_al_salir"}
    salidas = _llamadas_a(_metodo(arbol, "_execute_process"), "_restaurar_preset_al_salir")
    assert len(salidas) == 2
    # El `finally` distingue "salida normal" de "excepción propagándose" con el flag
    # `salida_normal`, que sube al final del `else`. Eso sólo es verdad si ninguna
    # rama del `try` retorna y todo `except` re-lanza: se congela acá.
    proceso = next(
        nodo
        for nodo in ast.walk(_metodo(arbol, "_execute_process"))
        if isinstance(nodo, ast.Try)
        and any(isinstance(n, ast.Name) and n.id == "salida_normal" for n in ast.walk(nodo))
    )
    anidadas = {
        id(r)
        for f in ast.walk(proceso)
        if isinstance(f, ast.FunctionDef | ast.AsyncFunctionDef)
        for r in ast.walk(f)
        if isinstance(r, ast.Return)
    }
    ramas = [proceso.body, *(h.body for h in proceso.handlers), proceso.orelse]
    retornos = [
        r for rama in ramas for st in rama for r in ast.walk(st) if isinstance(r, ast.Return) and id(r) not in anidadas
    ]
    assert retornos == []
    assert all(isinstance(h.body[-1], ast.Raise) for h in proceso.handlers)


async def test_un_preset_editado_entre_la_verificacion_y_el_snapshot_no_spawnea(tmp_path, sha_de_alpha209, monkeypatch):
    """El snapshot se toma de los bytes APROBADOS o no se toma (review Codex, PR #740)."""
    instalacion = _instalacion(tmp_path)
    gui = _gui(instalacion)
    runner = _runner(instalacion, _capacidad(gui, tmp_path / "estado", AprobadorFalso()))
    interaccion = _interaccion(instalacion)
    leer = ddl.leer_estado_de_preset

    def _leer_y_editar(ruta, **kwargs):
        estado = leer(ruta, **kwargs)
        # Un editor concurrente guarda OTRA configuración justo después de la verificación.
        ruta.write_bytes(PRESET_TEXGEN.replace(b"StitchObjects=1", b"StitchObjects=0"))
        return estado

    monkeypatch.setattr(ddl, "leer_estado_de_preset", _leer_y_editar)
    with pytest.raises(DynDOLODDesatendidoError) as error:
        _salida, lanzar, _p = await _ejecutar(runner, gui, instalacion, interaccion)

    assert error.value.codigo is CodigoDesatendido.PRESET_MISMATCH
    assert gui.invocaciones == []
    assert GuardaDePresets(tmp_path / "estado").pendientes() == []


class ProcesoQueElHumanoTermina(ProcesoDeGui):
    """Modo asistido: el humano opera y cierra la herramienta; ``wait()`` lo modela."""

    async def wait(self) -> int:
        self._gui.vivo = False
        return 0


async def test_ci_20_el_modo_asistido_no_toca_ni_presets_ni_el_driver(tmp_path, sha_de_alpha209):
    instalacion = _instalacion(tmp_path)
    gui = _gui(instalacion)
    runner = _runner(instalacion, _capacidad(gui, tmp_path / "estado", AprobadorFalso()))

    with mock.patch.object(
        ddl.asyncio, "create_subprocess_exec", mock.AsyncMock(return_value=ProcesoQueElHumanoTermina(gui))
    ):
        _o, _e, rc, _d = await runner._execute_process(  # noqa: SLF001
            executable=EXE_LANZADO["TexGen"],
            args=["-sse"],
            tool_name="TexGen",
            interaccion=InteraccionDeCorrida(modo=ModoDeInteraccion.ASISTIDO),
        )
    # ReadinessMode.DISABLED_FOR_TEST: el protocolo asistido es el de siempre y
    # el driver no participó (ninguna primitiva, ningún snapshot).
    assert rc == 0
    assert gui.invocaciones == [] and gui.escrituras == []
    assert not (tmp_path / "estado").exists()


# =============================================================================
# run_full_pipeline: aprobación remota de la configuración y cortes pre-spawn
# =============================================================================


async def _correr_pipeline(runner, *, preset="Medium", run_texgen=True):
    with mock.patch.object(ddl.asyncio, "create_subprocess_exec", mock.AsyncMock()) as lanzar:
        resultado = await runner.run_full_pipeline(
            run_texgen=run_texgen,
            preset=preset,
            modo_de_interaccion=ModoDeInteraccion.DESATENDIDO,
        )
    return resultado, lanzar


async def test_sin_capacidad_desatendida_no_hay_fallback_a_asistido(tmp_path, sha_de_alpha209):
    runner = _runner(_instalacion(tmp_path), None)
    resultado, lanzar = await _correr_pipeline(runner)
    assert isinstance(resultado, DynDOLODPipelineResult)
    assert not resultado.success
    assert resultado.codigos_desatendido == ("UNATTENDED_CAPABILITY_UNAVAILABLE",)
    lanzar.assert_not_awaited()


async def test_preparar_interaccion_desatendida_sin_layout_administrado_corta_con_codigo(tmp_path, sha_de_alpha209):
    """Sin `external_work_root` no hay Output administrado que aprobar: corte con código, no excepción.

    Es el estado NORMAL de una instalación sin DynDOLOD administrado. Desde que la
    preparación corre antes del lock (#740), una excepción acá saldría de
    `execute` en vez de su resultado con código (review CodeRabbit, PR #740).
    """
    instalacion = _instalacion(tmp_path)
    aprobador = AprobadorFalso()
    runner = _runner(instalacion, _capacidad(None, tmp_path / "estado", aprobador))
    runner._config.output_layout = None  # noqa: SLF001

    resultado = await runner.preparar_interaccion_desatendida(run_texgen=True, preset="Medium")

    assert isinstance(resultado, DynDOLODPipelineResult)
    assert resultado.codigos_desatendido == ("UNATTENDED_CAPABILITY_UNAVAILABLE",)
    assert aprobador.configuraciones == []


async def test_build_sin_contrato_corta_antes_de_preguntar(tmp_path, sha_de_alpha209):
    instalacion = _instalacion(tmp_path)
    aprobador = AprobadorFalso()
    runner = _runner(instalacion, _capacidad(None, tmp_path / "estado", aprobador))
    sha_de_alpha209["DynDOLODx64.exe"] = "2" * 64
    resultado, lanzar = await _correr_pipeline(runner)
    assert resultado.codigos_desatendido == ("UNATTENDED_BUILD_UNSUPPORTED",)
    assert aprobador.configuraciones == []
    lanzar.assert_not_awaited()


async def test_preset_ausente_pide_una_corrida_asistida_previa(tmp_path, sha_de_alpha209):
    instalacion = _instalacion(tmp_path)
    instalacion["preset_dyndolod"].unlink()
    runner = _runner(instalacion, _capacidad(None, tmp_path / "estado", AprobadorFalso()))
    resultado, _lanzar = await _correr_pipeline(runner)
    assert resultado.codigos_desatendido == ("PRESET_ABSENT",)
    assert "ASISTIDO" in resultado.errors[0]


async def test_ci_06_preset_de_calidad_distinto_del_pedido_corta(tmp_path, sha_de_alpha209):
    runner = _runner(_instalacion(tmp_path), _capacidad(None, tmp_path / "estado", AprobadorFalso()))
    resultado, _lanzar = await _correr_pipeline(runner, preset="High")
    assert resultado.codigos_desatendido == ("PRESET_MISMATCH",)
    assert "'Medium'" in resultado.errors[0]


@pytest.mark.parametrize(
    "decision",
    [ResultadoConfirmacion.DENEGADA, ResultadoConfirmacion.TIMEOUT, ResultadoConfirmacion.CANAL_NO_DISPONIBLE],
)
async def test_configuracion_no_aprobada_no_spawnea(tmp_path, sha_de_alpha209, decision):
    instalacion = _instalacion(tmp_path)
    aprobador = AprobadorFalso(configuracion=decision)
    runner = _runner(instalacion, _capacidad(None, tmp_path / "estado", aprobador))
    resultado, lanzar = await _correr_pipeline(runner)
    assert resultado.codigos_desatendido == ("UNATTENDED_CONFIGURATION_NOT_APPROVED",)
    lanzar.assert_not_awaited()
    # El operador vio las DOS herramientas con su build, su huella y su Output.
    (solicitud,) = aprobador.configuraciones
    assert [h.tool for h in solicitud.herramientas] == ["TexGen", "DynDOLOD"]
    assert solicitud.herramientas[1].preset_de_calidad == "Medium"
    assert solicitud.herramientas[0].exe_sha256 == CONTRATO_TEXGEN.exe_sha256
    assert all(h.output_esperado for h in solicitud.herramientas)


async def test_sin_texgen_la_aprobacion_cubre_solo_dyndolod(tmp_path, sha_de_alpha209):
    aprobador = AprobadorFalso(configuracion=ResultadoConfirmacion.DENEGADA)
    runner = _runner(_instalacion(tmp_path), _capacidad(None, tmp_path / "estado", aprobador))
    await _correr_pipeline(runner, run_texgen=False)
    assert [h.tool for h in aprobador.configuraciones[0].herramientas] == ["DynDOLOD"]


@pytest.mark.parametrize("modo", [ModoDeInteraccion.ASISTIDO, ModoDeInteraccion.DESATENDIDO])
async def test_un_residuo_de_preset_se_reconcilia_al_comienzo_de_cualquier_corrida(tmp_path, sha_de_alpha209, modo):
    instalacion = _instalacion(tmp_path)
    estado = tmp_path / "estado"
    GuardaDePresets(estado).proteger(
        instalacion["preset_texgen"], huella_aprobada=huella_de_configuracion(PRESET_TEXGEN)
    )
    instalacion["preset_texgen"].write_bytes(b"pisado por una corrida que murio")
    runner = _runner(
        instalacion, _capacidad(None, estado, AprobadorFalso(configuracion=ResultadoConfirmacion.DENEGADA))
    )
    interaccion = await runner._preparar_interaccion(modo, run_texgen=True, preset="Medium")  # noqa: SLF001
    assert instalacion["preset_texgen"].read_bytes() == PRESET_TEXGEN
    assert GuardaDePresets(estado).pendientes() == []
    if modo is ModoDeInteraccion.ASISTIDO:
        assert isinstance(interaccion, InteraccionDeCorrida)


async def test_aprobada_la_configuracion_la_interaccion_lleva_huellas_y_contratos(tmp_path, sha_de_alpha209):
    instalacion = _instalacion(tmp_path)
    runner = _runner(instalacion, _capacidad(None, tmp_path / "estado", AprobadorFalso()))
    interaccion = await runner._preparar_interaccion(  # noqa: SLF001
        ModoDeInteraccion.DESATENDIDO, run_texgen=True, preset="Medium"
    )
    assert isinstance(interaccion, InteraccionDeCorrida)
    assert set(interaccion.aprobadas) == {"TexGen", "DynDOLOD"}
    assert interaccion.aprobadas["DynDOLOD"].huella_preset == huella_de_configuracion(PRESET_DYNDOLOD)
    assert interaccion.aprobadas["TexGen"].contrato is CONTRATO_TEXGEN


# -----------------------------------------------------------------------------
# La aprobación remota se toma ANTES del lock (review CodeRabbit, PR #740)
# -----------------------------------------------------------------------------
#
# Esperar hasta 15 min la respuesta del operador con el lock de etapa 9 tomado y
# los mods de LOD previos apartados dejaba el juego sin LODs durante la espera, a
# la GUI bloqueada y, ante un "no", una TX PENDING por una corrida que nunca
# lanzó nada. La preparación SIN efectos (contrato, huellas, aprobación) va antes;
# la reconciliación de residuos sigue bajo el lock, porque restaura presets.


async def test_la_preparacion_previa_al_lock_no_reconcilia_residuos(tmp_path, sha_de_alpha209):
    instalacion = _instalacion(tmp_path)
    estado = tmp_path / "estado"
    GuardaDePresets(estado).proteger(
        instalacion["preset_texgen"], huella_aprobada=huella_de_configuracion(PRESET_TEXGEN)
    )
    runner = _runner(instalacion, _capacidad(None, estado, AprobadorFalso()))

    preparada = await runner.preparar_interaccion_desatendida(run_texgen=True, preset="Medium")

    assert isinstance(preparada, InteraccionDeCorrida)
    # Sin el lock, restaurar un preset podría pisar el de una corrida viva de otro proceso.
    assert len(GuardaDePresets(estado).pendientes()) == 1


async def test_una_interaccion_ya_aprobada_no_se_vuelve_a_preguntar(tmp_path, sha_de_alpha209):
    instalacion = _instalacion(tmp_path)
    estado = tmp_path / "estado"
    aprobador = AprobadorFalso()
    runner = _runner(instalacion, _capacidad(None, estado, aprobador))
    preparada = await runner.preparar_interaccion_desatendida(run_texgen=True, preset="Medium")
    GuardaDePresets(estado).proteger(
        instalacion["preset_dyndolod"], huella_aprobada=huella_de_configuracion(PRESET_DYNDOLOD)
    )

    interaccion = await runner._preparar_interaccion(  # noqa: SLF001
        ModoDeInteraccion.DESATENDIDO, run_texgen=True, preset="Medium", preparada=preparada
    )

    assert interaccion is preparada
    assert len(aprobador.configuraciones) == 1
    # Bajo el lock la reconciliación corre igual, con o sin interacción preparada.
    assert GuardaDePresets(estado).pendientes() == []


@pytest.mark.parametrize(
    ("modo", "modo_preparado"),
    [
        (ModoDeInteraccion.ASISTIDO, ModoDeInteraccion.DESATENDIDO),
        (ModoDeInteraccion.DESATENDIDO, ModoDeInteraccion.ASISTIDO),
    ],
    ids=["asistida_con_aprobacion", "desatendida_sin_aprobacion"],
)
async def test_una_interaccion_preparada_de_otro_modo_es_violacion_de_contrato(
    tmp_path, sha_de_alpha209, modo, modo_preparado
):
    runner = _runner(_instalacion(tmp_path), _capacidad(None, tmp_path / "estado", AprobadorFalso()))
    resultado = await runner._preparar_interaccion(  # noqa: SLF001
        modo, run_texgen=True, preset="Medium", preparada=InteraccionDeCorrida(modo=modo_preparado)
    )
    assert isinstance(resultado, DynDOLODPipelineResult)
    assert resultado.codigos_desatendido == ("UNATTENDED_CONTRACT_VIOLATION",)


def test_run_full_pipeline_reenvia_la_interaccion_preparada():
    pipeline = _metodo(_arbol(RUNNER), "run_full_pipeline")
    (preparacion,) = _llamadas_a(pipeline, "_preparar_interaccion")
    assert {kw.arg for kw in preparacion.keywords} >= {"preparada"}


# =============================================================================
# Anclas enumerantes (la regla del hermano)
# =============================================================================


def _arbol(ruta: pathlib.Path) -> ast.Module:
    return ast.parse(ruta.read_text(encoding="utf-8"))


def _metodo(arbol: ast.Module, nombre: str) -> ast.AST:
    return next(
        n for n in ast.walk(arbol) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef) and n.name == nombre
    )


def _llamadas_a(nodo: ast.AST, nombre: str) -> list[ast.Call]:
    return [
        n
        for n in ast.walk(nodo)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == nombre
    ]


def test_todo_spawn_recibe_la_interaccion_de_la_corrida():
    """Cada ``_execute_process`` del runner y cada lanzador de ``run_full_pipeline`` pasan ``interaccion=``."""
    arbol = _arbol(RUNNER)
    spawns = _llamadas_a(arbol, "_execute_process")
    assert len(spawns) == 2, "el detector tiene que ver los DOS lanzadores"
    for llamada in spawns:
        assert "interaccion" in {kw.arg for kw in llamada.keywords}
    pipeline = _metodo(arbol, "run_full_pipeline")
    lanzadores = _llamadas_a(pipeline, "run_texgen") + _llamadas_a(pipeline, "run_dyndolod")
    assert len(lanzadores) == 2
    for llamada in lanzadores:
        assert "interaccion" in {kw.arg for kw in llamada.keywords}


def test_todo_retorno_del_servicio_declara_el_modo_de_interaccion():
    llamadas = [
        n
        for n in ast.walk(_arbol(SERVICIO))
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "_attach_preflight"
    ]
    # 13: el corte desatendido previo al lock (#740) es un retorno más.
    assert len(llamadas) == 13, "el detector tiene que ver todos los retornos del servicio"
    for llamada in llamadas:
        assert len(llamada.args) == 3, f"_attach_preflight sin modo en la línea {llamada.lineno}"


def test_el_composition_root_cablea_la_capacidad_desatendida_productiva():
    arbol = _arbol(COMPOSICION)
    servicio = next(
        n
        for n in ast.walk(arbol)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "DynDOLODPipelineService"
    )
    assert "desatendido" in {kw.arg for kw in servicio.keywords}
    capacidad = next(
        n
        for n in ast.walk(arbol)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "CapacidadDesatendidaUIA"
    )
    nombres = {n.id for n in ast.walk(capacidad) if isinstance(n, ast.Name)} | {
        n.attr for n in ast.walk(capacidad) if isinstance(n, ast.Attribute)
    }
    # El ejecutor PRODUCTIVO (helper descartable), nunca el de tests en proceso.
    assert "EjecutorDePasoPorHelper" in nombres
    assert "EjecutorDePasoEnProceso" not in nombres
    assert "AprobadorDesatendidoHITL" in nombres
    assert "runtime_state_dir" in nombres


def test_el_servicio_pasa_la_capacidad_desatendida_al_runner():
    construir = _metodo(_arbol(SERVICIO), "_ensure_runner")
    runner = next(
        n
        for n in ast.walk(construir)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "DynDOLODRunner"
    )
    assert "desatendido" in {kw.arg for kw in runner.keywords}


# =============================================================================
# Servicio: el modo es un parámetro explícito del contrato público
# =============================================================================

from unittest.mock import AsyncMock  # noqa: E402

from tests.test_dyndolod_service import _make_success_result, _mock_config  # noqa: E402


@pytest.fixture
def bus():
    from sky_claw.app.core.event_bus import CoreEventBus

    evento = AsyncMock(spec=CoreEventBus)
    evento.publish = AsyncMock()
    return evento


@pytest.fixture
def servicio(bus):
    """Servicio con dobles mínimos: el mismo armado que ``tests/test_dyndolod_service.py``."""
    import time

    from sky_claw.app.db.locks import DistributedLockManager, LockInfo
    from sky_claw.app.db.snapshot_manager import FileSnapshotManager
    from sky_claw.local.tools.dyndolod_service import DynDOLODPipelineService

    locks = AsyncMock(spec=DistributedLockManager)
    info = LockInfo(
        resource_id="dyndolod-pipeline",
        agent_id="dyndolod-pipeline-service",
        acquired_at=time.time(),
        expires_at=time.time() + 3600.0,
    )
    locks.acquire_lock = AsyncMock(return_value=info)
    locks.get_lock_info = AsyncMock(return_value=info)
    locks.release_lock = AsyncMock(return_value=True)
    journal = AsyncMock()
    journal.begin_transaction = AsyncMock(return_value=42)
    journal.consultar_handoff_activo = AsyncMock(return_value=None)
    journal.transacciones_que_nombran = AsyncMock(return_value=[])
    resolver = MagicMock()
    for metodo in (
        "get_skyrim_path",
        "get_mo2_path",
        "get_mo2_instance_data_root",
        "get_mo2_mods_path",
        "get_dyndolod_exe",
        "get_texgen_exe",
    ):
        setattr(resolver, metodo, MagicMock(return_value=None))
    return DynDOLODPipelineService(
        lock_manager=locks,
        snapshot_manager=AsyncMock(spec=FileSnapshotManager),
        journal=journal,
        path_resolver=resolver,
        event_bus=bus,
        mo2_profile="Default",
    )


def _servicio_con_runner(servicio, tmp_path, resultado):
    runner = AsyncMock(spec=DynDOLODRunner)
    runner.run_full_pipeline = AsyncMock(return_value=resultado)
    runner.preparar_interaccion_desatendida = AsyncMock(
        return_value=InteraccionDeCorrida(modo=ModoDeInteraccion.DESATENDIDO)
    )
    runner.validate_dyndolod_output = AsyncMock(return_value=True)
    config = _mock_config(tmp_path)
    config.mo2_mods_path = tmp_path / "mods"
    (config.mo2_mods_path / "DynDOLOD Output").mkdir(parents=True)
    runner._config = config
    servicio._runner = runner
    return runner


async def test_un_corte_desatendido_previo_no_toma_el_lock_ni_aparta_los_lods(servicio, bus, tmp_path):
    """Un "no" del operador (o un build sin contrato) no muta nada ni deja TX PENDING."""
    runner = _servicio_con_runner(servicio, tmp_path, _make_success_result())
    corte = DynDOLODPipelineResult(
        success=False,
        texgen_result=None,
        dyndolod_result=None,
        errors=["UNATTENDED_CONFIGURATION_NOT_APPROVED: el operador no aprobó la configuración desatendida: denegada"],
        codigos_desatendido=("UNATTENDED_CONFIGURATION_NOT_APPROVED",),
    )
    runner.preparar_interaccion_desatendida = AsyncMock(return_value=corte)

    resultado = await servicio.execute(preset="Medium", create_snapshot=True, interaction_mode="desatendido")

    assert resultado["success"] is False
    assert resultado["unattended_error_codes"] == ["UNATTENDED_CONFIGURATION_NOT_APPROVED"]
    assert resultado["interaction_mode"] == "desatendido"
    servicio._lock_manager.acquire_lock.assert_not_awaited()
    servicio._journal.begin_transaction.assert_not_awaited()
    runner.run_full_pipeline.assert_not_awaited()
    # El mod de LODs previo sigue donde estaba: no hubo move-aside.
    assert (runner._config.mo2_mods_path / "DynDOLOD Output").is_dir()
    # El ciclo de eventos se cierra igual: started → terminal.
    temas = [llamada.args[0].topic for llamada in bus.publish.call_args_list]
    assert temas[0] == "pipeline.dyndolod.started"
    assert temas[-1] != "pipeline.dyndolod.started"


async def test_la_aprobacion_desatendida_precede_al_lock_y_viaja_al_runner(servicio, tmp_path):
    runner = _servicio_con_runner(servicio, tmp_path, _make_success_result())
    orden: list[str] = []
    preparada = InteraccionDeCorrida(modo=ModoDeInteraccion.DESATENDIDO)

    async def _preparar(**_kwargs):
        orden.append("aprobacion")
        return preparada

    adquirir = servicio._lock_manager.acquire_lock.side_effect
    info = servicio._lock_manager.acquire_lock.return_value

    async def _adquirir(*args, **kwargs):
        orden.append("lock")
        return info if adquirir is None else await adquirir(*args, **kwargs)

    runner.preparar_interaccion_desatendida = AsyncMock(side_effect=_preparar)
    servicio._lock_manager.acquire_lock = AsyncMock(side_effect=_adquirir)

    resultado = await servicio.execute(preset="Medium", create_snapshot=False, interaction_mode="desatendido")

    assert resultado["success"] is True
    assert orden[:2] == ["aprobacion", "lock"]
    runner.preparar_interaccion_desatendida.assert_awaited_once_with(run_texgen=True, preset="Medium")
    assert runner.run_full_pipeline.await_args.kwargs["interaccion"] is preparada


async def test_en_modo_asistido_no_hay_preparacion_desatendida(servicio, tmp_path):
    runner = _servicio_con_runner(servicio, tmp_path, _make_success_result())
    await servicio.execute(preset="Medium", create_snapshot=False)
    runner.preparar_interaccion_desatendida.assert_not_awaited()
    assert runner.run_full_pipeline.await_args.kwargs["interaccion"] is None


@pytest.mark.parametrize("modo", ["unattended", "DESATENDIDO", "auto", 1])
async def test_el_servicio_rechaza_un_modo_desconocido_sin_efectos(servicio, bus, tmp_path, modo):
    runner = _servicio_con_runner(servicio, tmp_path, _make_success_result())
    resultado = await servicio.execute(preset="Medium", interaction_mode=modo)
    assert resultado["success"] is False
    assert resultado["message"]
    # Sin modo válido no hubo corrida: nadie tiene que operar una ventana, así que
    # el resultado no puede decir ASISTIDO (revisión Qodo, PR #740).
    assert resultado["assisted"] is False
    assert resultado["interaction_mode"] is None
    runner.run_full_pipeline.assert_not_awaited()
    bus.publish.assert_not_awaited()


async def test_el_servicio_pasa_el_modo_al_runner_y_lo_publica(servicio, bus, tmp_path):
    runner = _servicio_con_runner(servicio, tmp_path, _make_success_result())
    resultado = await servicio.execute(preset="Medium", create_snapshot=False, interaction_mode="desatendido")
    assert resultado["success"] is True
    assert resultado["assisted"] is False
    assert resultado["interaction_mode"] == "desatendido"
    assert runner.run_full_pipeline.await_args.kwargs["modo_de_interaccion"] is ModoDeInteraccion.DESATENDIDO
    iniciado = bus.publish.call_args_list[0][0][0]
    assert iniciado.topic == "pipeline.dyndolod.started"
    assert iniciado.payload["assisted"] is False
    assert iniciado.payload["interaction_mode"] == "desatendido"
    assert "desatendida" in iniciado.payload["operator_instructions"]


async def test_el_default_del_servicio_sigue_siendo_asistido(servicio, bus, tmp_path):
    runner = _servicio_con_runner(servicio, tmp_path, _make_success_result())
    resultado = await servicio.execute(preset="Medium", create_snapshot=False)
    assert resultado["assisted"] is True
    assert resultado["interaction_mode"] == "asistido"
    assert runner.run_full_pipeline.await_args.kwargs["modo_de_interaccion"] is ModoDeInteraccion.ASISTIDO
    assert bus.publish.call_args_list[0][0][0].payload["assisted"] is True


async def test_un_fallo_desatendido_expone_sus_codigos_machine_readable(servicio, tmp_path):
    fallo = DynDOLODPipelineResult(
        success=False,
        texgen_result=None,
        dyndolod_result=None,
        errors=["TexGen execution failed: TexGen: BEGIN_OUTCOME_UNKNOWN: helper colgado; no se reintenta"],
        codigos_desatendido=("BEGIN_OUTCOME_UNKNOWN",),
    )
    _servicio_con_runner(servicio, tmp_path, fallo)
    resultado = await servicio.execute(preset="Medium", create_snapshot=False, interaction_mode="desatendido")
    assert resultado["success"] is False
    assert resultado["unattended_error_codes"] == ["BEGIN_OUTCOME_UNKNOWN"]
    assert resultado["interaction_mode"] == "desatendido"


# =============================================================================
# #661 × #739: el gate de procedencia de datos sobrevive al modo desatendido
# =============================================================================


@pytest.mark.parametrize("tool", ["TexGen", "DynDOLOD"])
async def test_en_modo_desatendido_un_data_path_desviado_tampoco_es_exito(tmp_path: pathlib.Path, tool: str) -> None:
    """El modo desatendido cambia CÓMO se conduce la GUI, nunca QUÉ se acepta.

    El quinto conjunct de éxito (``procedencia_de_datos_es_confiable``, #739) vive
    en los lanzadores, DESPUÉS de ``_execute_process``: una corrida desatendida con
    el artefacto presente y un ``-d:`` declarado distinto del administrado tiene
    que terminar en fallo igual que una asistida. El fake verifica además que la
    corrida viajó como DESATENDIDA, así que el test no puede pasar por el camino
    asistido.
    """
    from tests.test_dyndolod_evidencia_de_rutas import DATA_DESVIADA, _log_con_eco, _raiz_de_salida
    from tests.test_dyndolod_service import (
        _corrida_que_completa,
        _EjecucionFalsa,
        _escribir_salida,
        _runner_texgen,
    )

    config, runner = _runner_texgen(tmp_path)
    assert config.data_dir is not None
    raiz = _raiz_de_salida(config, tool)
    if tool == "TexGen":
        destino, artefacto = raiz / DynDOLODRunner.TEXGEN_OUTPUT_NAME, "a.dds"
    else:
        destino, artefacto = raiz / DynDOLODRunner.DYNDOLLOD_OUTPUT_NAME, "DynDOLOD.esp"
    log = _log_con_eco(
        f"Using Output Path: {raiz}\\",
        f"Using Skyrim Special Edition Data Path: {DATA_DESVIADA}",
        tool=tool,
    )
    modos_vistos: list[object] = []
    base = _EjecucionFalsa(
        return_code=0,
        al_ejecutar=_corrida_que_completa(tmp_path, tool, lambda: _escribir_salida(destino, artefacto), log=log),
    )

    async def _fake(*args: object, **kwargs: object) -> tuple[str, str, int, float]:
        interaccion = kwargs.get("interaccion")
        modos_vistos.append(getattr(interaccion, "modo", None))
        return await base(*args, **kwargs)

    interaccion = InteraccionDeCorrida(modo=ModoDeInteraccion.DESATENDIDO)
    with mock.patch.object(runner, "_execute_process", _fake):
        if tool == "TexGen":
            result = await runner.run_texgen(interaccion=interaccion)
        else:
            result = await runner.run_dyndolod(preset="Medium", interaccion=interaccion)

    assert modos_vistos == [ModoDeInteraccion.DESATENDIDO]
    assert result.success is False, "un -d: desviado no puede ser éxito tampoco en modo desatendido"
    assert DATA_DESVIADA in " ".join(result.errors)


def test_interaccion_de_corrida_al_acumular_codigos_no_se_declara_frozen() -> None:
    """``InteraccionDeCorrida.codigos`` ACUMULA los cortes de la corrida: no es frozen.

    Declararla frozen con una lista que se muta era un contrato mentiroso: el
    ``__hash__`` generado fallaría con la lista y cualquier lector asumiría que no
    cambia (revisión Qodo, PR #740).
    """
    import dataclasses

    interaccion = InteraccionDeCorrida(modo=ModoDeInteraccion.DESATENDIDO)
    interaccion.codigos.append(CodigoDesatendido.UIA_UNAVAILABLE)
    # Un frozen levantaría FrozenInstanceError acá: el contrato declarado es mutable.
    interaccion.modo = ModoDeInteraccion.DESATENDIDO
    assert interaccion.codigos == [CodigoDesatendido.UIA_UNAVAILABLE]
    assert [f.name for f in dataclasses.fields(InteraccionDeCorrida)] == ["modo", "aprobadas", "codigos"]


@pytest.mark.parametrize("modo", ["desatendido", "asistido"])
async def test_generate_lods_strategy_execute_con_cada_modo_lo_reenvia_al_servicio(modo: str) -> None:
    """Paridad de COMPORTAMIENTO, no sólo de claves: el dispatcher (GUI) pasa
    ``interaction_mode`` al MISMO ``execute`` del servicio (revisión Qodo, PR #740)."""
    from unittest.mock import AsyncMock

    from sky_claw.app.orchestrator.tool_strategies.generate_lods import GenerateLodsStrategy
    from sky_claw.local.validators.etapas_previas_lods import EvidenciaDeEtapasPrevias

    servicio = MagicMock()
    servicio.execute = AsyncMock(return_value={"success": True, "message": ""})
    # Etapas previas completas: el guard del modo desatendido (SOP §5.2) deja pasar.
    servicio.verificar_etapas_previas = MagicMock(return_value=EvidenciaDeEtapasPrevias(faltantes=()))
    await GenerateLodsStrategy(servicio).execute({"preset": "High", "interaction_mode": modo})
    servicio.execute.assert_awaited_once_with(preset="High", interaction_mode=modo)
