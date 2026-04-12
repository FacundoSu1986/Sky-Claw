"""Security layer – network egress control, path sandboxing, HITL, sanitization."""

from sky_claw.security.network_gateway import NetworkGateway
from sky_claw.security.path_validator import PathValidator, sandboxed_io
from sky_claw.security.hitl import HITLGuard
from sky_claw.security.sanitize import sanitize_for_prompt, safe_json_loads

# Nuevos componentes de ciberseguridad avanzada (v5.5 Titan)
from sky_claw.security.purple_scanner import PurpleScanner, run_scan
from sky_claw.security.text_inspector import TextInspector, scan_text
from sky_claw.security.governance import GovernanceManager
from sky_claw.security.metacognitive_logic import SecurityMetacognition, audit_resource

__all__ = [
    "NetworkGateway",
    "PathValidator",
    "sandboxed_io",
    "HITLGuard",
    "sanitize_for_prompt",
    "safe_json_loads",
    "PurpleScanner",
    "run_scan",
    "TextInspector",
    "scan_text",
    "GovernanceManager",
    "SecurityMetacognition",
    "audit_resource",
]
