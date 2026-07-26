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
import logging
import re
import unicodedata
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

# Characters that have special meaning in many LLM prompt formats.
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# Cap to avoid token-bomb attacks from massive scraped pages.
DEFAULT_MAX_LENGTH = 8192

# Maximum nesting depth for safe_json_loads to prevent stack overflow.
_MAX_JSON_SIZE = 1_000_000  # 1 MB

# H-05: Security policy file location (sibling of this module).
_SECURITY_POLICY_PATH = Path(__file__).parent / "security_policy.yaml"

# ---------------------------------------------------------------------------
# Homoglyph canonicalisation (L-01)
# ---------------------------------------------------------------------------

# Mapping of visually-identical characters from non-Latin scripts
# back to their ASCII look-alikes.  Only covers characters that are
# commonly used in prompt-injection evasion.
_HOMOGLYPH_MAP: dict[str, str] = {
    # Cyrillic small letters that look like Latin
    "\u0430": "a",  # а (U+0430)
    "\u0435": "e",  # е (U+0435)
    "\u043e": "o",  # о (U+043E)
    "\u0440": "p",  # р (U+0440)
    "\u0441": "c",  # с (U+0441)
    "\u0445": "x",  # х (U+0445)
    "\u0456": "i",  # і (U+0456)
    "\u0458": "j",  # ј (U+0458)
    "\u043a": "k",  # к (U+043A)
    "\u051b": "q",  # ԛ (U+051B)
    "\u0455": "s",  # ѕ (U+0455)
    "\u051d": "w",  # ԝ (U+051D)
    "\u0443": "y",  # у (U+0443)
    "\u044a": "'",  # ъ (U+044A)
    "\u044c": "'",  # ь (U+044C)
    # Cyrillic capital letters
    "\u0410": "A",  # А (U+0410)
    "\u0415": "E",  # Е (U+0415)
    "\u041e": "O",  # О (U+041E)
    "\u0420": "P",  # Р (U+0420)
    "\u0421": "C",  # С (U+0421)
    "\u0425": "X",  # Х (U+0425)
    "\u0406": "I",  # І (U+0406)
    # Greek small letters
    "\u03bf": "o",  # ο (U+03BF)
    "\u03c1": "p",  # ρ (U+03C1)
    "\u03c2": "s",  # ς (U+03C2)
    "\u03c5": "u",  # υ (U+03C5)
    "\u03c7": "x",  # χ (U+03C7)
    # Greek capital letters
    "\u039f": "O",  # Ο (U+039F)
    "\u03a1": "P",  # Ρ (U+03A1)
    "\u03a7": "X",  # Χ (U+03A7)
}

# Build a regex that matches any homoglyph character.
_HOMOGLYPH_RE = re.compile("|".join(map(re.escape, _HOMOGLYPH_MAP.keys())))

# ASCII letters/digits that indicate a token is "mixed-script suspicious".
_ASCII_BASIC_RE = re.compile(r"[A-Za-z0-9]")

# Characters from confusable scripts (Cyrillic + Greek blocks).
_CONFUSABLE_SCRIPT_RE = re.compile(r"[\u0400-\u04FF\u0370-\u03FF]")

# Word tokens include underscores, which appear in LLM tool markers such
# as ``tool_use`` and ``function_call``.
_WORD_TOKEN_RE = re.compile(r"\w+")


def _canonicalize_homoglyphs(text: str) -> str:
    """Replace mixed-script homoglyphs with their ASCII equivalents.

    To minimise false positives on legitimate non-English text, the
    replacement is applied *per token* (word run, including underscores)
    only when a token contains both ASCII characters and characters from a
    confusable script (Cyrillic or Greek).  Pure Cyrillic words such as
    ``русский`` are left untouched.
    """

    def _replace_token(token: str) -> str:
        # Token must contain at least one basic ASCII char AND at least
        # one character from a confusable script to be canonicalised.
        if not (_ASCII_BASIC_RE.search(token) and _CONFUSABLE_SCRIPT_RE.search(token)):
            return token
        return _HOMOGLYPH_RE.sub(lambda m: _HOMOGLYPH_MAP[m.group(0)], token)

    return _WORD_TOKEN_RE.sub(lambda match: _replace_token(match.group(0)), text)


