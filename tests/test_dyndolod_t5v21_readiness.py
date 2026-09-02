"""T5-v2.1 — readyness humana + final UIA gate sobre la MISMA instancia.

**Qué cierra este archivo y qué no.** Cierra el TOCTOU que el rig real de
Windows de PR #528 descubrió: entre el initial MATCH y el click humano en
Start, el operador puede cambiar el preset a uno cuyo Output apunte a una
ruta TRAP; sin el protocolo, DynDOLOD ya pasó el initial gate, así que el
runner no tenía manera de detectarlo antes del post-check. La solución NO
automatiza Start; sigue siendo humana, pero el runner vuelve a observar el
Output de ESA MISMA instancia después de que el operador declara
explícitamente que terminó de configurar (vía :class:`ConfirmadorDeConfiguracion`),
y antes de habilitarlo a pulsar Start.

El oracle principal es ``test_r4b_preseteo_trampa_despues_de_initial_match``:
representa el bug del rig. Los demás tests cubren las propiedades que el
seam del delta tiene que sostener sin romper el resto del camino.

**Cubre ambos binarios.** TexGen y DynDOLOD cruzan el MISMO protocolo
(§20, M10 invertido): un bypass para uno solo se prueba en
``test_m10_texgen_bypass_red``.

**Helpers duplicados con la suite T5-v2 a propósito.** El repo no tiene
un ``conftest`` que exporte dobles de UIA; las dos suites T5-v2 y T5-v2.1
viven independientes y sus dobles son MINIMALES. Duplicarlos acá (en vez
de importar entre archivos de tests) preserva la propiedad "si T5-v2
cambia un doble, T5-v2.1 puede romper a propósito".
"""

from __future__ import annotations

import ast
import asyncio
import pathlib
from unittest.mock import AsyncMock, patch

import pytest

from sky_claw.local.tools import dyndolod_runner as ddl
from sky_claw.local.tools.dyndolod_uia_gate import (
    OperatorConfigurationReadyRequest,
    ResultadoConfirmacion,
)
from sky_claw.local.tools.dyndolod_uia_preflight import (
    ControlObservado,
    EstadoPreflight,
    ProcesoObservado,
    RazonPreflight,
    VentanaObservada,
)

# ---------------------------------------------------------------------------
# Constantes y dobles locales (replican los de T5-v2 sin acoplar archivos)
# ---------------------------------------------------------------------------


PID = 4242
SALIDA_ADMINISTRADA_TXT = r"C:\Games\Skyrim Special Edition\Sky-Claw\DynDOLOD"
TRAP = r"D:\trap\dyndolod_output"
MODULO_RUNNER = pathlib.Path(ddl.__file__)


class _EOFStream:
    async def read(self, _n: int) -> bytes:
        return b""


class _RelojFalso:
    def __init__(self) -> None:
        self._t = 0.0
        self.suenos: list[float] = []

    def __call__(self) -> float:
        return self._t

    def dormir(self, delta: float) -> None:
        self.suenos.append(delta)
        self._t += delta


class _LocalizadorFalso:
    def __init__(self, procesos) -> None:
        self._procesos = tuple(procesos)

    def procesos(self):
        return self._procesos


class _Ronda:
    def __init__(self, ventanas=None, controles=None, valores=None) -> None:
        self.ventanas = tuple(ventanas or ())
        self.controles = controles or {}
        self.valores = valores or {}


class _ObservadorPorRonda:
    """Observador falso que entrega una :class:`_Ronda` por gate."""

    def __init__(self, rondas) -> None:
        assert rondas, "un observador sin rondas no modela nada"
        self._rondas = list(rondas)
        self._indice = 0
        self.observaciones = 0

    def _actual(self) -> _Ronda:
        return self._rondas[min(self._indice, len(self._rondas) - 1)]

    def ventanas_de_proceso(self, pid: int):
        self.observaciones += 1
        self._indice = max(self._indice, self.observaciones - 1)
        return tuple(v for v in self._actual().ventanas if v.pid == pid)

    def controles_de_ventana(self, ventana: VentanaObservada):
        return tuple(self._actual().controles.get(ventana.handle, ()))

    def leer_valor(self, control: ControlObservado):
        valores = self._actual().valores
        compuesta = (control.automation_id, control.nombre, control.tipo_de_control, control.pid)
        return valores.get(compuesta)


class _ProcesoFalso:
    def __init__(self, pid: int = PID) -> None:
        self.pid = pid
        self.returncode: int | None = None
        self.stdout = _EOFStream()
        self.stderr = _EOFStream()
        self.esperas = 0
        self.muertes = 0

    async def wait(self) -> int:
        self.esperas += 1
        self.returncode = 0
        return 0

    def kill(self) -> None:
        self.muertes += 1
        self.returncode = -9


