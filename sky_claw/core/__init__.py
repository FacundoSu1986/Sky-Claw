"""Core package for Sky-Claw daemon components."""

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
from .event_bus import CoreEventBus, Event
from .event_payloads import (
    ModlistChangedPayload,
    SynthesisPipelineCompletedPayload,
    SynthesisPipelineStartedPayload,
)
from .models import CircuitBreakerTripped, WSLInteropError
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
    # Excepciones
    "CircuitBreakerTripped",
    # Event Bus
    "CoreEventBus",
    # Database
    "DatabaseAgent",
    "Event",
    # Schemas
    "ModMetadata",
    "ModlistChangedPayload",
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
