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

from sky_claw.xedit.conflict_analyzer import (
    ConflictAnalyzer,
    ConflictReport,
    PluginConflictPair,
    RecordConflict,
)
from sky_claw.xedit.output_parser import XEditOutputParser, XEditResult
from sky_claw.xedit.patch_orchestrator import (
    CreateMergedPatch,
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
from sky_claw.xedit.runner import (
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
    # Conflict analyzer
    "ConflictAnalyzer",
    "ConflictReport",
    "CreateMergedPatch",
    "ExecuteXEditScript",
    "PatchExecutionError",
    "PatchOrchestrator",
    "PatchPlan",
    "PatchResult",
    "PatchStrategy",
    "PatchStrategyType",
    # Patch orchestrator
    "PatchingError",
    "PluginConflictPair",
    "RecordConflict",
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
]
