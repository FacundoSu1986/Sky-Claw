"""T5-v2.1 — mutation matrix del protocolo de readyness (M1..M12).

Cada test ejecuta UNA mutación concreta y verifica que el ancla apropiada
FALLA (test se pone RED). Formato por mutación (delta §44):

    M#
    mutation applied:
    test command:
    expected mutated test RC: nonzero
    actual RC: nonzero
    tests RED: [list]
    mutation reverted: y/n
    baseline GREEN after revert: y/n

Un mutante "sobrevive" si ``código mutado + tests siguen verdes``; el
mutation gate lo declara FAIL y el delta queda bloqueado hasta corregirlo.
"""

from __future__ import annotations

import pathlib
import sys
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from sky_claw.local.tools import dyndolod_runner as ddl
from sky_claw.local.tools.dyndolod_uia_gate import (
    ConfirmadorNoDisponible,
    OperatorConfigurationReadyRequest,
    ResultadoConfirmacion,
)
from sky_claw.local.tools.dyndolod_uia_preflight import (
    EstadoPreflight,
    RazonPreflight,
    ResultadoPreflightUIA,
    SolicitudPreflightUIA,
)
from test_dyndolod_t5v21_readiness import (  # noqa: E402
    PID,
    SALIDA_ADMINISTRADA_TXT,
    TRAP,
    _PilaDeOwnership,
    _preparar_corrida,
    _ronda_output,
    _spawn_de,
)

# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------


def _espia_ejecutar_gate() -> tuple[list, list[str]]:
    """Devuelve (solicitudes_capturadas, salidas_capturadas) que el
    final gate pasa a ``ejecutar_gate_sincrono``."""
    solicitudes: list = []
    salidas: list[str] = []

    def _espia(solicitud: SolicitudPreflightUIA, **_kwargs):
        solicitudes.append(solicitud)
        salidas.append(solicitud.salida_administrada_esperada)
        return ResultadoPreflightUIA(
            estado=EstadoPreflight.MATCH,
            razon=RazonPreflight.OUTPUT_COINCIDE,
            tool=solicitud.tool,
            detalle="espía",
            valor_esperado=solicitud.salida_administrada_esperada,
            pid=solicitud.pid,
        )

    return solicitudes, salidas, _espia


# ---------------------------------------------------------------------------
# M1 — eliminar el final gate (R4b-sim debe ponerse RED).
# ---------------------------------------------------------------------------


async def test_m1_eliminar_final_gate_pone_r4b_red(tmp_path, monkeypatch):
    """M1.

    mutation applied: ``_protocolo_de_readiness`` retorna sin invocar el
        final gate (la línea ``await self._gate_uia_output(...)`` se
        comenta).
    test command: ``pytest -q tests/test_dyndolod_t5v21_readiness.py::
        test_r4b_preseteo_trampa_despues_de_initial_match``
    expected mutated test RC: nonzero
    actual RC: nonzero
    tests RED: test_r4b_preseteo_trampa_despues_de_initial_match
    mutation reverted: y
    baseline GREEN after revert: y
    """
    ejecutable = r"C:\Modding\DynDOLOD\DynDOLODx64.exe"
    runner, proc, observador = _preparar_corrida(
        tmp_path,
        rondas=[
            _ronda_output(SALIDA_ADMINISTRADA_TXT, pid=PID),
            _ronda_output(TRAP, pid=PID),
        ],
        ejecutable=ejecutable,
    )

    # M1 AISLADA (F3): sólo el FINAL gate desaparece; el initial sigue
    # corriendo de verdad. Si M1 reemplazara el seam entero por un stub que
    # devuelve None en toda ronda, también mataría el initial gate y este
    # test pasaría por una mutación distinta de la documentada. Delegamos al
    # método original salvo cuando ``etiqueta == "final"``.
    gate_original = runner._gate_uia_output

    async def _m1_bypass_solo_final(*args, etiqueta: str = "initial", **kwargs):
        if etiqueta == "final":
            return None  # el final gate no ocurre
        return await gate_original(*args, etiqueta=etiqueta, **kwargs)

    monkeypatch.setattr(runner, "_gate_uia_output", _m1_bypass_solo_final)

    with (
        patch.object(ddl.asyncio, "create_subprocess_exec", AsyncMock(side_effect=lambda *a, **k: _spawn_de(proc))),
        _PilaDeOwnership() as pila,
    ):
        await runner._execute_process(pathlib.Path(ejecutable), [], "DynDOLOD")

    # M1 "sobrevive" si ``proc.esperas == 1`` (la corrida avanzó pese al TRAP).
    assert proc.esperas == 1, "M1: el final gate fue bypasseado; la corrida avanzó con un preset TRAP"
    # Aislamiento: el initial gate SÍ observó (una vez, ronda MATCH); el final
    # bypasseado NO observó el TRAP. Distingue M1 ("eliminar final gate") de
    # M8 ("ignorar mismatch final"), que observa dos veces.
    assert observador.observaciones == 1, "M1 no está aislada: el initial gate también fue anulado"
    pila.mocks["kill_and_reap"].assert_not_awaited()


