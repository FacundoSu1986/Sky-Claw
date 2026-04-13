"""Core package for Sky-Claw daemon components."""

from .models import CircuitBreakerTripped, WSLInteropError
from .database import DatabaseAgent
from .schemas import (
    ModMetadata,
    ScrapingQuery,
    SecurityAuditRequest,
    SecurityAuditResponse,
    AgentToolRequest,
    AgentToolResponse,
    RouteClassification,
)
from .contracts import (
    validate_input,
    validate_output,
    validate_contract,
    get_contract_schema,
    get_schema_class,
    list_registered_schemas,
    verify_contract,
)
from .event_bus import CoreEventBus, Event
from .event_payloads import (
    ModlistChangedPayload,
    SynthesisPipelineStartedPayload,
    SynthesisPipelineCompletedPayload,
)
from .path_resolver import PathResolver, PathResolutionService
from .validators import (
    SSRFValidator,
    SSRFValidationResult,
    validate_url_ssrf,
    PathTraversalValidator,
    PathValidationResult,
    validate_path_traversal,
    validate_path_strict,
)

__all__ = [
    # Excepciones
    "CircuitBreakerTripped",
    "WSLInteropError",
    # Database
    "DatabaseAgent",
    # Schemas
    "ModMetadata",
    "ScrapingQuery",
    "SecurityAuditRequest",
    "SecurityAuditResponse",
    "AgentToolRequest",
    "AgentToolResponse",
    "RouteClassification",
    # Contracts
    "validate_input",
    "validate_output",
    "validate_contract",
    "get_contract_schema",
    "get_schema_class",
    "list_registered_schemas",
    "verify_contract",
    # Event Bus
    "CoreEventBus",
    "Event",
    "ModlistChangedPayload",
    "SynthesisPipelineStartedPayload",
    "SynthesisPipelineCompletedPayload",
    # Path Resolution
    "PathResolver",
    "PathResolutionService",
    # Validators
    "SSRFValidator",
    "SSRFValidationResult",
    "validate_url_ssrf",
    "PathTraversalValidator",
    "PathValidationResult",
    "validate_path_traversal",
    "validate_path_strict",
]
