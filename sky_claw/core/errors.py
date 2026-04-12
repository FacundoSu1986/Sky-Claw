"""Base exception hierarchy for Sky-Claw."""

from __future__ import annotations


class AppNexusError(Exception):
    """Root exception for all Sky-Claw application errors."""


class FomodParserSecurityError(AppNexusError):
    """XML security violation detected during FOMOD parsing.

    Raised when ``defusedxml`` detects a forbidden DTD declaration,
    entity definition, or external reference in a FOMOD XML file.
    The SupervisorAgent should abort the mod installation when this
    is raised.
    """


class SecurityViolationError(AppNexusError):
    """Runtime security constraint violated in the agent pipeline.

    Raised by ``AgentGuardrail`` when:

    * Prompt-injection patterns (OWASP LLM01) are detected in user input.
    * PII or secret material (SSN, credit cards, API keys, passwords) is found.
    * Absolute filesystem paths are leaked in model output.

    Call sites must catch this, log a warning, and return a safe fallback
    message to the user — never let it crash the daemon or the Tkinter UI loop.
    """


class AgentOrchestrationError(AppNexusError):
    """Agent output failed structural / schema validation.

    Raised by ``AgentGuardrail`` when model output cannot be parsed into the
    expected Pydantic schema, indicating the model deviated from the required
    output contract (hallucinated schema).

    Call sites must catch this, log an error, and return a safe fallback
    message — never propagate to the asyncio event loop unhandled.
    """
