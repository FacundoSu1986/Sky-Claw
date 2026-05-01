"""Strategy for the `resolve_conflict_with_patch` tool.

Replaces supervisor.py:295-322. Builds the kwargs that
XEditPipelineService.execute_patch expects: `target_plugin` coerced to
pathlib.Path and `report` constructed as ConflictReport. The try/except
+ isinstance(dict) guard is provided by middleware.
"""

from __future__ import annotations

import pathlib
from typing import TYPE_CHECKING, Any

from sky_claw.local.xedit.conflict_analyzer import ConflictReport

if TYPE_CHECKING:
    from sky_claw.local.tools.xedit_service import XEditPipelineService


class ResolveConflictWithPatchStrategy:
    name = "resolve_conflict_with_patch"

    def __init__(self, service: XEditPipelineService) -> None:
        self.service = service

    async def execute(self, payload_dict: dict[str, Any]) -> dict[str, Any]:
        target_plugin = pathlib.Path(payload_dict["target_plugin"])
        report = ConflictReport(**payload_dict["report"])
        return await self.service.execute_patch(
            target_plugin=target_plugin,
            report=report,
        )
