"""Core package for Sky-Claw daemon components."""

from .async_path_resolver import AsyncPathResolutionError, AsyncPathResolver
from .contracts import (
    get_contract_schema,
    get_schema_class,
    list_registered_schemas,
    validate_contract,
    validate_input,
    validate_output,
    verify_contract,
)
from .database import DatabaseAgent
from .dlq_manager import DLQManager, DLQRow
from .event_bus import (
    OPS_PROCESS_CHANGE_TOPIC,
    OPS_SYSTEM_LOG_TOPIC,
    OPS_TELEMETRY_TOPIC,
    CoreEventBus,
    Event,
    create_bus_with_dlq,
)
from .event_payloads import (
    ModlistChangedPayload,
    OpsProcessChangePayload,
    OpsSystemLogPayload,
    OpsTelemetryPayload,
    SynthesisPipelineCompletedPayload,
    SynthesisPipelineStartedPayload,
)
from .models import CircuitBreakerTrippedError, WSLInteropError
from .path_resolver import PathResolutionService, PathResolver
from .schemas import (
    AgentToolRequest,
    AgentToolResponse,
    ModMetadata,
    RouteClassification,
    ScrapingQuery,
    SecurityAuditRequest,
    SecurityAuditResponse,
)
from .validators import (
    PathTraversalValidator,
    PathValidationResult,
    SSRFValidationResult,
    SSRFValidator,
    validate_path_strict,
    validate_path_traversal,
    validate_url_ssrf,
)

__all__ = [
    "AgentToolRequest",
    "AgentToolResponse",
    # Async Path Resolver (primitiva de infraestructura no bloqueante)
    "AsyncPathResolutionError",
    "AsyncPathResolver",
    # Excepciones
    "CircuitBreakerTrippedError",
    # Event Bus + DLQ
    "CoreEventBus",
    "OPS_PROCESS_CHANGE_TOPIC",
    "OPS_SYSTEM_LOG_TOPIC",
    "OPS_TELEMETRY_TOPIC",
    "create_bus_with_dlq",
    # Database
    "DatabaseAgent",
    # DLQ
    "DLQManager",
    "DLQRow",
    "Event",
    # Schemas
    "ModMetadata",
    "ModlistChangedPayload",
    # Ops payloads (GUI-facing, Fase 6)
    "OpsProcessChangePayload",
    "OpsSystemLogPayload",
    "OpsTelemetryPayload",
    "PathResolutionService",
    # Path Resolution
    "PathResolver",
    "PathTraversalValidator",
    "PathValidationResult",
    "RouteClassification",
    "SSRFValidationResult",
    # Validators
    "SSRFValidator",
    "ScrapingQuery",
    "SecurityAuditRequest",
    "SecurityAuditResponse",
    "SynthesisPipelineCompletedPayload",
    "SynthesisPipelineStartedPayload",
    "WSLInteropError",
    "get_contract_schema",
    "get_schema_class",
    "list_registered_schemas",
    "validate_contract",
    # Contracts
    "validate_input",
    "validate_output",
    "validate_path_strict",
    "validate_path_traversal",
    "validate_url_ssrf",
    "verify_contract",
]