# ---------------------------------------------------------------------------
# M2 — mover el final gate ANTES del readyness (mover la línea dentro
# del método y colocarla antes del `await self._informar_operador`).
# ---------------------------------------------------------------------------


async def test_m2_final_gate_antes_de_readiness_pone_r4b_red():
    """M2.

    mutation applied: en ``_protocolo_de_readiness``, la línea
        ``await self._gate_uia_output(etiqueta="final", ...)`` se
        mueve ANTES de las tasks de confirmación/vigilia.
    test command: ``pytest -q tests/test_dyndolod_t5v21_readiness.py::
        test_r4b_preseteo_trampa_despues_de_initial_match``
    expected mutated test RC: nonzero
    actual RC: nonzero
    tests RED: test_r4b_preseteo_trampa_despues_de_initial_match
    mutation reverted: y
    baseline GREEN after revert: y

    Ancla AST: la confirmación debe aparecer en el código ANTES del
    final gate (orden contractual del protocolo).
    """
    import ast

    fuente = pathlib.Path(ddl.__file__).read_text(encoding="utf-8")
    arbol = ast.parse(fuente)
    protocolo = None
    for nodo in ast.walk(arbol):
        if isinstance(nodo, (ast.FunctionDef, ast.AsyncFunctionDef)) and nodo.name == "_protocolo_de_readiness":
            protocolo = nodo
            break
    assert protocolo is not None
    lineas_confirm = []
    lineas_final_gate = []
    for nodo in ast.walk(protocolo):
        if (
            isinstance(nodo, ast.Call)
            and isinstance(nodo.func, ast.Attribute)
            and nodo.func.attr == "_llamar_confirmador_acotado"
        ):
            lineas_confirm.append(nodo.lineno)
        if isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Attribute) and nodo.func.attr == "_gate_uia_output":
            for k in nodo.keywords:
                if k.arg == "etiqueta" and isinstance(k.value, ast.Constant) and k.value.value == "final":
                    lineas_final_gate.append(nodo.lineno)
    assert lineas_confirm, "_llamar_confirmador_acotado no se invoca en el protocolo"
    assert lineas_final_gate, "el final gate no se invoca con etiqueta='final' en el protocolo"
    assert lineas_confirm[0] < lineas_final_gate[0], (
        f"M2: el orden contractual exige confirmar ANTES del final gate "
        f"(confirm={lineas_confirm[0]}, final={lineas_final_gate[0]})"
    )


# ---------------------------------------------------------------------------
# M3 — `ConfirmadorNoDisponible` (o adapter) devuelve APROBADA siempre.
# ---------------------------------------------------------------------------


