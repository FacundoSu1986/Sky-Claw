"""Construcción hermética del contrato de Grass Cache para acceptance BDD."""

from __future__ import annotations

import ast
import dataclasses
import inspect
import pathlib
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from sky_claw.app.db.journal import OperationJournal, OperationType, TransactionStatus
from sky_claw.app.orchestrator.dispatcher_dependencies import (
    OrchestrationDispatcherDependencies,
)
from sky_claw.app.orchestrator.supervisor import SupervisorAgent
from sky_claw.app.orchestrator.tool_dispatcher import OrchestrationToolDispatcher, build_orchestration_dispatcher
from sky_claw.app.orchestrator.tool_strategies.middleware import HitlGateMiddleware
from sky_claw.app.security.hitl import Decision, HITLGuard
from sky_claw.local.tools.grass_cache_runner import GrassCacheRunResult
from sky_claw.local.tools.grass_cache_service import GrassCacheService

SUPERFICIE_DISPATCHER_DEPENDENCIES = frozenset(
    {
        "default_profile_getter",
        "dyndolod_service",
        "grass_cache_service",
        "loot_service",
        "pandora_service",
        "plugin_limit_guard",
        "preview_service_provider",
        "real_journal_provider",
        "scan_asset_conflicts",
        "scan_asset_conflicts_json",
        "scraper",
        "synthesis_flow_provider",
        "synthesis_service_factory",
        "wrye_bash_pipeline",
        "xedit_service",
    }
)


def superficie_dispatcher_dependencies() -> frozenset[str]:
    """Enumera por AST todas las capacidades ``dependencies.*`` usadas."""
    modulo = inspect.getmodule(build_orchestration_dispatcher)
    assert modulo is not None
    arbol = ast.parse(inspect.getsource(modulo))
    return frozenset(
        nodo.attr
        for nodo in ast.walk(arbol)
        if isinstance(nodo, ast.Attribute) and isinstance(nodo.value, ast.Name) and nodo.value.id == "dependencies"
    )


def campos_dispatcher_dependencies() -> frozenset[str]:
    """Enumera los campos declarados en ``OrchestrationDispatcherDependencies``.

    Cruza con ``superficie_dispatcher_dependencies`` para que un campo
    agregado al dataclass y nunca cableado en ``build_orchestration_dispatcher``
    rompa el test, y un atributo ``dependencies.*`` usado por el dispatcher
    sin declarar también. Sin este ancla, los dos lados pueden divergir
    silenciosamente (regla "arreglar un hermano y no al otro").
    """
    return frozenset(campo.name for campo in dataclasses.fields(OrchestrationDispatcherDependencies))


def preparar_entorno_mo2(tmp_path: pathlib.Path) -> dict[str, pathlib.Path]:
    """Crea rutas aisladas con un ejecutable de juego mínimo y overwrite de MO2."""
    game = tmp_path / "game"
    game.mkdir(exist_ok=True)
    (game / "SkyrimSE.exe").write_bytes(b"MZ")
    mo2_root = tmp_path / "mo2"
    (mo2_root / "overwrite").mkdir(parents=True, exist_ok=True)
    return {"game": game, "mo2_root": mo2_root}


def _lock_tx_fake(resource_id: str, entradas: list[str]) -> MagicMock:
    """Async context manager no-op para observar los recursos solicitados."""
    tx = MagicMock()

    async def _entrar() -> MagicMock:
        entradas.append(resource_id)
        return tx

    tx.__aenter__ = AsyncMock(side_effect=_entrar)
    tx.__aexit__ = AsyncMock(return_value=False)
    tx.rollback_completed = False
    return tx


def construir_servicio_grass(
    tmp_path: pathlib.Path, *, journal: OperationJournal | None
) -> tuple[GrassCacheService, dict[str, Any]]:
    """Construye el servicio con journal real y dobles sólo en boundaries externos."""
    rutas = preparar_entorno_mo2(tmp_path)

    profile_manager = AsyncMock()
    profile_manager.clone_profile = "SkyClaw-GrassCache"
    profile_manager.teardown.return_value = []

    runner = AsyncMock()
    runner.run.return_value = GrassCacheRunResult(
        success=True,
        message="",
        outcome="completed",
        crash_count=3,
        cgid_count=120,
        cache_size_mb=45.5,
        elapsed_s=3600.0,
    )
    runner_factory = MagicMock(return_value=runner)

    llamadas_lock: list[dict[str, Any]] = []
    entradas_lock: list[str] = []
    locks_creados: dict[str, MagicMock] = {}

    def _fabrica_lock(**kwargs: Any) -> MagicMock:
        llamadas_lock.append(kwargs)
        resource_id = str(kwargs["resource_id"])
        lock = _lock_tx_fake(resource_id, entradas_lock)
        locks_creados[resource_id] = lock
        return lock

    event_bus = AsyncMock()
    colaboradores: dict[str, Any] = {
        "profile_manager": profile_manager,
        "runner": runner,
        "runner_factory": runner_factory,
        "event_bus": event_bus,
        "llamadas_lock": llamadas_lock,
        "entradas_lock": entradas_lock,
        "locks_creados": locks_creados,
    }
    service = GrassCacheService(
        lock_manager=MagicMock(),
        snapshot_manager=MagicMock(),
        journal=journal,
        event_bus=event_bus,
        profile_manager=profile_manager,
        mo2=MagicMock(),
        game_path=rutas["game"],
        overwrite_grass_dir=rutas["mo2_root"] / "overwrite" / "Grass",
        runner_factory=runner_factory,
        lock_factory=_fabrica_lock,
    )
    return service, colaboradores