def _ventana(pid: int = PID, handle: object = "w1") -> VentanaObservada:
    return VentanaObservada(pid=pid, titulo="DynDOLOD 3.0 x64 - SSE", class_name="TfrmMain", handle=handle)


def _control_output(pid: int = PID) -> ControlObservado:
    return ControlObservado(
        pid=pid,
        automation_id="1",
        nombre="",
        tipo_de_control="Edit",
        class_name="TEdit",
    )


def _ronda_output(valor: str, pid: int = PID) -> _Ronda:
    control = _control_output(pid)
    return _Ronda(
        ventanas=[_ventana(pid)],
        controles={"w1": [control]},
        valores={(control.automation_id, control.nombre, control.tipo_de_control, control.pid): valor},
    )


def _ronda_dos_candidatos(pid: int = PID) -> _Ronda:
    return _Ronda(
        ventanas=[_ventana(pid)],
        controles={
            "w1": [
                ControlObservado(pid=pid, automation_id="a", nombre="", tipo_de_control="Edit", class_name="TEdit"),
                ControlObservado(pid=pid, automation_id="b", nombre="", tipo_de_control="Edit", class_name="TEdit"),
            ]
        },
    )


def _proceso_falso_del_rig(ejecutable: str) -> ProcesoObservado:
    return ProcesoObservado(
        pid=PID, nombre_ejecutable=pathlib.PureWindowsPath(ejecutable).name, ruta_ejecutable=ejecutable
    )


def _config(tmp_path: pathlib.Path, *, timeout: float = 5.0):
    juego = tmp_path / "juego"
    juego.mkdir(exist_ok=True)
    exe = tmp_path / "DynDOLODx64.exe"
    exe.write_bytes(b"pe")
    mo2 = tmp_path / "mo2"
    mo2.mkdir(exist_ok=True)
    mods = tmp_path / "mods"
    mods.mkdir(exist_ok=True)
    return ddl.DynDOLODConfig(
        game_path=juego,
        mo2_path=mo2,
        mo2_mods_path=mods,
        dyndolod_exe=exe,
        output_root=pathlib.PureWindowsPath(SALIDA_ADMINISTRADA_TXT),
        uia_gate_timeout_seconds=timeout,
        uia_gate_poll_interval_seconds=1.0,
        uia_final_gate_timeout_seconds=timeout,
        uia_final_gate_poll_interval_seconds=1.0,
        readiness_timeout_seconds=600.0,
    )


class _ConfirmadorFalso:
    """Adaptador mínimo del puerto :class:`ConfirmadorDeConfiguracion`."""

    def __init__(self, resultado: ResultadoConfirmacion | None = None) -> None:
        self.resultado: ResultadoConfirmacion = resultado if resultado is not None else ResultadoConfirmacion.APROBADA
        self.informes: list[tuple[str, str]] = []
        self.solicitudes: list[OperatorConfigurationReadyRequest] = []

    async def confirmar(self, solicitud: OperatorConfigurationReadyRequest) -> ResultadoConfirmacion:
        self.solicitudes.append(solicitud)
        return self.resultado

    async def informar(self, *, tool: str, mensaje: str) -> None:
        self.informes.append((tool, mensaje))


def _preparar_corrida(
    tmp_path: pathlib.Path,
    *,
    rondas,
    ejecutable: str,
    timeout: float = 5.0,
    readiness: bool = True,
    confirmador: object | None = None,
    asyncio_sleep: object | None = None,
    resultado_confirmacion: ResultadoConfirmacion | None = None,
):
    config = _config(tmp_path, timeout=timeout)
    reloj = _RelojFalso()
    observador = _ObservadorPorRonda(rondas)
    if confirmador is not None:
        confirmador_inyectado = confirmador
    elif readiness:
        confirmador_inyectado = _ConfirmadorFalso(resultado=resultado_confirmacion)
    else:
        confirmador_inyectado = None
    runner = ddl.DynDOLODRunner(
        config,
        uia_localizador=_LocalizadorFalso([_proceso_falso_del_rig(ejecutable)]),
        uia_observador_factory=lambda: observador,
        uia_reloj=reloj,
        uia_dormir=reloj.dormir,
        asyncio_sleep=asyncio_sleep if asyncio_sleep is not None else asyncio.sleep,
        confirmador_de_configuracion=confirmador_inyectado,
    )
    proc = _ProcesoFalso()
    return runner, proc, observador


def _spawn_de(proc):
    return proc


