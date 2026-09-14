"""T5-v2.1 — protocolo de readiness del runner: UIA read-only + HITL mid-run.

Cubre W1-W16 del encargo de Fase 3 sobre ``DynDOLODRunner`` con la capa pura de
Fase 2 REAL y la frontera de proceso falsificada: lo que se prueba es el
lifecycle (orden de los gates, carrera con la muerte del proceso, cleanup en
cada rechazo, cancelación) — no COM ni subprocess.

**W1-W4** (expected == subroot exclusivo, nunca ``family_root``, y == la MISMA
fuente del ``-o:``) y **W16** (simetría TexGen/DynDOLOD) viven acá junto con
**W5-W15**, porque todas dependen del mismo helper de runner.

El modo directo (``readiness=None``) no corre el protocolo: es el contrato que
el censo de constructores de ``test_dyndolod_t5v21_wiring.py`` exige cablear en
producción.
"""

from __future__ import annotations

import ast
import asyncio
import pathlib
from unittest import mock
from unittest.mock import MagicMock

import pytest

import sky_claw.local.tools.dyndolod_runner as ddl
from sky_claw.local.tools.dyndolod_runner import (
    DynDOLODPreflightUIAError,
    DynDOLODReadinessProtocolError,
    DynDOLODRunner,
)
from sky_claw.local.tools.dyndolod_uia_gate import (
    CapacidadDeReadinessUIA,
    ResolucionProtocoloReadiness,
    ResultadoConfirmacion,
)
from sky_claw.local.tools.dyndolod_uia_preflight import (
    ControlObservado,
    ProcesoObservado,
    VentanaObservada,
    canonicalizar_ruta_windows,
)
from sky_claw.local.tools.output_targets import (
    HerramientaDynDOLOD,
    derivar_layout_de_dyndolod,
)

BINARIOS = {"TexGen": "TexGenx64.exe", "DynDOLOD": "DynDOLODx64.exe"}
#: Un pid por herramienta: el resolver filtra por pid ANTES de probar identidad,
#: y la revalidación anti-reciclado vuelve a buscar por pid. Dos procesos con el
#: MISMO pid harían que esa relectura encontrara al hermano y el veredicto
#: cayera en IDENTIDAD_CAMBIO (que es justamente lo que el guard debe hacer).
PID_POR_TOOL = {"TexGen": 4242, "DynDOLOD": 5151}

RAIZ = pathlib.Path(__file__).resolve().parents[1]
MODULO_RUNNER = RAIZ / "sky_claw" / "local" / "tools" / "dyndolod_runner.py"


# ---------------------------------------------------------------------------
# Dobles
# ---------------------------------------------------------------------------


class StreamVacio:
    """Stream que da EOF de inmediato: los drains cierran sin colgar el gather."""

    def __init__(self) -> None:
        self.lecturas = 0

    async def read(self, _n: int) -> bytes:
        self.lecturas += 1
        return b""


class ProcesoFalso:
    """Proceso falso: VIVO (``returncode is None``) hasta que alguien lo espera.

    Que nazca vivo es load-bearing: el gate corta temprano si el proceso ya
    murió, así que un doble con ``returncode`` fijo haría imposible observar el
    camino feliz. ``terminar()`` es el guion explícito (muerte temprana);
    ``wait()`` modela "el operador dio Start y la herramienta terminó".
    """

    def __init__(self, *, tool: str = "TexGen") -> None:
        self.pid = PID_POR_TOOL[tool]
        self.returncode: int | None = None
        self.stdout = StreamVacio()
        self.stderr = StreamVacio()
        self.kill_llamado = False
        self.esperas = 0

    def terminar(self, codigo: int = 0) -> None:
        self.returncode = codigo

    def kill(self) -> None:
        self.kill_llamado = True
        self.terminar(-9)

    async def wait(self) -> int:
        self.esperas += 1
        if self.returncode is None:
            self.terminar(0)
        return self.returncode if self.returncode is not None else 0