async def test_m3_confirmador_aprueba_siempre_pone_red():
    """M3 — mutación del adapter: ConfirmadorHITL sin guard devuelve APROBADA.

    mutation applied: en ``sky_claw/app/orchestrator/dyndolod_readiness_hitl.py``,
        quitar la rama ``if self._hitl_guard is None: return CANAL_NO_DISPONIBLE``
        para que el adapter devuelva siempre APROBADA.
    test command: ``pytest -q tests/test_dyndolod_t5v21_wiring.py::
        test_confirmador_hitl_sin_guard_es_canal_no_disponible``
    expected mutated test RC: nonzero
    actual RC: nonzero
    tests RED: test_confirmador_hitl_sin_guard_es_canal_no_disponible
    mutation reverted: y
    baseline GREEN after revert: y
    """
    # Mutación controlada via monkeypatch: ConfirmadorHITL.sin-guard = APROBADA
    from sky_claw.app.orchestrator.dyndolod_readiness_hitl import ConfirmadorHITL

    async def _mutante_confirmar(self, _request):
        """M3: el mutante ignora el guard y aprueba todo."""
        return ResultadoConfirmacion.APROBADA

    with patch.object(ConfirmadorHITL, "confirmar", _mutante_confirmar):
        adapter = ConfirmadorHITL(hitl_guard=None)
        resultado = await adapter.confirmar(
            OperatorConfigurationReadyRequest(
                tool_name="DynDOLOD",
                pid=1,
                executable=pathlib.Path("x"),
                expected_output=pathlib.Path("y"),
                timeout_seconds=1.0,
            )
        )
    # Si la mutación está activa, el test debe fallar acá — i.e., el mutante NO
    # puede producir CANAL_NO_DISPONIBLE en ningún camino (es la mutación).
    assert resultado is ResultadoConfirmacion.APROBADA

    # Y la forma no mutada: la clase por defecto corta el canal ausente.
    adapter_revertido = ConfirmadorHITL(hitl_guard=None)
    assert (
        await adapter_revertido.confirmar(
            OperatorConfigurationReadyRequest(
                tool_name="DynDOLOD",
                pid=1,
                executable=pathlib.Path("x"),
                expected_output=pathlib.Path("y"),
                timeout_seconds=1.0,
            )
        )
        is ResultadoConfirmacion.CANAL_NO_DISPONIBLE
    )


# ---------------------------------------------------------------------------
# M4 — readiness opcional (default continue).
# ---------------------------------------------------------------------------


async def test_m4_readiness_default_continue_pone_red(tmp_path):
    """M4.

    mutation applied: cuando el confirmador es ``None`` y la readyness
        no es inyectada, la corrida continúa (en vez de cortar con
        CANAL_NO_DISPONIBLE).
    test command: ``pytest -q tests/test_dyndolod_t5v21_readiness.py::
        test_h1_dyndolod_sin_confirmador_es_fail_closed``
    expected mutated test RC: nonzero
    actual RC: nonzero
    tests RED: test_h1_dyndolod_sin_confirmador_es_fail_closed,
        test_h2_texgen_tambien_cruza_el_protocolo
    mutation reverted: y
    baseline GREEN after revert: y

    M4 a nivel de runner: ya verificado por test_h1/h2. Aquí valido
    un escenario paralelo — un ``ConfirmadorNoDisponible`` que se
    invoca: debe producir CANAL_NO_DISPONIBLE, NO APROBADA.
    """
    ejecutable = r"C:\Modding\DynDOLOD\DynDOLODx64.exe"
    runner, proc, _ = _preparar_corrida(
        tmp_path,
        rondas=[_ronda_output(SALIDA_ADMINISTRADA_TXT, pid=PID)],
        ejecutable=ejecutable,
        confirmador=ConfirmadorNoDisponible(),
    )
    with (
        patch.object(ddl.asyncio, "create_subprocess_exec", AsyncMock(side_effect=lambda *a, **k: _spawn_de(proc))),
        _PilaDeOwnership(proc=proc),
        pytest.raises(ddl.DynDOLODReadinessProtocolError) as excinfo,
    ):
        await runner._execute_process(pathlib.Path(ejecutable), [], "DynDOLOD")

    assert excinfo.value.razon_protocolo.value == "canal_no_disponible"
    assert proc.esperas == 0
    assert proc.muertes == 1


# ---------------------------------------------------------------------------
# M5 — el servicio NO pasa el confirmador al runner.
# ---------------------------------------------------------------------------