def _espías_de_ownership(proc: _ProcesoFalso | None = None):
    if proc is None:
        return {
            "assign_kill_on_close_job": MagicMock(return_value=4242),
            "kill_and_reap": AsyncMock(),
            "close_job": MagicMock(),
        }

    def _mata(p):
        p.kill()

    return {
        "assign_kill_on_close_job": MagicMock(return_value=4242),
        "kill_and_reap": AsyncMock(side_effect=_mata),
        "close_job": MagicMock(),
    }


class _PilaDeOwnership:
    def __init__(self, proc: _ProcesoFalso | None = None) -> None:
        self.mocks = _espías_de_ownership(proc=proc)
        self._cms = [patch.object(ddl, nombre, mock) for nombre, mock in self.mocks.items()]

    def __enter__(self):
        for cm in self._cms:
            cm.__enter__()
        return self

    def __exit__(self, *exc_info):
        for cm in reversed(self._cms):
            cm.__exit__(*exc_info)
        return False


# Local import after top-level to keep structure clean.
from unittest.mock import MagicMock  # noqa: E402

# ---------------------------------------------------------------------------
# Anclas AST
# ---------------------------------------------------------------------------


def _funcion_del_runner(nombre: str):
    arbol = ast.parse(MODULO_RUNNER.read_text(encoding="utf-8"))
    for nodo in ast.walk(arbol):
        if isinstance(nodo, (ast.FunctionDef, ast.AsyncFunctionDef)) and nodo.name == nombre:
            return nodo
    raise AssertionError(f"{MODULO_RUNNER.name} ya no define {nombre!r}: revisá estas anclas")


def _lineas_de_llamadas_a_en(nombre_metodo: str, funcion: str) -> list[int]:
    seam = _funcion_del_runner(funcion)
    return sorted(
        nodo.lineno
        for nodo in ast.walk(seam)
        if isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Attribute) and nodo.func.attr == nombre_metodo
    )


# ===========================================================================
# R4b — bug del rig (oracle principal del fix)
# ===========================================================================


async def test_r4b_preseteo_trampa_despues_de_initial_match(tmp_path):
    """R4b — initial MATCH, humano cambia preset, ready, final MISMATCH → cleanup.

    Reproduce el TOCTOU del rig Windows de PR #528: el initial gate vio el
    Output managed; el operador cargó un preset TRAP; el operador apretó
    «Configuración lista»; el runner volvió a observar y vio TRAP. La
    propiedad: el proceso se TERMINA, el árbol queda limpio,
    ``DynDOLODPreflightUIAError`` con razón ``OUTPUT_DIFIERE`` se reporta,
    y ``wait`` NUNCA se llamó.
    """
    ejecutable = r"C:\Modding\DynDOLOD\DynDOLODx64.exe"
    runner, proc, _ = _preparar_corrida(
        tmp_path,
        rondas=[
            _ronda_output(SALIDA_ADMINISTRADA_TXT, pid=PID),  # initial MATCH
            _ronda_output(TRAP, pid=PID),  # final MISMATCH
        ],
        ejecutable=ejecutable,
    )
    with (
        patch.object(ddl.asyncio, "create_subprocess_exec", AsyncMock(side_effect=lambda *a, **k: _spawn_de(proc))),
        _PilaDeOwnership(proc=proc) as pila,
        pytest.raises(ddl.DynDOLODPreflightUIAError) as excinfo,
    ):
        await runner._execute_process(pathlib.Path(ejecutable), [], "DynDOLOD")

    resultado = excinfo.value.resultado
    assert resultado.estado is EstadoPreflight.MISMATCH
    assert resultado.razon is RazonPreflight.OUTPUT_DIFIERE
    assert resultado.pid == PID
    assert resultado.tool == "DynDOLOD"
    assert proc.esperas == 0, "el cambio de preset antes del ready NO debe alcanzar process.wait"
    assert proc.muertes == 1
    pila.mocks["kill_and_reap"].assert_awaited_once_with(proc)
    pila.mocks["close_job"].assert_called_once_with(4242)


# ===========================================================================
# H1..H11 — contratos del handshake
# ===========================================================================


async def test_h1_dyndolod_sin_confirmador_es_fail_closed(tmp_path):
    """H1 — sin confirmador, default = CANAL_NO_DISPONIBLE; la corrida NO continúa."""
    ejecutable = r"C:\Modding\DynDOLOD\DynDOLODx64.exe"
    runner, proc, _ = _preparar_corrida(
        tmp_path,
        rondas=[_ronda_output(SALIDA_ADMINISTRADA_TXT, pid=PID)],
        ejecutable=ejecutable,
        readiness=False,  # confirmador = None
    )
    with (
        patch.object(ddl.asyncio, "create_subprocess_exec", AsyncMock(side_effect=lambda *a, **k: _spawn_de(proc))),
        _PilaDeOwnership(proc=proc) as pila,
        pytest.raises(ddl.DynDOLODReadinessProtocolError) as excinfo,
    ):
        await runner._execute_process(pathlib.Path(ejecutable), [], "DynDOLOD")

    assert excinfo.value.razon_protocolo.value == "canal_no_disponible"
    assert proc.esperas == 0
    assert proc.muertes == 1
    pila.mocks["kill_and_reap"].assert_awaited_once_with(proc)


