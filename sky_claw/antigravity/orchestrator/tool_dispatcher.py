"""OrchestrationToolDispatcher — registry-based replacement for the
legacy SupervisorAgent.dispatch_tool match/case (Strangler Fig refactor).

This dispatcher serves the orchestration layer (LangGraph callbacks,
internal services) and returns dicts. It is INTENTIONALLY separate from
sky_claw.antigravity.agent.tools.AsyncToolRegistry, which serves the LLM-facing
agent layer (returns JSON strings, integrates with LLMRouter).

Two registries, one clear seam each.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from sky_claw.antigravity.orchestrator.tool_state_machine import ToolStateMachine
from sky_claw.antigravity.orchestrator.tool_strategies.analyze_grass_prerequisites import (
    AnalyzeGrassPrerequisitesStrategy,
)
from sky_claw.antigravity.orchestrator.tool_strategies.base import (
    DuplicateToolError,
    NextCall,
    ToolMiddleware,
    ToolNotFoundError,
    ToolStrategy,
)
from sky_claw.antigravity.orchestrator.tool_strategies.execute_loot_sorting import (
    ExecuteLootSortingStrategy,
)
from sky_claw.antigravity.orchestrator.tool_strategies.execute_synthesis import (
    ExecuteSynthesisPipelineStrategy,
)
from sky_claw.antigravity.orchestrator.tool_strategies.generate_animations import (
    GenerateAnimationsStrategy,
)
from sky_claw.antigravity.orchestrator.tool_strategies.generate_bashed_patch import (
    GenerateBashedPatchStrategy,
)
from sky_claw.antigravity.orchestrator.tool_strategies.generate_grass_cache import (
    GenerateGrassCacheStrategy,
)
from sky_claw.antigravity.orchestrator.tool_strategies.generate_lods import GenerateLodsStrategy
from sky_claw.antigravity.orchestrator.tool_strategies.middleware import (
    DictResultGuardMiddleware,
    ErrorWrappingMiddleware,
    HitlGateMiddleware,
    IdempotencyMiddleware,
    LoopGuardrailMiddleware,
)
from sky_claw.antigravity.orchestrator.tool_strategies.preview_chain import (
    PreviewChainStrategy,
)
from sky_claw.antigravity.orchestrator.tool_strategies.query_mod_metadata import (
    QueryModMetadataStrategy,
)
from sky_claw.antigravity.orchestrator.tool_strategies.quick_auto_clean import (
    QuickAutoCleanStrategy,
)
from sky_claw.antigravity.orchestrator.tool_strategies.resolve_conflict_patch import (
    ResolveConflictWithPatchStrategy,
)
from sky_claw.antigravity.orchestrator.tool_strategies.scan_asset_conflicts import (
    ScanAssetConflictsJsonStrategy,
    ScanAssetConflictsStrategy,
)
from sky_claw.antigravity.orchestrator.tool_strategies.validate_plugin_limit import (
    ValidatePluginLimitStrategy,
)

if TYPE_CHECKING:
    from sky_claw.antigravity.orchestrator.preview.chain_preview_service import ChainPreviewService
    from sky_claw.antigravity.orchestrator.supervisor import SupervisorAgent

logger = logging.getLogger(__name__)


class OrchestrationToolDispatcher:
    """Registry of ToolStrategy + per-strategy middleware chains.

    Caller-facing contract — `dispatch(tool_name, payload_dict)`:
      - On match: run the middleware chain (outer → inner → strategy.execute).
      - On miss: return {"status": "error", "reason": "ToolNotFound"} (legacy
        contract preserved verbatim from supervisor.py:345-347).
    """

    def __init__(self, *, global_middleware: list[ToolMiddleware] | None = None) -> None:
        # F1a: middleware GLOBAL aplicado OUTERMOST a todas las tools (antes de
        # la cadena por-tool). Hoy lo usa el LoopGuardrailMiddleware: el
        # cortacircuitos cognitivo debe cubrir también a las tools registradas
        # sin middleware propio (un loop de queries sigue siendo un loop).
        self._global_middleware: list[ToolMiddleware] = list(global_middleware) if global_middleware else []
        self._strategies: dict[str, ToolStrategy] = {}
        self._middleware: dict[str, list[ToolMiddleware]] = {}

    def register(
        self,
        strategy: ToolStrategy,
        *,
        middleware: list[ToolMiddleware] | None = None,
    ) -> None:
        """Add a strategy keyed by `strategy.name`.

        Middleware list is applied OUTER-FIRST: middleware[0] wraps middleware[1]
        which wraps ... which wraps strategy.execute. Each middleware decides
        whether to invoke `next_call` (advance) or short-circuit.
        """
        if strategy.name in self._strategies:
            raise DuplicateToolError(f"Tool '{strategy.name}' already registered.")
        self._strategies[strategy.name] = strategy
        self._middleware[strategy.name] = list(middleware) if middleware else []

    async def dispatch(
        self,
        tool_name: str,
        payload_dict: dict[str, Any],
    ) -> dict[str, Any]:
        strategy = self._strategies.get(tool_name)
        if strategy is None:
            logger.error(f"RCA: LLM alucinó la herramienta '{tool_name}'.")
            return {"status": "error", "reason": "ToolNotFound"}

        middlewares = [*self._global_middleware, *self._middleware[tool_name]]

        async def innermost() -> dict[str, Any]:
            return await strategy.execute(payload_dict)

        current: NextCall = innermost
        for mw in reversed(middlewares):
            current = _make_thunk(mw, strategy, payload_dict, current)

        return await current()

    def registered_tools(self) -> list[str]:
        """Snapshot of currently registered tool names (for introspection / tests)."""
        return list(self._strategies.keys())

    async def drain(self) -> None:
        """Espera el trabajo en background de las strategies (review Codex #322).

        Duck-typed: toda strategy que exponga ``drain_pendientes()`` (p. ej.
        las resoluciones de journal post-cancelación de Synthesis) es esperada
        acá. El supervisor lo invoca en su shutdown ANTES de cerrar journal y
        DB, para que ningún cleanup en vuelo corra contra recursos cerrados.
        """
        for strategy in self._strategies.values():
            drain_pendientes = getattr(strategy, "drain_pendientes", None)
            if drain_pendientes is None:
                continue
            try:
                await drain_pendientes()
            except Exception:
                logger.warning("drain de la strategy '%s' falló (no bloquea el shutdown)", strategy.name, exc_info=True)


def _make_thunk(
    middleware: ToolMiddleware,
    strategy: ToolStrategy,
    payload_dict: dict[str, Any],
    next_call: NextCall,
) -> NextCall:
    """Bind middleware + its successor into a zero-arg awaitable.

    A separate function (not a closure inside the loop) prevents the classic
    late-binding bug where every iteration would capture the final `mw`.
    """

    async def thunk() -> dict[str, Any]:
        return await middleware(strategy, payload_dict, next_call)

    return thunk


def _build_chain_preview_service(supervisor: SupervisorAgent) -> ChainPreviewService:
    """Lazily build a :class:`ChainPreviewService` from the supervisor's collaborators.

    Invoked only when ``preview_chain`` is dispatched (not at registration time),
    so wiring the dispatcher never requires the LOOT/xEdit binaries to be present.
    Raises ``RuntimeError`` when the tool paths are not configured; the strategy's
    ErrorWrappingMiddleware converts that into a serializable error dict.
    """
    # Local imports keep dispatcher construction cheap and avoid import cycles.
    import pathlib

    from sky_claw.antigravity.orchestrator.preview.chain_preview_service import ChainPreviewService
    from sky_claw.local.loot.cli import LOOTConfig, LOOTRunner
    from sky_claw.local.xedit.conflict_analyzer import ConflictAnalyzer
    from sky_claw.local.xedit.runner import XEditRunner

    path_resolver = supervisor._path_resolver
    game_path = path_resolver.get_skyrim_path()
    xedit_path = path_resolver.get_xedit_path()
    if game_path is None or xedit_path is None:
        raise RuntimeError("Cannot preview the chain: SKYRIM_PATH and XEDIT_PATH must be configured.")

    loot_exe = path_resolver.get_loot_exe() or pathlib.Path("loot.exe")
    loot_runner = LOOTRunner(
        LOOTConfig(loot_exe=loot_exe, game_path=game_path),
        path_validator=supervisor._path_validator,
    )
    xedit_runner = XEditRunner(
        xedit_path=xedit_path,
        game_path=game_path,
        output_dir=pathlib.Path(".skyclaw_backups/patches"),
    )

    return ChainPreviewService(
        lock_manager=supervisor._lock_manager,
        snapshot_manager=supervisor.snapshot_manager,
        journal=supervisor.journal,
        path_resolver=path_resolver,
        path_validator=supervisor._path_validator,
        event_bus=supervisor._event_bus,
        loot_runner=loot_runner,
        xedit_runner=xedit_runner,
        conflict_analyzer=ConflictAnalyzer(),
    )


def _build_synthesis_sandbox_flow(supervisor: SupervisorAgent) -> Any:
    """Lazily build the :class:`SandboxPromotionFlow` for the Synthesis Ritual.

    Invoked only when ``execute_synthesis_pipeline`` is dispatched (not at
    registration time), so wiring the dispatcher never requires MO2 to be
    present. Raises ``RuntimeError`` when MO2 is not configured; the strategy's
    ErrorWrappingMiddleware converts that into a serializable error dict.
    """
    # Local imports keep dispatcher construction cheap and avoid import cycles.
    from sky_claw.antigravity.orchestrator.sandbox_promotion import SandboxPromotionFlow
    from sky_claw.local.mo2.profile_sandbox import ProfileSandbox

    mo2_path = supervisor._path_resolver.get_mo2_path()
    if mo2_path is None:
        raise RuntimeError("Cannot sandbox the Synthesis pipeline: MO2_PATH must be configured.")

    return SandboxPromotionFlow(
        sandbox=ProfileSandbox(mo2_root=mo2_path, profile=supervisor.profile_name),
        hitl_guard=supervisor._hitl_guard,
    )


def _build_sandboxed_synthesis_service(supervisor: SupervisorAgent, output_path: Any, journal: Any) -> Any:
    """Fresh :class:`SynthesisPipelineService` writing into the sandbox clone.

    Un servicio por run sandboxeado (patrón documentado en el propio servicio,
    T-27b): mismas dependencias que ``supervisor._synthesis_service`` pero con
    ``output_path`` apuntando a ``SandboxClone.overwrite_copy``.
    """
    from sky_claw.local.tools.synthesis_service import SynthesisPipelineService

    return SynthesisPipelineService(
        lock_manager=supervisor._lock_manager,
        snapshot_manager=supervisor.snapshot_manager,
        journal=journal,
        path_resolver=supervisor._path_resolver,
        event_bus=supervisor._event_bus,
        pipeline_config_path=supervisor._synthesis_service._pipeline_config_path,
        output_path=output_path,
    )


def build_orchestration_dispatcher(
    supervisor: SupervisorAgent,
    *,
    hitl_gate: HitlGateMiddleware | None = None,
    loop_guardrail: LoopGuardrailMiddleware | None = None,
    idempotency: IdempotencyMiddleware | None = None,
) -> OrchestrationToolDispatcher:
    """Wire all migrated tool strategies onto a fresh dispatcher.

    Called from SupervisorAgent.__init__ AFTER all collaborators
    (services, agents, daemons) are constructed. Strategies are migrated
    one-by-one in the Strangler Fig refactor; the legacy match/case in
    SupervisorAgent.dispatch_tool delegates to this dispatcher only for
    tool names that have been moved over.

    FASE 1.5.1: Destructive tools are wrapped with HitlGateMiddleware.
    Without a ``hitl_gate`` instance the default gate is FAIL-CLOSED:
    destructive tools are denied (``HITLGateUnavailable``) until a
    HITLGuard-backed gate is injected.
    """
    # F1a (informe #319): el cortacircuitos cognitivo corre como middleware
    # global del dispatcher — el ÚNICO camino real de ejecución de tools. Antes
    # vivía en los callbacks del StateGraph, que nada ejecutaba en producción.
    # El supervisor inyecta su instancia para poder rearmarla ante intervención
    # humana (reset_loop_guardrail); None crea una propia (tests/standalone).
    guardrail = loop_guardrail if loop_guardrail is not None else LoopGuardrailMiddleware()
    # F4 (auditoría 2026-07-18): IdempotencyMiddleware (FASE 1.5.4) existía y
    # estaba testeado en aislamiento, pero nunca se registraba acá — la misma
    # laguna que F1a documentó para el loop guardrail. Mismo patrón de
    # inyección: el supervisor retiene su instancia, None arma una propia.
    dedupe = idempotency if idempotency is not None else IdempotencyMiddleware(ToolStateMachine())
    dispatcher = OrchestrationToolDispatcher(global_middleware=[guardrail, dedupe])

    # FASE 1.5.1: Shared HITL gate for destructive tools.
    # When hitl_gate is None, the default gate denies destructive tools
    # (fail-closed). Tests opt out explicitly via allow_unattended=True.
    gate = hitl_gate or HitlGateMiddleware()

    dispatcher.register(QueryModMetadataStrategy(scraper=supervisor.scraper))

    # FASE 1.5.1: execute_loot_sorting is destructive → HITL gate
    dispatcher.register(
        ExecuteLootSortingStrategy(
            service=supervisor._loot_service,
        ),
        middleware=[gate],
    )

    # T-27b·2: Synthesis corre SIEMPRE en sandbox — el flow resuelve
    # promote/discard vía HITL post-run sobre el diff real (ADR 0005). Sin
    # gate pre-ejecución: sería double-gating (precedente PR #173). Lambdas
    # lazy: registrar no exige MO2 presente y los tests pueden monkey-patchear
    # los builders a nivel módulo.
    dispatcher.register(
        ExecuteSynthesisPipelineStrategy(
            flow_provider=lambda: _build_synthesis_sandbox_flow(supervisor),
            service_factory=lambda output_path, journal: _build_sandboxed_synthesis_service(
                supervisor, output_path, journal
            ),
            real_journal_provider=lambda: supervisor.journal,
        ),
        middleware=[
            ErrorWrappingMiddleware("SynthesisPipelineExecutionFailed"),
            DictResultGuardMiddleware("InvalidSynthesisPipelineResult"),
        ],
    )

    # FASE 1.5.1: resolve_conflict_with_patch is destructive → HITL gate runs
    # INNERMOST so its pre-prompt validation failures are converted to the
    # legacy {"status": "error", ...} dict by ErrorWrapping/DictResultGuard.
    dispatcher.register(
        ResolveConflictWithPatchStrategy(service=supervisor._xedit_service),
        middleware=[
            ErrorWrappingMiddleware("XEditPatchExecutionFailed"),
            DictResultGuardMiddleware("InvalidXEditPatchResult"),
            gate,
        ],
    )

    # FASE 1.5.1: generate_lods is destructive → HITL gate
    dispatcher.register(
        GenerateLodsStrategy(service=supervisor._dyndolod_service),
        middleware=[gate],
    )

    # Follow-up A: generate_animations (Pandora) is destructive → HITL gate
    dispatcher.register(
        GenerateAnimationsStrategy(service=supervisor._pandora_service),
        middleware=[gate],
    )

    # Follow-up B: quick_auto_clean (SSEEdit QuickAutoClean) is destructive → HITL gate
    dispatcher.register(
        QuickAutoCleanStrategy(service=supervisor._xedit_service),
        middleware=[gate],
    )

    # PR-5 grass cache: la Fase A es read-only (sin gate, con wrapping como
    # preview_chain); el ritual completo es destructivo → gate innermost.
    dispatcher.register(
        AnalyzeGrassPrerequisitesStrategy(service=supervisor._grass_cache_service),
        middleware=[
            ErrorWrappingMiddleware("GrassAnalysisFailed"),
            DictResultGuardMiddleware("InvalidGrassAnalysisResult"),
        ],
    )
    dispatcher.register(
        GenerateGrassCacheStrategy(service=supervisor._grass_cache_service),
        middleware=[
            ErrorWrappingMiddleware("GrassCacheExecutionFailed"),
            DictResultGuardMiddleware("InvalidGrassCacheResult"),
            gate,
        ],
    )

    # Lambdas re-resolve attributes on each call so test fixtures can
    # monkey-patch the supervisor methods AFTER the dispatcher is wired
    # (and so the lazy `asset_detector` property keeps its semantics).
    dispatcher.register(
        ScanAssetConflictsStrategy(
            scan_callable=lambda: supervisor.scan_asset_conflicts(),
        ),
    )

    dispatcher.register(
        ScanAssetConflictsJsonStrategy(
            scan_json_callable=lambda: supervisor.scan_asset_conflicts_json(),
        ),
    )

    # FASE 1.5.1: generate_bashed_patch is destructive → HITL gate
    dispatcher.register(
        GenerateBashedPatchStrategy(
            wrye_bash_pipeline=lambda **kwargs: supervisor.execute_wrye_bash_pipeline(**kwargs),
        ),
        middleware=[gate],
    )

    dispatcher.register(
        ValidatePluginLimitStrategy(
            plugin_limit_guard=lambda profile: supervisor._run_plugin_limit_guard(profile),
            default_profile_getter=lambda: supervisor.profile_name,
        ),
    )

    # preview_chain is READ-ONLY (reverts everything) → no HITL gate. The
    # ChainPreviewService is built lazily so registration never needs binaries.
    dispatcher.register(
        PreviewChainStrategy(
            service_provider=lambda: _build_chain_preview_service(supervisor),
        ),
        middleware=[
            ErrorWrappingMiddleware("ChainPreviewFailed"),
            DictResultGuardMiddleware("InvalidChainPreviewResult"),
        ],
    )

    return dispatcher


__all__ = [
    "DuplicateToolError",
    "OrchestrationToolDispatcher",
    "ToolNotFoundError",
    "build_orchestration_dispatcher",
]