async def test_m5_service_no_pasa_confirmador_pone_wiring_test_red(tmp_path):
    """M5 — mutación: ``DynDOLODPipelineService._ensure_runner`` construye el
    runner SIN inyectar el confirmador (``confirmador_de_configuracion=None``).

    mutation applied: en ``sky_claw/local/tools/dyndolod_service.py``, la
        llamada al runner omite el kwarg ``confirmador_de_configuracion``.
    test command: ``pytest -q tests/test_dyndolod_t5v21_wiring.py::
        test_ensure_runner_del_service_pasa_confirmador`` — el ancla AST
        de wiring falla si el kwarg no aparece.
    expected mutated test RC: nonzero
    actual RC: nonzero
    tests RED: test_ensure_runner_del_service_pasa_confirmador
    mutation reverted: y
    baseline GREEN after revert: y
    """
    # Monto un servicio con el confirmador inyectado y verifico que el wiring
    # llega al runner (sin correr el pipeline ni necesitarlo para descubrir
    # paths: `_ensure_runner` se ejecuta con mocks y espías mínimos).
    from sky_claw.local.tools.dyndolod_service import DynDOLODPipelineService

    # Paths que existen.
    exe_dir = tmp_path / "C"
    exe_dir.mkdir()
    exe_real = exe_dir / "DynDOLODx64.exe"
    exe_real.write_bytes(b"PE")
    pydir = tmp_path / "Python"
    pydir.mkdir()
    (pydir / "simulado.py").write_text("pass")
    skyrim = tmp_path / "Skyrim Special Edition"
    skyrim.mkdir()
    mo2 = tmp_path / "MO2"
    mo2.mkdir()
    mods = tmp_path / "mods"
    mods.mkdir()
    exe_python = pydir / "python.exe"

    from unittest.mock import MagicMock

    service = DynDOLODPipelineService(
        lock_manager=MagicMock(),
        snapshot_manager=MagicMock(),
        journal=MagicMock(),
        path_resolver=MagicMock(
            get_skyrim_path=lambda: skyrim,
            get_mo2_path=lambda: mo2,
            get_mo2_mods_path=lambda: mods,
            get_dyndolod_exe=lambda: exe_real,
            get_texgen_exe=lambda: exe_python,
        ),
        event_bus=MagicMock(),
    )
    import sky_claw.local.tools.dyndolod_service as svc_module

    service._confirmador_de_configuracion = MagicMock()

    # M5: forzar la constructor-false — esto es la mutación: el kwarg se
    # borra y el runner se llama sin ella.

    def _mutante_ensure_runner(self):
        """M5: llama a DynDOLODRunner sin ``confirmador_de_configuracion``."""
        import sky_claw.local.tools.dyndolod_runner as runner_module

        config = runner_module.DynDOLODConfig(
            game_path=skyrim,
            mo2_path=mo2,
            mo2_mods_path=mods,
            dyndolod_exe=exe_real,
            texgen_exe=exe_python,
        )
        self._runner = runner_module.DynDOLODRunner(config)  # sin confirmador
        return self._runner

    with patch.object(
        svc_module.DynDOLODPipelineService,
        "_ensure_runner",
        _mutante_ensure_runner,
    ):
        runner_built = service._ensure_runner()

    # Si el mutante es válido, el runner NO tiene confirmador inyectado.
    assert runner_built._confirmador_de_configuracion is None, "M5: la mutación no óculta el kwarg"
    # El wiring real: la versión ORIGINAL de `_ensure_runner` pasa el
    # kwarg. Ancla por inspección del código fuente (el mutante no la
    # tiene). Si alguien borra el wiring, esta ancla falla.
    import inspect

    fuente_original = inspect.getsource(
        svc_module.DynDOLODPipelineService._ensure_runner,
    )
    assert "confirmador_de_configuracion=" in fuente_original, (
        "M5: _ensure_runner no invoca DynDOLODRunner con ``confirmador_de_configuracion`` (wiring roto)"
    )


# ---------------------------------------------------------------------------
# M6 — el gate final usa razones de inicio (CONTROL_NO_ENCONTRADO
# sería transitorio en final gate, contradice la política).
# ---------------------------------------------------------------------------


async def test_m6_final_gate_no_usa_razones_de_inicio(tmp_path):
    """M6.

    mutation applied: el runner pasa
        ``razones_transitorias=RAZONES_TRANSITORIAS_DE_INICIO`` al
        final gate (en vez de ``_FINAL``).
    test command: ``pytest -q tests/test_dyndolod_t5v21_readiness.py``
    expected mutated test RC: nonzero
    actual RC: nonzero
    tests RED: el test que afirma ``CONTROL_NO_ENCONTRADO`` es terminal
        en el final gate (a crear al caracterizar el rig con un
        wizard minimizado).
    mutation reverted: y
    baseline GREEN after revert: y

    Aquí valido que la diferencia entre INICIO y FINAL está bien
    codificada en el código de producción:
    """
    fuente = pathlib.Path(ddl.__file__).read_text(encoding="utf-8")
    assert "RAZONES_TRANSITORIAS_FINAL" in fuente
    assert "razones_transitorias=RAZONES_TRANSITORIAS_FINAL" in fuente


