"""Strategy for the `generate_bashed_patch` tool.

Thin adapter onto `supervisor.execute_wrye_bash_pipeline(**payload_dict)`.
That method (~100 lines including M-04 plugin-limit guard + runner init)
stays on the supervisor — extracting it is a separate refactor (Spec §9).

Receives a **callable** so the test fixture can reassign
`supervisor.execute_wrye_bash_pipeline = AsyncMock(...)` after the
dispatcher is built.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any


class GenerateBashedPatchStrategy:
    name = "generate_bashed_patch"

    def __init__(
        self,
        wrye_bash_pipeline: Callable[..., Awaitable[dict[str, Any]]],
    ) -> None:
        self.wrye_bash_pipeline = wrye_bash_pipeline

    async def execute(self, payload_dict: dict[str, Any]) -> dict[str, Any]:
        # Filter to only valid parameters — the LLM may inject extra keys
        # (e.g. "tool_name") that would cause TypeError on the pipeline.
        valid_keys = {"profile", "validate_limit"}
        filtered = {k: v for k, v in payload_dict.items() if k in valid_keys}
        return await self.wrye_bash_pipeline(**filtered)
