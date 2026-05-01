"""Strategy for the `generate_lods` tool.

Replaces supervisor.py:325-326. Thin pass-through to
DynDOLODPipelineService.execute(**payload_dict).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sky_claw.local.tools.dyndolod_service import DynDOLODPipelineService


class GenerateLodsStrategy:
    name = "generate_lods"

    def __init__(self, service: DynDOLODPipelineService) -> None:
        self.service = service

    async def execute(self, payload_dict: dict[str, Any]) -> dict[str, Any]:
        return await self.service.execute(**payload_dict)