async def test_h2_texgen_tambien_cruza_el_protocolo(tmp_path):
    """H2 — TexGen NO es un bypass: sin confirmador, CANAL_NO_DISPONIBLE igual.

    Invirtiendo el M10 anterior: si el runner se construye con TexGen y sin
    confirmador, la corrida debe cortar con ``CANAL_NO_DISPONIBLE``. Es la
    propiedad de «seam único, ambos binarios» del delta.
    """
    ejecutable = r"C:\Modding\DynDOLOD\TexGenx64.exe"
    runner, proc, _ = _preparar_corrida(
        tmp_path,
        rondas=[_ronda_output(SALIDA_ADMINISTRADA_TXT, pid=PID)],
        ejecutable=ejecutable,
        readiness=False,  # confirmador = None; TexGen igual debe cortar
    )
    with (
        patch.object(ddl.asyncio, "create_subprocess_exec", AsyncMock(side_effect=lambda *a, **k: _spawn_de(proc))),
        _PilaDeOwnership(proc=proc) as pila,
        pytest.raises(ddl.DynDOLODReadinessProtocolError) as excinfo,
    ):
        await runner._execute_process(pathlib.Path(ejecutable), [], "TexGen")

    assert excinfo.value.razon_protocolo.value == "canal_no_disponible"
    assert proc.esperas == 0
    pila.mocks["kill_and_reap"].assert_awaited_once_with(proc)


async def test_h3_dyndolod_final_match_continua_exactamente_una_vez(tmp_path):
    """H3 — initial MATCH, ready, final MATCH: la corrida avanza exactamente una vez."""
    ejecutable = r"C:\Modding\DynDOLOD\DynDOLODx64.exe"
    runner, proc, observador = _preparar_corrida(
        tmp_path,
        rondas=[
            _ronda_output(SALIDA_ADMINISTRADA_TXT, pid=PID),
            _ronda_output(SALIDA_ADMINISTRADA_TXT, pid=PID),
        ],
        ejecutable=ejecutable,
    )
    with (
        patch.object(ddl.asyncio, "create_subprocess_exec", AsyncMock(side_effect=lambda *a, **k: _spawn_de(proc))),
        _PilaDeOwnership() as pila,
    ):
        await runner._execute_process(pathlib.Path(ejecutable), [], "DynDOLOD")

    assert proc.esperas == 1
    assert proc.muertes == 0
    assert observador.observaciones == 2  # uno por gate, ni uno más
    pila.mocks["kill_and_reap"].assert_not_awaited()
    pila.mocks["close_job"].assert_called_once_with(4242)


async def test_h4_dyndolod_final_mismatch_falla_cerrado_y_limpia(tmp_path):
    """H4 — final MISMATCH: mismo fail-closed que el initial, mismo cleanup."""
    ejecutable = r"C:\Modding\DynDOLOD\DynDOLODx64.exe"
    runner, proc, _ = _preparar_corrida(
        tmp_path,
        rondas=[
            _ronda_output(SALIDA_ADMINISTRADA_TXT, pid=PID),
            _ronda_output(r"D:\otra\cosa", pid=PID),
        ],
        ejecutable=ejecutable,
    )
    with (
        patch.object(ddl.asyncio, "create_subprocess_exec", AsyncMock(side_effect=lambda *a, **k: _spawn_de(proc))),
        _PilaDeOwnership(proc=proc) as pila,
        pytest.raises(ddl.DynDOLODPreflightUIAError) as excinfo,
    ):
        await runner._execute_process(pathlib.Path(ejecutable), [], "DynDOLOD")

    assert excinfo.value.resultado.estado is EstadoPreflight.MISMATCH
    assert excinfo.value.resultado.razon is RazonPreflight.OUTPUT_DIFIERE
    assert proc.esperas == 0
    pila.mocks["kill_and_reap"].assert_awaited_once_with(proc)
    pila.mocks["close_job"].assert_called_once_with(4242)


