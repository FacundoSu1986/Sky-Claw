"""Dependencias explícitas y providers lazy del dispatcher de orquestación."""

from __future__ import annotations

import pathlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from sky_claw.app.core.event_bus import CoreEventBus
    from sky_claw.app.core.path_resolver import PathResolutionService
    from sky_claw.app.db.journal import OperationJournal
    from sky_claw.app.db.locks import DistributedLockManager
    from sky_claw.app.db.snapshot_manager import FileSnapshotManager
    from sky_claw.app.orchestrator.preview.chain_preview_service import ChainPreviewService
    from sky_claw.app.orchestrator.sandbox_promotion import SandboxPromotionFlow
    from sky_claw.app.scraper.scraper_agent import ScraperAgent
    from sky_claw.app.security.hitl import HITLGuard
    from sky_claw.app.security.path_validator import PathValidator
    from sky_claw.local.assets import AssetConflictReport
    from sky_claw.local.tools.dyndolod_service import DynDOLODPipelineService
    from sky_claw.local.tools.grass_cache_service import GrassCacheService
    from sky_claw.local.tools.loot_service import LootSortingService
    from sky_claw.local.tools.pandora_service import PandoraPipelineService
    from sky_claw.local.tools.synthesis_service import SynthesisPipelineService
    from sky_claw.local.tools.wrye_bash_service import WryeBashPipelineService
    from sky_claw.local.tools.xedit_service import XEditPipelineService


class WryeBashPipeline(Protocol):
    """Capacidad mínima que consume ``GenerateBashedPatchStrategy``."""

    def __call__(
        self,
        profile: str | None = None,
        validate_limit: bool = True,
    ) -> Awaitable[dict[str, Any]]: ...


@dataclass(frozen=True, slots=True)
class OrchestrationDispatcherDependencies:
    """Capacidades necesarias para registrar las tools, sin service locator."""

    scraper: ScraperAgent
    loot_service: LootSortingService
    synthesis_flow_provider: Callable[[], SandboxPromotionFlow]
    synthesis_service_factory: Callable[[pathlib.Path, OperationJournal], SynthesisPipelineService]
    real_journal_provider: Callable[[], OperationJournal]
    xedit_service: XEditPipelineService
    dyndolod_service: DynDOLODPipelineService
    pandora_service: PandoraPipelineService
    grass_cache_service: GrassCacheService
    scan_asset_conflicts: Callable[[], list[AssetConflictReport]]
    scan_asset_conflicts_json: Callable[[], str]
    wrye_bash_pipeline: WryeBashPipeline
    plugin_limit_guard: Callable[[str], Awaitable[dict[str, Any]]]
    default_profile_getter: Callable[[], str]
    preview_service_provider: Callable[[], ChainPreviewService]


def build_preview_chain_service_provider(
    *,
    path_resolver: PathResolutionService,
    path_validator: PathValidator,
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    journal: OperationJournal,
    event_bus: CoreEventBus,
) -> Callable[[], ChainPreviewService]:
    """Cierra dependencias concretas y difiere la resolución de binarios."""

    def provide() -> ChainPreviewService:
        from sky_claw.app.orchestrator.preview.chain_preview_service import ChainPreviewService
        from sky_claw.local.loot.cli import LOOTConfig, LOOTRunner
        from sky_claw.local.xedit.conflict_analyzer import ConflictAnalyzer
        from sky_claw.local.xedit.runner import XEditRunner

        game_path = path_resolver.get_skyrim_path()
        xedit_path = path_resolver.get_xedit_path()
        if game_path is None or xedit_path is None:
            raise RuntimeError("Cannot preview the chain: SKYRIM_PATH and XEDIT_PATH must be configured.")

        loot_exe = path_resolver.get_loot_exe() or pathlib.Path("loot.exe")
        loot_runner = LOOTRunner(
            LOOTConfig(loot_exe=loot_exe, game_path=game_path),
            path_validator=path_validator,
        )
        xedit_runner = XEditRunner(
            xedit_path=xedit_path,
            game_path=game_path,
            output_dir=pathlib.Path(".skyclaw_backups/patches"),
        )
        return ChainPreviewService(
            lock_manager=lock_manager,
            snapshot_manager=snapshot_manager,
            journal=journal,
            path_resolver=path_resolver,
            path_validator=path_validator,
            event_bus=event_bus,
            loot_runner=loot_runner,
            xedit_runner=xedit_runner,
            conflict_analyzer=ConflictAnalyzer(),
        )

    return provide


def build_synthesis_flow_provider(
    *,
    path_resolver: PathResolutionService,
    profile_name: str,
    hitl_guard: HITLGuard | None,
) -> Callable[[], SandboxPromotionFlow]:
    """Cierra resolver, perfil y guard; MO2 se resuelve recién al ejecutar."""

    def provide() -> SandboxPromotionFlow:
        from sky_claw.app.orchestrator.sandbox_promotion import SandboxPromotionFlow
        from sky_claw.local.mo2.profile_sandbox import ProfileSandbox

        mo2_path = path_resolver.get_mo2_path()
        if mo2_path is None:
            raise RuntimeError("Cannot sandbox the Synthesis pipeline: MO2_PATH must be configured.")
        return SandboxPromotionFlow(
            sandbox=ProfileSandbox(mo2_root=mo2_path, profile=profile_name),
            hitl_guard=hitl_guard,
        )

    return provide


def build_synthesis_service_factory(
    *,
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    path_resolver: PathResolutionService,
    event_bus: CoreEventBus,
    pipeline_config_path: pathlib.Path,
) -> Callable[[pathlib.Path, OperationJournal], SynthesisPipelineService]:
    """Cierra infraestructura compartida y crea un servicio por sandbox."""

    def create(
        output_path: pathlib.Path,
        journal: OperationJournal,
    ) -> SynthesisPipelineService:
        from sky_claw.local.tools.synthesis_service import SynthesisPipelineService

        return SynthesisPipelineService(
            lock_manager=lock_manager,
            snapshot_manager=snapshot_manager,
            journal=journal,
            path_resolver=path_resolver,
            event_bus=event_bus,
            pipeline_config_path=pipeline_config_path,
            output_path=output_path,
        )

    return create


def build_wrye_bash_pipeline(
    *,
    service: WryeBashPipelineService,
    default_profile: str,
) -> WryeBashPipeline:
    """Adapta servicio+perfil al callable estrecho que consume la estrategia."""

    async def execute(
        profile: str | None = None,
        validate_limit: bool = True,
    ) -> dict[str, Any]:
        return await service.execute_pipeline(
            profile=profile or default_profile,
            validate_limit=validate_limit,
        )

    return execute


__all__ = [
    "OrchestrationDispatcherDependencies",
    "WryeBashPipeline",
    "build_preview_chain_service_provider",
    "build_synthesis_flow_provider",
    "build_synthesis_service_factory",
    "build_wrye_bash_pipeline",
]