class LocalizadorFalso:
    def __init__(self, procesos) -> None:
        self._procesos = tuple(procesos)

    def procesos(self):
        return self._procesos


class GuionUIA:
    """Valores de Output por ronda, compartidos entre observadores del gate.

    El initial y el final gate construyen observadores DISTINTOS (cada
    ``ejecutar_gate_sincrono`` arma el suyo), así que el guion vive afuera: es lo
    que permite guionar "MATCH inicial, MISMATCH final" (W12).
    """

    def __init__(self, valores) -> None:
        self.valores = list(valores)
        self.rondas = 0
        self.liberaciones = 0
        self.ventanas = 1

    def fabricar(self):
        return _ObservadorDeGuion(self)


class _ObservadorDeGuion:
    def __init__(self, guion: GuionUIA) -> None:
        self._guion = guion

    def ventanas_de_proceso(self, pid):
        if self._guion.ventanas == 0:
            return ()
        return (VentanaObservada(pid=pid, titulo="TexGen 3.00", class_name="TMainForm", handle="w1"),)

    def controles_de_ventana(self, ventana):
        return (
            ControlObservado(
                pid=ventana.pid,
                automation_id="",
                nombre="",
                tipo_de_control="Edit",
                class_name="TEdit",
            ),
        )

    def leer_valor(self, control):
        guion = self._guion
        guion.rondas += 1
        return guion.valores[min(guion.rondas - 1, len(guion.valores) - 1)]

    def liberar(self) -> None:
        self._guion.liberaciones += 1


def _termina_al_confirmar(proc: ProcesoFalso):
    """Callback que hace MORIR el proceso justo al confirmar (race de W14)."""

    async def _callback(_solicitud) -> None:
        proc.terminar(1)

    return _callback


class ConfirmadorFalso:
    """Confirmador guionado. ``bloqueante`` modela un operador que no responde."""

    def __init__(self, resultado=ResultadoConfirmacion.APROBADA, *, al_confirmar=None, bloqueante=False) -> None:
        self.resultado = resultado
        self.llamadas = 0
        self.ultima_solicitud = None
        self.informes: list[tuple[str, str]] = []
        self._al_confirmar = al_confirmar
        self._bloqueante = bloqueante

    async def confirmar(self, solicitud):
        self.llamadas += 1
        self.ultima_solicitud = solicitud
        if self._al_confirmar is not None:
            await self._al_confirmar(solicitud)
        if self._bloqueante:
            await asyncio.Event().wait()
        return self.resultado

    async def informar(self, *, tool: str, mensaje: str) -> None:
        self.informes.append((tool, mensaje))


class FabricaQueFalla:
    def __init__(self, exc) -> None:
        self._exc = exc

    def __call__(self):
        raise self._exc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _capacidad(guion: GuionUIA | None, confirmador, **overrides) -> CapacidadDeReadinessUIA:
    base = {
        "fabrica_observador": guion.fabricar if guion is not None else lambda: None,
        # Los DOS binarios, cada uno con su pid: el resolver filtra por pid y la
        # revalidación anti-reciclado vuelve a encontrar al mismo proceso.
        "localizador": LocalizadorFalso(
            [
                ProcesoObservado(pid=PID_POR_TOOL["TexGen"], nombre_ejecutable="TexGenx64.exe", ruta_ejecutable=None),
                ProcesoObservado(
                    pid=PID_POR_TOOL["DynDOLOD"], nombre_ejecutable="DynDOLODx64.exe", ruta_ejecutable=None
                ),
            ]
        ),
        "confirmador": confirmador,
        "gate_timeout_segundos": 1.0,
        "gate_intervalo_segundos": 0.01,
        "gate_final_timeout_segundos": 1.0,
        "gate_final_intervalo_segundos": 0.01,
        "readiness_timeout_segundos": 1.0,
        "gracia_externa_segundos": 2.0,
        "intervalo_vigilia_segundos": 0.01,
    }
    base.update(overrides)
    return CapacidadDeReadinessUIA(**base)


