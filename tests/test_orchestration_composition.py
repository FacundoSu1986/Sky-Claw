"""Anclas de la composition tipada del grafo de tools del Orchestrator.

PR2 del Strangler Fig de ``SupervisorAgent``: la construcción de servicios,
providers, middleware, dependencias del dispatcher y dispatcher vive en
``orchestration_composition.py``. Estos tests fijan la frontera:

- T1: la dataclass es frozen, slots y con superficie exhaustiva.
- T2: ``orchestration_composition.py`` no conoce ``SupervisorAgent``.
- T3: ``SupervisorAgent.__init__`` no construye los 7 servicios (AST).
- T4: los servicios de la composition son los mismos de dispatcher_dependencies.
- T5: construir la composition no resuelve paths/executables (lazy).
- T6: el servicio base de Synthesis y la factory comparten config path.
- T7: construir la composition no ejecuta los seams residuales.
- T8: el supervisor conserva identidad de la composition recibida.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import pathlib
from unittest.mock import MagicMock

import pytest

from sky_claw.app.orchestrator.orchestration_composition import (
    OrchestrationComposition,
    build_orchestration_composition,
)
from sky_claw.app.orchestrator.supervisor import SupervisorAgent
from sky_claw.app.orchestrator.tool_dispatcher import OrchestrationToolDispatcher


@pytest.fixture
def mo2_root(tmp_path, monkeypatch):
    """Layout MO2 descartable + MO2_PATH, con cwd movido a tmp (patrón de
    ``test_supervisor_construction.py``: los SQLite del rollback caen en tmp)."""
    monkeypatch.chdir(tmp_path)
    mo2 = tmp_path / "MO2"
    (mo2 / "profiles" / "Default").mkdir(parents=True)
    monkeypatch.setenv("MO2_PATH", str(mo2))
    return mo2


_SERVICIOS_DEL_GRAFO = [
    "SynthesisPipelineService",
    "DynDOLODPipelineService",
    "XEditPipelineService",
    "LootSortingService",
    "PandoraPipelineService",
    "GrassCacheService",
    "WryeBashPipelineService",
]

_CAMPOS_COMPOSITION = [
    "synthesis_service",
    "dyndolod_service",
    "xedit_service",
    "loot_service",
    "pandora_service",
    "grass_cache_service",
    "wrye_bash_service",
    "dispatcher_dependencies",
    "tool_dispatcher",
    "loop_guardrail_middleware",
    "tool_state_machine",
]


# ---------------------------------------------------------------------------
# T1 — dataclass: frozen, slots, superficie exhaustiva
# ---------------------------------------------------------------------------


def test_composition_es_frozen_slots_y_superficie_exhaustiva() -> None:
    assert OrchestrationComposition.__dataclass_params__.frozen is True
    assert OrchestrationComposition.__slots__ == tuple(_CAMPOS_COMPOSITION)
    assert [campo.name for campo in dataclasses.fields(OrchestrationComposition)] == _CAMPOS_COMPOSITION


# ---------------------------------------------------------------------------
# T2 — frontera: orchestration_composition no conoce SupervisorAgent
# ---------------------------------------------------------------------------


def test_composition_no_conoce_supervisor() -> None:
    """La composición no importa ni manipula al supervisor.

    Es AST-real, no string matching: detecta cualquier referencia a
    ``SupervisorAgent`` en nombres, atributos, imports y decoradores
    del módulo de composición, incluyendo imports perezosos o locales.
    """
    ruta = pathlib.Path(inspect.getfile(build_orchestration_composition))
    arbol = ast.parse(ruta.read_text(encoding="utf-8"))

    inventario: set[str] = set()
    for nodo in ast.walk(arbol):
        if isinstance(nodo, (ast.Import, ast.ImportFrom)):
            if isinstance(nodo, ast.ImportFrom) and nodo.module:
                inventario.add(nodo.module)
            for alias in nodo.names:
                inventario.add(alias.name)
        elif isinstance(nodo, ast.Name):
            inventario.add(nodo.id)
        elif isinstance(nodo, ast.Attribute):
            cursor = nodo
            while isinstance(cursor, ast.Attribute):
                inventario.add(cursor.attr)
                cursor = cursor.value
            if isinstance(cursor, ast.Name):
                inventario.add(cursor.id)

    assert "SupervisorAgent" not in inventario, (
        "orchestration_composition referencia 'SupervisorAgent' — "
        "el flujo correcto es composition → supervisor, nunca al revés"
    )
    # Ningún módulo del supervisor se importa.
    assert "sky_claw.app.orchestrator.supervisor" not in inventario


# ---------------------------------------------------------------------------
# T3 — SupervisorAgent.__init__ no construye los 7 servicios (AST)
# ---------------------------------------------------------------------------


def _nombres_de_calls_en_init() -> set[str]:
    """Nombres de funciones llamadas dentro de ``SupervisorAgent.__init__``.

    Para llamadas con prefijo de módulo (``loot_mod.LootSortingService(...)``)
    registra el nombre del atributo final. Además resuelve aliases de
    ``from X import Y as Z`` (tanto a nivel de módulo como locales al
    ``__init__``), de modo que ``LS(...)`` con ``LS = LootSortingService``
    se reporta como ``LootSortingService``.
    """
    ruta = pathlib.Path(inspect.getfile(SupervisorAgent))
    arbol = ast.parse(ruta.read_text(encoding="utf-8"))

    # Alias map: nombre-visto -> nombre-real. Los aliases locales al __init__
    # pisan los del módulo (shadowing intencional).
    alias_map: dict[str, str] = {}
    for nodo in arbol.body:
        if isinstance(nodo, ast.ImportFrom):
            for alias in nodo.names:
                alias_map[alias.asname or alias.name] = alias.name

    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.ClassDef) and nodo.name == "SupervisorAgent":
            for miembro in nodo.body:
                if isinstance(miembro, ast.FunctionDef) and miembro.name == "__init__":
                    for sent in ast.walk(miembro):
                        if isinstance(sent, ast.ImportFrom):
                            for alias in sent.names:
                                alias_map[alias.asname or alias.name] = alias.name

                    llamadas: set[str] = set()
                    for sub in ast.walk(miembro):
                        if isinstance(sub, ast.Call):
                            if isinstance(sub.func, ast.Name):
                                llamador = sub.func.id
                            elif isinstance(sub.func, ast.Attribute):
                                # dotted call: el nombre del atributo final
                                # es el identificador del constructor.
                                llamador = sub.func.attr
                            else:
                                continue
                            llamadas.add(alias_map.get(llamador, llamador))
                    return llamadas
    raise AssertionError("No se encontró SupervisorAgent.__init__ en el AST")


def test_supervisor_init_no_construye_los_servicios_del_grafo() -> None:
    llamadas = _nombres_de_calls_en_init()
    for servicio in _SERVICIOS_DEL_GRAFO:
        assert servicio not in llamadas, (
            f"SupervisorAgent.__init__ volvió a construir {servicio} — "
            "la construcción debe vivir en build_orchestration_composition()"
        )


def test_supervisor_init_delega_en_build_orchestration_composition() -> None:
    llamadas = _nombres_de_calls_en_init()
    assert "build_orchestration_composition" in llamadas


# ---------------------------------------------------------------------------
# Helpers para construir una composition real con infra mockeada
# ---------------------------------------------------------------------------


def _build_composicion_con_dobles() -> OrchestrationComposition:
    """Construye la composition real pasando dobles para la infraestructura."""
    return build_orchestration_composition(
        scraper=MagicMock(),
        lock_manager=MagicMock(),
        snapshot_manager=MagicMock(),
        journal=MagicMock(),
        path_resolver=MagicMock(),
        path_validator=MagicMock(),
        event_bus=MagicMock(),
        profile_name="PerfilSesion",
        hitl_guard=MagicMock(),
        loot_runner=None,
        require_vfs=False,
        patch_advisor_llm=None,
        grass_runtime_deps_provider=MagicMock(),
        plugin_limit_guard=MagicMock(),
        scan_asset_conflicts=MagicMock(),
        scan_asset_conflicts_json=MagicMock(),
    )


# ---------------------------------------------------------------------------
# T4 — identidad: servicios de la composition == servicios del dispatcher
# ---------------------------------------------------------------------------


def test_servicios_de_la_composition_son_los_del_dispatcher() -> None:
    composition = _build_composicion_con_dobles()
    deps = composition.dispatcher_dependencies

    assert deps.loot_service is composition.loot_service
    assert deps.xedit_service is composition.xedit_service
    assert deps.dyndolod_service is composition.dyndolod_service
    assert deps.pandora_service is composition.pandora_service
    assert deps.grass_cache_service is composition.grass_cache_service
    # Wrye Bash no aparece directo en deps: entra como callable construido
    # sobre composition.wrye_bash_service (perfil por defecto).
    assert deps.wrye_bash_pipeline is not None


# ---------------------------------------------------------------------------
# T5 — lazy: construir la composition no resuelve paths/executables
# ---------------------------------------------------------------------------


def test_construir_composition_no_resuelve_paths_ni_executables() -> None:
    resolver = MagicMock()
    build_orchestration_composition(
        scraper=MagicMock(),
        lock_manager=MagicMock(),
        snapshot_manager=MagicMock(),
        journal=MagicMock(),
        path_resolver=resolver,
        path_validator=MagicMock(),
        event_bus=MagicMock(),
        profile_name="PerfilSesion",
        hitl_guard=MagicMock(),
        loot_runner=None,
        require_vfs=False,
        patch_advisor_llm=None,
        grass_runtime_deps_provider=MagicMock(),
        plugin_limit_guard=MagicMock(),
        scan_asset_conflicts=MagicMock(),
        scan_asset_conflicts_json=MagicMock(),
    )

    resolver.get_skyrim_path.assert_not_called()
    resolver.get_skyrim_path_raw.assert_not_called()
    resolver.get_xedit_path.assert_not_called()
    resolver.get_loot_exe.assert_not_called()
    resolver.get_mo2_path.assert_not_called()
    resolver.get_mo2_mods_path.assert_not_called()
    resolver.detect_mo2_path.assert_not_called()


# ---------------------------------------------------------------------------
# T6 — Synthesis: base service y sandbox factory comparten config path
# ---------------------------------------------------------------------------


def test_synthesis_base_y_factory_comparten_config_path() -> None:
    composition = _build_composicion_con_dobles()
    base_path = composition.synthesis_service._pipeline_config_path
    factory = composition.dispatcher_dependencies.synthesis_service_factory

    sandbox_service = factory(pathlib.Path("overwrite/Synthesis.esp"), MagicMock())

    assert sandbox_service is not composition.synthesis_service  # fresco por ejecución
    assert sandbox_service._pipeline_config_path == base_path
    assert base_path == pathlib.Path(".skyclaw_backups/") / "synthesis_pipeline.json"


# ---------------------------------------------------------------------------
# T7 — residual seams: no se ejecutan al construir la composition
# ---------------------------------------------------------------------------


def test_construir_composition_no_ejecuta_los_seams_residuales() -> None:
    grass = MagicMock()
    plugin_limit = MagicMock()
    scan = MagicMock()
    scan_json = MagicMock()

    build_orchestration_composition(
        scraper=MagicMock(),
        lock_manager=MagicMock(),
        snapshot_manager=MagicMock(),
        journal=MagicMock(),
        path_resolver=MagicMock(),
        path_validator=MagicMock(),
        event_bus=MagicMock(),
        profile_name="PerfilSesion",
        hitl_guard=MagicMock(),
        loot_runner=None,
        require_vfs=False,
        patch_advisor_llm=None,
        grass_runtime_deps_provider=grass,
        plugin_limit_guard=plugin_limit,
        scan_asset_conflicts=scan,
        scan_asset_conflicts_json=scan_json,
    )

    grass.assert_not_called()
    plugin_limit.assert_not_called()
    scan.assert_not_called()
    scan_json.assert_not_called()


# ---------------------------------------------------------------------------
# T8 — supervisor wiring: conserva identidad de la composition recibida
# ---------------------------------------------------------------------------


def test_supervisor_conserva_identidad_de_la_composition(mo2_root, tmp_path) -> None:
    from sky_claw.app.security.path_validator import PathValidator

    sandbox = PathValidator(roots=[mo2_root, tmp_path])
    sup = SupervisorAgent(path_validator=sandbox)

    # Servicios de la composition → supervisor (identidad, no isinstance).
    assert sup._loot_service is sup._dispatcher_dependencies.loot_service
    assert sup._xedit_service is sup._dispatcher_dependencies.xedit_service
    assert sup._dyndolod_service is sup._dispatcher_dependencies.dyndolod_service
    assert sup._pandora_service is sup._dispatcher_dependencies.pandora_service
    assert sup._grass_cache_service is sup._dispatcher_dependencies.grass_cache_service

    # Dispatcher: instancia del tipo real y con el loop guardrail de la composition.
    assert isinstance(sup._tool_dispatcher, OrchestrationToolDispatcher)
    assert sup._tool_dispatcher._global_middleware[0] is sup._loop_guardrail_middleware
    # Idempotency global usa la state machine conservada por el supervisor.
    assert sup._tool_dispatcher._global_middleware[1]._sm is sup._tool_state_machine
    # Shutdown contract: el dispatcher conserva drain().
    assert callable(sup._tool_dispatcher.drain)


# ---------------------------------------------------------------------------
# T9 — lifecycle: shutdown order en SupervisorAgent.start()
# ---------------------------------------------------------------------------


def _nombres_de_await_en_finally_de_start() -> list[str]:
    """Nombres de métodos await en el finally del método start() de SupervisorAgent.

    Retorna el orden exacto de las expresiones ``await self.<algo>`` dentro del
    bloque ``finally`` de ``start()``, para anclar el invariante de shutdown.
    """
    ruta = pathlib.Path(inspect.getfile(SupervisorAgent))
    arbol = ast.parse(ruta.read_text(encoding="utf-8"))
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.ClassDef) and nodo.name == "SupervisorAgent":
            for miembro in nodo.body:
                if isinstance(miembro, (ast.FunctionDef, ast.AsyncFunctionDef)) and miembro.name == "start":
                    for sub in ast.walk(miembro):
                        if isinstance(sub, ast.Try) and sub.finalbody:
                            return _awaits_del_bloque(sub.finalbody)
    raise AssertionError("No se encontró SupervisorAgent.start() → finally en el AST")


def _awaits_del_bloque(stmts: list[ast.stmt]) -> list[str]:
    """Extrae ``await x.y()`` del primer nivel de un bloque, en orden.

    Maneja tanto ``self.db.close()`` (Attribute → Name) como
    ``self._tool_dispatcher.drain()`` (Attribute → Attribute → Name).
    """
    orden: list[str] = []
    for stmt in stmts:
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Await):
            llamada = stmt.value.value
            if isinstance(llamada, ast.Call) and isinstance(llamada.func, ast.Attribute):
                metodo = llamada.func.attr
                objetivo = llamada.func.value
                if isinstance(objetivo, ast.Attribute):
                    # self._tool_dispatcher.drain → "_tool_dispatcher.drain"
                    orden.append(f"{objetivo.attr}.{metodo}")
                elif isinstance(objetivo, ast.Name):
                    orden.append(f"{objetivo.id}.{metodo}")
    return orden


def test_shutdown_order_invariante() -> None:
    orden = _nombres_de_await_en_finally_de_start()
    assert orden == [
        "_tool_dispatcher.drain",
        "_event_bus.stop",
        "_lock_manager.close",
        "journal.close",
        "db.close",
    ], f"El orden de shutdown cambió: {orden}"