async def test_h5_dyndolod_final_unknown_falla_cerrado_y_limpia(tmp_path):
    """H5 — final UNKNOWN (dos candidatos): fail-closed + cleanup."""
    ejecutable = r"C:\Modding\DynDOLOD\DynDOLODx64.exe"
    runner, proc, _ = _preparar_corrida(
        tmp_path,
        rondas=[
            _ronda_output(SALIDA_ADMINISTRADA_TXT, pid=PID),
            _ronda_dos_candidatos(pid=PID),
        ],
        ejecutable=ejecutable,
    )
    with (
        patch.object(ddl.asyncio, "create_subprocess_exec", AsyncMock(side_effect=lambda *a, **k: _spawn_de(proc))),
        _PilaDeOwnership(proc=proc) as pila,
        pytest.raises(ddl.DynDOLODPreflightUIAError) as excinfo,
    ):
        await runner._execute_process(pathlib.Path(ejecutable), [], "DynDOLOD")

    assert excinfo.value.resultado.estado is EstadoPreflight.UNKNOWN
    assert proc.esperas == 0
    pila.mocks["kill_and_reap"].assert_awaited_once_with(proc)


async def test_h6_dyndolod_ready_deny_falla_cerrado_y_limpia(tmp_path):
    """H6 — el operador DENEGA: DENEGADA + cleanup; el final gate NO se invoca."""
    ejecutable = r"C:\Modding\DynDOLOD\DynDOLODx64.exe"
    runner, proc, observador = _preparar_corrida(
        tmp_path,
        rondas=[_ronda_output(SALIDA_ADMINISTRADA_TXT, pid=PID)],
        ejecutable=ejecutable,
        resultado_confirmacion=ResultadoConfirmacion.DENEGADA,
    )
    with (
        patch.object(ddl.asyncio, "create_subprocess_exec", AsyncMock(side_effect=lambda *a, **k: _spawn_de(proc))),
        _PilaDeOwnership(proc=proc) as pila,
        pytest.raises(ddl.DynDOLODReadinessProtocolError) as excinfo,
    ):
        await runner._execute_process(pathlib.Path(ejecutable), [], "DynDOLOD")

    assert excinfo.value.razon_protocolo.value == "denegada"
    assert proc.esperas == 0
    assert proc.muertes == 1
    # Initial MATCH consumió una observación; el final gate NO corrió.
    assert observador.observaciones == 1
    pila.mocks["kill_and_reap"].assert_awaited_once_with(proc)


async def test_h7_dyndolod_ready_timeout_falla_cerrado_y_limpia(tmp_path):
    """H7 — el callable nunca devuelve: TIMEOUT + cleanup."""
    ejecutable = r"C:\Modding\DynDOLOD\DynDOLODx64.exe"

    async def _nunca_devuelve(_request: OperatorConfigurationReadyRequest) -> ResultadoConfirmacion:
        await asyncio.Event().wait()  # cuelga para siempre
        return ResultadoConfirmacion.APROBADA  # pragma: no cover

    runner, proc, observador = _preparar_corrida(
        tmp_path,
        rondas=[_ronda_output(SALIDA_ADMINISTRADA_TXT, pid=PID)],
        ejecutable=ejecutable,
        confirmador=_ConfirmadorFalso() if False else None,  # noqa: unused - usamos uno custom abajo
    )
    # Inyectar manualmente un confirmador que cuelga.
    object.__setattr__(
        runner,
        "_confirmador_de_configuracion",
        _ConfirmadorFalso(),
    )

    # Reemplazar confirmar por uno que cuelga
    async def _colgado(_request: OperatorConfigurationReadyRequest) -> ResultadoConfirmacion:
        await asyncio.Event().wait()
        return ResultadoConfirmacion.APROBADA  # pragma: no cover

    object.__setattr__(runner._confirmador_de_configuracion, "confirmar", _colgado)
    object.__setattr__(runner._config, "readiness_timeout_seconds", 0.2)

    with (
        patch.object(ddl.asyncio, "create_subprocess_exec", AsyncMock(side_effect=lambda *a, **k: _spawn_de(proc))),
        _PilaDeOwnership(proc=proc) as pila,
        pytest.raises(ddl.DynDOLODReadinessProtocolError) as excinfo,
    ):
        await runner._execute_process(pathlib.Path(ejecutable), [], "DynDOLOD")

    assert excinfo.value.razon_protocolo.value == "timeout"
    assert proc.esperas == 0
    assert proc.muertes == 1
    assert observador.observaciones == 1
    pila.mocks["kill_and_reap"].assert_awaited_once_with(proc)


