"""Strategy for the `generate_bashed_patch` tool."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger(__name__)

_VALID_BASHED_PATCH_KEYS = {"profile", "validate_limit"}


class GenerateBashedPatchStrategy:
    name = "generate_bashed_patch"

    def __init__(
        self,
        wrye_bash_pipeline: Callable[..., Awaitable[dict[str, Any]]],
    ) -> None:
        self.wrye_bash_pipeline = wrye_bash_pipeline

    def describe_for_approval(self, payload_dict: dict[str, Any]) -> str:
        filtered = self._filter_payload(payload_dict)
        if not filtered:
            return "payload: <empty>"
        parts = [f"{key}={value!r}" for key, value in sorted(filtered.items())]
        return "payload: " + ", ".join(parts)

    async def execute(self, payload_dict: dict[str, Any]) -> dict[str, Any]:
        filtered = self._filter_payload(payload_dict)
        return await self.wrye_bash_pipeline(**filtered)

    def _filter_payload(self, payload_dict: dict[str, Any]) -> dict[str, Any]:
        filtered = {key: value for key, value in payload_dict.items() if key in _VALID_BASHED_PATCH_KEYS}
        unexpected = payload_dict.keys() - _VALID_BASHED_PATCH_KEYS
        if unexpected:
            logger.warning("Dropping unexpected payload keys in %s: %s", self.name, unexpected)
        return filtered
