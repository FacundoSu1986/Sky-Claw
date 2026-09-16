"""T5-v2.1 — wiring productivo: censo de constructores y puertos satisfechos.

El modo directo del runner (``readiness=None``) NO corre el protocolo. Eso es
correcto para tests/rig y peligroso en producción, así que la garantía no es un
default sino un CENSO: se enumeran todos los sitios que construyen el runner en
``sky_claw/**`` **y en ``docs/validation/**``** (harnesses de rig comprometidos,
callers ejecutables reales — F2 de #590) y se exige que cada uno cablee el modo
de readiness. Un sitio nuevo —o uno que la olvide— rompe el test en vez de
salir a producción sin gate.

Es el mismo instrumento que el repo ya usa para el servicio
(``tests/test_dyndolod_workspace.py::test_censo_de_constructores_del_servicio_dyndolod``):
enumerar, no muestrear.
"""

from __future__ import annotations

import ast
import pathlib
import sys

import pytest

from sky_claw.local.tools.dyndolod_uia_ejecutor import (
    FLAG_HELPER_UIA,
    EjecutorGateEnProceso,
    EjecutorGatePorHelper,
    comando_del_helper,
)
from sky_claw.local.tools.dyndolod_uia_gate import ConfirmadorDeConfiguracion
from sky_claw.local.tools.dyndolod_uia_preflight import (
    LocalizadorDeProcesos,
    LocalizadorPsutil,
)
from sky_claw.local.tools.dyndolod_uia_windows import ObservadorUIAWindows, construir_observador_windows

RAIZ = pathlib.Path(__file__).resolve().parents[1]

#: Sitios que construyen el runner. ``dyndolod_service.py`` es el productivo; el
#: harness del rig real PR-580 está COMPROMETIDO en ``docs/validation`` y es un
#: caller ejecutable de verdad — F2 (#590) midió que dejarlo fuera del censo
#: permitió que quedara roto (``DynDOLODRunner(cfg)`` sin ``readiness=``,
#: ``TypeError`` al correrlo) durante rondas enteras. El ejemplo del docstring
#: de ``dyndolod_runner`` no cuenta: es texto, no un ``ast.Call``.
CONSTRUCTORES_DEL_RUNNER: frozenset[str] = frozenset(
    {
        "sky_claw/local/tools/dyndolod_service.py",
        "docs/validation/2026-09-13_pr580_real_rig/run_phase.py",
    }
)

#: Árboles de fuentes Python EJECUTABLES comprometidos que el censo enumera.
#: ``docs/validation`` entra por F2 (#590): un harness de rig es código que se
#: corre contra la máquina real, no documentación narrativa — el censo no hace
#: grep sobre prosa, parsea ASTs de archivos ``.py`` y exige ``readiness=`` en
#: cada ``ast.Call`` a ``DynDOLODRunner``. ``tests/`` queda afuera a propósito:
#: sus constructores son dobles de prueba, no callers comprometidos.
CENSO_DE_CARPETAS_DEL_RUNNER: tuple[str, ...] = ("sky_claw", "docs/validation")

#: Sitios que construyen el SERVICIO. El preview es plan-only y declara el
#: opt-out explícito; el composition root cablea la capacidad productiva. El
#: censo exige ``readiness=`` en los dos: un ``None`` silencioso en un servicio
#: nuevo no puede dejar la etapa 9 sin gate.
CONSTRUCTORES_DEL_SERVICIO: frozenset[str] = frozenset(
    {
        "sky_claw/app/orchestrator/orchestration_composition.py",
        "sky_claw/app/orchestrator/preview/chain_preview_service.py",
    }
)

#: Archivos que participan del wiring de T5-v2.1.
COMPOSITION = RAIZ / "sky_claw" / "app" / "orchestrator" / "orchestration_composition.py"
ADAPTER = RAIZ / "sky_claw" / "app" / "orchestrator" / "dyndolod_readiness_hitl.py"
SERVICE = RAIZ / "sky_claw" / "local" / "tools" / "dyndolod_service.py"
RUNNER = RAIZ / "sky_claw" / "local" / "tools" / "dyndolod_runner.py"
GATE = RAIZ / "sky_claw" / "local" / "tools" / "dyndolod_uia_gate.py"
EJECUTOR = RAIZ / "sky_claw" / "local" / "tools" / "dyndolod_uia_ejecutor.py"
HELPER = RAIZ / "sky_claw" / "local" / "tools" / "dyndolod_uia_helper.py"
MAIN = RAIZ / "sky_claw" / "__main__.py"


