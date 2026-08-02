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
