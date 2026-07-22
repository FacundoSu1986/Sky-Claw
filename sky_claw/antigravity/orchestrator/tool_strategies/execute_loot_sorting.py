"""Strategy for the `execute_loot_sorting` tool.

The shared dispatcher HITL gate owns operator approval. This strategy owns
payload validation and the lock-protected LOOT service call.
"""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, Any

from sky_claw.antigravity.core.models import LootExecutionParams

if TYPE_CHECKING:
    from sky_claw.local.tools.loot_service import LootSortingService


class ExecuteLootSortingStrategy:
    name = "execute_loot_sorting"

    def __init__(self, service: LootSortingService) -> None:
        self.service = service

    def validate_for_approval(self, payload_dict: dict[str, Any]) -> None:
        self._parse_params(payload_dict)

    def describe_for_approval(self, payload_dict: dict[str, Any]) -> str:
        params = self._parse_params(payload_dict)
        return f"profile_name={params.profile_name!r}, update_masterlist={params.update_masterlist!r}"

    async def prepare_for_approval(self, payload_dict: dict[str, Any]) -> None:
        params = self._parse_params(payload_dict)
        prepared = self.service.prepare_vfs_attestation(params)
        if inspect.isawaitable(prepared):
            await prepared

    def clear_approval_preparation(self, payload_dict: dict[str, Any]) -> None:
        params = self._parse_params(payload_dict)
        self.service.clear_vfs_attestation(params)

    async def execute(self, payload_dict: dict[str, Any]) -> dict[str, Any]:
        params = self._parse_params(payload_dict)
        return await self.service.sort_load_order(params)

    @staticmethod
    def _parse_params(payload_dict: dict[str, Any]) -> LootExecutionParams:
        return LootExecutionParams(**payload_dict)