def _arbol(archivo: pathlib.Path) -> ast.Module:
    return ast.parse(archivo.read_text(encoding="utf-8"))


def _nombre_de_calle(func: ast.AST) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _llamadas(arbol: ast.Module, nombre: str) -> list[ast.Call]:
    return [nodo for nodo in ast.walk(arbol) if isinstance(nodo, ast.Call) and _nombre_de_calle(nodo.func) == nombre]


def _kwargs(llamada: ast.Call) -> set[str]:
    return {kw.arg for kw in llamada.keywords if kw.arg is not None}


# ---------------------------------------------------------------------------
# Censo de constructores del runner
# ---------------------------------------------------------------------------


def test_censo_de_constructores_del_runner() -> None:
    """Todo constructor comprometido del runner pasa ``readiness=``, sin excepciones.

    Enumeración, no muestreo: se parsea por AST cada ``.py`` de los árboles
    comprometidos (:data:`CENSO_DE_CARPETAS_DEL_RUNNER`) y toda llamada a
    ``DynDOLODRunner`` que aparezca tiene que llevar el kwarg explícito. Un
    harness de rig nuevo (o uno viejo sin migrar, como el del PR-580) rompe el
    test en vez de quedar roto en silencio hasta la próxima corrida real.
    """
    encontrados: dict[str, bool] = {}
    for carpeta in CENSO_DE_CARPETAS_DEL_RUNNER:
        for archivo in sorted((RAIZ / carpeta).rglob("*.py")):
            arbol = _arbol(archivo)
            llamadas = _llamadas(arbol, "DynDOLODRunner")
            if not llamadas:
                continue
            clave = archivo.relative_to(RAIZ).as_posix()
            encontrados[clave] = all("readiness" in _kwargs(llamada) for llamada in llamadas)

    assert set(encontrados) == CONSTRUCTORES_DEL_RUNNER, (
        f"constructores del runner inesperados: {sorted(set(encontrados) ^ CONSTRUCTORES_DEL_RUNNER)}"
    )
    sin_wiring = sorted(clave for clave, cablea in encontrados.items() if not cablea)
    assert not sin_wiring, f"construyen el runner sin la capacidad de readiness: {sin_wiring}"


def test_censo_de_constructores_del_servicio_exige_readiness() -> None:
    """Ningún servicio productivo puede quedar con el ``None`` silencioso.

    El runner ya no acepta ``None``; el servicio lo traduce a
    ``DISABLED_FOR_TEST`` cuando un doble de test no pasa nada. Para que esa
    traducción no sea el default de producción, TODO constructor del servicio en
    ``sky_claw/**`` declara el modo: la capacidad (composition root) o el opt-out
    (preview plan-only). Un servicio nuevo sin ``readiness=`` rompe acá.
    """
    encontrados: dict[str, bool] = {}
    for archivo in sorted((RAIZ / "sky_claw").rglob("*.py")):
        llamadas = _llamadas(_arbol(archivo), "DynDOLODPipelineService")
        if not llamadas:
            continue
        clave = archivo.relative_to(RAIZ).as_posix()
        encontrados[clave] = all("readiness" in _kwargs(llamada) for llamada in llamadas)

    assert set(encontrados) == CONSTRUCTORES_DEL_SERVICIO, (
        f"constructores del servicio inesperados: {sorted(set(encontrados) ^ CONSTRUCTORES_DEL_SERVICIO)}"
    )
    sin_eleccion = sorted(clave for clave, elige in encontrados.items() if not elige)
    assert not sin_eleccion, f"construyen el servicio sin elegir el modo de readiness: {sin_eleccion}"


