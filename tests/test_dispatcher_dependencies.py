"""Anclas de la frontera explícita de dependencias del dispatcher."""

from __future__ import annotations

import ast
import dataclasses
import inspect
import pathlib
from unittest.mock import MagicMock, patch

from sky_claw.app.orchestrator.dispatcher_dependencies import (
    OrchestrationDispatcherDependencies,
    build_preview_chain_service_provider,
    build_synthesis_flow_provider,
    build_synthesis_service_factory,
    build_wrye_bash_pipeline,
)
from sky_claw.app.orchestrator.tool_dispatcher import build_orchestration_dispatcher
from tests._orchestration_dispatcher_dependencies import crear_dependencias_dispatcher

_CAMPOS_DEPENDENCIAS = [
    "scraper",
    "loot_service",
    "synthesis_flow_provider",
    "synthesis_service_factory",
    "real_journal_provider",
    "xedit_service",
    "dyndolod_service",
    "pandora_service",
    "grass_cache_service",
    "scan_asset_conflicts",
    "scan_asset_conflicts_json",
    "wrye_bash_pipeline",
    "plugin_limit_guard",
    "default_profile_getter",
    "preview_service_provider",
]

_TOOLS = [
    "query_mod_metadata",
    "execute_loot_sorting",
    "execute_synthesis_pipeline",
    "resolve_conflict_with_patch",
    "generate_lods",
    "generate_animations",
    "quick_auto_clean",
    "analyze_grass_prerequisites",
    "generate_grass_cache",
    "scan_asset_conflicts",
    "scan_asset_conflicts_json",
    "generate_bashed_patch",
    "validate_plugin_limit",
    "preview_chain",
]


def test_dependencias_tienen_campos_inmutables_y_exhaustivos() -> None:
    assert [campo.name for campo in dataclasses.fields(OrchestrationDispatcherDependencies)] == _CAMPOS_DEPENDENCIAS
    assert OrchestrationDispatcherDependencies.__dataclass_params__.frozen is True
    assert OrchestrationDispatcherDependencies.__slots__ == tuple(_CAMPOS_DEPENDENCIAS)


def test_modulos_de_la_frontera_no_conocen_supervisor_y_exponen_la_firma_nueva() -> None:
    rutas = [
        pathlib.Path(inspect.getfile(build_orchestration_dispatcher)),
        pathlib.Path(inspect.getfile(OrchestrationDispatcherDependencies)),
    ]
    for ruta in rutas:
        fuente = ruta.read_text(encoding="utf-8")
        ast.parse(fuente)
        assert "SupervisorAgent" not in fuente
    assert list(inspect.signature(build_orchestration_dispatcher).parameters) == [
        "dependencies",
        "hitl_gate",
        "loop_guardrail",
        "idempotency",
    ]


def test_builder_registra_exactamente_las_catorce_tools() -> None:
    dispatcher = build_orchestration_dispatcher(crear_dependencias_dispatcher())
    assert dispatcher.registered_tools() == _TOOLS


def test_gate_hitl_envuelve_exactamente_las_siete_tools_destructivas() -> None:
    from sky_claw.app.orchestrator.tool_strategies.middleware import HitlGateMiddleware

    gate = HitlGateMiddleware(allow_unattended=True)
    dispatcher = build_orchestration_dispatcher(
        crear_dependencias_dispatcher(),
        hitl_gate=gate,
    )
    gated = {
        nombre for nombre, middleware in dispatcher._middleware.items() if any(item is gate for item in middleware)
    }
    assert gated == {
        "execute_loot_sorting",
        "resolve_conflict_with_patch",
        "generate_lods",
        "generate_animations",
        "quick_auto_clean",
        "generate_grass_cache",
        "generate_bashed_patch",
    }


def test_strategies_retienen_las_mismas_instancias_y_callables() -> None:
    deps = crear_dependencias_dispatcher()
    dispatcher = build_orchestration_dispatcher(deps)

    assert dispatcher._strategies["query_mod_metadata"].scraper is deps.scraper
    assert dispatcher._strategies["execute_loot_sorting"].service is deps.loot_service
    assert dispatcher._strategies["execute_synthesis_pipeline"]._flow_provider is deps.synthesis_flow_provider
    assert dispatcher._strategies["execute_synthesis_pipeline"]._service_factory is deps.synthesis_service_factory
    assert dispatcher._strategies["execute_synthesis_pipeline"]._real_journal_provider is deps.real_journal_provider
    assert dispatcher._strategies["resolve_conflict_with_patch"].service is deps.xedit_service
    assert dispatcher._strategies["generate_lods"].service is deps.dyndolod_service
    assert dispatcher._strategies["generate_animations"].service is deps.pandora_service
    assert dispatcher._strategies["quick_auto_clean"].service is deps.xedit_service
    assert dispatcher._strategies["analyze_grass_prerequisites"].service is deps.grass_cache_service
    assert dispatcher._strategies["generate_grass_cache"].service is deps.grass_cache_service
    assert dispatcher._strategies["scan_asset_conflicts"].scan_callable is deps.scan_asset_conflicts
    assert dispatcher._strategies["scan_asset_conflicts_json"].scan_json_callable is deps.scan_asset_conflicts_json
    assert dispatcher._strategies["generate_bashed_patch"].wrye_bash_pipeline is deps.wrye_bash_pipeline
    assert dispatcher._strategies["validate_plugin_limit"].plugin_limit_guard is deps.plugin_limit_guard
    assert dispatcher._strategies["preview_chain"]._service_provider is deps.preview_service_provider