def _runner(tmp_path, capacidad: CapacidadDeReadinessUIA | None):
    layout = derivar_layout_de_dyndolod(external_work_root=tmp_path)
    config = MagicMock()
    config.timeout_seconds = 3600
    config.heartbeat_interval = 60
    config.fence_ownership = None
    config.output_layout = layout
    config.external_work_root = None
    config.texgen_exe = pathlib.Path("TexGenx64.exe")
    config.dyndolod_exe = pathlib.Path("DynDOLODx64.exe")
    return DynDOLODRunner(config, readiness=capacidad), layout


async def _correr(runner, proc: ProcesoFalso, *, tool: str = "TexGen"):
    with mock.patch.object(ddl.asyncio, "create_subprocess_exec", mock.AsyncMock(return_value=proc)):
        return await runner._execute_process(  # noqa: SLF001 -- el seam bajo prueba ES privado
            executable=pathlib.Path(BINARIOS[tool]),
            args=["-sse"],
            tool_name=tool,
        )


# ---------------------------------------------------------------------------
# W1-W4 / W16 — el expected es el subroot exclusivo, y es el MISMO del -o:
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tool", "herramienta"), [("TexGen", HerramientaDynDOLOD.TEXGEN), ("DynDOLOD", HerramientaDynDOLOD.DYNDOLOD)]
)
def test_w1_w2_el_expected_es_el_subroot_exclusivo_de_cada_herramienta(tmp_path, tool, herramienta):
    runner, layout = _runner(tmp_path, None)
    assert runner._expected_output_de(tool) == layout.raiz_de(herramienta)  # noqa: SLF001


def test_w3_family_root_nunca_es_el_expected(tmp_path):
    """El family_root no es output: jamás puede ser lo que el gate exige ver."""
    runner, layout = _runner(tmp_path, None)
    for tool in BINARIOS:
        assert runner._expected_output_de(tool) != layout.family_root  # noqa: SLF001


@pytest.mark.parametrize(
    ("tool", "herramienta"), [("TexGen", HerramientaDynDOLOD.TEXGEN), ("DynDOLOD", HerramientaDynDOLOD.DYNDOLOD)]
)
def test_w4_el_expected_coincide_con_el_root_del_argv_o(tmp_path, tool, herramienta):
    """Una sola autoridad: el ``-o:`` del argv y el expected del gate son lo mismo.

    Se comparan CANONICALIZADOS con el mismo canonicalizador que usa el gate, así
    que la igualdad no depende del ``\\`` final que agrega ``_switch_de_ruta``.
    """
    runner, _layout = _runner(tmp_path, None)
    args = runner._build_xedit_args([], herramienta=herramienta)  # noqa: SLF001
    switch = next(a for a in args if a.startswith("-o:"))
    assert canonicalizar_ruta_windows(switch[3:]) == canonicalizar_ruta_windows(
        str(runner._expected_output_de(tool))  # noqa: SLF001
    )


def test_w16_tool_desconocida_es_fail_closed(tmp_path):
    runner, _layout = _runner(tmp_path, None)
    with pytest.raises(ddl.DynDOLODValidationError):
        runner._expected_output_de("LOOT")  # noqa: SLF001


# ---------------------------------------------------------------------------
# W5 — los drains se crean ANTES de esperar el gate
# ---------------------------------------------------------------------------


