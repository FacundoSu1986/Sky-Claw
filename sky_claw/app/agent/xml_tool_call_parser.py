"""Parser seguro para llamadas de tools mediante XML."""

from __future__ import annotations

import json
import re
from typing import Any

TOOL_CALL_RE = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.DOTALL)


def has_tool_calls(text: str) -> bool:
    """Devuelve True si *text* contiene al menos un bloque <tool_call>."""
    return bool(TOOL_CALL_RE.search(text))


def extract_tool_calls(text: str) -> list[dict[str, Any]]:
    """Extrae los bloques <tool_call> de *text* y analiza sus payloads JSON.

    Lanza:
        ValueError: si algún bloque contiene JSON inválido, carece de la clave 'name'
            o su valor 'arguments' no es un objeto JSON.
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
        arguments = parsed.get("arguments", {})
        if not isinstance(arguments, dict):
            raise ValueError(f"'arguments' must be a JSON object, got {type(arguments).__name__}")
        results.append({"name": str(parsed["name"]), "arguments": arguments})
    return results


__all__ = ["TOOL_CALL_RE", "extract_tool_calls", "has_tool_calls"]
