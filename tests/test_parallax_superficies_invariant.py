"""Ancla de las superficies de las tools de lectura del generador nativo de parallax (NP-A0k).

La propiedad, como propiedad del mecanismo y no como recordatorio de proceso: *una tool de lectura de parallax contesta lo mismo en la
GUI y en el agente LLM solo si las DOS llaman a LA MISMA función de resultado y las dos existen*. El defecto dominante del repo es el
que aterriza en una superficie y deja a su gemela sin tocar (AGENTS.md); por eso esto ENUMERA, no muestrea: por AST, quién llama a
cada función de resultado, y por introspección, qué tools de parallax hay en cada registro.

Rompe, hasta que se lo declare acá junto a las demás, cuando: aparece un tercer llamador de una función de resultado; una tool existe
en una superficie y no en la otra; una superficie pasa por su cuenta un parámetro que la función compartida fija (el plazo de la
lectura de mallas o el tope del resultado); o una tool de parallax nueva se registra sin sumarse a este censo.
"""

from __future__ import annotations

import ast
import pathlib
from unittest.mock import MagicMock

import pytest

from sky_claw.app.agent.tools import AsyncToolRegistry
from sky_claw.app.orchestrator.tool_dispatcher import build_orchestration_dispatcher
from tests._orchestration_dispatcher_dependencies import crear_dependencias_dispatcher

_RAIZ = pathlib.Path(__file__).resolve().parent.parent

_GUI = "sky_claw/app/orchestrator/tool_strategies/"
_AGENTE = "sky_claw/app/agent/tools/"

#: Función de resultado compartida -> los dos módulos que la llaman, uno por superficie. Igualdad literal.
SUPERFICIES_POR_FUNCION_DE_RESULTADO: dict[str, set[str]] = {
    "diagnostico_como_resultado": {
        f"{_GUI}diagnosticar_perfil_parallax.py",
        f"{_AGENTE}parallax_tools.py",
    },
    "elegibilidad_como_resultado": {
        f"{_GUI}evaluar_elegibilidad_parallax.py",
        f"{_AGENTE}parallax_tools.py",
    },
}

#: Las tools de lectura de parallax que existen en las dos superficies. Igualdad literal.
TOOLS_DE_LECTURA_DE_PARALLAX: set[str] = {
    "diagnosticar_parallax_perfil",
    "evaluar_parallax_elegibilidad",
}

#: Parámetros que la función compartida fija para las dos superficies: ninguna los pasa por su cuenta.
PARAMETROS_FIJADOS_POR_LA_FUNCION: dict[str, set[str]] = {
    "elegibilidad_como_resultado": {"plazo", "maximo_de_texturas"},
    "diagnostico_como_resultado": {"maximo_de_hallazgos"},
}


def _llamadas(nombre: str) -> list[tuple[str, ast.Call]]:
    """Cada llamada (por nombre simple o atributo) a *nombre* bajo ``sky_claw/``, con el módulo que la contiene."""
    encontradas: list[tuple[str, ast.Call]] = []
    for archivo in (_RAIZ / "sky_claw").rglob("*.py"):
        for nodo in ast.walk(ast.parse(archivo.read_text(encoding="utf-8"))):
            if not isinstance(nodo, ast.Call):
                continue
            f = nodo.func
            llamado = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None
            # ``asyncio.to_thread(funcion, ...)`` pasa la función como argumento: también cuenta como llamarla.
            pasada = {
                a.id if isinstance(a, ast.Name) else a.attr
                for a in nodo.args
                if isinstance(a, ast.Name | ast.Attribute)
            }
            if llamado == nombre or nombre in pasada:
                encontradas.append((archivo.relative_to(_RAIZ).as_posix(), nodo))
    return encontradas


@pytest.mark.parametrize("funcion", sorted(SUPERFICIES_POR_FUNCION_DE_RESULTADO))
def test_cada_funcion_de_resultado_la_llaman_exactamente_las_dos_superficies(funcion: str) -> None:
    llamadores = {modulo for modulo, _ in _llamadas(funcion)}

    assert llamadores == SUPERFICIES_POR_FUNCION_DE_RESULTADO[funcion]


