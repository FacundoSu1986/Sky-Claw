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
    DynDOLODTimeoutError,
    ReadinessMode,
)
from sky_claw.local.tools.dyndolod_uia_ejecutor import EjecutorGateEnProceso
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


class ProcesoQueNoTermina(ProcesoFalso):
    """Vivo hasta ``kill()``/``terminar()``: modela ``proc.wait()`` bloqueado.

    El doble feliz termina al primer ``wait()``; acá el presupuesto global se
    prueba contra un proceso que seguiría generando si el runner le regalara
    un timeout fresco después del readiness.
    """

    def __init__(self, *, tool: str = "TexGen") -> None:
        super().__init__(tool=tool)
        self._salida = asyncio.Event()

    def terminar(self, codigo: int = 0) -> None:
        self.returncode = codigo
        self._salida.set()

    async def wait(self) -> int:
        self.esperas += 1
        await self._salida.wait()
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
        #: Cuántos controles devuelve el árbol. 2 = selector ambiguo.
        self.controles = 1

    def fabricar(self):
        return _ObservadorDeGuion(self)


class _ObservadorDeGuion:
    def __init__(self, guion: GuionUIA) -> None:
        self._guion = guion

    def ventanas_de_proceso(self, pid):
        if self._guion.ventanas == 0:
            return ()
        return tuple(
            VentanaObservada(pid=pid, titulo=f"TexGen 3.00 {n}", class_name="TMainForm", handle=f"w{n}")
            for n in range(self._guion.ventanas)
        )

    def controles_de_ventana(self, ventana):
        return tuple(
            ControlObservado(
                pid=ventana.pid,
                automation_id="",
                nombre="",
                tipo_de_control="Edit",
                class_name="TEdit",
            )
            for _ in range(self._guion.controles)
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

    def __init__(
        self,
        resultado=ResultadoConfirmacion.APROBADA,
        *,
        al_confirmar=None,
        bloqueante=False,
        informar_bloqueante=False,
    ) -> None:
        self.resultado = resultado
        self.llamadas = 0
        self.ultima_solicitud = None
        self.informes: list[tuple[str, str]] = []
        self._al_confirmar = al_confirmar
        self._bloqueante = bloqueante
        #: Un aviso que nunca retorna: el runner lo acota con su gracia externa.
        self._informar_bloqueante = informar_bloqueante

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
        if self._informar_bloqueante:
            await asyncio.Event().wait()


class FabricaQueFalla:
    def __init__(self, exc) -> None:
        self._exc = exc

    def __call__(self):
        raise self._exc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _capacidad(guion: GuionUIA | None, confirmador, **overrides) -> CapacidadDeReadinessUIA:
    """Capacidad con el ejecutor EN PROCESO (observadores falsos, cooperativos).

    El ejecutor productivo (helper descartable) tiene su propia suite
    (``test_dyndolod_uia_ejecutor.py``); acá lo que se prueba es el lifecycle del
    protocolo, que es el mismo para los dos ejecutores.
    """
    fabrica = overrides.pop("fabrica_observador", guion.fabricar if guion is not None else lambda: None)
    ejecutor = EjecutorGateEnProceso(
        fabrica_observador=fabrica,
        # Los DOS binarios, cada uno con su pid: el resolver filtra por pid y la
        # revalidación anti-reciclado vuelve a encontrar al mismo proceso.
        localizador=LocalizadorFalso(
            [
                ProcesoObservado(pid=PID_POR_TOOL["TexGen"], nombre_ejecutable="TexGenx64.exe", ruta_ejecutable=None),
                ProcesoObservado(
                    pid=PID_POR_TOOL["DynDOLOD"], nombre_ejecutable="DynDOLODx64.exe", ruta_ejecutable=None
                ),
            ]
        ),
        gracia_externa_segundos=2.0,
    )
    base = {
        "ejecutor": ejecutor,
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


def _runner(tmp_path, capacidad: CapacidadDeReadinessUIA | ReadinessMode):
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


async def _correr(runner, proc: ProcesoFalso, *, tool: str = "TexGen", timeout: int | None = None):
    with mock.patch.object(ddl.asyncio, "create_subprocess_exec", mock.AsyncMock(return_value=proc)):
        return await runner._execute_process(  # noqa: SLF001 -- el seam bajo prueba ES privado
            executable=pathlib.Path(BINARIOS[tool]),
            args=["-sse"],
            tool_name=tool,
            timeout=timeout,
        )


async def _timeout_otorgado_a_proc_wait(proc: ProcesoFalso, orig, aw, timeout=None, **kwargs):
    """Envuelve ``wait_for`` y captura el timeout SOLO cuando arranca ``proc.wait()``."""
    esperas_antes = proc.esperas
    try:
        return await orig(aw, timeout=timeout, **kwargs)
    finally:
        if proc.esperas > esperas_antes:
            proc.timeouts_de_wait.append(timeout)  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# W1-W4 / W16 — el expected es el subroot exclusivo, y es el MISMO del -o:
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tool", "herramienta"), [("TexGen", HerramientaDynDOLOD.TEXGEN), ("DynDOLOD", HerramientaDynDOLOD.DYNDOLOD)]
)
def test_w1_w2_el_expected_es_el_subroot_exclusivo_de_cada_herramienta(tmp_path, tool, herramienta):
    runner, layout = _runner(tmp_path, ReadinessMode.DISABLED_FOR_TEST)
    assert runner._expected_output_de(tool) == layout.raiz_de(herramienta)  # noqa: SLF001


def test_w3_family_root_nunca_es_el_expected(tmp_path):
    """El family_root no es output: jamás puede ser lo que el gate exige ver."""
    runner, layout = _runner(tmp_path, ReadinessMode.DISABLED_FOR_TEST)
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
    runner, _layout = _runner(tmp_path, ReadinessMode.DISABLED_FOR_TEST)
    args = runner._build_xedit_args([], herramienta=herramienta)  # noqa: SLF001
    switch = next(a for a in args if a.startswith("-o:"))
    assert canonicalizar_ruta_windows(switch[3:]) == canonicalizar_ruta_windows(
        str(runner._expected_output_de(tool))  # noqa: SLF001
    )


def test_w16_tool_desconocida_es_fail_closed(tmp_path):
    runner, _layout = _runner(tmp_path, ReadinessMode.DISABLED_FOR_TEST)
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


async def test_w7_initial_mismatch_canonicalizable_convoca_al_confirmador(tmp_path):
    """W7 REFUTADO POR EL RIG REAL (2026-09-10) — el MISMATCH inicial es CONFIGURABLE.

    La versión anterior de este test exigía que un MISMATCH inicial matara el
    proceso sin llamar al confirmador. El rig real refutó esa expectativa: TexGen
    arranca mostrando el Output del preset rancio, y el único que puede
    corregirlo es el operador. Bloquear ahí hacía imposible el flujo documentado
    "el operador corrige Output y después confirma". Lo que este test ancla
    ahora: un MISMATCH concluyente (proceso e identidad probados, ventana y
    control únicos, valor legible y canonicalizable) llega al HITL con el valor
    observado; la autorización de Start sigue siendo del final gate.
    """
    stale = tmp_path / "Stale TexGen"
    guion = GuionUIA([str(stale)])
    proc = ProcesoFalso()
    confirmador = ConfirmadorFalso(resultado=ResultadoConfirmacion.DENEGADA)
    runner, _layout = _runner(tmp_path, _capacidad(guion, confirmador))

    with pytest.raises(DynDOLODReadinessProtocolError) as excinfo:
        await _correr(runner, proc)

    assert excinfo.value.razon is ResolucionProtocoloReadiness.DENEGADA
    assert confirmador.llamadas == 1, "un MISMATCH configurable DEBE llegar al operador"
    assert confirmador.ultima_solicitud.observed_output == str(stale)
    assert proc.kill_llamado, "un deny mata el proceso: no se deja la GUI abierta"
    assert guion.rondas == 1, "el MISMATCH es concluyente: no se reintenta, se convoca"


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


async def test_el_aviso_al_operador_que_se_cuelga_no_mata_la_corrida_verificada(tmp_path):
    """Finding Qodo refutado con conducta: un aviso colgado no tumba el run.

    El runner acota ``informar`` con ``asyncio.wait_for``; ese timeout convierte
    la cancelación del aviso en ``TimeoutError``, que ``_informar_operador`` ya
    atrapa y loguea. Los dos gates ya pasaron: la corrida sigue y el proceso NO
    se mata. (Absorber ``CancelledError`` dentro del guard, como pedía el
    finding, rompería la semántica de cancelación externa del run.)
    """
    layout = derivar_layout_de_dyndolod(external_work_root=tmp_path)
    guion = GuionUIA([str(layout.texgen_root)])
    proc = ProcesoFalso()
    confirmador = ConfirmadorFalso(informar_bloqueante=True)
    runner, _layout = _runner(tmp_path, _capacidad(guion, confirmador, gracia_externa_segundos=0.2))

    _stdout, _stderr, returncode, _dur = await _correr(runner, proc)

    assert returncode == 0, "el aviso colgado no puede tumbar una corrida verificada"
    assert not proc.kill_llamado, "el proceso no se mata por un aviso best-effort"
    assert confirmador.informes and confirmador.informes[0][0] == "TexGen"


async def test_w14c_confirmador_rapido_que_mata_el_proceso_igual_falla_cerrado(tmp_path):
    """La race 'confirmación rápida vs muerte' corta fail-closed por ALGUNO de los dos caminos.

    Si gana la vigilia, el protocolo corta con ``PROCESO_TERMINO``; si gana la
    confirmación rápida, el final gate ve la instancia muerta y corta igual. En
    los dos casos el proceso se mata y el veredicto es tipado — no hay camino en
    el que la corrida continúe sobre un binario inexistente.
    """
    layout = derivar_layout_de_dyndolod(external_work_root=tmp_path)
    # El final gate nunca va a leer un MATCH: la observación no puede concluir
    # antes de que la vigilia vea la muerte.
    guion = GuionUIA([str(layout.texgen_root), None])
    proc = ProcesoFalso()

    async def _muere_al_confirmar(_solicitud):
        proc.terminar(1)

    confirmador = ConfirmadorFalso(al_confirmar=_muere_al_confirmar)
    runner, _layout = _runner(tmp_path, _capacidad(guion, confirmador))

    with pytest.raises((DynDOLODReadinessProtocolError, DynDOLODPreflightUIAError)):
        await _correr(runner, proc)

    assert proc.kill_llamado


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
# A1-A11 — la familia del encargo de Fase 3 (initial readiness ≠ autorización)
# ---------------------------------------------------------------------------
#
# A9-A11 viven en ``tests/test_hitl.py`` (son el texto del prompt del adapter);
# el resto acá, porque son lifecycle del runner.


async def test_a1_initial_mismatch_canonicalizable_invoca_el_hitl(tmp_path):
    """A1: el MISMATCH válido entra a la fase de corrección humana (no mata)."""
    stale = tmp_path / "Stale TexGen"
    guion = GuionUIA([str(stale)])
    proc = ProcesoFalso()
    confirmador = ConfirmadorFalso(resultado=ResultadoConfirmacion.DENEGADA)
    runner, _layout = _runner(tmp_path, _capacidad(guion, confirmador))

    with pytest.raises(DynDOLODReadinessProtocolError):
        await _correr(runner, proc)

    assert confirmador.llamadas == 1, "el initial CONFIGURABLE_MISMATCH debe llegar al operador"
    assert confirmador.ultima_solicitud.observed_output == str(stale)


async def test_a2_initial_mismatch_mas_deny_mata_el_proceso(tmp_path):
    """A2: MISMATCH inicial configurable + DENY del operador → proceso muerto."""
    guion = GuionUIA([str(tmp_path / "Stale TexGen")])
    proc = ProcesoFalso()
    confirmador = ConfirmadorFalso(resultado=ResultadoConfirmacion.DENEGADA)
    runner, _layout = _runner(tmp_path, _capacidad(guion, confirmador))

    with pytest.raises(DynDOLODReadinessProtocolError) as excinfo:
        await _correr(runner, proc)

    assert excinfo.value.razon is ResolucionProtocoloReadiness.DENEGADA
    assert proc.kill_llamado
    assert confirmador.informes == [], "no se anuncia 'Start habilitado' tras un deny"


async def test_a3_initial_mismatch_approve_y_final_match_continua(tmp_path):
    """A3: la corrección humana funciona: MISMATCH inicial → approve → MATCH final."""
    layout = derivar_layout_de_dyndolod(external_work_root=tmp_path)
    guion = GuionUIA([str(tmp_path / "Stale TexGen"), str(layout.texgen_root)])
    proc = ProcesoFalso()
    confirmador = ConfirmadorFalso()
    runner, _layout = _runner(tmp_path, _capacidad(guion, confirmador))

    _stdout, _stderr, returncode, _dur = await _correr(runner, proc)

    assert returncode == 0
    assert confirmador.llamadas == 1
    assert guion.rondas == 2, "el final gate RE-OBSERVA; no reusa el MISMATCH inicial"
    assert confirmador.informes and confirmador.informes[0][0] == "TexGen"


async def test_a4_initial_mismatch_approve_y_final_mismatch_fail_closed(tmp_path):
    """A4: la excepción del initial NO se hereda al final: MISMATCH final corta."""
    guion = GuionUIA([str(tmp_path / "Stale A"), str(tmp_path / "Stale B")])
    proc = ProcesoFalso()
    confirmador = ConfirmadorFalso()
    runner, _layout = _runner(tmp_path, _capacidad(guion, confirmador))

    with pytest.raises(DynDOLODPreflightUIAError) as excinfo:
        await _correr(runner, proc)

    assert excinfo.value.resultado.razon.value == "OUTPUT_DIFIERE"
    assert guion.rondas == 2
    assert proc.kill_llamado
    assert confirmador.informes == [], "no se anuncia 'Start habilitado' sobre un veredicto rojo"


async def test_a5_initial_match_igual_invoca_el_hitl(tmp_path):
    """A5: MATCH inicial también convoca: falta elegir preset/worldspaces."""
    layout = derivar_layout_de_dyndolod(external_work_root=tmp_path)
    guion = GuionUIA([str(layout.texgen_root)])
    proc = ProcesoFalso()
    confirmador = ConfirmadorFalso(resultado=ResultadoConfirmacion.DENEGADA)
    runner, _layout = _runner(tmp_path, _capacidad(guion, confirmador))

    with pytest.raises(DynDOLODReadinessProtocolError):
        await _correr(runner, proc)

    assert confirmador.llamadas == 1
    assert confirmador.ultima_solicitud.observed_output is not None


async def test_a6_uia_unavailable_no_invoca_el_hitl(tmp_path):
    """A6: sin sensor no hay configuración humana posible: fail-closed directo."""
    from sky_claw.local.tools.dyndolod_uia_preflight import UIANoDisponibleError

    proc = ProcesoFalso()
    confirmador = ConfirmadorFalso()
    capacidad = _capacidad(None, confirmador, fabrica_observador=FabricaQueFalla(UIANoDisponibleError("sin COM")))
    runner, _layout = _runner(tmp_path, capacidad)

    with pytest.raises(DynDOLODPreflightUIAError):
        await _correr(runner, proc)

    assert confirmador.llamadas == 0
    assert proc.kill_llamado


async def test_a7_ventana_ambigua_no_invoca_el_hitl(tmp_path):
    """A7: dos ventanas top-level = no se sabe cuál es: no hay corrección humana."""
    guion = GuionUIA([str(tmp_path / "Stale")])
    guion.ventanas = 2
    proc = ProcesoFalso()
    confirmador = ConfirmadorFalso()
    runner, _layout = _runner(tmp_path, _capacidad(guion, confirmador))

    with pytest.raises(DynDOLODPreflightUIAError) as excinfo:
        await _correr(runner, proc)

    assert excinfo.value.resultado.razon.value == "VENTANA_AMBIGUA"
    assert confirmador.llamadas == 0
    assert guion.rondas == 0, "la ambigüedad corta antes de leer el control"


async def test_a8_control_ambiguo_no_invoca_el_hitl(tmp_path):
    """A8: dos controles que matchean el selector = la lectura no es inequívoca."""
    guion = GuionUIA([str(tmp_path / "Stale")])
    guion.controles = 2
    proc = ProcesoFalso()
    confirmador = ConfirmadorFalso()
    runner, _layout = _runner(tmp_path, _capacidad(guion, confirmador))

    with pytest.raises(DynDOLODPreflightUIAError) as excinfo:
        await _correr(runner, proc)

    assert excinfo.value.resultado.razon.value == "CONTROL_AMBIGUO"
    assert confirmador.llamadas == 0
    assert guion.rondas == 0, "el control ambiguo corta antes de leer el valor"


# ---------------------------------------------------------------------------
# Modo directo — opt-out EXPLÍCITO
# ---------------------------------------------------------------------------


async def test_el_opt_out_explicito_no_corre_el_protocolo(tmp_path):
    """``ReadinessMode.DISABLED_FOR_TEST`` es el único modo directo: no hay gate."""
    guion = GuionUIA([str(tmp_path)])
    proc = ProcesoFalso()
    proc.terminar(0)
    runner, _layout = _runner(tmp_path, ReadinessMode.DISABLED_FOR_TEST)

    _stdout, _stderr, returncode, _dur = await _correr(runner, proc)

    assert returncode == 0
    assert guion.rondas == 0


def test_readiness_none_ya_no_es_un_modo_valido(tmp_path):
    """El default silencioso quedó prohibido también en runtime (no sólo por tipo)."""
    layout = derivar_layout_de_dyndolod(external_work_root=tmp_path)
    config = MagicMock()
    config.timeout_seconds = 3600
    config.heartbeat_interval = 60
    config.fence_ownership = None
    config.output_layout = layout
    config.texgen_exe = pathlib.Path("TexGenx64.exe")
    config.dyndolod_exe = pathlib.Path("DynDOLODx64.exe")

    with pytest.raises(ValueError, match="readiness es obligatorio"):
        DynDOLODRunner(config, readiness=None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Presupuesto whole-process: el reloj nace en el spawn y cubre el readiness
# ---------------------------------------------------------------------------
#
# El contrato de ``timeout_seconds`` es un único presupuesto desde que el
# subprocess ya existe. El bug validado era: readiness corre sin cota global y
# ``proc.wait()`` recibe un ``effective_timeout`` fresco.


def _layout_windows(tmp_path):
    """Raíz con sintaxis Win32: el gate rechaza POSIX (``ESPERADO_NO_CANONICALIZABLE``)."""
    return derivar_layout_de_dyndolod(external_work_root=pathlib.Path(r"C:\SkyClawWork") / tmp_path.name)


def _correr_con_layout_windows(tmp_path, capacidad):
    runner, _posix = _runner(tmp_path, capacidad)
    layout = _layout_windows(tmp_path)
    runner._config.output_layout = layout  # noqa: SLF001
    return runner, layout


@pytest.mark.parametrize("tool", ["TexGen", "DynDOLOD"])
async def test_el_readiness_que_agota_el_presupuesto_es_timeout_global(tmp_path, tool):
    """Caso A: el HITL que se cuelga no recibe un timeout de proceso nuevo.

    Si el presupuesto global no cubriera el readiness, el confirmador bloqueante
    caducaría por su cota INTERNA y el veredicto sería de protocolo — no
    ``DynDOLODTimeoutError``. El proceso no puede quedar vivo.
    """
    herramienta = HerramientaDynDOLOD.TEXGEN if tool == "TexGen" else HerramientaDynDOLOD.DYNDOLOD
    layout = _layout_windows(tmp_path)
    guion = GuionUIA([str(layout.raiz_de(herramienta))])
    proc = ProcesoQueNoTermina(tool=tool)
    confirmador = ConfirmadorFalso(bloqueante=True)
    runner, _layout = _correr_con_layout_windows(
        tmp_path,
        _capacidad(
            guion,
            confirmador,
            readiness_timeout_segundos=5.0,
            gracia_externa_segundos=0.05,
            gate_timeout_segundos=5.0,
            gate_final_timeout_segundos=5.0,
        ),
    )

    with pytest.raises(DynDOLODTimeoutError) as excinfo:
        await _correr(runner, proc, tool=tool, timeout=1)

    assert excinfo.value.timeout_seconds == 1
    assert excinfo.value.tool_name == tool
    assert proc.kill_llamado, "el timeout global debe matar el árbol"
    assert proc.returncode is not None, "el proceso no puede quedar vivo"
    assert confirmador.llamadas == 1, "el HITL arrancó: el timeout no fue un gate inicial"


@pytest.mark.parametrize("tool", ["TexGen", "DynDOLOD"])
async def test_proc_wait_recibe_solo_el_presupuesto_restante(tmp_path, tool, monkeypatch):
    """Caso B: readiness consume parte del presupuesto; ``proc.wait`` no se reinicia.

    El HITL espera 0.4 s reales. El timeout configurado es 1 s. ``proc.wait()``
    tiene que recibir el resto (< 1 s), no un presupuesto fresco de 1 s.
    No se parchea ``time.monotonic``: asyncio también lo usa para ``wait_for``.
    """
    herramienta = HerramientaDynDOLOD.TEXGEN if tool == "TexGen" else HerramientaDynDOLOD.DYNDOLOD
    layout = _layout_windows(tmp_path)
    guion = GuionUIA([str(layout.raiz_de(herramienta))])
    proc = ProcesoQueNoTermina(tool=tool)
    proc.timeouts_de_wait = []

    async def _consumir_presupuesto(_solicitud) -> None:
        await asyncio.sleep(0.4)

    confirmador = ConfirmadorFalso(al_confirmar=_consumir_presupuesto)
    runner, _layout = _correr_con_layout_windows(tmp_path, _capacidad(guion, confirmador))

    orig = ddl.asyncio.wait_for

    async def _espiar(aw, timeout=None, **kwargs):
        return await _timeout_otorgado_a_proc_wait(proc, orig, aw, timeout=timeout, **kwargs)

    monkeypatch.setattr(ddl.asyncio, "wait_for", _espiar)

    with pytest.raises(DynDOLODTimeoutError) as excinfo:
        await _correr(runner, proc, tool=tool, timeout=1)

    assert excinfo.value.timeout_seconds == 1, "el error reporta el presupuesto original, no el resto"
    assert proc.timeouts_de_wait, "proc.wait debió arrancar con el resto del presupuesto"
    restante = proc.timeouts_de_wait[0]
    assert restante is not None
    assert restante < 1.0, "un timeout fresco de 1 s significa que el readiness no consumió presupuesto"
    assert restante > 0.0
    assert proc.kill_llamado


@pytest.mark.parametrize("tool", ["TexGen", "DynDOLOD"])
async def test_readiness_rapido_y_wait_dentro_del_resto_es_exito(tmp_path, tool):
    """Caso C: gates + HITL cortos y ``proc.wait`` que termina dentro del resto."""
    herramienta = HerramientaDynDOLOD.TEXGEN if tool == "TexGen" else HerramientaDynDOLOD.DYNDOLOD
    layout = _layout_windows(tmp_path)
    expected = str(layout.raiz_de(herramienta))
    guion = GuionUIA([expected])
    proc = ProcesoFalso(tool=tool)
    confirmador = ConfirmadorFalso()
    runner, _layout = _correr_con_layout_windows(tmp_path, _capacidad(guion, confirmador))

    _stdout, _stderr, returncode, _dur = await _correr(runner, proc, tool=tool, timeout=5)

    assert returncode == 0
    assert confirmador.llamadas == 1
    assert guion.rondas == 2
    assert not proc.kill_llamado
    assert confirmador.informes and confirmador.informes[0][0] == tool


async def test_cancelacion_durante_readiness_no_se_reporta_como_timeout(tmp_path):
    """Caso E: una cancelación externa sigue siendo ``CancelledError``, no timeout."""
    layout = _layout_windows(tmp_path)
    guion = GuionUIA([str(layout.texgen_root)])
    proc = ProcesoQueNoTermina()
    confirmador = ConfirmadorFalso(bloqueante=True)
    runner, _layout = _correr_con_layout_windows(tmp_path, _capacidad(guion, confirmador))

    tarea = asyncio.create_task(_correr(runner, proc, timeout=30))
    await asyncio.sleep(0.05)
    tarea.cancel()
    with pytest.raises(asyncio.CancelledError):
        await tarea

    assert proc.kill_llamado, "la cancelación debe matar el árbol antes de propagar"
