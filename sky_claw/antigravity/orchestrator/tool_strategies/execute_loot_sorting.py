"""Strategy for the `execute_loot_sorting` tool.

Replaces supervisor.py:253-269 (case "execute_loot_sorting"). HITL gate
lives INSIDE the strategy on purpose: Pydantic validation of
`LootExecutionParams` must run BEFORE the human is prompted, so the
context dict shown in Telegram contains the validated profile name.
Wrapping HITL in a middleware would invert that ordering. See the header
note in `middleware.py` for the YAGNI rationale.

Option B (Spec §13): action_type is now `"reorder_load_order"` (was
`"destructive_xedit"` re-used). The Literal in `core/models.py` was
extended additively, so existing HITL consumers keep working.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sky_claw.antigravity.core.models import HitlApprovalRequest, LootExecutionParams

if TYPE_CHECKING:
    from sky_claw.antigravity.comms.interface import InterfaceAgent
    from sky_claw.antigravity.core.windows_interop import ModdingToolsAgent


class ExecuteLootSortingStrategy:
    name = "execute_loot_sorting"

    def __init__(self, tools: ModdingToolsAgent, interface: InterfaceAgent) -> None:
        self.tools = tools
        self.interface = interface

    async def execute(self, payload_dict: dict[str, Any]) -> dict[str, Any]:
        params = LootExecutionParams(**payload_dict)
        hitl_req = HitlApprovalRequest(
            action_type="reorder_load_order",
            reason="Se va a reordenar el Load Order, lo que podría afectar partidas guardadas.",
            context_data={"profile": params.profile_name},
        )
        decision = await self.interface.request_hitl(hitl_req)

        if decision == "approved":
            return await self.tools.run_loot(params)
        return {
            "status": "aborted",
            "reason": "Usuario denegó la operación.",
        }