@pytest.mark.parametrize("funcion", sorted(SUPERFICIES_POR_FUNCION_DE_RESULTADO))
def test_cada_funcion_de_resultado_tiene_una_superficie_de_la_gui_y_una_del_agente(funcion: str) -> None:
    """La estructura del censo, no solo sus nombres: una sola de las dos superficies no es una pareja."""
    modulos = SUPERFICIES_POR_FUNCION_DE_RESULTADO[funcion]

    assert sum(m.startswith(_GUI) for m in modulos) == 1
    assert sum(m.startswith(_AGENTE) for m in modulos) == 1


@pytest.mark.parametrize("funcion", sorted(PARAMETROS_FIJADOS_POR_LA_FUNCION))
def test_ninguna_superficie_pasa_un_parametro_que_la_funcion_compartida_fija(funcion: str) -> None:
    fijados = PARAMETROS_FIJADOS_POR_LA_FUNCION[funcion]
    pasados = {
        (modulo, kw.arg) for modulo, llamada in _llamadas(funcion) for kw in llamada.keywords if kw.arg in fijados
    }

    assert pasados == set()


def _registro() -> AsyncToolRegistry:
    mo2 = MagicMock()
    return AsyncToolRegistry(registry=MagicMock(), mo2=mo2, sync_engine=MagicMock(), mo2_profile="Default")


def test_las_tools_de_lectura_de_parallax_existen_en_las_dos_superficies_y_solo_ellas() -> None:
    """Se enumeran por patrón (``parallax`` en el nombre), no por lista: una tool nueva en UNA superficie rompe esto."""
    en_el_agente = {nombre for nombre in _registro().tools if "parallax" in nombre}
    en_la_gui = {
        nombre
        for nombre in build_orchestration_dispatcher(crear_dependencias_dispatcher()).registered_tools()
        if "parallax" in nombre
    }

    assert en_el_agente == TOOLS_DE_LECTURA_DE_PARALLAX
    assert en_la_gui == TOOLS_DE_LECTURA_DE_PARALLAX


def test_ninguna_tool_de_lectura_de_parallax_lleva_gate_hitl_en_la_gui() -> None:
    """Son de solo lectura: sin aprobación, como ``scan_asset_conflicts`` (política base lock-only, #217)."""
    from sky_claw.app.orchestrator.tool_strategies.middleware import HitlGateMiddleware

    gate = HitlGateMiddleware(allow_unattended=True)
    dispatcher = build_orchestration_dispatcher(crear_dependencias_dispatcher(), hitl_gate=gate)

    for nombre in TOOLS_DE_LECTURA_DE_PARALLAX:
        assert not any(m is gate for m in dispatcher._middleware[nombre]), nombre


def test_las_tools_de_lectura_de_parallax_se_declaran_de_solo_lectura_en_el_agente() -> None:
    herramientas = _registro().tools

    for nombre in TOOLS_DE_LECTURA_DE_PARALLAX:
        assert herramientas[nombre].description.startswith("Read-only"), nombre


def test_ninguna_tool_de_parallax_del_agente_serializa_por_su_cuenta() -> None:
    """El router recorta a mano por el final lo que pasa de 4000 caracteres: toda respuesta de estas tools pasa por ``_para_el_modelo``
    (que la ajusta al presupuesto con la leyenda de alcance primero). Una tool nueva que hiciera su propio ``json.dumps`` volvería a
    mandarle al modelo veredictos sin sus salvedades; este censo la rompe hasta que pase por el mismo camino."""
    arbol = ast.parse((_RAIZ / _AGENTE / "parallax_tools.py").read_text(encoding="utf-8"))
    funciones_con_dumps = {
        funcion.name
        for funcion in ast.walk(arbol)
        if isinstance(funcion, ast.FunctionDef | ast.AsyncFunctionDef)
        for nodo in ast.walk(funcion)
        if isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Attribute) and nodo.func.attr == "dumps"
    }
    tools = {funcion.name for funcion in ast.walk(arbol) if isinstance(funcion, ast.AsyncFunctionDef)}

    assert funciones_con_dumps == {"_para_el_modelo"}
    assert tools == {"diagnosticar_parallax_perfil", "evaluar_parallax_elegibilidad"}
