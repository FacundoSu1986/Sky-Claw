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
from sky_claw.local.tools.dyndolod_uia_preflight import (
    LocalizadorDeProcesos,
    LocalizadorPsutil,
)
from sky_claw.local.tools.dyndolod_uia_windows import ObservadorUIAWindows, construir_observador_windows

RAIZ = pathlib.Path(__file__).resolve().parents[1]

CONSTRUCTORES_DEL_RUNNER: frozenset[str] = frozenset(
    {
        "sky_claw/local/tools/dyndolod_service.py",
        "docs/validation/2026-09-13_pr580_real_rig/run_phase.py",
    }
)

CENSO_DE_CARPETAS_DEL_RUNNER: tuple[str, ...] = ("sky_claw", "docs/validation")

CONSTRUCTORES_DEL_SERVICIO: frozenset[str] = frozenset(
    {
        "sky_claw/app/orchestrator/orchestration_composition.py",
        "sky_claw/app/orchestrator/preview/chain_preview_service.py",
    }
)

COMPOSITION = RAIZ / "sky_claw" / "app" / "orchestrator" / "orchestration_composition.py"
ADAPTER = RAIZ / "sky_claw" / "app" / "orchestrator" / "dyndolod_readiness_hitl.py"
SERVICE = RAIZ / "sky_claw" / "local" / "tools" / "dyndolod_service.py"
RUNNER = RAIZ / "sky_claw" / "local" / "tools" / "dyndolod_runner.py"
GATE = RAIZ / "sky_claw" / "local" / "tools" / "dyndolod_uia_gate.py"
EJECUTOR = RAIZ / "sky_claw" / "local" / "tools" / "dyndolod_uia_ejecutor.py"
HELPER = RAIZ / "sky_claw" / "local" / "tools" / "dyndolod_uia_helper.py"
MAIN = RAIZ / "sky_claw" / "__main__.py"
PREVIEW = RAIZ / "sky_claw" / "app" / "orchestrator" / "preview" / "chain_preview_service.py"


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


def test_censo_de_constructores_del_runner() -> None:
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


def test_el_preview_opta_out_solo_en_dry_run() -> None:
    """DISABLED_FOR_TEST del preview queda anclado a execute(..., dry_run=True)."""
    arbol = _arbol(PREVIEW)
    tiene_opt_out = False
    for llamada in _llamadas(arbol, "DynDOLODPipelineService"):
        valor = _valor_del_kwarg(llamada, "readiness")
        assert valor is not None
        if _menciona(valor, "DISABLED_FOR_TEST"):
            tiene_opt_out = True
    assert tiene_opt_out, "el preview dejó de declarar el opt-out explícito"
    executes = _llamadas(arbol, "execute")
    assert executes, "el preview ya no llama execute: revisá este ancla"
    for llamada in executes:
        dry = _valor_del_kwarg(llamada, "dry_run")
        assert dry is not None, "execute del preview tiene que pasar dry_run="
        assert isinstance(dry, ast.Constant) and dry.value is True, (
            "DISABLED_FOR_TEST sólo es lícito si el preview sigue siendo dry_run=True"
        )


def test_el_composition_root_no_usa_el_opt_out() -> None:
    arbol = _arbol(COMPOSITION)
    for llamada in _llamadas(arbol, "DynDOLODRunner") + _llamadas(arbol, "DynDOLODPipelineService"):
        valor = _valor_del_kwarg(llamada, "readiness")
        assert valor is not None
        assert not _menciona(valor, "DISABLED_FOR_TEST")
        assert not (isinstance(valor, ast.Constant) and valor.value is None)


def test_el_servicio_inyecta_la_capacidad_en_el_runner() -> None:
    arbol = _arbol(SERVICE)
    llamadas = _llamadas(arbol, "DynDOLODRunner")
    assert llamadas
    assert all("readiness" in _kwargs(llamada) for llamada in llamadas)


def test_el_servicio_recibe_la_capacidad_y_la_guarda() -> None:
    arbol = _arbol(SERVICE)
    servicio = next(
        nodo for nodo in ast.walk(arbol) if isinstance(nodo, ast.ClassDef) and nodo.name == "DynDOLODPipelineService"
    )
    init = next(hijo for hijo in servicio.body if isinstance(hijo, ast.FunctionDef) and hijo.name == "__init__")
    argumentos = [arg.arg for arg in init.args.kwonlyargs]
    assert "readiness" in argumentos
    assert "workspace" in argumentos
    assert "stage9_coordination" in argumentos


def test_el_composition_root_arma_la_capacidad_con_el_ejecutor_descartable() -> None:
    arbol = _arbol(COMPOSITION)
    llamadas = _llamadas(arbol, "CapacidadDeReadinessUIA")
    assert len(llamadas) == 1
    assert _kwargs(llamadas[0]) == {"ejecutor", "confirmador"}
    fuente = COMPOSITION.read_text(encoding="utf-8")
    assert "ejecutor=EjecutorGatePorHelper()" in fuente
    assert "confirmador=ConfirmadorHITL(hitl_guard=hitl_guard)" in fuente


