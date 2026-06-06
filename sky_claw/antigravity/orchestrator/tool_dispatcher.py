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
from sky_claw.antigravity.orchestrator.tool_strategies.generate_bashed_patch import (
    GenerateBashedPatchStrategy,
)
from sky_claw.antigravity.orchestrator.tool_strategies.generate_lods import GenerateLodsStrategy
from sky_claw.antigravity.orchestrator.tool_strategies.middleware import (
    DictResultGuardMiddleware,
    ErrorWrappingMiddleware,
    HitlGateMiddleware,
)
from sky_claw.antigravity.orchestrator.tool_strategies.preview_chain import (
    PreviewChainStrategy,
)
from sky_claw.antigravity.orchestrator.tool_strategies.query_mod_metadata import (
    QueryModMetadataStrategy,
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

    def __init__(self) -> None:
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

        middlewares = self._middleware[tool_name]

        async def innermost() -> dict[str, Any]:
            return await strategy.execute(payload_dict)

        current: NextCall = innermost
        for mw in reversed(middlewares):
            current = _make_thunk(mw, strategy, payload_dict, current)

        return await current()

    def registered_tools(self) -> list[str]:
        """Snapshot of currently registered tool names (for introspection / tests)."""
        return list(self._strategies.keys())


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

    loot_runner = LOOTRunner(
        LOOTConfig(loot_exe=pathlib.Path("loot.exe"), game_path=game_path),
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


def build_orchestration_dispatcher(
    supervisor: SupervisorAgent,
    *,
    hitl_gate: HitlGateMiddleware | None = None,
) -> OrchestrationToolDispatcher:
    """Wire all migrated tool strategies onto a fresh dispatcher.

    Called from SupervisorAgent.__init__ AFTER all collaborators
    (services, agents, daemons) are constructed. Strategies are migrated
    one-by-one in the Strangler Fig refactor; the legacy match/case in
    SupervisorAgent.dispatch_tool delegates to this dispatcher only for
    tool names that have been moved over.

    FASE 1.5.1: Destructive tools are wrapped with HitlGateMiddleware
    when a ``hitl_gate`` instance is provided. Without it, the gate
    operates in fail-open mode (logs a warning, proceeds without approval).
    """
    dispatcher = OrchestrationToolDispatcher()

    # FASE 1.5.1: Shared HITL gate for destructive tools.
    # When hitl_gate is None, a default fail-open gate is created that
    # logs warnings but proceeds — safe for migration / testing.
    gate = hitl_gate or HitlGateMiddleware()

    dispatcher.register(QueryModMetadataStrategy(scraper=supervisor.scraper))

    # FASE 1.5.1: execute_loot_sorting is destructive → HITL gate
    dispatcher.register(
        ExecuteLootSortingStrategy(
            service=supervisor._loot_service,
            interface=supervisor.interface,
        ),
        middleware=[gate],
    )

    dispatcher.register(
        ExecuteSynthesisPipelineStrategy(service=supervisor._synthesis_service),
        middleware=[
            ErrorWrappingMiddleware("SynthesisPipelineExecutionFailed"),
            DictResultGuardMiddleware("InvalidSynthesisPipelineResult"),
        ],
    )

    # FASE 1.5.1: resolve_conflict_patch is destructive → HITL gate outermost
    dispatcher.register(
        ResolveConflictWithPatchStrategy(service=supervisor._xedit_service),
        middleware=[
            gate,
            ErrorWrappingMiddleware("XEditPatchExecutionFailed"),
            DictResultGuardMiddleware("InvalidXEditPatchResult"),
        ],
    )

    # FASE 1.5.1: generate_lods is destructive → HITL gate
    dispatcher.register(
        GenerateLodsStrategy(service=supervisor._dyndolod_service),
        middleware=[gate],
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
