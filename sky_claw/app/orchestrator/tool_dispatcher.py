"""OrchestrationToolDispatcher — registry-based replacement for the
legacy monolithic ``dispatch_tool`` match/case (Strangler Fig refactor).

This dispatcher serves the orchestration layer (LangGraph callbacks,
internal services) and returns dicts. It is INTENTIONALLY separate from
sky_claw.app.agent.tools.AsyncToolRegistry, which serves the LLM-facing
agent layer (returns JSON strings, integrates with LLMRouter).

Two registries, one clear seam each.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from sky_claw.app.orchestrator.tool_state_machine import ToolStateMachine
from sky_claw.app.orchestrator.tool_strategies.analyze_grass_prerequisites import (
    AnalyzeGrassPrerequisitesStrategy,
)
from sky_claw.app.orchestrator.tool_strategies.base import (
    DuplicateToolError,
    NextCall,
    ToolMiddleware,
    ToolNotFoundError,
    ToolStrategy,
)
from sky_claw.app.orchestrator.tool_strategies.execute_loot_sorting import (
    ExecuteLootSortingStrategy,
)
from sky_claw.app.orchestrator.tool_strategies.execute_synthesis import (
    ExecuteSynthesisPipelineStrategy,
)
from sky_claw.app.orchestrator.tool_strategies.generate_animations import (
    GenerateAnimationsStrategy,
)
from sky_claw.app.orchestrator.tool_strategies.generate_bashed_patch import (
    GenerateBashedPatchStrategy,
)
from sky_claw.app.orchestrator.tool_strategies.generate_grass_cache import (
    GenerateGrassCacheStrategy,
)
from sky_claw.app.orchestrator.tool_strategies.generate_lods import GenerateLodsStrategy
from sky_claw.app.orchestrator.tool_strategies.middleware import (
    DictResultGuardMiddleware,
    ErrorWrappingMiddleware,
    HitlGateMiddleware,
    IdempotencyMiddleware,
    LoopGuardrailMiddleware,
)
from sky_claw.app.orchestrator.tool_strategies.preview_chain import (
    PreviewChainStrategy,
)
from sky_claw.app.orchestrator.tool_strategies.query_mod_metadata import (
    QueryModMetadataStrategy,
)
from sky_claw.app.orchestrator.tool_strategies.quick_auto_clean import (
    QuickAutoCleanStrategy,
)
from sky_claw.app.orchestrator.tool_strategies.resolve_conflict_patch import (
    ResolveConflictWithPatchStrategy,
)
from sky_claw.app.orchestrator.tool_strategies.scan_asset_conflicts import (
    ScanAssetConflictsJsonStrategy,
    ScanAssetConflictsStrategy,
)
from sky_claw.app.orchestrator.tool_strategies.validate_plugin_limit import (
    ValidatePluginLimitStrategy,
)

if TYPE_CHECKING:
    from sky_claw.app.orchestrator.dispatcher_dependencies import (
        OrchestrationDispatcherDependencies,
    )

logger = logging.getLogger(__name__)


class OrchestrationToolDispatcher:
    """Registry of ToolStrategy + per-strategy middleware chains.

    Caller-facing contract — `dispatch(tool_name, payload_dict)`:
      - On match: run the middleware chain (outer → inner → strategy.execute).
      - On miss: return {"status": "error", "reason": "ToolNotFound"} (legacy
        contract preserved verbatim from the former inline dispatcher).
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
        acá. La raíz de composición lo invoca en su shutdown ANTES de cerrar journal y
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


