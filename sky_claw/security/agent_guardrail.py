"""Zero-Trust AgentGuardrail middleware — Titan v7.0.

Provides an injectable, *stateless* security layer that inspects every message
entering and leaving the LLM:

* **before_model_callback** – detects prompt-injection attempts, PII, and
  secret material in user input; sanitizes the text before it reaches the
  model.
* **after_model_callback** – detects absolute path leakage in model output
  and validates the output against a Pydantic schema when one is provided.
* **secure_llm_call** – async orchestrator that composes the two callbacks
  around a provider.chat() call, fully concurrent (no locking).

Design invariants (Titan v7.0):
- Stateless: no mutable instance state → safe for concurrent use by N agents.
- Fail-closed: all detectors default to *on*; callers opt out explicitly via
  ``AgentGuardrailConfig``.
- No asyncio.Lock wrapping provider I/O (that would serialize all agents).
- Never import from sky_claw.agent to avoid circular dependencies.

Usage::

    from sky_claw.security import AgentGuardrail, AgentGuardrailConfig, secure_llm_call

    guardrail = AgentGuardrail()                         # default: all checks on
    response  = await secure_llm_call(provider, msgs, guardrail)

    # Disable path detection for an agent that legitimately emits MO2 paths:
    mo2_guardrail = AgentGuardrail(AgentGuardrailConfig(detect_paths=False))
"""

from __future__ import annotations

import logging
import re
from typing import Any

import pydantic
from pydantic import BaseModel, ConfigDict, Field

from sky_claw.core.errors import AgentOrchestrationError, SecurityViolationError
from sky_claw.security.prompt_armor import validate_prompt_integrity
from sky_claw.security.sanitize import sanitize_for_prompt
from sky_claw.security.text_inspector import TextInspector

logger = logging.getLogger("SkyClaw.AgentGuardrail")

# ---------------------------------------------------------------------------
# Module-level compiled patterns (compiled once at import time for performance)
# ---------------------------------------------------------------------------

# PII — combined detector. One compiled pattern with named groups beats four
# sequential `re.search` calls in both clarity and runtime (single scan instead
# of four). Alternation order encodes priority: SSN > card > api_key > password.
# IGNORECASE applies to the whole pattern; SSN/card alternations are digit-only
# so the flag is harmless for them.
_COMBINED_PII_RE = re.compile(
    r"(?P<ssn>\b\d{3}-\d{2}-\d{4}\b)"
    r"|(?P<card>\b(?:\d{4}[ -]?){3}\d{1,4}\b)"
    r"|(?P<api_key>\b(?:sk-[A-Za-z0-9]{15,}|[A-Za-z0-9]{32,}(?:_key|_secret|_token))\b)"
    r"|(?P<password>password\s*[=:]\s*\S+)",
    re.IGNORECASE,
)

_PII_MESSAGES: dict[str, str] = {
    "ssn": "PII detected in input: SSN pattern found",
    "card": "PII detected in input: credit/debit card number pattern found",
    "api_key": "PII detected in input: API key pattern found",
    "password": "PII detected in input: password assignment pattern found",
}

# Path leakage — Windows absolute path (e.g. C:\Users\...)
_WIN_ABS_PATH_RE = re.compile(r"[A-Za-z]:\\[\\A-Za-z0-9_.+\- ]+")

# Path leakage — Unix absolute paths under sensitive top-level dirs.
# /mnt/ is intentionally excluded: WSL mount points (/mnt/c/...) appear
# legitimately in tool output and must not trigger false positives.
_UNIX_ABS_PATH_RE = re.compile(r"(?<!\w)/(?:etc|usr|var|home|root|proc|sys|tmp|dev)/\S+")

# Path leakage — UNC / network share paths (\\server\share)
_UNC_PATH_RE = re.compile(r"\\\\[A-Za-z0-9_.\-]+\\[A-Za-z0-9_$.\-]+")

# Severities that warrant an immediate block on injection detection
_BLOCK_SEVERITIES = frozenset({"CRITICAL", "HIGH"})

