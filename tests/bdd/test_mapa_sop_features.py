"""Ancla del mapa "tool destructiva del SOP ↔ feature de acceptance".

Enumera, no muestrea. El valor declarado de esta capa es la trazabilidad
SOP↔spec; sin un ancla, esa trazabilidad es prosa y envejece como cualquier
doc estático. Con el mapa congelado por igualdad literal, agregar una tool
destructiva sin decidir su acceptance rompe el test — que es la única forma de
regla que este repo cumple sostenidamente.

Molde: ``tests/test_ritual_dispatch.py`` (igualdad literal de dict) y
``tests/test_db_connection_invariant.py`` (conjunto congelado por detección).

Hoy la mayoría de las entradas están en ``PENDIENTE`` a propósito: el ancla
vale por lo que rompe, no por lo que documenta. Cerrar una entrada = escribir
su ``.feature`` y reemplazar el ``PENDIENTE`` por la ruta.
"""

from __future__ import annotations

import ast
import pathlib

from sky_claw.app.orchestrator.tool_strategies.middleware import DESTRUCTIVE_TOOL_PATTERNS

#: Marcador explícito: la tool es destructiva pero todavía no tiene acceptance.
PENDIENTE = None

#: Rutas relativas a ``tests/bdd/``.
MAPA_SOP_ACCEPTANCE: dict[str, str | None] = {
    "generate_grass_cache": "features/sop_modding/generacion_grass_cache.feature",
    "execute_loot_sorting": PENDIENTE,
    "generate_bashed_patch": PENDIENTE,
    "generate_lods": PENDIENTE,
    "generate_animations": PENDIENTE,
    "quick_auto_clean": PENDIENTE,
    "resolve_conflict_with_patch": PENDIENTE,
}


def test_el_mapa_enumera_todas_las_tools_destructivas() -> None:
    """Una tool destructiva nueva exige decidir su acceptance (aunque sea PENDIENTE)."""
    assert set(MAPA_SOP_ACCEPTANCE) == set(DESTRUCTIVE_TOOL_PATTERNS)


def test_cada_feature_declarado_existe_en_disco() -> None:
    """Un PENDIENTE cerrado con una ruta inventada (o un rename) rompe acá."""
    raiz = pathlib.Path(__file__).parent
    declarados = {tool: ruta for tool, ruta in MAPA_SOP_ACCEPTANCE.items() if ruta is not PENDIENTE}

    assert declarados, "al menos una tool destructiva debe tener acceptance"
    faltantes = [f"{tool} → {ruta}" for tool, ruta in declarados.items() if not (raiz / str(ruta)).is_file()]
    assert not faltantes, f"features declarados que no existen: {faltantes}"


def _rutas_ligadas_via_scenarios(directorio_step_defs: pathlib.Path) -> set[str]:
    """Rutas de feature (resueltas y normalizadas) ligadas vía ``scenarios(...)``.

    ``scenarios()`` liga TODOS los escenarios de un feature; un ``@scenario(...)``
    manual puede dejar alguno sin test — el gap real que motivó este ancla
    (review #420). Se detecta por AST y no por import: importar cada módulo de
    ``step_defs/`` para inspeccionarlo en runtime dispararía su colección de
    steps por partida doble.
    """
    resultado: set[str] = set()
    for modulo in directorio_step_defs.glob("*.py"):
        arbol = ast.parse(modulo.read_text(encoding="utf-8"))
        # Constantes de módulo de un solo nivel (ej. ``FEATURE = "../features/..."``):
        # ``scenarios(FEATURE)`` pasa una referencia, no un literal en la llamada.
        constantes = {
            objetivo.id: nodo.value.value
            for nodo in ast.walk(arbol)
            if isinstance(nodo, ast.Assign)
            and isinstance(nodo.value, ast.Constant)
            and isinstance(nodo.value.value, str)
            for objetivo in nodo.targets
            if isinstance(objetivo, ast.Name)
        }
        for nodo in ast.walk(arbol):
            if not (isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Name) and nodo.func.id == "scenarios"):
                continue
            for arg in nodo.args:
                valor: str | None = None
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    valor = arg.value
                elif isinstance(arg, ast.Name) and arg.id in constantes:
                    valor = constantes[arg.id]
                if valor is not None:
                    resultado.add(str((modulo.parent / valor).resolve()))
    return resultado


def test_cada_feature_declarado_esta_ligado_via_scenarios() -> None:
    """``@scenario(...)`` manual puede dejar un escenario nuevo sin test (review
    #420: ocurrió acá mismo, con 5 decoradores a mano). Exige el patrón que lo
    ata todo — ``scenarios(ruta)`` — para cada feature ya cerrada del mapa.
    """
    raiz = pathlib.Path(__file__).parent
    declarados = {tool: ruta for tool, ruta in MAPA_SOP_ACCEPTANCE.items() if ruta is not PENDIENTE}
    ligadas = _rutas_ligadas_via_scenarios(raiz / "step_defs")

    sin_ligar = [
        f"{tool} → {ruta}" for tool, ruta in declarados.items() if str((raiz / str(ruta)).resolve()) not in ligadas
    ]
    assert not sin_ligar, f"features declarados sin scenarios(...) en step_defs/: {sin_ligar}"