def _valor_del_kwarg(llamada: ast.Call, nombre: str) -> ast.AST | None:
    return next((kw.value for kw in llamada.keywords if kw.arg == nombre), None)


def _menciona(nodo: ast.AST, nombre: str) -> bool:
    return nombre in {getattr(hijo, "id", None) for hijo in ast.walk(nodo)} or nombre in {
        getattr(hijo, "attr", None) for hijo in ast.walk(nodo)
    }


def test_el_composition_root_no_usa_el_opt_out() -> None:
    """La ejecución productiva cablea la CAPACIDAD; el opt-out es de tests/preview."""
    arbol = _arbol(COMPOSITION)
    for llamada in _llamadas(arbol, "DynDOLODRunner") + _llamadas(arbol, "DynDOLODPipelineService"):
        valor = _valor_del_kwarg(llamada, "readiness")
        assert valor is not None
        assert not _menciona(valor, "DISABLED_FOR_TEST"), (
            "el composition root no puede declarar el opt-out: la etapa 9 de producción corre con gate"
        )
        assert not (isinstance(valor, ast.Constant) and valor.value is None), (
            "el composition root no puede pasar None: eso reabre el default silencioso"
        )


def test_el_servicio_inyecta_la_capacidad_en_el_runner() -> None:
    arbol = _arbol(SERVICE)
    llamadas = _llamadas(arbol, "DynDOLODRunner")
    assert llamadas, "el servicio ya no construye el runner: revisá este ancla"
    assert all("readiness" in _kwargs(llamada) for llamada in llamadas)


def test_el_servicio_recibe_la_capacidad_y_la_guarda() -> None:
    arbol = _arbol(SERVICE)
    servicio = next(
        nodo for nodo in ast.walk(arbol) if isinstance(nodo, ast.ClassDef) and nodo.name == "DynDOLODPipelineService"
    )
    init = next(hijo for hijo in servicio.body if isinstance(hijo, ast.FunctionDef) and hijo.name == "__init__")
    argumentos = [arg.arg for arg in init.args.kwonlyargs]
    assert "readiness" in argumentos
    assert "workspace" in argumentos, "el wiring de P2.2 no puede perderse"
    assert "stage9_coordination" in argumentos


# ---------------------------------------------------------------------------
# Composition root
# ---------------------------------------------------------------------------


def test_el_composition_root_arma_la_capacidad_con_el_ejecutor_descartable() -> None:
    arbol = _arbol(COMPOSITION)
    llamadas = _llamadas(arbol, "CapacidadDeReadinessUIA")
    assert len(llamadas) == 1, "la capacidad debe armarse UNA vez en el composition root"
    assert _kwargs(llamadas[0]) == {
        "ejecutor",
        "confirmador",
    }, "la capacidad se arma con el ejecutor y el canal humano explícitos"

    fuente = COMPOSITION.read_text(encoding="utf-8")
    assert "ejecutor=EjecutorGatePorHelper()" in fuente, (
        "el ejecutor productivo es el helper descartable: la observación COM no puede correr en un hilo del padre"
    )
    assert "confirmador=ConfirmadorHITL(hitl_guard=hitl_guard)" in fuente


def test_el_composition_root_inyecta_la_capacidad_en_el_servicio() -> None:
    arbol = _arbol(COMPOSITION)
    llamadas = _llamadas(arbol, "DynDOLODPipelineService")
    assert llamadas, "el composition root ya no construye el servicio: revisá este ancla"
    for llamada in llamadas:
        assert "readiness" in _kwargs(llamada), "el servicio de producción se construye sin el gate cableado"
        assert "workspace" in _kwargs(llamada), "el ownership de P2.2 no puede perderse"
        assert "stage9_coordination" in _kwargs(llamada)