def test_construir_dispatcher_no_resuelve_providers_ni_binarios() -> None:
    resolver = MagicMock()
    preview_provider = build_preview_chain_service_provider(
        path_resolver=resolver,
        path_validator=MagicMock(),
        lock_manager=MagicMock(),
        snapshot_manager=MagicMock(),
        journal=MagicMock(),
        event_bus=MagicMock(),
    )
    synthesis_flow_provider = build_synthesis_flow_provider(
        path_resolver=resolver,
        profile_name="Perfil",
        hitl_guard=MagicMock(),
    )
    deps = crear_dependencias_dispatcher(
        preview_service_provider=preview_provider,
        synthesis_flow_provider=synthesis_flow_provider,
    )

    build_orchestration_dispatcher(deps)

    resolver.get_skyrim_path.assert_not_called()
    resolver.get_xedit_path.assert_not_called()
    resolver.get_loot_exe.assert_not_called()
    resolver.get_mo2_path.assert_not_called()


def test_preview_lazy_conserva_wiring_y_fallback_de_loot() -> None:
    resolver = MagicMock()
    resolver.get_skyrim_path.return_value = pathlib.Path("/skyrim")
    resolver.get_xedit_path.return_value = pathlib.Path("/xedit.exe")
    resolver.get_loot_exe.return_value = None
    colaboradores = {
        "path_validator": MagicMock(),
        "lock_manager": MagicMock(),
        "snapshot_manager": MagicMock(),
        "journal": MagicMock(),
        "event_bus": MagicMock(),
    }
    provider = build_preview_chain_service_provider(path_resolver=resolver, **colaboradores)

    with (
        patch("sky_claw.local.loot.cli.LOOTConfig") as loot_config,
        patch("sky_claw.local.loot.cli.LOOTRunner") as loot_runner,
        patch("sky_claw.local.xedit.runner.XEditRunner") as xedit_runner,
        patch("sky_claw.local.xedit.conflict_analyzer.ConflictAnalyzer") as analyzer,
        patch("sky_claw.app.orchestrator.preview.chain_preview_service.ChainPreviewService") as chain,
    ):
        resultado = provider()

    assert resultado is chain.return_value
    assert loot_config.call_args.kwargs == {
        "loot_exe": pathlib.Path("loot.exe"),
        "game_path": pathlib.Path("/skyrim"),
    }
    loot_runner.assert_called_once_with(
        loot_config.return_value,
        path_validator=colaboradores["path_validator"],
    )
    chain.assert_called_once_with(
        lock_manager=colaboradores["lock_manager"],
        snapshot_manager=colaboradores["snapshot_manager"],
        journal=colaboradores["journal"],
        path_resolver=resolver,
        path_validator=colaboradores["path_validator"],
        event_bus=colaboradores["event_bus"],
        loot_runner=loot_runner.return_value,
        xedit_runner=xedit_runner.return_value,
        conflict_analyzer=analyzer.return_value,
    )


def test_synthesis_lazy_conserva_sandbox_y_factory() -> None:
    resolver = MagicMock()
    resolver.get_mo2_path.return_value = pathlib.Path("/mo2")
    lock_manager = MagicMock()
    snapshot_manager = MagicMock()
    journal = MagicMock()
    event_bus = MagicMock()
    pipeline_config_path = pathlib.Path("pipeline.json")
    hitl_guard = MagicMock()
    flow_provider = build_synthesis_flow_provider(
        path_resolver=resolver,
        profile_name="Perfil",
        hitl_guard=hitl_guard,
    )
    service_factory = build_synthesis_service_factory(
        lock_manager=lock_manager,
        snapshot_manager=snapshot_manager,
        path_resolver=resolver,
        event_bus=event_bus,
        pipeline_config_path=pipeline_config_path,
    )

    with (
        patch("sky_claw.local.mo2.profile_sandbox.ProfileSandbox") as sandbox,
        patch("sky_claw.app.orchestrator.sandbox_promotion.SandboxPromotionFlow") as flow,
        patch("sky_claw.local.tools.synthesis_service.SynthesisPipelineService") as service,
    ):
        flow_result = flow_provider()
        service_result = service_factory(pathlib.Path("/clone/overwrite"), journal)

    assert flow_result is flow.return_value
    sandbox.assert_called_once_with(mo2_root=pathlib.Path("/mo2"), profile="Perfil")
    flow.assert_called_once_with(sandbox=sandbox.return_value, hitl_guard=hitl_guard)
    assert service_result is service.return_value
    service.assert_called_once_with(
        lock_manager=lock_manager,
        snapshot_manager=snapshot_manager,
        journal=journal,
        path_resolver=resolver,
        event_bus=event_bus,
        pipeline_config_path=pipeline_config_path,
        output_path=pathlib.Path("/clone/overwrite"),
    )


async def test_wrye_bash_callable_acepta_cero_args_y_preserva_fallback() -> None:
    service = MagicMock()
    service.execute_pipeline = MagicMock()

    async def ejecutar(**kwargs: object) -> dict[str, object]:
        service.kwargs = kwargs
        return {"success": True}

    service.execute_pipeline.side_effect = ejecutar
    pipeline = build_wrye_bash_pipeline(
        service=service,
        default_profile="PerfilSesion",
    )

    assert await pipeline() == {"success": True}
    assert service.kwargs == {"profile": "PerfilSesion", "validate_limit": True}
    await pipeline(profile="", validate_limit=False)
    assert service.kwargs == {"profile": "PerfilSesion", "validate_limit": False}
    await pipeline(profile="Explicito", validate_limit=False)
    assert service.kwargs == {"profile": "Explicito", "validate_limit": False}
