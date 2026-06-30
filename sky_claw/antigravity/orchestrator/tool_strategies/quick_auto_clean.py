"""Strategy for the `quick_auto_clean` tool (SSEEdit QuickAutoClean).

The shared dispatcher HITL gate owns operator approval (cleaning rewrites the
official master plugins in place → destructive). This strategy delegates to the
lock-protected ``XEditPipelineService.quick_auto_clean``; it takes no payload.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sky_claw.local.tools.xedit_service import XEditPipelineService


class QuickAutoCleanStrategy:
    name = "quick_auto_clean"

    def __init__(self, service: XEditPipelineService) -> None:
        self.service = service

    async def execute(self, payload_dict: dict[str, Any]) -> dict[str, Any]:
        return await self.service.quick_auto_clean()