async def test_h8_dyndolod_callback_explota_es_fail_closed(tmp_path):
    """H8 — el confirmador lanza una excepción arbitraria: CANAL_NO_DISPONIBLE + cleanup."""
    ejecutable = r"C:\Modding\DynDOLOD\DynDOLODx64.exe"

    class _ConfirmadorRompe:
        async def confirmar(self, _request: OperatorConfigurationReadyRequest) -> ResultadoConfirmacion:
            raise RuntimeError("callback roto")

        async def informar(self, *, tool: str, mensaje: str) -> None:
            return

    runner, proc, _ = _preparar_corrida(
        tmp_path,
        rondas=[_ronda_output(SALIDA_ADMINISTRADA_TXT, pid=PID)],
        ejecutable=ejecutable,
        confirmador=_ConfirmadorRompe(),
    )
    with (
        patch.object(ddl.asyncio, "create_subprocess_exec", AsyncMock(side_effect=lambda *a, **k: _spawn_de(proc))),
        _PilaDeOwnership(proc=proc) as pila,
        pytest.raises(ddl.DynDOLODReadinessProtocolError) as excinfo,
    ):
        await runner._execute_process(pathlib.Path(ejecutable), [], "DynDOLOD")

    assert excinfo.value.razon_protocolo.value == "canal_no_disponible"
    assert proc.esperas == 0
    assert proc.muertes == 1
    pila.mocks["kill_and_reap"].assert_awaited_once_with(proc)


async def test_h9_cancelacion_durante_readiness_limpia_y_conserva_identidad(tmp_path):
    """H9 — cancelación externa durante la espera de readyness: cleanup + CancelledError."""
    ejecutable = r"C:\Modding\DynDOLOD\DynDOLODx64.exe"

    release = asyncio.Event()
    request_vista: list[OperatorConfigurationReadyRequest] = []

    class _ConfirmadorBloquea:
        async def confirmar(self, req: OperatorConfigurationReadyRequest) -> ResultadoConfirmacion:
            request_vista.append(req)
            await release.wait()
            return ResultadoConfirmacion.APROBADA  # pragma: no cover

        async def informar(self, *, tool: str, mensaje: str) -> None:
            return

    runner, proc, _ = _preparar_corrida(
        tmp_path,
        rondas=[_ronda_output(SALIDA_ADMINISTRADA_TXT, pid=PID)],
        ejecutable=ejecutable,
        confirmador=_ConfirmadorBloquea(),
    )

    with (
        patch.object(ddl.asyncio, "create_subprocess_exec", AsyncMock(side_effect=lambda *a, **k: _spawn_de(proc))),
        _PilaDeOwnership(proc=proc) as pila,
    ):
        task = asyncio.create_task(runner._execute_process(pathlib.Path(ejecutable), [], "DynDOLOD"))
        for _ in range(50):
            await asyncio.sleep(0.01)
            if request_vista:
                break
        assert request_vista, "el confirmador no se invocó"
        task.cancel()
        try:
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            release.set()
        await asyncio.sleep(0)

    assert proc.esperas == 0
    assert proc.muertes == 1
    pila.mocks["kill_and_reap"].assert_awaited_once_with(proc)
    pila.mocks["close_job"].assert_called_once_with(4242)
    assert request_vista[0].pid == PID


async def test_h10_proceso_muere_durante_confirmacion_es_proceso_termino(tmp_path):
    """H10 — el proceso termina antes que la confirmación: PROCESO_TERMINO gana."""
    ejecutable = r"C:\Modding\DynDOLOD\DynDOLODx64.exe"

    class _ConfirmadorQueEspera:
        async def confirmar(self, _request: OperatorConfigurationReadyRequest) -> ResultadoConfirmacion:
            await asyncio.sleep(0.5)
            return ResultadoConfirmacion.APROBADA  # pragma: no cover

        async def informar(self, *, tool: str, mensaje: str) -> None:
            return

    runner, proc, _ = _preparar_corrida(
        tmp_path,
        rondas=[_ronda_output(SALIDA_ADMINISTRADA_TXT, pid=PID)],
        ejecutable=ejecutable,
        confirmador=_ConfirmadorQueEspera(),
    )

    # Marcar el proceso como muerto ANTES de iniciar la espera. La vigilia
    # del seam detectará el returncode en la primera iteración y corta.
    proc.returncode = 0

    with (
        patch.object(ddl.asyncio, "create_subprocess_exec", AsyncMock(side_effect=lambda *a, **k: _spawn_de(proc))),
        _PilaDeOwnership(proc=proc) as pila,
        pytest.raises(ddl.DynDOLODReadinessProtocolError) as excinfo,
    ):
        await runner._execute_process(pathlib.Path(ejecutable), [], "DynDOLOD")

    assert excinfo.value.razon_protocolo.value == "proceso_termino"
    assert proc.muertes == 1
    pila.mocks["kill_and_reap"].assert_awaited_once_with(proc)


