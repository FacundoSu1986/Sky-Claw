"""Zero-trust Hermes-style XML tool-call parser."""

from __future__ import annotations

import json
import re
from typing import Any

TOOL_CALL_RE = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.DOTALL)


def has_tool_calls(text: str) -> bool:
    """Return True if *text* contains at least one <tool_call> block."""
    return bool(TOOL_CALL_RE.search(text))


def extract_tool_calls(text: str) -> list[dict[str, Any]]:
    """Extract all <tool_call> blocks from *text* and parse their JSON payloads.

    Raises:
        ValueError: if any block contains malformed JSON or is missing the 'name' key.
    """
    results: list[dict[str, Any]] = []
    for raw in TOOL_CALL_RE.findall(text):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Malformed JSON in <tool_call>: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ValueError(f"<tool_call> payload must be a JSON object, got {type(parsed).__name__}: {raw!r}")
        if "name" not in parsed:
            raise ValueError(f"Missing 'name' key in tool call: {raw!r}")
        arguments = parsed.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise ValueError(f"'arguments' must be a JSON object, got {type(arguments).__name__}")
        results.append({"name": str(parsed["name"]), "arguments": arguments})
    return results