def construir_dispatcher_grass(
    service: GrassCacheService, *, con_guard: bool = True
) -> tuple[OrchestrationToolDispatcher, HITLGuard | None]:
    """Construye el dispatcher de producción con Grass Cache real y demás boundaries mockeados.

    ``con_guard=False`` deja ``hitl_guard=None``. El ``HitlGateMiddleware`` se
    construye SIEMPRE de forma explícita — igual que ``SupervisorAgent.__init__``
    (``tool_dispatcher.py:303-308``: ``hitl_gate=HitlGateMiddleware(hitl_guard=hitl_guard)``,
    nunca ``hitl_gate=None``) — y NO se delega en el fallback propio de
    ``build_orchestration_dispatcher`` (``gate = hitl_gate or HitlGateMiddleware()``).
    Ese fallback es el "hermano" que nunca corre en producción: el único caller
    real siempre pasa un ``HitlGateMiddleware`` armado, así que el fail-closed
    que importa anclar es el de ``HitlGateMiddleware(hitl_guard=None)`` — no el
    de ``build_orchestration_dispatcher`` ante un ``hitl_gate`` ausente (review
    #420: probar el segundo dejaba sin cubrir el primero, que es el que
    realmente se alcanza). NUNCA se pasa ``allow_unattended=True``, que
    desactivaría el fail-closed en vez de anclarlo.
    """
    assert campos_dispatcher_dependencies() == SUPERFICIE_DISPATCHER_DEPENDENCIES
    assert superficie_dispatcher_dependencies() == SUPERFICIE_DISPATCHER_DEPENDENCIES
    hitl_guard: HITLGuard | None = None
    if con_guard:
        hitl_guard = MagicMock(spec=HITLGuard)
        hitl_guard.request_approval = AsyncMock(return_value=Decision.DENIED)
    gate = HitlGateMiddleware(hitl_guard=hitl_guard)
    supervisor = SupervisorAgent.__new__(SupervisorAgent)
    supervisor.scraper = MagicMock()
    supervisor.snapshot_manager = MagicMock()
    supervisor.journal = MagicMock()
    supervisor._event_bus = MagicMock()
    supervisor._hitl_guard = hitl_guard
    supervisor._lock_manager = MagicMock()
    supervisor._path_resolver = MagicMock()
    supervisor._path_validator = MagicMock()
    supervisor._loot_service = MagicMock()
    supervisor._synthesis_service = MagicMock()
    supervisor._xedit_service = MagicMock()
    supervisor._dyndolod_service = MagicMock()
    supervisor._pandora_service = MagicMock()
    supervisor._grass_cache_service = service
    supervisor._run_plugin_limit_guard = MagicMock()
    supervisor.execute_wrye_bash_pipeline = AsyncMock()
    supervisor.scan_asset_conflicts = AsyncMock()
    supervisor.scan_asset_conflicts_json = AsyncMock()
    supervisor.profile_name = "BDDProfile"
    from tests._orchestration_dispatcher_dependencies import crear_dependencias_desde_doble

    dispatcher = build_orchestration_dispatcher(
        crear_dependencias_desde_doble(supervisor),
        hitl_gate=gate,
    )
    return dispatcher, hitl_guard


async def abrir_journal_real(db_path: pathlib.Path) -> OperationJournal:
    """Abre un journal SQLite ligado al loop del escenario."""
    journal = OperationJournal(db_path)
    await journal.open()
    return journal


async def sembrar_loot_completado(journal: OperationJournal) -> None:
    """Registra el FlightReport commiteado que satisface el guard Stage 5→8."""
    loot_tx = await journal.begin_transaction("LOOT sort", agent_id=GrassCacheService.LOOT_AGENT_ID)
    loot_op = await journal.begin_operation(
        agent_id=GrassCacheService.LOOT_AGENT_ID,
        operation_type=OperationType.FILE_MODIFY,
        target_path="plugins.txt",
        transaction_id=loot_tx,
        metadata={"kind": "flight_report", "transaction_status": TransactionStatus.COMMITTED.value},
    )
    await journal.complete_operation(loot_op)
    await journal.commit_transaction(loot_tx)
