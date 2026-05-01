"""Strategy for the `execute_synthesis_pipeline` tool.

Replaces supervisor.py:271-293. The try/except + isinstance(dict) guard
that wrapped the legacy branch is now provided by ErrorWrappingMiddleware
+ DictResultGuardMiddleware (registered in `tool_dispatcher.py`).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sky_claw.local.tools.synthesis_service import SynthesisPipelineService

_LOGGER = logging.getLogger(__name__)


class ExecuteSynthesisPipelineStrategy:
    name = "execute_synthesis_pipeline"

    def __init__(self, service: SynthesisPipelineService) -> None:
        self.service = service

    async def execute(self, payload_dict: dict[str, Any]) -> dict[str, Any]:
        # Filter to only valid parameters — the LLM may inject extra keys
        # (e.g. "tool_name") that would cause TypeError on the service.
        valid_keys = {"patcher_ids", "create_snapshot"}
        filtered = {k: v for k, v in payload_dict.items() if k in valid_keys}
        unexpected = payload_dict.keys() - valid_keys
        if unexpected:
            _LOGGER.warning("Dropping unexpected payload keys in %s: %s", self.name, unexpected)
        return await self.service.execute_pipeline(**filtered)