# Supplementary guardrail-level injection patterns — these extend TextInspector
# to cover phrases not in its corpus and MEDIUM-severity phrases that still
# warrant a hard block at the agent boundary.
_GUARDRAIL_INJECTION_RE = re.compile(
    r"(?i)(?:"
    # Override / jailbreak openers
    r"ignore\s+(?:all\s+)?(?:prior|previous)\s+(?:context|instructions?|commands?|directives?)"
    r"|system\s*:\s*(?:you\s+are|override|now\b)"  # "system: you are now..."
    r"|forget\s+(?:everything|all|prior|previous)"  # "Forget everything..."
    r"|act\s+as\s+(?:DAN|an?\s+unrestricted|a\s+free)"  # "act as DAN"
    r"|no\s+longer\s+bound\s+(?:by|to)"  # "no longer bound by..."
    r"|you\s+are\s+no\s+longer\s+bound"  # "you are no longer bound"
    r"|as\s+a\s+developer\s+you\s+must"  # social engineering
    r"|disregard\s+(?:all\s+)?(?:prior|previous|your)"  # "disregard all prior..."
    r"|new\s+(?:primary\s+)?directive\s*:"  # "New directive:"
    r"|override\s+(?:all\s+)?(?:safety|security|instructions?)"
    r")"
)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class AgentGuardrailConfig(BaseModel):
    """Immutable configuration for ``AgentGuardrail``.

    All detectors default to *True* (fail-closed).  Pass ``False`` to any
    flag when a specific agent legitimately needs to bypass that check — e.g.
    ``detect_paths=False`` for an agent whose output intentionally contains
    Mod Organizer 2 file paths.
    """

    model_config = ConfigDict(frozen=True)

    max_input_length: int = Field(default=8192, gt=0)
    max_output_length: int = Field(default=16384, gt=0)
    detect_pii: bool = True
    detect_paths: bool = True
    detect_injection: bool = True
    sanitize_input: bool = True
    validate_prompt_integrity: bool = True


# ---------------------------------------------------------------------------
# Guardrail
# ---------------------------------------------------------------------------


class AgentGuardrail:
    """Stateless Zero-Trust middleware for LLM input/output validation.

    Stateless design (Titan v7.0):
    - No mutable instance fields after ``__init__``.
    - Safe to share across concurrent async tasks without any locking.
    - One instance can serve all agents simultaneously.

    Thread-safety note:
    - Designed for use within a single asyncio event loop.
    - If the same instance is shared across OS threads, wrap the call site
      in an external ``threading.Lock``; the guardrail itself does not provide
      this protection.
    """

    __slots__ = ("_config", "_inspector")

    def __init__(self, config: AgentGuardrailConfig | None = None) -> None:
        self._config: AgentGuardrailConfig = config or AgentGuardrailConfig()
        self._inspector: TextInspector = TextInspector(max_chars=self._config.max_input_length)

    # ------------------------------------------------------------------
    # Input gate
    # ------------------------------------------------------------------

    async def before_model_callback(self, user_input: str) -> str:
        """Inspect and sanitize *user_input* before it reaches the model.

        Processing order (matters — detection runs on raw text):

        1. Truncate to ``max_input_length`` (token-bomb prevention).
        2. Injection detection via ``TextInspector`` (OWASP LLM01 phrases).
        3. PII detection via compiled regexes (SSN, card, key, password).
        4. Sanitization via ``sanitize_for_prompt`` (strips delimiters, etc.).

        Detection runs *before* sanitization so that attackers cannot bypass
        detection by embedding patterns inside sanitizable delimiters.

        Raises:
            SecurityViolationError: on injection or PII detection.

        Returns:
            Sanitized string safe for prompt embedding.
        """
        cfg = self._config

        # 1 — truncate
        text = user_input[: cfg.max_input_length]

        # 2 — injection detection (on raw text, before sanitization)
        if cfg.detect_injection:
            # 2a — TextInspector: OWASP LLM01 phrase patterns (CRITICAL/HIGH only)
            findings = self._inspector.inspect(text)
            blocking = [f for f in findings if f.get("severity") in _BLOCK_SEVERITIES]
            if blocking:
                msg = blocking[0]["message"]
                logger.warning("Guardrail: injection blocked (TextInspector) — %s", msg)
                raise SecurityViolationError(f"Prompt injection detected in user input: {msg}")
            # 2b — supplementary guardrail patterns (covers phrases TextInspector
            #      rates MEDIUM or does not include at all)
            if m := _GUARDRAIL_INJECTION_RE.search(text):
                logger.warning("Guardrail: injection blocked (guardrail pattern) — %r", m.group())
                raise SecurityViolationError(f"Prompt injection detected in user input: {m.group()!r}")

        # 3 — PII detection
        if cfg.detect_pii:
            _check_pii(text)

        # 4 — sanitize for safe embedding
        if cfg.sanitize_input:
            text = sanitize_for_prompt(text, max_length=cfg.max_input_length)

        return text

    async def validate_messages_integrity(
        self,
        messages: list[dict[str, Any]],
    ) -> None:
        """FASE 1.5.1: Validate that <external_data> tags only appear in user messages.

        Raises:
            SecurityViolationError: If <external_data> tags are found in
                system or assistant messages (indicates prompt tampering).
        """
        if not self._config.validate_prompt_integrity:
            return
        if not validate_prompt_integrity(messages):
            raise SecurityViolationError(
                "Prompt integrity violation: <external_data> tags detected "
                "in non-user message. Possible prompt injection attack."
            )

    # ------------------------------------------------------------------
    # Output gate
    # ------------------------------------------------------------------

    async def after_model_callback(
        self,
        model_output: str,
        expected_schema: type[BaseModel] | None = None,
    ) -> str:
        """Validate *model_output* before it is returned to the caller.

        Processing order:

        1. Truncate to ``max_output_length``.
        2. Absolute path leakage detection (Windows, Unix sensitive dirs, UNC).
        3. Optional schema validation via ``model_validate_json`` (Pydantic v2).

        Raises:
            SecurityViolationError: when an absolute path is detected.
            AgentOrchestrationError: when output fails schema validation.

        Returns:
            *model_output* unchanged (the caller may still need the raw text).
        """
        cfg = self._config

        # 1 — truncate
        text = model_output[: cfg.max_output_length]

        # 2 — path leakage detection
        if cfg.detect_paths:
            _check_paths(text)

        # 3 — schema validation (single Pydantic v2 optimized call)
        if expected_schema is not None:
            try:
                expected_schema.model_validate_json(text)
            except pydantic.ValidationError as exc:
                logger.error("Guardrail: schema violation in model output — %s", exc)
                raise AgentOrchestrationError(
                    f"Model output does not conform to {expected_schema.__name__}: {exc}"
                ) from exc

        return text


