"""Strategy for the `generate_lods` tool.

Replaces supervisor.py:325-326. Thin pass-through to
DynDOLODPipelineService.execute(**payload_dict).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sky_claw.local.tools.dyndolod_service import DynDOLODPipelineService

_LOGGER = logging.getLogger(__name__)


class GenerateLodsStrategy:
    name = "generate_lods"

    def __init__(self, service: DynDOLODPipelineService) -> None:
        self.service = service

    async def execute(self, payload_dict: dict[str, Any]) -> dict[str, Any]:
        # Filter to only valid parameters — the LLM may inject extra keys
        # (e.g. "tool_name") that would cause TypeError on the service.
        valid_keys = {"preset", "run_texgen", "create_snapshot", "texgen_args", "dyndolod_args"}
        filtered = {k: v for k, v in payload_dict.items() if k in valid_keys}
        unexpected = payload_dict.keys() - valid_keys
        if unexpected:
            _LOGGER.warning("Dropping unexpected payload keys in %s: %s", self.name, unexpected)
        return await self.service.execute(**filtered)
