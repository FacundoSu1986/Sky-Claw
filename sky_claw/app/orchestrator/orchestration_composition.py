"""Composición tipada del grafo de tools y servicios del Orchestrator.

PR2 del Strangler Fig: extrae la construcción de servicios, providers,
middleware, dependencias del dispatcher y dispatcher a una composition
tipada externa.

La clase que antes actuaba como composition root invoca
``build_orchestration_composition()`` y recibe un
:class:`OrchestrationComposition` con los objetos ya cableados. Tras PR3,
el seam de Grass vive en ``grass_runtime_deps.GrassRuntimeDepsProvider``
(deps explícitas, resolución lazy); los seams residuales de asset scan y
plugin-limit guard siguen entregándose como callables estrechos (PR4 los
extraerá).

Sin cambio funcional intencional.
"""

from __future__ import annotations

import pathlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sky_claw.app.orchestrator.dispatcher_dependencies import (
    OrchestrationDispatcherDependencies,
    build_preview_chain_service_provider,
    build_synthesis_flow_provider,
    build_synthesis_service_factory,
    build_wrye_bash_pipeline,
)
from sky_claw.app.orchestrator.tool_dispatcher import (
    OrchestrationToolDispatcher,
    build_orchestration_dispatcher,
)
from sky_claw.app.orchestrator.tool_state_machine import ToolStateMachine
from sky_claw.app.orchestrator.tool_strategies.middleware import (
    HitlGateMiddleware,
    IdempotencyMiddleware,
    LoopGuardrailMiddleware,
)
from sky_claw.local.tools.dyndolod_service import DynDOLODPipelineService
from sky_claw.local.tools.grass_cache_service import GrassCacheService
from sky_claw.local.tools.loot_service import LootSortingService
from sky_claw.local.tools.pandora_service import PandoraPipelineService
from sky_claw.local.tools.synthesis_service import SynthesisPipelineService
from sky_claw.local.tools.wrye_bash_service import WryeBashPipelineService
from sky_claw.local.tools.xedit_service import XEditPipelineService

if TYPE_CHECKING:
    from sky_claw.app.core.event_bus import CoreEventBus
    from sky_claw.app.core.path_resolver import PathResolutionService
    from sky_claw.app.db.journal import OperationJournal
    from sky_claw.app.db.locks import DistributedLockManager
    from sky_claw.app.db.snapshot_manager import FileSnapshotManager
    from sky_claw.app.scraper.scraper_agent import ScraperAgent
    from sky_claw.app.security.hitl import HITLGuard
    from sky_claw.app.security.path_validator import PathValidator
    from sky_claw.local.ai.patch_advisor_llm import LLMCallable
    from sky_claw.local.assets import AssetConflictReport
    from sky_claw.local.tools.grass_cache_service import GrassRuntimeDeps
    from sky_claw.local.tools.loot_service import LootRunnerProtocol

#: Directorio de staging de backups (compartido con supervisor.py).
# Nota: duplicado intencional para evitar que orchestration_composition
# importe a supervisor.py (invariante arquitectónico). El servicio de
# Synthesis también tiene su copia local.
_BACKUP_STAGING_DIR = ".skyclaw_backups/"


@dataclass(frozen=True, slots=True)
class OrchestrationComposition:
    """Composición tipada del grafo de tools del Orchestrator.

    Contiene los objetos finales que el lifecycle coordinator necesita
    conservar después de construir el grafo, sin conocer cómo se construyeron.
    """

    # Servicios de pipeline
    synthesis_service: SynthesisPipelineService
    dyndolod_service: DynDOLODPipelineService
    xedit_service: XEditPipelineService
    loot_service: LootSortingService
    pandora_service: PandoraPipelineService
    grass_cache_service: GrassCacheService
    wrye_bash_service: WryeBashPipelineService

    # Dependencias del dispatcher (cableadas con los servicios de arriba)
    dispatcher_dependencies: OrchestrationDispatcherDependencies

    # Dispatcher construido
    tool_dispatcher: OrchestrationToolDispatcher

    # Middleware global (conservado para reset manual desde GUI)
    loop_guardrail_middleware: LoopGuardrailMiddleware

    # Máquina de estados de tools (conservada para introspección)
    tool_state_machine: ToolStateMachine