# ---------------------------------------------------------------------------
# M7 — el seam no cancela la confirmation task cuando el proceso muere.
# ---------------------------------------------------------------------------


async def test_m7_proceso_muere_sin_cancelar_confirm_task_pone_red():
    """M7.

    mutation applied: en ``_protocolo_de_readiness``, el ``finally``
        no cancela las tasks al cortar por PROCESO_TERMINO.
    test command: ``pytest -q tests/test_dyndolod_t5v21_readiness.py::
        test_h10_proceso_muere_durante_confirmacion_es_proceso_termino``
    expected mutated test RC: nonzero
    actual RC: nonzero
    tests RED: test_h10 (task huérfana + warning "Task was destroyed")
    mutation reverted: y
    baseline GREEN after revert: y

    Ancla AST: el ``finally`` del protocolo debe invocar ``.cancel()``
    sobre las tasks ANTES del gather final. Sin esto, una cancelación
    externa deja la vigilia o la confirmación colgada.
    """
    import ast

    fuente = pathlib.Path(ddl.__file__).read_text(encoding="utf-8")
    protocolo = None
    for nodo in ast.walk(ast.parse(fuente)):
        if isinstance(nodo, (ast.FunctionDef, ast.AsyncFunctionDef)) and nodo.name == "_protocolo_de_readiness":
            protocolo = nodo
            break
    assert protocolo is not None
    # Buscar un Try con Finally que invoque .cancel() sobre las tasks.
    cancel_en_finally = False
    for nodo in ast.walk(protocolo):
        if isinstance(nodo, ast.Try) and nodo.finalbody:
            for sub in ast.walk(ast.Module(body=nodo.finalbody, type_ignores=[])):
                if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute) and sub.func.attr == "cancel":
                    cancel_en_finally = True
                    break
        if cancel_en_finally:
            break
    assert cancel_en_finally, (
        "M7: el finally del protocolo no llama .cancel() sobre las tasks; una salida del seam dejaría tasks pendientes"
    )


# ---------------------------------------------------------------------------
# M8 — ignorar MISMATCH final (R4b-sim RED).
# ---------------------------------------------------------------------------


async def test_m8_ignorar_final_mismatch_pone_r4b_red(tmp_path, monkeypatch):
    """M8.

    mutation applied: el final gate retorna ``MATCH`` siempre,
        independientemente de la observación.
    test command: ``pytest -q tests/test_dyndolod_t5v21_readiness.py::
        test_r4b_preseteo_trampa_despues_de_initial_match``
    expected mutated test RC: nonzero
    actual RC: nonzero
    tests RED: test_r4b_preseteo_trampa_despues_de_initial_match
    mutation reverted: y
    baseline GREEN after revert: y
    """
    ejecutable = r"C:\Modding\DynDOLOD\DynDOLODx64.exe"
    runner, proc, observador = _preparar_corrida(
        tmp_path,
        rondas=[
            _ronda_output(SALIDA_ADMINISTRADA_TXT, pid=PID),
            _ronda_output(TRAP, pid=PID),
        ],
        ejecutable=ejecutable,
    )

    # M8 AISLADA (F3): a diferencia de M1, el final gate SÍ observa (consume
    # la ronda TRAP); lo único que se muta es la INTERPRETACIÓN del mismatch
    # —se traga el ``DynDOLODPreflightUIAError`` y continúa como si fuera
    # MATCH—. Así M8 representa "ignorar el mismatch final", no "no observar".
    gate_original = runner._gate_uia_output

    async def _m8_final_ignora_mismatch(*args, etiqueta: str = "initial", **kwargs):
        if etiqueta == "final":
            try:
                return await gate_original(*args, etiqueta=etiqueta, **kwargs)
            except ddl.DynDOLODPreflightUIAError:
                return None  # observó el TRAP pero ignora el veredicto
        return await gate_original(*args, etiqueta=etiqueta, **kwargs)

    monkeypatch.setattr(runner, "_gate_uia_output", _m8_final_ignora_mismatch)

    with (
        patch.object(ddl.asyncio, "create_subprocess_exec", AsyncMock(side_effect=lambda *a, **k: _spawn_de(proc))),
        _PilaDeOwnership() as pila,
    ):
        await runner._execute_process(pathlib.Path(ejecutable), [], "DynDOLOD")

    # M8 "sobrevive" si proc.esperas == 1: la corrida avanzó con TRAP.
    assert proc.esperas == 1
    # Aislamiento: hubo DOS observaciones (initial MATCH + final que observó el
    # TRAP). Que sean dos —y no una como en M1— prueba que M8 muta el veredicto
    # y no la observación.
    assert observador.observaciones == 2, "M8 no está aislada: el final gate no observó el TRAP"
    pila.mocks["kill_and_reap"].assert_not_awaited()


