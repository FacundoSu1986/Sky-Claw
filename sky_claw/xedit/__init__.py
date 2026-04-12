"""xEdit (SSEEdit) headless wrapper for conflict detection and dynamic patching.

Phase 2 Extensions:
    - Dynamic Pascal script generation via ScriptGenerator
    - Headless execution with write flags (-IKnowWhatImDoing)
    - ScriptExecutionResult for detailed execution feedback
    - Integration with PatchOrchestrator via execute_patch()

Usage:
    from sky_claw.xedit import XEditRunner, ScriptGenerator, ScriptExecutionResult

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

from sky_claw.xedit.runner import (
    XEditError,
    XEditNotFoundError,
    XEditValidationError,
    XEditScriptError,
    XEditWriteError,
    XEditTimeoutError,
    XEditRunner,
    ScriptGenerator,
    ScriptExecutionResult,
)
from sky_claw.xedit.output_parser import XEditOutputParser, XEditResult
from sky_claw.xedit.conflict_analyzer import (
    ConflictAnalyzer,
    ConflictReport,
    RecordConflict,
    PluginConflictPair,
)
from sky_claw.xedit.patch_orchestrator import (
    PatchingError,
    StrategySelectionError,
    PatchExecutionError,
    ScriptGenerationError,
    PatchStrategyType,
    PatchPlan,
    PatchResult,
    PatchStrategy,
    CreateMergedPatch,
    ExecuteXEditScript,
    PatchOrchestrator,
)

__all__ = [
    # Runner exceptions
    "XEditError",
    "XEditNotFoundError",
    "XEditValidationError",
    "XEditScriptError",
    "XEditWriteError",
    "XEditTimeoutError",
    # Runner classes
    "XEditRunner",
    "ScriptGenerator",
    "ScriptExecutionResult",
    # Output parser
    "XEditOutputParser",
    "XEditResult",
    # Conflict analyzer
    "ConflictAnalyzer",
    "ConflictReport",
    "RecordConflict",
    "PluginConflictPair",
    # Patch orchestrator
    "PatchingError",
    "StrategySelectionError",
    "PatchExecutionError",
    "ScriptGenerationError",
    "PatchStrategyType",
    "PatchPlan",
    "PatchResult",
    "PatchStrategy",
    "CreateMergedPatch",
    "ExecuteXEditScript",
    "PatchOrchestrator",
]