def _strip_unicode_format_controls(text: str) -> str:
    """Remove invisible Unicode format controls used for prompt obfuscation."""
    return "".join(char for char in text if unicodedata.category(char) != "Cf")


# ---------------------------------------------------------------------------
# Policy loader (R-01 – fail-closed)
# ---------------------------------------------------------------------------


def load_injection_patterns(
    policy_path: Path = _SECURITY_POLICY_PATH,
) -> re.Pattern[str]:
    """Load injection patterns from ``security_policy.yaml`` and compile.

    H-05: Patterns are externalized so the security team can update
    them without touching core code.  Call ``reload_injection_patterns()``
    to apply YAML changes to the active sanitizer at runtime.

    **Fail-closed contract**: any problem reading, parsing, or
    validating the policy file results in a ``RuntimeError``.  The
    sanitizer MUST NOT start (or continue) with an empty/invalid
    pattern set.
    """
    # --- Read file ---
    try:
        raw = policy_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"SECURITY: Security policy file missing: {policy_path}. "
            "Sanitizer cannot operate without injection patterns."
        ) from exc
    except OSError as exc:
        raise RuntimeError(f"SECURITY: Cannot read security policy file {policy_path}: {exc}") from exc

    # --- Parse YAML ---
    try:
        policy = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise RuntimeError(f"SECURITY: Security policy YAML malformed: {exc}") from exc

    if not isinstance(policy, dict):
        raise RuntimeError("SECURITY: Security policy root must be a YAML mapping.")

    # --- Validate structure ---
    entries = policy.get("injection_patterns")
    if entries is None:
        raise RuntimeError("SECURITY: Security policy missing 'injection_patterns' key.")
    if not isinstance(entries, list) or len(entries) == 0:
        raise RuntimeError("SECURITY: Security policy 'injection_patterns' must be a non-empty list.")

    fragments: list[str] = []
    for idx, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise RuntimeError(f"SECURITY: injection_patterns[{idx}] is not a mapping.")
        pattern = entry.get("pattern")
        if pattern is None:
            raise RuntimeError(f"SECURITY: injection_patterns[{idx}] missing 'pattern' key.")
        if not isinstance(pattern, str) or not pattern:
            raise RuntimeError(f"SECURITY: injection_patterns[{idx}] has empty or invalid pattern.")
        fragments.append(pattern)

    combined = "|".join(f"(?:{fragment})" for fragment in fragments)
    try:
        return re.compile(combined, re.IGNORECASE)
    except re.error as exc:
        raise RuntimeError(f"SECURITY: Failed to compile combined injection regex: {exc}") from exc


# Compiled regex loaded from YAML at import time.
_INJECTION_PATTERNS = load_injection_patterns()


def reload_injection_patterns(
    policy_path: Path = _SECURITY_POLICY_PATH,
) -> re.Pattern[str]:
    """Reload policy YAML and update the regex used by ``sanitize_for_prompt``."""
    global _INJECTION_PATTERNS
    _INJECTION_PATTERNS = load_injection_patterns(policy_path)
    return _INJECTION_PATTERNS


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def sanitize_for_prompt(
    text: str,
    *,
    max_length: int = DEFAULT_MAX_LENGTH,
    strip_control: bool = True,
) -> str:
    """Clean *text* so it is safe to embed in an LLM prompt.

    1. Canonicalises mixed-script homoglyphs (L-01).
    2. Normalizes Unicode to NFKC to neutralise additional homoglyph attacks.
    3. Removes invisible Unicode format controls (bidi/zero-width).
    4. Removes ASCII control characters (except ``\\n``, ``\\r``, ``\\t``).
    5. Removes all known prompt-injection delimiters via a bounded
       multi-pass regex loop for nested/recursive patterns.
    6. Truncates to *max_length* characters to bound token usage.

    Args:
        text: Raw external content.
        max_length: Maximum allowed length after sanitization.
        strip_control: Whether to strip ASCII control characters.

    Returns:
        Sanitized string safe for prompt embedding.
    """
    # L-01: Neutralise inter-script homoglyphs in mixed tokens.
    text = _canonicalize_homoglyphs(text)

    # Normalize Unicode to NFKC to collapse compatibility homoglyphs
    # (e.g., fullwidth brackets, Cyrillic look-alikes) into their ASCII equivalents.
    text = unicodedata.normalize("NFKC", text)
    text = _strip_unicode_format_controls(text)

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
