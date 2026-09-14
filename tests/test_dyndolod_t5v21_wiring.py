"""T5-v2.1 — wiring productivo: censo de constructores y puertos satisfechos.

El modo directo del runner (``readiness=None``) NO corre el protocolo. Eso es
correcto para tests/rig y peligroso en producción, así que la garantía no es un
default sino un CENSO: se enumeran todos los sitios que construyen el runner en
``sky_claw/**`` y se exige que cada uno cablee la capacidad. Un sitio nuevo —o
uno que la olvide— rompe el test en vez de salir a producción sin gate.

Es el mismo instrumento que el repo ya usa para el servicio
(``tests/test_dyndolod_workspace.py::test_censo_de_constructores_del_servicio_dyndolod``):
enumerar, no muestrear.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from sky_claw.local.tools.dyndolod_uia_gate import ConfirmadorDeConfiguracion
from sky_claw.local.tools.dyndolod_uia_preflight import (
    LocalizadorDeProcesos,
    LocalizadorPsutil,
)
from sky_claw.local.tools.dyndolod_uia_windows import ObservadorUIAWindows, construir_observador_windows

RAIZ = pathlib.Path(__file__).resolve().parents[1]

#: ÚNICO sitio productivo que construye el runner. El ejemplo del docstring de
#: ``dyndolod_runner`` no cuenta: es texto, no un ``ast.Call``.
CONSTRUCTORES_DEL_RUNNER: frozenset[str] = frozenset({"sky_claw/local/tools/dyndolod_service.py"})

#: Archivos que participan del wiring de T5-v2.1.
COMPOSITION = RAIZ / "sky_claw" / "app" / "orchestrator" / "orchestration_composition.py"
ADAPTER = RAIZ / "sky_claw" / "app" / "orchestrator" / "dyndolod_readiness_hitl.py"
SERVICE = RAIZ / "sky_claw" / "local" / "tools" / "dyndolod_service.py"
RUNNER = RAIZ / "sky_claw" / "local" / "tools" / "dyndolod_runner.py"
GATE = RAIZ / "sky_claw" / "local" / "tools" / "dyndolod_uia_gate.py"


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
    """Todo constructor productivo del runner pasa ``readiness=``, sin excepciones."""
    encontrados: dict[str, bool] = {}
    for archivo in sorted((RAIZ / "sky_claw").rglob("*.py")):
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


def test_el_composition_root_arma_la_capacidad_con_el_backend_real() -> None:
    arbol = _arbol(COMPOSITION)
    llamadas = _llamadas(arbol, "CapacidadDeReadinessUIA")
    assert len(llamadas) == 1, "la capacidad debe armarse UNA vez en el composition root"
    assert _kwargs(llamadas[0]) == {
        "fabrica_observador",
        "localizador",
        "confirmador",
    }, "la capacidad se arma con sus tres colaboradores explícitos"

    fuente = COMPOSITION.read_text(encoding="utf-8")
    assert "fabrica_observador=construir_observador_windows" in fuente
    assert "localizador=LocalizadorPsutil()" in fuente
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
    assert isinstance(LocalizadorPsutil(), LocalizadorDeProcesos)
    assert callable(construir_observador_windows)
    for nombre in ("ventanas_de_proceso", "controles_de_ventana", "leer_valor"):
        assert callable(getattr(ObservadorUIAWindows, nombre)), f"el backend no implementa {nombre}"
    assert not hasattr(ObservadorUIAWindows, "liberar") or callable(ObservadorUIAWindows.liberar)

    from sky_claw.app.orchestrator.dyndolod_readiness_hitl import ConfirmadorHITL  # noqa: PLC0415

    confirmador = ConfirmadorHITL(hitl_guard=None)
    for nombre in ("confirmar", "informar"):
        assert callable(getattr(confirmador, nombre)), f"el adapter no implementa {nombre}"
    assert "confirmar" in ConfirmadorDeConfiguracion.__annotations__ or hasattr(ConfirmadorDeConfiguracion, "confirmar")


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