async def test_h11_final_gate_usa_mismo_pid_y_mismo_output(tmp_path):
    """H11 — el final gate ata al MISMO pid y la MISMA salida esperada."""
    ejecutable = r"C:\Modding\DynDOLOD\DynDOLODx64.exe"
    runner, proc, _ = _preparar_corrida(
        tmp_path,
        rondas=[
            _ronda_output(SALIDA_ADMINISTRADA_TXT, pid=PID),
            _ronda_output(SALIDA_ADMINISTRADA_TXT, pid=PID),
        ],
        ejecutable=ejecutable,
    )
    expected = str(runner._config.output_root)

    solicitudes_capturadas: list = []
    salidas_capturadas: list[str] = []

    def _espia(solicitud, **_kwargs):
        solicitudes_capturadas.append(solicitud)
        salidas_capturadas.append(solicitud.salida_administrada_esperada)
        from sky_claw.local.tools.dyndolod_uia_preflight import ResultadoPreflightUIA

        return ResultadoPreflightUIA(
            estado=EstadoPreflight.MATCH,
            razon=RazonPreflight.OUTPUT_COINCIDE,
            tool=solicitud.tool,
            detalle="espía",
            valor_esperado=solicitud.salida_administrada_esperada,
            pid=solicitud.pid,
        )

    with (
        patch.object(ddl.asyncio, "create_subprocess_exec", AsyncMock(side_effect=lambda *a, **k: _spawn_de(proc))),
        patch.object(ddl, "ejecutar_gate_sincrono", _espia),
        _PilaDeOwnership(),
    ):
        await runner._execute_process(pathlib.Path(ejecutable), [], "DynDOLOD")

    assert [s.pid for s in solicitudes_capturadas] == [PID, PID]
    assert salidas_capturadas == [expected, expected]


async def test_h12_confirmador_recibe_texto_y_espera(tmp_path):
    """H12 — la solicitud lleva tool_name, pid, executable, expected_output y timeout_seconds."""
    ejecutable = r"C:\Modding\DynDOLOD\DynDOLODx64.exe"
    confirmador = _ConfirmadorFalso(resultado=ResultadoConfirmacion.APROBADA)
    runner, proc, _ = _preparar_corrida(
        tmp_path,
        rondas=[_ronda_output(SALIDA_ADMINISTRADA_TXT, pid=PID)],
        ejecutable=ejecutable,
        confirmador=confirmador,
    )
    with (
        patch.object(ddl.asyncio, "create_subprocess_exec", AsyncMock(side_effect=lambda *a, **k: _spawn_de(proc))),
        _PilaDeOwnership(),
    ):
        await runner._execute_process(pathlib.Path(ejecutable), [], "DynDOLOD")

    assert confirmador.solicitudes, "el confirmador no fue invocado"
    req = confirmador.solicitudes[0]
    assert req.tool_name == "DynDOLOD"
    assert req.pid == PID
    assert req.expected_output == runner._config.output_root
    assert req.timeout_seconds == float(runner._config.readiness_timeout_seconds)


async def test_h13_informar_se_llama_en_cada_punto_esperado(tmp_path):
    """H13 — el seam ``informar`` se invoca antes de la confirmación y tras el final MATCH."""
    ejecutable = r"C:\Modding\DynDOLOD\DynDOLODx64.exe"
    confirmador = _ConfirmadorFalso(resultado=ResultadoConfirmacion.APROBADA)
    runner, proc, _ = _preparar_corrida(
        tmp_path,
        rondas=[
            _ronda_output(SALIDA_ADMINISTRADA_TXT, pid=PID),
            _ronda_output(SALIDA_ADMINISTRADA_TXT, pid=PID),
        ],
        ejecutable=ejecutable,
        confirmador=confirmador,
    )
    with (
        patch.object(ddl.asyncio, "create_subprocess_exec", AsyncMock(side_effect=lambda *a, **k: _spawn_de(proc))),
        _PilaDeOwnership(),
    ):
        await runner._execute_process(pathlib.Path(ejecutable), [], "DynDOLOD")

    # Dos informes: «instrucciones» y «Start habilitado». El orden importa.
    assert len(confirmador.informes) >= 2
    mensajes = [m for _, m in confirmador.informes]
    assert any("NO pulses Start" in m for m in mensajes), confirmador.informes
    assert any("puede pulsar start" in m.lower() for m in mensajes), confirmador.informes


# ===========================================================================
# G1..G5 — anclas AST
# ===========================================================================