def test_w5_los_drains_se_crean_antes_de_esperar_el_gate():
    """Ancla enumerante sobre el ORDEN de ``_execute_process``.

    Esperar el gate (que puede durar la configuración humana entera) con los
    PIPE sin consumir haría backpressure sobre la GUI, que escribe su log a
    stderr. El ancla lee las sentencias de primer nivel de la función y exige
    que la creación de ``drain_out``/``drain_err``/``heartbeat`` preceda a la
    llamada del protocolo.
    """
    arbol = ast.parse(MODULO_RUNNER.read_text(encoding="utf-8"))
    ejecutar = next(
        nodo for nodo in ast.walk(arbol) if isinstance(nodo, ast.AsyncFunctionDef) and nodo.name == "_execute_process"
    )

    def _primera_linea(predicado) -> int:
        lineas = [nodo.lineno for nodo in ast.walk(ejecutar) if predicado(nodo)]
        assert lineas, "el ancla no encontró el nodo que dice vigilar"
        return min(lineas)

    def _asignacion(nombre: str):
        return lambda nodo: (
            isinstance(nodo, ast.Assign)
            and any(isinstance(objetivo, ast.Name) and objetivo.id == nombre for objetivo in nodo.targets)
        )

    def _llamada_de_metodo(nombre: str):
        return lambda nodo: (
            isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Attribute) and nodo.func.attr == nombre
        )

    linea_drains = max(_primera_linea(_asignacion(nombre)) for nombre in ("drain_out", "drain_err", "heartbeat"))
    linea_gate = _primera_linea(_llamada_de_metodo("_protocolo_de_readiness"))
    linea_wait = _primera_linea(
        lambda nodo: (
            isinstance(nodo, ast.Call)
            and isinstance(nodo.func, ast.Attribute)
            and nodo.func.attr == "wait"
            and isinstance(nodo.func.value, ast.Name)
            and nodo.func.value.id == "proc"
        )
    )
    assert linea_drains < linea_gate, "el protocolo de readiness corre antes de crear los drains"
    assert linea_gate < linea_wait, "el protocolo de readiness debe correr antes de esperar la salida"


# ---------------------------------------------------------------------------
# W6-W8 — initial gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tool", "herramienta"),
    [("TexGen", HerramientaDynDOLOD.TEXGEN), ("DynDOLOD", HerramientaDynDOLOD.DYNDOLOD)],
)
async def test_w6_initial_match_convoca_al_confirmador(tmp_path, tool, herramienta):
    """W6/W16: el camino feliz es idéntico para las dos herramientas hermanas."""
    layout = derivar_layout_de_dyndolod(external_work_root=tmp_path)
    guion = GuionUIA([str(layout.raiz_de(herramienta))])
    proc = ProcesoFalso(tool=tool)
    confirmador = ConfirmadorFalso()
    runner, _layout = _runner(tmp_path, _capacidad(guion, confirmador))

    await _correr(runner, proc, tool=tool)

    assert confirmador.llamadas == 1
    assert confirmador.ultima_solicitud.tool_name == tool
    assert confirmador.ultima_solicitud.expected_output == layout.raiz_de(herramienta)
    assert confirmador.ultima_solicitud.pid == proc.pid


async def test_w7_initial_mismatch_no_convoca_al_confirmador_y_mata(tmp_path):
    guion = GuionUIA([str(tmp_path / "DynDOLOD")])  # family_root: divergente
    proc = ProcesoFalso()
    confirmador = ConfirmadorFalso()
    runner, _layout = _runner(tmp_path, _capacidad(guion, confirmador))

    with pytest.raises(DynDOLODPreflightUIAError):
        await _correr(runner, proc)

    assert confirmador.llamadas == 0, "un MISMATCH definitivo no debe llegar al operador"
    assert proc.kill_llamado, "el proceso debe morir: no se deja la GUI abierta"
    assert guion.rondas == 1, "un MISMATCH es concluyente: no se reintenta"


async def test_w8_initial_unavailable_no_continua(tmp_path):
    from sky_claw.local.tools.dyndolod_uia_preflight import UIANoDisponibleError

    proc = ProcesoFalso()
    confirmador = ConfirmadorFalso()
    capacidad = _capacidad(None, confirmador, fabrica_observador=FabricaQueFalla(UIANoDisponibleError("sin COM")))
    runner, _layout = _runner(tmp_path, capacidad)

    with pytest.raises(DynDOLODPreflightUIAError) as excinfo:
        await _correr(runner, proc)

    assert excinfo.value.resultado.razon.value == "UIA_UNAVAILABLE"
    assert confirmador.llamadas == 0
    assert proc.kill_llamado