def build_orchestration_composition(
    *,
    # Infraestructura compartida
    scraper: ScraperAgent,
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    journal: OperationJournal,
    path_resolver: PathResolutionService,
    path_validator: PathValidator,
    event_bus: CoreEventBus,
    profile_name: str,
    # Seguridad
    hitl_guard: HITLGuard | None,
    # Parámetros específicos de servicios
    loot_runner: LootRunnerProtocol | None,
    require_vfs: bool,
    patch_advisor_llm: LLMCallable | None,
    # Seams residuales de asset scan y plugin-limit (transitorios — PR4);
    # el provider de Grass ya llega extraído desde PR3.
    grass_runtime_deps_provider: Callable[[], GrassRuntimeDeps | None],
    plugin_limit_guard: Callable[[str], Awaitable[dict[str, Any]]],
    scan_asset_conflicts: Callable[[], list[AssetConflictReport]],
    scan_asset_conflicts_json: Callable[[], str],
) -> OrchestrationComposition:
    """Construye el grafo completo de servicios, providers, middleware y dispatcher.

    Construye **en orden** los servicios (porque ``GrassCacheService`` necesita
    ``xedit_service.ensure_xedit_runner``), luego los providers lazy, luego
    las dependencias del dispatcher, luego el dispatcher mismo.

    **NO** resuelve paths de Skyrim/xEdit/LOOT/MO2 durante la construcción:
    todos los servicios usan construcción perezosa de runners.
    **NO** invoca los seams residuales (grass, plugin-limit, asset scan).
    """

    # ------------------------------------------------------------------
    # 1. Ruta de configuración compartida de Synthesis
    # ------------------------------------------------------------------
    synthesis_pipeline_config_path = pathlib.Path(_BACKUP_STAGING_DIR) / "synthesis_pipeline.json"

    # ------------------------------------------------------------------
    # 2. Servicios de pipeline
    # ------------------------------------------------------------------
    synthesis_service = SynthesisPipelineService(
        lock_manager=lock_manager,
        snapshot_manager=snapshot_manager,
        journal=journal,
        path_resolver=path_resolver,
        event_bus=event_bus,
        pipeline_config_path=synthesis_pipeline_config_path,
    )

    # D2 (PR #493): mo2_profile es la identidad esperada del dueño del
    # handoff durable de DynDOLOD.
    dyndolod_service = DynDOLODPipelineService(
        lock_manager=lock_manager,
        snapshot_manager=snapshot_manager,
        journal=journal,
        path_resolver=path_resolver,
        event_bus=event_bus,
        mo2_profile=profile_name,
    )

    xedit_service = XEditPipelineService(
        lock_manager=lock_manager,
        snapshot_manager=snapshot_manager,
        journal=journal,
        path_resolver=path_resolver,
        event_bus=event_bus,
        llm=patch_advisor_llm,  # Fase 1 AI-assisted (None = fail-closed)
    )

    # Audit #190: LOOT --sort reescribe el load order compartido, así que la
    # corrida real va bajo el lock de load-order (LOOTRunner construido
    # perezosamente desde el resolver, como los otros servicios de tools).
    # T-26 (review Codex PR #243): el journal se cablea acá (path
    # GUI/dispatcher) para que el Ritual de LOOT emita el ActionManifest antes
    # de mutar — sin esto el guard era test-only.
    loot_service = LootSortingService(
        lock_manager=lock_manager,
        snapshot_manager=snapshot_manager,
        path_resolver=path_resolver,
        path_validator=path_validator,
        loot_runner=loot_runner,
        require_vfs=require_vfs,
        journal=journal,
    )

    # Follow-up A: Pandora regenera behavior graphs, así que la corrida real va
    # bajo el lock de behavior-graphs (runner perezoso desde el resolver).
    # T-26/T-28 (ADR 0002): el journal se cablea acá (path GUI/dispatcher) para
    # emitir la caja negra de vuelo — el path del agente (system_tools.
    # run_pandora) NO lo cablea (honesto).
    pandora_service = PandoraPipelineService(
        lock_manager=lock_manager,
        snapshot_manager=snapshot_manager,
        path_resolver=path_resolver,
        journal=journal,
    )

    # Grass cache: orquestador del Stage 8 (NGIO). El runner de xEdit de la
    # Fase A se resuelve perezosamente vía xedit_service.ensure_xedit_runner;
    # las dependencias de Fases B/C (perfil MO2, game path, overwrite/Grass)
    # llegan como PROVIDER LAZY (grass_runtime_deps_provider): se resuelven al
    # ejecutar el ritual, no en construcción, porque en la GUI MO2_PATH/
    # SKYRIM_PATH se hidratan DESPUÉS de construir el supervisor (review
    # Codex #301). El servicio devuelve error accionable del contrato si
    # todavía no están configuradas.
    grass_cache_service = GrassCacheService(
        lock_manager=lock_manager,
        snapshot_manager=snapshot_manager,
        journal=journal,
        event_bus=event_bus,
        xedit_runner_provider=xedit_service.ensure_xedit_runner,
        runtime_deps_provider=grass_runtime_deps_provider,
    )

    # PR A (caja negra de Wrye Bash): extracción Strangler-Fig del pipeline que
    # antes vivía inline en el supervisor. Era el ÚNICO ritual mutante sin
    # SnapshotTransactionLock (hueco de concurrencia); el servicio lo corre bajo
    # el lock distribuido. El guard M-04 (compartido, expuesto por
    # validate_plugin_limit) se inyecta desde acá porque no es exclusivo de
    # Wrye Bash. T-26/T-28 ("PR C" del #315): el journal cableado emite la caja
    # negra de vuelo.
    wrye_bash_service = WryeBashPipelineService(
        lock_manager=lock_manager,
        snapshot_manager=snapshot_manager,
        path_resolver=path_resolver,
        plugin_limit_guard=plugin_limit_guard,
        journal=journal,
    )

    # ------------------------------------------------------------------
    # 3. Middleware y máquina de estados
    # ------------------------------------------------------------------
    loop_guardrail_middleware = LoopGuardrailMiddleware()
    tool_state_machine = ToolStateMachine()

    # ------------------------------------------------------------------
    # 4. Providers lazy
    # ------------------------------------------------------------------
    synthesis_flow_provider = build_synthesis_flow_provider(
        path_resolver=path_resolver,
        profile_name=profile_name,
        hitl_guard=hitl_guard,
    )
    synthesis_service_factory = build_synthesis_service_factory(
        lock_manager=lock_manager,
        snapshot_manager=snapshot_manager,
        path_resolver=path_resolver,
        event_bus=event_bus,
        pipeline_config_path=synthesis_pipeline_config_path,
    )
    preview_chain_service_provider = build_preview_chain_service_provider(
        path_resolver=path_resolver,
        path_validator=path_validator,
        lock_manager=lock_manager,
        snapshot_manager=snapshot_manager,
        journal=journal,
        event_bus=event_bus,
    )

    # ------------------------------------------------------------------
    # 5. Dependencias del dispatcher (callable estrecho para Wrye Bash)
    # ------------------------------------------------------------------
    default_profile = profile_name
    dispatcher_dependencies = OrchestrationDispatcherDependencies(
        scraper=scraper,
        loot_service=loot_service,
        synthesis_flow_provider=synthesis_flow_provider,
        synthesis_service_factory=synthesis_service_factory,
        real_journal_provider=lambda: journal,
        xedit_service=xedit_service,
        dyndolod_service=dyndolod_service,
        pandora_service=pandora_service,
        grass_cache_service=grass_cache_service,
        scan_asset_conflicts=scan_asset_conflicts,
        scan_asset_conflicts_json=scan_asset_conflicts_json,
        wrye_bash_pipeline=build_wrye_bash_pipeline(
            service=wrye_bash_service,
            default_profile=default_profile,
        ),
        plugin_limit_guard=plugin_limit_guard,
        default_profile_getter=lambda: default_profile,
        preview_service_provider=preview_chain_service_provider,
    )

    # ------------------------------------------------------------------
    # 6. Dispatcher con middleware
    # ------------------------------------------------------------------
    tool_dispatcher = build_orchestration_dispatcher(
        dispatcher_dependencies,
        hitl_gate=HitlGateMiddleware(hitl_guard=hitl_guard),
        loop_guardrail=loop_guardrail_middleware,
        idempotency=IdempotencyMiddleware(state_machine=tool_state_machine),
    )

    # ------------------------------------------------------------------
    # 7. Composición
    # ------------------------------------------------------------------
    return OrchestrationComposition(
        synthesis_service=synthesis_service,
        dyndolod_service=dyndolod_service,
        xedit_service=xedit_service,
        loot_service=loot_service,
        pandora_service=pandora_service,
        grass_cache_service=grass_cache_service,
        wrye_bash_service=wrye_bash_service,
        dispatcher_dependencies=dispatcher_dependencies,
        tool_dispatcher=tool_dispatcher,
        loop_guardrail_middleware=loop_guardrail_middleware,
        tool_state_machine=tool_state_machine,
    )