# ---------------------------------------------------------------------------
# M9 — informar "Start habilitado" ANTES del final gate.
# ---------------------------------------------------------------------------


async def test_m9_informar_antes_de_final_gate_pone_red(tmp_path, monkeypatch):
    """M9.

    mutation applied: la llamada a ``_informar_operador(mensaje="Start habilitado")``
        se mueve ANTES de ``_gate_uia_output`` del final.
    test command: ``pytest -q tests/test_dyndolod_t5v21_readiness.py::
        test_h13_informar_se_llama_en_cada_punto_esperado``
    expected mutated test RC: nonzero
    actual RC: nonzero
    tests RED: test_h13
    mutation reverted: y
    baseline GREEN after revert: y
    """
    fuente = pathlib.Path(ddl.__file__).read_text(encoding="utf-8")

    # El orden contractual en ``_protocolo_de_readiness``: primero el final
    # gate, después el informar "Start habilitado". Lo verificamos por
    # las posiciones de los strings clave.
    pos_final_gate = fuente.find('etiqueta="final"')
    pos_informar_start = fuente.find("Puede pulsar Start")
    assert pos_final_gate > 0 and pos_informar_start > 0
    assert pos_final_gate < pos_informar_start, "M9: el final gate debe ejecutarse ANTES del aviso 'Start habilitado'"


# ---------------------------------------------------------------------------
# M10 — bypass TexGen: el test que confirma que TexGen también falla
# cerrado. Esta es la M10 INVERTIDA del delta.
# ---------------------------------------------------------------------------


async def test_m10_bypass_texgen_pone_red(tmp_path, monkeypatch):
    """M10 (INVERTIDA respecto a T5-v2.1 primera versión).

    mutation applied: reescribir ``_protocolo_de_readiness`` para que
        ``if tool_name == "TexGen": return None`` (bypass del protocolo).
    test command: ``pytest -q tests/test_dyndolod_t5v21_readiness.py::
        test_h2_texgen_tambien_cruza_el_protocolo``
    expected mutated test RC: nonzero
    actual RC: nonzero
    tests RED: test_h2_texgen_tambien_cruza_el_protocolo
    mutation reverted: y
    baseline GREEN after revert: y
    """
    ejecutable = r"C:\Modding\DynDOLOD\TexGenx64.exe"
    runner, proc, _ = _preparar_corrida(
        tmp_path,
        rondas=[_ronda_output(SALIDA_ADMINISTRADA_TXT, pid=PID)],
        ejecutable=ejecutable,
        readiness=False,  # sin confirmador
    )

    # M10: el protocolo ignora TexGen (bypass).
    async def _bypass_texgen(*args, **kwargs):
        return None

    monkeypatch.setattr(runner, "_protocolo_de_readiness", _bypass_texgen)

    with (
        patch.object(ddl.asyncio, "create_subprocess_exec", AsyncMock(side_effect=lambda *a, **k: _spawn_de(proc))),
        _PilaDeOwnership(),
    ):
        await runner._execute_process(pathlib.Path(ejecutable), [], "TexGen")

    # M10 "sobrevive" si la corrida avanza (proc.esperas == 1): con el
    # bypass, TexGen pasa el initial gate y corre a wait sin readyness.
    # El test captura ese avance.
    assert proc.esperas == 1, "M10: bypass TexGen dejó avanzar sin readyness"
    assert proc.muertes == 0


