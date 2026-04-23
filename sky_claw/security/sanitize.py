"""Input sanitization for external content injected into LLM prompts.

Scraped web content (Nexus Mods descriptions, LOOT metadata, etc.)
must be sanitized before being embedded in prompts to prevent:

* **Prompt Injection** – adversarial text that overrides the system
  prompt or forces unintended tool calls.
* **Malformed JSON** – broken Unicode or control characters that break
  downstream JSON serialization.
"""

from __future__ import annotations

import json
import re
import unicodedata
from typing import Any

# Characters that have special meaning in many LLM prompt formats.
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# Cap to avoid token-bomb attacks from massive scraped pages.
DEFAULT_MAX_LENGTH = 8192

# Maximum nesting depth for safe_json_loads to prevent stack overflow.
_MAX_JSON_SIZE = 1_000_000  # 1 MB

# Compiled regex for all known prompt-injection delimiters (multi-pass safe).
# Covers: OpenAI (<|...|>), Llama/Mistral ([INST], <<SYS>>),
# Anthropic (\n\nHuman:, \n\nAssistant:), generic XML-style tags,
# and tool/function injection markers.
_INJECTION_PATTERNS = re.compile(
    r"<\|"  # OpenAI-style opening <|
    r"|\|>"  # OpenAI-style closing |>
    r"|<<SYS>>"  # Llama system open
    r"|<</SYS>>"  # Llama system close
    r"|\[INST\]"  # Llama/Mistral instruction open
    r"|\[/INST\]"  # Llama/Mistral instruction close
    r"|\[SYSTEM\]"  # Generic system marker
    r"|\{system\}"  # Template-style system marker
    r"|\n\nHuman:"  # Anthropic Human turn
    r"|\n\nAssistant:"  # Anthropic Assistant turn
    r"|<human>"  # Anthropic XML-style
    r"|</human>"
    r"|<assistant>"
    r"|</assistant>"
    r"|<tool_use>"  # Tool injection markers
    r"|</tool_use>"
    r"|<tool_call>"  # Hermes-style tool injection markers
    r"|</tool_call>"
    r"|<function_call>"
    r"|</function_call>"
    r"|<tool_result>"
    r"|</tool_result>"
    r"|<\|endoftext\|>"  # Special tokens
    r"|<\|im_start\|>"
    r"|<\|im_end\|>"
    r"|<\|pad\|>"
    r"|<\|diff_marker\|>",
    re.IGNORECASE,
)


def sanitize_for_prompt(
    text: str,
    *,
    max_length: int = DEFAULT_MAX_LENGTH,
    strip_control: bool = True,
) -> str:
    """Clean *text* so it is safe to embed in an LLM prompt.

    1. Normalizes Unicode to NFKC to neutralize homoglyph attacks.
    2. Removes ASCII control characters (except ``\\n``, ``\\r``, ``\\t``).
    3. Removes all known prompt-injection delimiters via compiled regex
       (handles nested/recursive patterns in a single pass).
    4. Truncates to *max_length* characters to bound token usage.

    Args:
        text: Raw external content.
        max_length: Maximum allowed length after sanitization.
        strip_control: Whether to strip ASCII control characters.

    Returns:
        Sanitized string safe for prompt embedding.
    """
    # Normalize Unicode to NFKC to collapse homoglyphs (e.g., fullwidth
    # brackets, Cyrillic look-alikes) into their ASCII equivalents.
    text = unicodedata.normalize("NFKC", text)

    if strip_control:
        text = _CONTROL_CHAR_RE.sub("", text)

    # Remove all known injection delimiters, repeating until stable.
    # This prevents reconstructed bypasses (e.g. "[IN[INST]ST]" → "[INST]")
    # by re-applying the regex until no further matches are found.
    # Cap at 10 iterations to avoid performance degradation.
    _max_passes = 10
    for _ in range(_max_passes):
        cleaned = _INJECTION_PATTERNS.sub("", text)
        if cleaned == text:
            break
        text = cleaned

    if len(text) > max_length:
        suffix = "... [truncated]"
        text = text[: max_length - len(suffix)] + suffix if max_length > len(suffix) else text[:max_length]

    return text


def safe_json_loads(raw: str) -> dict[str, Any] | list[Any] | None:
    """Attempt to parse *raw* as JSON, returning ``None`` on failure.

    Rejects payloads larger than 1 MB to prevent resource exhaustion.
    This is a convenience wrapper that never raises, making it safe to
    use on untrusted scraped payloads.
    """
    if not isinstance(raw, str):
        return None
    if len(raw) > _MAX_JSON_SIZE:
        return None
    try:
        result = json.loads(raw)
        if isinstance(result, (dict, list)):
            return result
        return None
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