def build_orchestration_dispatcher(
    dependencies: OrchestrationDispatcherDependencies,
    *,
    hitl_gate: HitlGateMiddleware | None = None,
    loop_guardrail: LoopGuardrailMiddleware | None = None,
    idempotency: IdempotencyMiddleware | None = None,
) -> OrchestrationToolDispatcher:
    """Wire all migrated tool strategies onto a fresh dispatcher.

    Called by the composition root AFTER all collaborators (services, agents,
    daemons) are constructed. The factory receives only the capabilities each
    strategy consumes.

    FASE 1.5.1: Destructive tools are wrapped with HitlGateMiddleware.
    Without a ``hitl_gate`` instance the default gate is FAIL-CLOSED:
    destructive tools are denied (``HITLGateUnavailable``) until a
    HITLGuard-backed gate is injected.
    """
    # F1a (informe #319): el cortacircuitos cognitivo corre como middleware
    # global del dispatcher — el ÚNICO camino real de ejecución de tools. Antes
    # vivía en los callbacks del StateGraph, que nada ejecutaba en producción.
    # La raíz de composición inyecta su instancia para rearmarla ante intervención
    # humana (reset_loop_guardrail); None crea una propia (tests/standalone).
    guardrail = loop_guardrail if loop_guardrail is not None else LoopGuardrailMiddleware()
    # F4 (auditoría 2026-07-18): IdempotencyMiddleware (FASE 1.5.4) existía y
    # estaba testeado en aislamiento, pero nunca se registraba acá — la misma
    # laguna que F1a documentó para el loop guardrail. Mismo patrón de
    # inyección: el caller retiene su instancia, None arma una propia.
    dedupe = idempotency if idempotency is not None else IdempotencyMiddleware(ToolStateMachine())
    dispatcher = OrchestrationToolDispatcher(global_middleware=[guardrail, dedupe])

    # FASE 1.5.1: Shared HITL gate for destructive tools.
    # When hitl_gate is None, the default gate denies destructive tools
    # (fail-closed). Tests opt out explicitly via allow_unattended=True.
    gate = hitl_gate or HitlGateMiddleware()

    dispatcher.register(QueryModMetadataStrategy(scraper=dependencies.scraper))

    # FASE 1.5.1: execute_loot_sorting is destructive → HITL gate
    dispatcher.register(
        ExecuteLootSortingStrategy(
            service=dependencies.loot_service,
            # El Ritual de la GUI despacha con payload vacío: sin esto el perfil salía
            # del default de pydantic ("Default") y la GUI ordenaba otro load order
            # que el agente LLM, sobre el mismo plugins.txt.
            default_profile_getter=dependencies.default_profile_getter,
        ),
        middleware=[
            ErrorWrappingMiddleware("LootExecutionFailed"),
            DictResultGuardMiddleware("InvalidLootResult"),
            gate,
        ],
    )

    # T-27b·2: Synthesis corre SIEMPRE en sandbox — el flow resuelve
    # promote/discard vía HITL post-run sobre el diff real (ADR 0005). Sin
    # gate pre-ejecución: sería double-gating (precedente PR #173). Los providers
    # lazy hacen que registrar no exija MO2 presente.
    dispatcher.register(
        ExecuteSynthesisPipelineStrategy(
            flow_provider=dependencies.synthesis_flow_provider,
            service_factory=dependencies.synthesis_service_factory,
            real_journal_provider=dependencies.real_journal_provider,
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
        ResolveConflictWithPatchStrategy(service=dependencies.xedit_service),
        middleware=[
            ErrorWrappingMiddleware("XEditPatchExecutionFailed"),
            DictResultGuardMiddleware("InvalidXEditPatchResult"),
            gate,
        ],
    )

    # FASE 1.5.1: generate_lods is destructive → HITL gate
    dispatcher.register(
        GenerateLodsStrategy(service=dependencies.dyndolod_service),
        middleware=[gate],
    )

    # Follow-up A: generate_animations (Pandora) is destructive → HITL gate
    dispatcher.register(
        GenerateAnimationsStrategy(service=dependencies.pandora_service),
        middleware=[gate],
    )

    # Follow-up B: quick_auto_clean (SSEEdit QuickAutoClean) is destructive → HITL gate
    dispatcher.register(
        QuickAutoCleanStrategy(service=dependencies.xedit_service),
        middleware=[gate],
    )

    # PR-5 grass cache: la Fase A es read-only (sin gate, con wrapping como
    # preview_chain); el ritual completo es destructivo → gate innermost.
    dispatcher.register(
        AnalyzeGrassPrerequisitesStrategy(service=dependencies.grass_cache_service),
        middleware=[
            ErrorWrappingMiddleware("GrassAnalysisFailed"),
            DictResultGuardMiddleware("InvalidGrassAnalysisResult"),
        ],
    )
    dispatcher.register(
        GenerateGrassCacheStrategy(service=dependencies.grass_cache_service),
        middleware=[
            ErrorWrappingMiddleware("GrassCacheExecutionFailed"),
            DictResultGuardMiddleware("InvalidGrassCacheResult"),
            gate,
        ],
    )

    # Los callables explícitos conservan la resolución lazy del detector de
    # assets sin exponer el objeto que compone toda la aplicación.
    dispatcher.register(
        ScanAssetConflictsStrategy(
            scan_callable=dependencies.scan_asset_conflicts,
        ),
    )

    dispatcher.register(
        ScanAssetConflictsJsonStrategy(
            scan_json_callable=dependencies.scan_asset_conflicts_json,
        ),
    )

    # FASE 1.5.1: generate_bashed_patch is destructive → HITL gate
    dispatcher.register(
        GenerateBashedPatchStrategy(
            wrye_bash_pipeline=dependencies.wrye_bash_pipeline,
        ),
        middleware=[gate],
    )

    dispatcher.register(
        ValidatePluginLimitStrategy(
            plugin_limit_guard=dependencies.plugin_limit_guard,
            default_profile_getter=dependencies.default_profile_getter,
        ),
    )

    # preview_chain is READ-ONLY (reverts everything) → no HITL gate. The
    # ChainPreviewService is built lazily so registration never needs binaries.
    dispatcher.register(
        PreviewChainStrategy(
            service_provider=dependencies.preview_service_provider,
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