def test_los_colaboradores_de_produccion_satisfacen_los_puertos() -> None:
    """Los tipos reales cumplen los Protocolos del puerto (chequeo estructural).

    ``ObservadorUIAWindows`` no se puede INSTANCIAR fuera de Windows (COM), pero
    su clase debe declarar los tres métodos de lectura del protocolo.
    """
    from sky_claw.app.orchestrator.dyndolod_readiness_hitl import ConfirmadorHITL  # noqa: PLC0415
    from sky_claw.local.tools.dyndolod_uia_gate import EjecutorDeGateUIA  # noqa: PLC0415

    assert isinstance(LocalizadorPsutil(), LocalizadorDeProcesos)
    assert callable(construir_observador_windows)
    for nombre in ("ventanas_de_proceso", "controles_de_ventana", "leer_valor"):
        assert callable(getattr(ObservadorUIAWindows, nombre)), f"el backend no implementa {nombre}"
    assert not hasattr(ObservadorUIAWindows, "liberar") or callable(ObservadorUIAWindows.liberar)

    # El ejecutor productivo implementa el puerto del gate (firma async con la
    # política y los dos plazos), y el de tests existe para el mismo contrato.
    assert callable(EjecutorGatePorHelper.ejecutar)
    assert callable(EjecutorGateEnProceso.ejecutar)
    assert "ejecutar" in EjecutorDeGateUIA.__annotations__ or hasattr(EjecutorDeGateUIA, "ejecutar")

    confirmador = ConfirmadorHITL(hitl_guard=None)
    for nombre in ("confirmar", "informar"):
        assert callable(getattr(confirmador, nombre)), f"el adapter no implementa {nombre}"
    assert "confirmar" in ConfirmadorDeConfiguracion.__annotations__ or hasattr(ConfirmadorDeConfiguracion, "confirmar")


# ---------------------------------------------------------------------------
# El helper descartable: backend real, canal por archivos y modo congelado
# ---------------------------------------------------------------------------


def test_el_helper_productivo_usa_el_backend_real() -> None:
    """El worker corre el MISMO backend que midió el rig, no una copia de test."""
    arbol = _arbol(HELPER)
    mencionados = {getattr(nodo, "id", None) for nodo in ast.walk(arbol) if isinstance(nodo, ast.Name)} | {
        nodo.attr for nodo in ast.walk(arbol) if isinstance(nodo, ast.Attribute)
    }
    assert "construir_observador_windows" in mencionados, "el helper no usa el backend COM real"
    llamadas = {
        nodo.func.id for nodo in ast.walk(arbol) if isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Name)
    }
    assert "ejecutar_gate_sincrono" in llamadas, "el helper no corre el gate acotado"


def test_el_comando_del_helper_cubre_el_modo_congelado(monkeypatch: pytest.MonkeyPatch) -> None:
    """La fuente usa ``-m``; el ejecutable congelado se re-invoca con el flag.

    Un binario PyInstaller no puede correr ``-m``, así que el helper del producto
    que se distribuye ES el propio exe con ``FLAG_HELPER_UIA``. Las dos ramas se
    prueban: la que corre hoy el rig y la que corre el release.
    """
    monkeypatch.delattr(sys, "frozen", raising=False)
    fuente = comando_del_helper()
    assert fuente[0] == sys.executable
    assert fuente[1] == "-m"

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    congelado = comando_del_helper()
    assert congelado == (sys.executable, FLAG_HELPER_UIA)


def test_el_main_despacha_el_helper_antes_de_importar_la_app() -> None:
    """El despacho del worker en ``__main__`` no arrastra NiceGUI/Web.

    El helper congelado ES ``SkyClawApp.exe --skyclaw-uia-helper``: si el flag no
    se despacha antes de los imports de la app, el worker abre la GUI. El ancla
    compara líneas: el ``if`` del flag tiene que preceder al primer import de
    ``sky_claw.app``.
    """
    fuente = MAIN.read_text(encoding="utf-8")
    arbol = ast.parse(fuente)
    linea_flag = next(
        (
            nodo.lineno
            for nodo in ast.walk(arbol)
            if isinstance(nodo, ast.If) and _menciona(nodo.test, "FLAG_HELPER_UIA")
        ),
        None,
    )
    assert linea_flag is not None, "el flag del worker UIA dejó de despacharse en __main__"
    linea_app = min(
        (
            nodo.lineno
            for nodo in ast.walk(arbol)
            if isinstance(nodo, ast.ImportFrom) and nodo.module is not None and nodo.module.startswith("sky_claw.app")
        ),
        default=10**9,
    )
    assert linea_flag < linea_app, "el worker UIA se despacha después de importar la app (abriría la GUI)"


