"""xEdit (SSEEdit) headless wrapper for conflict detection and dynamic patching.

Phase 2 Extensions:
    - Dynamic Pascal script generation via ScriptGenerator
    - Headless execution with write flags (-IKnowWhatImDoing)
    - ScriptExecutionResult for detailed execution feedback
    - Integration with PatchOrchestrator via execute_patch()

Usage:
    from sky_claw.local.xedit import XEditRunner, ScriptGenerator, ScriptExecutionResult

    runner = XEditRunner(
        xedit_path=Path("SSEEdit.exe"),
        game_path=Path("Skyrim Special Edition"),
    )

    # Generate and execute a dynamic script
    script = ScriptGenerator.generate_merge_script(
        output_plugin="Merged.esp",
        record_types=["LVLI", "LVLN"],
    )
    result = await runner.run_dynamic_script(script, ["plugin1.esp"])
"""

from __future__ import annotations

from sky_claw.local.xedit.conflict_analyzer import (
    CRITICAL_FLAGS,
    ConflictAnalyzer,
    ConflictReport,
    OverrideFlagState,
    PluginConflictPair,
    RecordConflict,
)
from sky_claw.local.xedit.flag_rules import (
    DEFAULT_FLAG_RULES,
    FlagAlert,
    FlagRule,
    evaluate_flag_rules,
)
from sky_claw.local.xedit.output_parser import XEditOutputParser, XEditResult
from sky_claw.local.xedit.patch_advisor import (
    BASHED_PATCH,
    DEFAULT_STRATEGY_RULES,
    REVIEW,
    SYNTHESIS,
    XEDIT_MANUAL,
    PatchRecommendation,
    StrategyRule,
    recommend,
)
from sky_claw.local.xedit.patch_orchestrator import (
    CreateMergedPatch,
    DelegateToBashedPatch,
    ExecuteXEditScript,
    PatchExecutionError,
    PatchingError,
    PatchOrchestrator,
    PatchPlan,
    PatchResult,
    PatchStrategy,
    PatchStrategyType,
    ScriptGenerationError,
    StrategySelectionError,
)
from sky_claw.local.xedit.runner import (
    ScriptExecutionResult,
    ScriptGenerator,
    XEditError,
    XEditNotFoundError,
    XEditRunner,
    XEditScriptError,
    XEditTimeoutError,
    XEditValidationError,
    XEditWriteError,
)

__all__ = [
    "BASHED_PATCH",
    "CRITICAL_FLAGS",
    "DEFAULT_FLAG_RULES",
    "DEFAULT_STRATEGY_RULES",
    "REVIEW",
    "SYNTHESIS",
    "XEDIT_MANUAL",
    # Conflict analyzer
    "ConflictAnalyzer",
    "ConflictReport",
    "CreateMergedPatch",
    "DelegateToBashedPatch",
    "ExecuteXEditScript",
    "FlagAlert",
    "FlagRule",
    "OverrideFlagState",
    # Patch advisor (T-20)
    "PatchExecutionError",
    "PatchOrchestrator",
    "PatchPlan",
    "PatchRecommendation",
    "PatchResult",
    "PatchStrategy",
    "PatchStrategyType",
    # Patch orchestrator
    "PatchingError",
    "PluginConflictPair",
    "RecordConflict",
    "StrategyRule",
    "ScriptExecutionResult",
    "ScriptGenerationError",
    "ScriptGenerator",
    "StrategySelectionError",
    # Runner exceptions
    "XEditError",
    "XEditNotFoundError",
    # Output parser
    "XEditOutputParser",
    "XEditResult",
    # Runner classes
    "XEditRunner",
    "XEditScriptError",
    "XEditTimeoutError",
    "XEditValidationError",
    "XEditWriteError",
    "evaluate_flag_rules",
    "recommend",
]