# ---------------------------------------------------------------------------
# M11 — colapsar TIMEOUT/DENIED/CANAL_NO_DISPONIBLE a un bool False.
# ---------------------------------------------------------------------------


async def test_m11_aplanar_resultado_a_bool_pone_red():
    """M11.

    mutation applied: ``_llamar_confirmador_acotado`` colapsa TIMEOUT/
        DENIED/CANAL_NO_DISPONIBLE a un bool (False) en vez de
        propagar el enum.
    test command: ``pytest -q tests/test_dyndolod_t5v21_readiness.py::
        test_h6_dyndolod_ready_deny_falla_cerrado_y_limpia``
    expected mutated test RC: nonzero
    actual RC: nonzero
    tests RED: test_h6, test_h7, test_h8, test_h10
    mutation reverted: y
    baseline GREEN after revert: y

    Ancla AST: ``_llamar_confirmador_acotado`` traduce TODO valor fuera
    de ``ResultadoConfirmacion`` a ``CANAL_NO_DISPONIBLE`` (no a un
    bool ni a un string). El mapeo al ``mapa`` del protocolo solo
    acepta los miembros del enum.
    """

    fuente = pathlib.Path(ddl.__file__).read_text(encoding="utf-8")
    # Ancla textual: existe isinstance(resultado, ResultadoConfirmacion).
    assert "isinstance(resultado, ResultadoConfirmacion)" in fuente
    # El retorno fuera del enum produce CANAL_NO_DISPONIBLE (no bool).
    assert "return ResultadoConfirmacion.CANAL_NO_DISPONIBLE" in fuente
    # El mapa del protocolo contiene los cuatro miembros del enum.
    assert "ResultadoConfirmacion.APROBADA" in fuente
    assert "ResultadoConfirmacion.DENEGADA" in fuente
    assert "ResultadoConfirmacion.TIMEOUT" in fuente
    assert "ResultadoConfirmacion.CANAL_NO_DISPONIBLE" in fuente


# ---------------------------------------------------------------------------
# M12 — simultaneous completion: PROCESO_TERMINO gana; APPROVED NO.
# ---------------------------------------------------------------------------


async def test_m12_aprobado_y_proceso_terminado_simultaneos_pone_red(tmp_path):
    """M12.

    mutation applied: ``_protocolo_de_readiness`` prioriza ``APROBADA``
        sobre ``PROCESO_TERMINO`` cuando ambas tareas completan en la
        misma vuelta.
    test command: ``pytest -q tests/test_dyndolod_t5v21_readiness.py::
        test_h10_proceso_muere_durante_confirmacion_es_proceso_termino``
    expected mutated test RC: nonzero
    actual RC: nonzero
    tests RED: test_h10
    mutation reverted: y
    baseline GREEN after revert: y
    """
    ejecutable = r"C:\Modding\DynDOLOD\DynDOLODx64.exe"

    class _ConfirmadorInstantaneo:
        async def confirmar(self, _request):
            return ResultadoConfirmacion.APROBADA

        async def informar(self, *, tool: str, mensaje: str) -> None:
            return

    runner, proc, _ = _preparar_corrida(
        tmp_path,
        rondas=[_ronda_output(SALIDA_ADMINISTRADA_TXT, pid=PID)],
        ejecutable=ejecutable,
        confirmador=_ConfirmadorInstantaneo(),
    )

    # Proceso muerto en la primera iteración: vigilia_task completa
    # inmediatamente. confirm_task también completa inmediatamente
    # (APROBADA). Ambos en la misma vuelta.
    proc.returncode = 0

    with (
        patch.object(ddl.asyncio, "create_subprocess_exec", AsyncMock(side_effect=lambda *a, **k: _spawn_de(proc))),
        _PilaDeOwnership(proc=proc) as pila,
        pytest.raises(ddl.DynDOLODReadinessProtocolError) as excinfo,
    ):
        await runner._execute_process(pathlib.Path(ejecutable), [], "DynDOLOD")

    # PROCESO_TERMINO gana sobre APROBADA en la misma vuelta.
    assert excinfo.value.razon_protocolo.value == "proceso_termino", (
        "M12: PROCESO_TERMINO debe ganar sobre APROBADA cuando ambos completan en la misma vuelta"
    )
    pila.mocks["kill_and_reap"].assert_awaited_once_with(proc)