def test_el_puerto_del_observador_sigue_congelado_en_tres_metodos() -> None:
    """El wiring no puede haber agregado un cuarto método al puerto."""
    declarados = tuple(
        hijo.name
        for nodo in ast.walk(_arbol(RAIZ / "sky_claw" / "local" / "tools" / "dyndolod_uia_preflight.py"))
        if isinstance(nodo, ast.ClassDef) and nodo.name == "ObservadorUIA"
        for hijo in nodo.body
        if isinstance(hijo, ast.FunctionDef) and not hijo.name.startswith("_")
    )
    assert declarados == ("ventanas_de_proceso", "controles_de_ventana", "leer_valor")


# ---------------------------------------------------------------------------
# El adapter no conoce al runner ni el gate conoce a la app
# ---------------------------------------------------------------------------


def test_el_gate_puro_no_importa_la_capa_de_aplicacion() -> None:
    """La capa local define el puerto; el adapter vive en ``app.orchestrator``."""
    fuente = GATE.read_text(encoding="utf-8")
    arbol = _arbol(GATE)
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.ImportFrom) and nodo.module:
            assert not nodo.module.startswith("sky_claw.app"), f"el gate importa la app: {nodo.module}"
        elif isinstance(nodo, ast.Import):
            for alias in nodo.names:
                assert not alias.name.startswith("sky_claw.app"), f"el gate importa la app: {alias.name}"
    assert "asyncio" not in {
        alias.name for nodo in ast.walk(arbol) if isinstance(nodo, ast.Import) for alias in nodo.names
    }
    assert fuente.count("def ") > 0


def test_el_adapter_no_importa_al_runner() -> None:
    """El adapter traduce a HITL; el runner lo consume. Al revés sería un ciclo."""
    arbol = _arbol(ADAPTER)
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.ImportFrom) and nodo.module:
            assert "dyndolod_runner" not in nodo.module


# ---------------------------------------------------------------------------
# §32 — guard de frescura del donor: lo que PR-3 eliminó sigue ausente
# ---------------------------------------------------------------------------


FRESCURA_PROHIBIDA = ("_firma_de_veredicto", "_firmas_de_salida", "firmas_previas", "_SIN_ARTEFACTO")


@pytest.mark.parametrize("archivo", [RUNNER, GATE, ADAPTER], ids=lambda p: p.name)
def test_el_wiring_no_reintroduce_la_frescura_de_artefactos(archivo: pathlib.Path) -> None:
    """PR-3 borró la comparación pre/post del artefacto: no vuelve por el wiring."""
    fuente = archivo.read_text(encoding="utf-8")
    for prohibido in FRESCURA_PROHIBIDA:
        assert prohibido not in fuente, f"{archivo.name} reintroduce {prohibido}"


def test_el_runner_no_recupera_el_output_root_legacy() -> None:
    """El expected del gate sale del layout, no del ``output_root`` pre-PR2."""
    fuente = RUNNER.read_text(encoding="utf-8")
    arbol = ast.parse(fuente)
    atributos = {nodo.attr for nodo in ast.walk(arbol) if isinstance(nodo, ast.Attribute)}
    assert "output_root" not in atributos


def test_el_expected_del_gate_sale_del_layout_por_herramienta() -> None:
    """M9/M10 — el expected se deriva de ``layout.raiz_de(herramienta)``."""
    arbol = _arbol(RUNNER)
    metodo = next(
        nodo for nodo in ast.walk(arbol) if isinstance(nodo, ast.FunctionDef) and nodo.name == "_expected_output_de"
    )
    atributos = {nodo.attr for nodo in ast.walk(metodo) if isinstance(nodo, ast.Attribute)}
    assert "raiz_de" in atributos, "el expected dejó de derivarse del layout"
    assert "family_root" not in atributos, "el expected usa el family_root (no es output)"