def test_el_composition_root_inyecta_la_capacidad_en_el_servicio() -> None:
    arbol = _arbol(COMPOSITION)
    llamadas = _llamadas(arbol, "DynDOLODPipelineService")
    assert llamadas
    for llamada in llamadas:
        assert "readiness" in _kwargs(llamada)
        assert "workspace" in _kwargs(llamada)
        assert "stage9_coordination" in _kwargs(llamada)


def test_los_colaboradores_de_produccion_satisfacen_los_puertos() -> None:
    from sky_claw.app.orchestrator.dyndolod_readiness_hitl import ConfirmadorHITL

    assert isinstance(LocalizadorPsutil(), LocalizadorDeProcesos)
    assert callable(construir_observador_windows)
    for nombre in ("ventanas_de_proceso", "controles_de_ventana", "leer_valor"):
        assert callable(getattr(ObservadorUIAWindows, nombre))
    assert callable(EjecutorGatePorHelper.ejecutar)
    assert callable(EjecutorGateEnProceso.ejecutar)
    confirmador = ConfirmadorHITL(hitl_guard=None)
    for nombre in ("confirmar", "informar"):
        assert callable(getattr(confirmador, nombre))


def test_el_helper_productivo_usa_el_backend_real() -> None:
    arbol = _arbol(HELPER)
    mencionados = {getattr(nodo, "id", None) for nodo in ast.walk(arbol) if isinstance(nodo, ast.Name)} | {
        nodo.attr for nodo in ast.walk(arbol) if isinstance(nodo, ast.Attribute)
    }
    assert "construir_observador_windows" in mencionados
    llamadas = {
        nodo.func.id for nodo in ast.walk(arbol) if isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Name)
    }
    assert "ejecutar_gate_sincrono" in llamadas


def test_el_comando_del_helper_cubre_el_modo_congelado(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delattr(sys, "frozen", raising=False)
    fuente = comando_del_helper()
    assert fuente[0] == sys.executable
    assert fuente[1] == "-m"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert comando_del_helper() == (sys.executable, FLAG_HELPER_UIA)


def test_el_main_despacha_el_helper_antes_de_importar_la_app() -> None:
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
    assert linea_flag is not None
    linea_app = min(
        (
            nodo.lineno
            for nodo in ast.walk(arbol)
            if isinstance(nodo, ast.ImportFrom) and nodo.module is not None and nodo.module.startswith("sky_claw.app")
        ),
        default=10**9,
    )
    assert linea_flag < linea_app


def test_el_puerto_del_observador_sigue_congelado_en_tres_metodos() -> None:
    declarados = tuple(
        hijo.name
        for nodo in ast.walk(_arbol(RAIZ / "sky_claw" / "local" / "tools" / "dyndolod_uia_preflight.py"))
        if isinstance(nodo, ast.ClassDef) and nodo.name == "ObservadorUIA"
        for hijo in nodo.body
        if isinstance(hijo, ast.FunctionDef) and not hijo.name.startswith("_")
    )
    assert declarados == ("ventanas_de_proceso", "controles_de_ventana", "leer_valor")


def test_el_gate_puro_no_importa_la_capa_de_aplicacion() -> None:
    arbol = _arbol(GATE)
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.ImportFrom) and nodo.module:
            assert not nodo.module.startswith("sky_claw.app")
        elif isinstance(nodo, ast.Import):
            for alias in nodo.names:
                assert not alias.name.startswith("sky_claw.app")


def test_el_adapter_no_importa_al_runner() -> None:
    arbol = _arbol(ADAPTER)
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.ImportFrom) and nodo.module:
            assert "dyndolod_runner" not in nodo.module


FRESCURA_PROHIBIDA = ("_firma_de_veredicto", "_firmas_de_salida", "firmas_previas", "_SIN_ARTEFACTO")


@pytest.mark.parametrize("archivo", [RUNNER, GATE, ADAPTER], ids=lambda p: p.name)
def test_el_wiring_no_reintroduce_la_frescura_de_artefactos(archivo: pathlib.Path) -> None:
    fuente = archivo.read_text(encoding="utf-8")
    for prohibido in FRESCURA_PROHIBIDA:
        assert prohibido not in fuente


def test_el_runner_no_recupera_el_output_root_legacy() -> None:
    fuente = RUNNER.read_text(encoding="utf-8")
    arbol = ast.parse(fuente)
    atributos = {nodo.attr for nodo in ast.walk(arbol) if isinstance(nodo, ast.Attribute)}
    assert "output_root" not in atributos


def test_el_expected_del_gate_sale_del_layout_por_herramienta() -> None:
    arbol = _arbol(RUNNER)
    metodo = next(
        nodo for nodo in ast.walk(arbol) if isinstance(nodo, ast.FunctionDef) and nodo.name == "_expected_output_de"
    )
    atributos = {nodo.attr for nodo in ast.walk(metodo) if isinstance(nodo, ast.Attribute)}
    assert "raiz_de" in atributos
    assert "family_root" not in atributos
