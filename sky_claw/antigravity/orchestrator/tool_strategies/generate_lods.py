"""Strategy for the `generate_lods` tool."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sky_claw.local.tools.dyndolod_service import DynDOLODPipelineService

logger = logging.getLogger(__name__)

_VALID_LOD_KEYS = {"preset", "run_texgen", "create_snapshot", "texgen_args", "dyndolod_args"}


class GenerateLodsStrategy:
    name = "generate_lods"

    def __init__(self, service: DynDOLODPipelineService) -> None:
        self.service = service

    def describe_for_approval(self, payload_dict: dict[str, Any]) -> str:
        filtered = self._filter_payload(payload_dict)
        if not filtered:
            return "payload: <empty>"
        parts = [f"{key}={value!r}" for key, value in sorted(filtered.items())]
        return "payload: " + ", ".join(parts)

    async def execute(self, payload_dict: dict[str, Any]) -> dict[str, Any]:
        filtered = self._filter_payload(payload_dict)
        return await self.service.execute(**filtered)

    def _filter_payload(self, payload_dict: dict[str, Any]) -> dict[str, Any]:
        filtered = {key: value for key, value in payload_dict.items() if key in _VALID_LOD_KEYS}
        unexpected = payload_dict.keys() - _VALID_LOD_KEYS
        if unexpected:
            logger.warning("Dropping unexpected payload keys in %s: %s", self.name, unexpected)
        return filtered
