"""Construcción hermética del contrato de Grass Cache para acceptance BDD."""

from __future__ import annotations

import pathlib
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from sky_claw.app.db.journal import OperationJournal, OperationType, TransactionStatus
from sky_claw.app.orchestrator.supervisor import SupervisorAgent
from sky_claw.app.orchestrator.tool_dispatcher import OrchestrationToolDispatcher, build_orchestration_dispatcher
from sky_claw.app.orchestrator.tool_strategies.middleware import HitlGateMiddleware
from sky_claw.app.security.hitl import Decision, HITLGuard
from sky_claw.local.tools.grass_cache_runner import GrassCacheRunResult
from sky_claw.local.tools.grass_cache_service import GrassCacheService


def preparar_entorno_mo2(tmp_path: pathlib.Path) -> dict[str, pathlib.Path]:
    """Crea rutas aisladas con un ejecutable de juego mínimo y overwrite de MO2."""
    game = tmp_path / "game"
    game.mkdir(exist_ok=True)
    (game / "SkyrimSE.exe").write_bytes(b"MZ")
    mo2_root = tmp_path / "mo2"
    (mo2_root / "overwrite").mkdir(parents=True, exist_ok=True)
    return {"game": game, "mo2_root": mo2_root}


def _lock_tx_fake() -> MagicMock:
    """Async context manager no-op para observar los recursos solicitados."""
    tx = MagicMock()
    tx.__aenter__ = AsyncMock(return_value=tx)
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

    def _fabrica_lock(**kwargs: Any) -> MagicMock:
        llamadas_lock.append(kwargs)
        return _lock_tx_fake()

    event_bus = AsyncMock()
    colaboradores: dict[str, Any] = {
        "profile_manager": profile_manager,
        "runner": runner,
        "runner_factory": runner_factory,
        "event_bus": event_bus,
        "llamadas_lock": llamadas_lock,
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


def construir_dispatcher_grass(service: GrassCacheService) -> tuple[OrchestrationToolDispatcher, HITLGuard]:
    """Construye el dispatcher de producción con Grass Cache real y demás boundaries mockeados."""
    hitl_guard = MagicMock(spec=HITLGuard)
    hitl_guard.request_approval = AsyncMock(return_value=Decision.DENIED)
    gate = HitlGateMiddleware(hitl_guard=hitl_guard)
    supervisor = SupervisorAgent.__new__(SupervisorAgent)
    supervisor.scraper = MagicMock()
    supervisor.tools = MagicMock()
    supervisor.interface = MagicMock()
    supervisor._loot_service = MagicMock()
    supervisor._synthesis_service = MagicMock()
    supervisor._xedit_service = MagicMock()
    supervisor._dyndolod_service = MagicMock()
    supervisor._pandora_service = MagicMock()
    supervisor._grass_cache_service = service
    supervisor.profile_name = "BDDProfile"
    dispatcher = build_orchestration_dispatcher(supervisor, hitl_gate=gate)
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
