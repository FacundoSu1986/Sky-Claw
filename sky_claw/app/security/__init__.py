"""Security layer – network egress control, path sandboxing, HITL, sanitization."""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # Zero-Trust LLM guardrail middleware (Titan v7.0)
    from sky_claw.app.security.agent_guardrail import (
        AgentGuardrail,
        AgentGuardrailConfig,
        secure_llm_call,
    )
    from sky_claw.app.security.governance import GovernanceManager
    from sky_claw.app.security.hitl import HITLGuard
    from sky_claw.app.security.loop_guardrail import AgenticLoopGuardrail
    from sky_claw.app.security.metacognitive_logic import SecurityMetacognition, audit_resource
    from sky_claw.app.security.network_gateway import NetworkGateway
    from sky_claw.app.security.path_validator import PathValidator, sandboxed_io

    # FASE 1.5.1: Prompt Armor — semantic hardening against file-based injection
    from sky_claw.app.security.prompt_armor import (
        PromptArmor,
        PromptArmorConfig,
        build_system_header,
        encapsulate_external_data,
        validate_prompt_integrity,
    )

    # Nuevos componentes de ciberseguridad avanzada (v5.5 Titan)
    from sky_claw.app.security.purple_scanner import PurpleScanner, run_scan
    from sky_claw.app.security.sanitize import safe_json_loads, sanitize_for_prompt
    from sky_claw.app.security.text_inspector import TextInspector, scan_text

_LAZY_SUBMODULES: dict[str, str] = {
    "AgentGuardrail": "sky_claw.app.security.agent_guardrail",
    "AgentGuardrailConfig": "sky_claw.app.security.agent_guardrail",
    "secure_llm_call": "sky_claw.app.security.agent_guardrail",
    "GovernanceManager": "sky_claw.app.security.governance",
    "HITLGuard": "sky_claw.app.security.hitl",
    "AgenticLoopGuardrail": "sky_claw.app.security.loop_guardrail",
    "SecurityMetacognition": "sky_claw.app.security.metacognitive_logic",
    "audit_resource": "sky_claw.app.security.metacognitive_logic",
    "NetworkGateway": "sky_claw.app.security.network_gateway",
    "PathValidator": "sky_claw.app.security.path_validator",
    "sandboxed_io": "sky_claw.app.security.path_validator",
    "PromptArmor": "sky_claw.app.security.prompt_armor",
    "PromptArmorConfig": "sky_claw.app.security.prompt_armor",
    "build_system_header": "sky_claw.app.security.prompt_armor",
    "encapsulate_external_data": "sky_claw.app.security.prompt_armor",
    "validate_prompt_integrity": "sky_claw.app.security.prompt_armor",
    "PurpleScanner": "sky_claw.app.security.purple_scanner",
    "run_scan": "sky_claw.app.security.purple_scanner",
    "safe_json_loads": "sky_claw.app.security.sanitize",
    "sanitize_for_prompt": "sky_claw.app.security.sanitize",
    "TextInspector": "sky_claw.app.security.text_inspector",
    "scan_text": "sky_claw.app.security.text_inspector",
}

__all__ = [
    # Titan v7.0 guardrail
    "AgentGuardrail",
    "AgentGuardrailConfig",
    "AgenticLoopGuardrail",
    "GovernanceManager",
    "HITLGuard",
    "NetworkGateway",
    "PathValidator",
    "PromptArmor",
    "PromptArmorConfig",
    "PurpleScanner",
    "SecurityMetacognition",
    "TextInspector",
    "audit_resource",
    "build_system_header",
    "encapsulate_external_data",
    "run_scan",
    "safe_json_loads",
    "sandboxed_io",
    "sanitize_for_prompt",
    "scan_text",
    "secure_llm_call",
    "validate_prompt_integrity",
]


def __getattr__(name: str) -> Any:
    """Carga perezosa por submódulo para que ``path_validator`` no importe ``config``/``keyring``."""
    module_name = _LAZY_SUBMODULES.get(name)
    if module_name is not None:
        module = importlib.import_module(module_name)
        return getattr(module, name)
    msg = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(msg)