def test_g1_orden_initial_protocolo_final_en_el_camino_comun():
    """G1 — initial gate → _protocolo_de_readiness → final gate.

    El initial gate se invoca directamente en ``_execute_process``; el
    protocol method encapsula el resto (confirmación + final gate +
    informes). El orden contractual es:

        _execute_process:
            _gate_uia_output()      ← initial
            _protocolo_de_readiness() ← confirm + final gate + informes
            proc.wait
    """
    lineas_initial = _lineas_de_llamadas_a_en("_gate_uia_output", "_execute_process")
    lineas_protocolo = _lineas_de_llamadas_a_en("_protocolo_de_readiness", "_execute_process")
    assert lineas_initial, "el seam no invoca el initial gate"
    assert lineas_protocolo, "el seam no invoca el protocolo de readyness"
    assert lineas_initial[0] < lineas_protocolo[0], (
        f"el initial gate debe invocarse ANTES del protocolo: {lineas_initial[0]} < {lineas_protocolo[0]}"
    )
    # El final gate vive DENTRO del protocolo, no directamente en _execute_process.
    lineas_final = [ln for ln in _lineas_de_llamadas_a_en("_gate_uia_output", "_protocolo_de_readiness")]
    assert len(lineas_final) == 1, f"el protocolo invoca el final gate exactamente una vez; vio {len(lineas_final)}"


def test_g2_final_gate_reusa_el_mismo_seam():
    """G2 — el final gate reusa ``_gate_uia_output`` con ``etiqueta="final"``."""
    fuente = MODULO_RUNNER.read_text(encoding="utf-8")
    assert "_gate_uia_output_final" not in fuente, (
        "el final gate debe reusar _gate_uia_output, no crear un método gemelo"
    )
    protocolo = _funcion_del_runner("_protocolo_de_readiness")
    etiquetas_final = [
        nodo.value.value
        for nodo in ast.walk(protocolo)
        if isinstance(nodo, ast.keyword)
        and nodo.arg == "etiqueta"
        and isinstance(nodo.value, ast.Constant)
        and nodo.value.value == "final"
    ]
    assert etiquetas_final, "_protocolo_de_readiness no etiqueta la llamada al final gate como 'final'"


def test_g3_protocolo_sin_branch_tool_specific():
    """G3 — el seam del protocolo NO tiene un branch específico por ``tool_name``."""
    protocolo = _funcion_del_runner("_protocolo_de_readiness")
    # Ancla negativa: dentro del cuerpo del método, NO debe haber un
    # ``if tool_name == "DynDOLOD"`` (o "TexGen") que separe el camino.
    for nodo in ast.walk(protocolo):
        if not isinstance(nodo, ast.If):
            continue
        for comparador in (*nodo.test.comparators, nodo.test.left) if isinstance(nodo.test, ast.Compare) else ():
            if isinstance(comparador, ast.Constant) and comparador.value in {"DynDOLOD", "TexGen"}:
                pytest.fail(f"el protocolo tiene un branch específico por tool_name ({comparador.value!r})")


def test_g4_primitivas_mutantes_prohibidas_en_la_superficie_de_readiness():
    """G4 — la superficie no pulsa, no escribe, no inyecta mouse/teclado."""
    superficies = [
        MODULO_RUNNER.read_text(encoding="utf-8"),
        (MODULO_RUNNER.parent / "dyndolod_uia_gate.py").read_text(encoding="utf-8"),
    ]
    nombres_prohibidos = {
        "SetValue",
        "InvokePattern",
        "Invoke",
        "send_keys",
        "keybd_event",
        "mouse_event",
    }
    for fuente in superficies:
        for nombre in nombres_prohibidos:
            assert nombre not in fuente, f"primitiva UIA mutante {nombre!r} presente en la superficie de readyness"


def test_g5_la_solicitud_es_inmutable_y_completa():
    """G5 — sanity del dataclass ``OperatorConfigurationReadyRequest`` (frozen + campos)."""
    pid = 1234
    expected = pathlib.Path(r"C:\Games\Salida")
    exe = pathlib.Path(r"C:\Tools\DynDOLODx64.exe")
    req = OperatorConfigurationReadyRequest(
        tool_name="DynDOLOD",
        pid=pid,
        executable=exe,
        expected_output=expected,
        timeout_seconds=60.0,
    )
    assert req.pid == pid
    assert req.expected_output == expected
    assert req.executable == exe
    assert req.tool_name == "DynDOLOD"
    assert req.timeout_seconds == 60.0
    with pytest.raises((AttributeError, TypeError)):
        req.pid = 9999  # type: ignore[misc]


def test_g6_excepcion_del_protocolo_es_hija_de_dyndolodexecutionerror():
    """G6 — ``DynDOLODReadinessProtocolError`` extiende ``DynDOLODExecutionError``."""
    from sky_claw.local.tools.dyndolod_runner import DynDOLODExecutionError

    assert issubclass(ddl.DynDOLODReadinessProtocolError, DynDOLODExecutionError)
    # Y NO contamina ``DynDOLODPreflightUIAError`` (los dominios siguen
    # separados: el gate sigue siendo ``DynDOLODPreflightUIAError``, el
    # canal/lifecycle es esta excepción).
    assert not issubclass(ddl.DynDOLODReadinessProtocolError, ddl.DynDOLODPreflightUIAError)