# ---------------------------------------------------------------------------
# W9-W12 — confirmación humana y final gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "resultado",
    [ResultadoConfirmacion.DENEGADA, ResultadoConfirmacion.TIMEOUT, ResultadoConfirmacion.CANAL_NO_DISPONIBLE],
)
async def test_w9_w10_un_rechazo_mata_el_proceso_y_no_corre_el_final(tmp_path, resultado):
    layout = derivar_layout_de_dyndolod(external_work_root=tmp_path)
    guion = GuionUIA([str(layout.texgen_root)])
    proc = ProcesoFalso()
    confirmador = ConfirmadorFalso(resultado)
    runner, _layout = _runner(tmp_path, _capacidad(guion, confirmador))

    with pytest.raises(DynDOLODReadinessProtocolError) as excinfo:
        await _correr(runner, proc)

    assert excinfo.value.razon.value == resultado.value
    assert proc.kill_llamado
    assert guion.rondas == 1, "el final gate NO debe observar tras un rechazo"
    assert confirmador.informes == []


async def test_w11_tras_aprobar_el_final_gate_vuelve_a_observar(tmp_path):
    layout = derivar_layout_de_dyndolod(external_work_root=tmp_path)
    guion = GuionUIA([str(layout.texgen_root)])  # siempre correcto
    proc = ProcesoFalso()
    confirmador = ConfirmadorFalso()
    runner, _layout = _runner(tmp_path, _capacidad(guion, confirmador))

    stdout, _stderr, returncode, _dur = await _correr(runner, proc)

    assert guion.rondas == 2, "el final gate debe RE-observar, no reusar el resultado inicial"
    assert returncode == 0
    assert stdout == ""
    assert confirmador.informes and confirmador.informes[0][0] == "TexGen"


async def test_w12_initial_match_con_output_cambiado_despues_del_hitl_es_fail_closed(tmp_path):
    """La race que justifica el final gate: el operador edita el Output y aprueba."""
    layout = derivar_layout_de_dyndolod(external_work_root=tmp_path)
    guion = GuionUIA([str(layout.texgen_root), str(layout.family_root)])
    proc = ProcesoFalso()
    confirmador = ConfirmadorFalso()
    runner, _layout = _runner(tmp_path, _capacidad(guion, confirmador))

    with pytest.raises(DynDOLODPreflightUIAError) as excinfo:
        await _correr(runner, proc)

    assert excinfo.value.resultado.razon.value == "OUTPUT_DIFIERE"
    assert guion.rondas == 2
    assert proc.kill_llamado
    assert confirmador.informes == [], "no se anuncia 'Start habilitado' sobre un veredicto rojo"


# ---------------------------------------------------------------------------
# W13 — stale transitorio que se corrige antes del deadline
# ---------------------------------------------------------------------------


async def test_w13_stale_ilegible_luego_expected_llega_al_hitl(tmp_path):
    """El TEdit puede no exponer texto al nacer; el gate espera y cierra en MATCH."""
    layout = derivar_layout_de_dyndolod(external_work_root=tmp_path)
    guion = GuionUIA([None, None, str(layout.texgen_root)])
    proc = ProcesoFalso()
    confirmador = ConfirmadorFalso()
    runner, _layout = _runner(tmp_path, _capacidad(guion, confirmador))

    await _correr(runner, proc)

    assert confirmador.llamadas == 1
    assert guion.rondas == 4, "3 rondas del initial gate + 1 del final"


# ---------------------------------------------------------------------------
# W14 — muerte temprana del proceso
# ---------------------------------------------------------------------------


