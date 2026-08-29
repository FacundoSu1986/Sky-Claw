"""Helpers compartidos para construir el dispatcher sin un service locator."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

from sky_claw.app.orchestrator.dispatcher_dependencies import (
    OrchestrationDispatcherDependencies,
)


def crear_dependencias_dispatcher(
    *,
    scraper: Any | None = None,
    loot_service: Any | None = None,
    synthesis_flow_provider: Any | None = None,
    synthesis_service_factory: Any | None = None,
    real_journal_provider: Any | None = None,
    xedit_service: Any | None = None,
    dyndolod_service: Any | None = None,
    pandora_service: Any | None = None,
    grass_cache_service: Any | None = None,
    scan_asset_conflicts: Any | None = None,
    scan_asset_conflicts_json: Any | None = None,
    wrye_bash_pipeline: Any | None = None,
    plugin_limit_guard: Any | None = None,
    default_profile_getter: Any | None = None,
    preview_service_provider: Any | None = None,
) -> OrchestrationDispatcherDependencies:
    """Crea dependencias explícitas con dobles inocuos y overrides puntuales."""
    return OrchestrationDispatcherDependencies(
        scraper=scraper if scraper is not None else MagicMock(),
        loot_service=loot_service if loot_service is not None else MagicMock(),
        synthesis_flow_provider=(synthesis_flow_provider if synthesis_flow_provider is not None else MagicMock()),
        synthesis_service_factory=(synthesis_service_factory if synthesis_service_factory is not None else MagicMock()),
        real_journal_provider=(real_journal_provider if real_journal_provider is not None else MagicMock()),
        xedit_service=xedit_service if xedit_service is not None else MagicMock(),
        dyndolod_service=dyndolod_service if dyndolod_service is not None else MagicMock(),
        pandora_service=pandora_service if pandora_service is not None else MagicMock(),
        grass_cache_service=(grass_cache_service if grass_cache_service is not None else MagicMock()),
        scan_asset_conflicts=(scan_asset_conflicts if scan_asset_conflicts is not None else MagicMock()),
        scan_asset_conflicts_json=(scan_asset_conflicts_json if scan_asset_conflicts_json is not None else MagicMock()),
        wrye_bash_pipeline=(wrye_bash_pipeline if wrye_bash_pipeline is not None else AsyncMock()),
        plugin_limit_guard=(plugin_limit_guard if plugin_limit_guard is not None else AsyncMock()),
        default_profile_getter=(
            default_profile_getter if default_profile_getter is not None else lambda: "TestProfile"
        ),
        preview_service_provider=(preview_service_provider if preview_service_provider is not None else MagicMock()),
    )


def crear_dependencias_desde_doble(supervisor: Any) -> OrchestrationDispatcherDependencies:
    """Adapta dobles históricos; producción no depende de esta conveniencia."""

    async def ejecutar_wrye_bash(
        profile: str | None = None,
        validate_limit: bool = True,
    ) -> dict[str, Any]:
        pipeline = getattr(supervisor, "execute_wrye_bash_pipeline", AsyncMock())
        return await pipeline(
            profile=profile,
            validate_limit=validate_limit,
        )

    async def validar_limite(profile: str) -> dict[str, Any]:
        guard = getattr(supervisor, "_run_plugin_limit_guard", AsyncMock())
        return await guard(profile)

    return crear_dependencias_dispatcher(
        scraper=supervisor.scraper,
        loot_service=supervisor._loot_service,
        synthesis_flow_provider=getattr(supervisor, "_synthesis_flow_provider", MagicMock()),
        synthesis_service_factory=getattr(supervisor, "_synthesis_service_factory", MagicMock()),
        real_journal_provider=lambda: supervisor.journal,
        xedit_service=supervisor._xedit_service,
        dyndolod_service=supervisor._dyndolod_service,
        pandora_service=supervisor._pandora_service,
        grass_cache_service=supervisor._grass_cache_service,
        scan_asset_conflicts=lambda: getattr(supervisor, "scan_asset_conflicts", MagicMock())(),
        scan_asset_conflicts_json=lambda: getattr(
            supervisor,
            "scan_asset_conflicts_json",
            MagicMock(),
        )(),
        wrye_bash_pipeline=ejecutar_wrye_bash,
        plugin_limit_guard=validar_limite,
        default_profile_getter=lambda: supervisor.profile_name,
        preview_service_provider=getattr(
            supervisor,
            "_preview_chain_service_provider",
            MagicMock(),
        ),
    )
