"""Security layer – network egress control, path sandboxing, HITL, sanitization."""

# Zero-Trust LLM guardrail middleware (Titan v7.0)
from sky_claw.antigravity.security.agent_guardrail import (
    AgentGuardrail,
    AgentGuardrailConfig,
    secure_llm_call,
)
from sky_claw.antigravity.security.governance import GovernanceManager
from sky_claw.antigravity.security.hitl import HITLGuard
from sky_claw.antigravity.security.loop_guardrail import AgenticLoopGuardrail
from sky_claw.antigravity.security.metacognitive_logic import SecurityMetacognition, audit_resource
from sky_claw.antigravity.security.network_gateway import NetworkGateway
from sky_claw.antigravity.security.path_validator import PathValidator, sandboxed_io

# FASE 1.5.1: Prompt Armor — semantic hardening against file-based injection
from sky_claw.antigravity.security.prompt_armor import (
    PromptArmor,
    PromptArmorConfig,
    build_system_header,
    encapsulate_external_data,
    validate_prompt_integrity,
)

# Nuevos componentes de ciberseguridad avanzada (v5.5 Titan)
from sky_claw.antigravity.security.purple_scanner import PurpleScanner, run_scan
from sky_claw.antigravity.security.sanitize import safe_json_loads, sanitize_for_prompt
from sky_claw.antigravity.security.text_inspector import TextInspector, scan_text

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