async def test_w14_proceso_que_muere_durante_la_espera_corta_temprano(tmp_path):
    """No se espera el deadline de readiness: la instancia ya no existe."""
    layout = derivar_layout_de_dyndolod(external_work_root=tmp_path)
    guion = GuionUIA([str(layout.texgen_root)])

    async def _muere_al_confirmar(_solicitud):
        proc.terminar(1)

    proc = ProcesoFalso()
    confirmador = ConfirmadorFalso(bloqueante=True, al_confirmar=_muere_al_confirmar)
    runner, _layout = _runner(tmp_path, _capacidad(guion, confirmador))

    with pytest.raises(DynDOLODReadinessProtocolError) as excinfo:
        await _correr(runner, proc)

    assert excinfo.value.razon is ResolucionProtocoloReadiness.PROCESO_TERMINO
    assert guion.rondas == 1, "no se re-observa un proceso muerto"


async def test_w14b_proceso_que_muere_durante_el_initial_gate_corta_temprano(tmp_path):
    """El gate ve ``proceso_vivo=False`` y devuelve el UNKNOWN de inmediato."""
    guion = GuionUIA([None])
    guion.ventanas = 0  # sin ventana: UNKNOWN transitorio

    async def _muere(_solicitud):
        proc.terminar(1)

    proc = ProcesoFalso()
    confirmador = ConfirmadorFalso()
    capacidad = _capacidad(guion, confirmador)
    runner, _layout = _runner(tmp_path, capacidad)

    async def _matar_pronto():
        await asyncio.sleep(0.02)
        proc.terminar(1)

    tarea = asyncio.create_task(_matar_pronto())
    with pytest.raises(DynDOLODPreflightUIAError):
        await _correr(runner, proc)
    await tarea
    assert confirmador.llamadas == 0


# ---------------------------------------------------------------------------
# W15 — cancelación
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("punto", ["initial", "hitl", "final"])
async def test_w15_cancelacion_mata_limpia_y_propaga(tmp_path, punto):
    layout = derivar_layout_de_dyndolod(external_work_root=tmp_path)
    if punto == "initial":
        guion = GuionUIA([None])
        guion.ventanas = 0  # el initial gate queda esperando
        confirmador = ConfirmadorFalso()
    elif punto == "hitl":
        guion = GuionUIA([str(layout.texgen_root)])
        confirmador = ConfirmadorFalso(bloqueante=True)
    else:
        guion = GuionUIA([str(layout.texgen_root), None])
        confirmador = ConfirmadorFalso()

    proc = ProcesoFalso()
    runner, _layout = _runner(tmp_path, _capacidad(guion, confirmador))

    tarea = asyncio.create_task(_correr(runner, proc))
    await asyncio.sleep(0.05)
    tarea.cancel()
    with pytest.raises(asyncio.CancelledError):
        await tarea

    assert proc.kill_llamado, "una cancelación debe matar el árbol antes de propagar"
    # El gate corre en un hilo del pool: cancelar el await NO lo mata, y su
    # `finally` libera cuando su propio deadline se agota (cota acotada, no
    # huérfano infinito). Se espera esa liberación en vez de asumirla inmediata.
    for _ in range(400):
        if guion.liberaciones >= 1:
            break
        await asyncio.sleep(0.01)
    assert guion.liberaciones >= 1, "el observador debe liberarse en el hilo que lo abrió"


# ---------------------------------------------------------------------------
# Modo directo
# ---------------------------------------------------------------------------


async def test_sin_capacidad_el_protocolo_no_corre(tmp_path):
    """``readiness=None`` es el modo directo (tests/rig): no hay gate ni HITL."""
    guion = GuionUIA([str(tmp_path)])
    proc = ProcesoFalso()
    proc.terminar(0)
    runner, _layout = _runner(tmp_path, None)

    _stdout, _stderr, returncode, _dur = await _correr(runner, proc)

    assert returncode == 0
    assert guion.rondas == 0
