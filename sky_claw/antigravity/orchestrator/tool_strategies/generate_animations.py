"""Strategy for the `generate_animations` tool (Pandora behavior graphs).

The shared dispatcher HITL gate owns operator approval (Pandora is destructive: it
rewrites behavior graphs). This strategy just delegates to the lock-protected
``PandoraPipelineService``; it takes no payload.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sky_claw.local.tools.pandora_service import PandoraPipelineService


class GenerateAnimationsStrategy:
    name = "generate_animations"

    def __init__(self, service: PandoraPipelineService) -> None:
        self.service = service

    async def execute(self, payload_dict: dict[str, Any]) -> dict[str, Any]:
        return await self.service.generate_animations()