# ---------------------------------------------------------------------------
# Secure orchestrator
# ---------------------------------------------------------------------------


async def secure_llm_call(
    provider: Any,
    messages: list[dict[str, Any]],
    guardrail: AgentGuardrail,
    expected_schema: type[BaseModel] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Async orchestrator: before_callback → provider.chat() → after_callback.

    Fully concurrent — no locks.  The guardrail is stateless so N agents can
    call this simultaneously on the same ``AgentGuardrail`` instance.

    Args:
        provider:        Any LLMProvider with ``async chat(messages, **kwargs)``.
        messages:        Conversation history.  This list is **never mutated**;
                         a shallow copy is passed to the provider.
        guardrail:       ``AgentGuardrail`` instance (shared or per-session).
        expected_schema: Optional Pydantic model class for output validation.
        **kwargs:        Forwarded verbatim to ``provider.chat()``.

    Returns:
        The raw response dict from ``provider.chat()``.

    Raises:
        SecurityViolationError:   if input or output violates security policy.
        AgentOrchestrationError:  if output fails schema validation.
    """
    # Extract last user message text for inspection
    user_text = _extract_last_user_content(messages)

    # Gate 1 — sanitize / validate input (may raise SecurityViolationError)
    sanitized = await guardrail.before_model_callback(user_text)

    # Build a shallow-copy of messages with the sanitized last user message
    messages_copy = _replace_last_user_content(messages, sanitized)

    # Call the LLM — free async I/O, no lock held
    response: dict[str, Any] = await provider.chat(messages=messages_copy, **kwargs)

    # Collect all text blocks from the response
    combined_text = _collect_text(response)

    # Gate 2 — validate output (may raise SecurityViolationError or AgentOrchestrationError)
    await guardrail.after_model_callback(combined_text, expected_schema)

    return response


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _check_pii(text: str) -> None:
    """Raise ``SecurityViolationError`` if *text* contains PII patterns."""
    match = _COMBINED_PII_RE.search(text)
    if match is None:
        return
    for name, value in match.groupdict().items():
        if value is not None:
            raise SecurityViolationError(_PII_MESSAGES[name])


def _check_paths(text: str) -> None:
    """Raise ``SecurityViolationError`` if *text* leaks absolute filesystem paths."""
    if m := _WIN_ABS_PATH_RE.search(text):
        raise SecurityViolationError(f"Absolute path leaked in model output: {m.group()!r}")
    if m := _UNIX_ABS_PATH_RE.search(text):
        raise SecurityViolationError(f"Absolute path leaked in model output: {m.group()!r}")
    if m := _UNC_PATH_RE.search(text):
        raise SecurityViolationError(f"Absolute path leaked in model output: {m.group()!r}")


def _extract_last_user_content(messages: list[dict[str, Any]]) -> str:
    """Return the ``content`` string of the last user-role message."""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            return content if isinstance(content, str) else str(content)
    return ""


def _replace_last_user_content(
    messages: list[dict[str, Any]],
    new_content: str,
) -> list[dict[str, Any]]:
    """Return a *shallow copy* of messages with the last user message updated.

    The caller's original list and message dicts are never mutated.
    """
    copy: list[dict[str, Any]] = list(messages)
    for i in range(len(copy) - 1, -1, -1):
        if copy[i].get("role") == "user":
            # Replace the dict at this index with a new dict containing updated content
            copy[i] = {**copy[i], "content": new_content}
            break
    return copy


def _collect_text(response: dict[str, Any]) -> str:
    """Concatenate all ``type=text`` blocks from a provider response."""
    parts: list[str] = []
    for block in response.get("content", []):
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "\n".join(parts)
