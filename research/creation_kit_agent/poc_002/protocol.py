"""
Protocolo tipado, DTOs inmutables y validación centralizada de identificadores.
Resuelve P0-9: operation_id tratado estrictamente como identificador alfanumérico cerrado, no como path.
"""
from dataclasses import dataclass, field
from enum import Enum
import re
import time
from typing import Any, Dict, Optional, Tuple

from exceptions import PolicyViolationError

# Política cerrada de operation_id: 1 a 64 caracteres [A-Za-z0-9][A-Za-z0-9._-]*
OPERATION_ID_REGEX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def validate_operation_id(operation_id: str) -> str:
    """
    Valida que un operation_id cumpla con el formato estricto de identificador.
    Rechaza separadores de ruta ('/', '\\'), secuencias '..', ':' y caracteres no autorizados.
    """
    if not isinstance(operation_id, str):
        raise PolicyViolationError(f"operation_id debe ser str, obtenido: {type(operation_id).__name__}")

    if not OPERATION_ID_REGEX.match(operation_id) or ".." in operation_id:
        raise PolicyViolationError(f"operation_id inválido o potencialmente peligroso: {operation_id!r}")

    return operation_id


class EvidenceLevel(Enum):
    E0_PLAN_VALID = "E0_PLAN_VALID"
    E1_WORKER_COMPLETED = "E1_WORKER_COMPLETED"
    E2_REOPENED_ASSERTIONS_PASS = "E2_REOPENED_ASSERTIONS_PASS"
    E3_STATIC_VALIDATION_PASS = "E3_STATIC_VALIDATION_PASS"
    E4_HITL_APPROVED = "E4_HITL_APPROVED"
    E5_RUNTIME_VERIFIED = "E5_RUNTIME_VERIFIED"


class RiskLevel(Enum):
    LEVEL_0_READONLY = 0
    LEVEL_1_SIMPLE_WRITE = 1
    LEVEL_2_INTERDEPENDENT = 2
    LEVEL_3_COMPLEX_STRUCTURAL = 3


class ClosedOperation(Enum):
    """Allowlist exhaustiva. Cualquier otra se rechaza fail-closed."""
    INSPECT_HEADER = "INSPECT_HEADER"
    FIND_RECORD = "FIND_RECORD"
    CREATE_MISC_ITEM = "CREATE_MISC_ITEM"
    CREATE_WEAPON = "CREATE_WEAPON"
    CREATE_ARMOR = "CREATE_ARMOR"
    CREATE_RECIPE = "CREATE_RECIPE"
    EDIT_LEVELED_LIST = "EDIT_LEVELED_LIST"
    COMPILE_PAPYRUS = "COMPILE_PAPYRUS"
    ATTACH_SCRIPT = "ATTACH_SCRIPT"
    VALIDATE_PLUGIN = "VALIDATE_PLUGIN"


class ProtocolStatus(Enum):
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    ABORTED = "ABORTED"
    UNSUPPORTED_CAPABILITY = "UNSUPPORTED_CAPABILITY"
    POLICY_VIOLATION = "POLICY_VIOLATION"


@dataclass(frozen=True)
class ModPlanOperation:
    operation_id: str
    kind: ClosedOperation
    payload: Tuple[Tuple[str, Any], ...]
    target_plugin: str

    @classmethod
    def create(cls, operation_id: str, kind: ClosedOperation, payload: Dict[str, Any], target_plugin: str) -> "ModPlanOperation":
        # Defensa en profundidad: validación inmediata al instanciar
        validated_id = validate_operation_id(operation_id)
        return cls(
            operation_id=validated_id,
            kind=kind,
            payload=tuple(sorted(payload.items())),
            target_plugin=target_plugin
        )

    def get_payload_dict(self) -> Dict[str, Any]:
        return dict(self.payload)


@dataclass(frozen=True)
class ModPlan:
    plan_id: str
    description: str
    operations: Tuple[ModPlanOperation, ...]


@dataclass(frozen=True)
class AssertionResult:
    check_type: str
    expected: Any
    actual: Any
    passed: bool
    details: str = ""


@dataclass(frozen=True)
class OperationReceipt:
    operation_id: str
    operation: ClosedOperation
    backend: str
    status: ProtocolStatus
    evidence_level: EvidenceLevel
    input_sha256: Optional[str]
    output_sha256: Optional[str]
    assertions: Tuple[AssertionResult, ...] = field(default_factory=tuple)
    warnings: Tuple[str, ...] = field(default_factory=tuple)
    error_message: Optional[str] = None
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "protocol_version": 1,
            "operation_id": self.operation_id,
            "operation": self.operation.value,
            "backend": self.backend,
            "status": self.status.value,
            "evidence_level": self.evidence_level.value,
            "input_sha256": self.input_sha256,
            "output_sha256": self.output_sha256,
            "assertions": [
                {
                    "check_type": a.check_type,
                    "expected": a.expected,
                    "actual": a.actual,
                    "passed": a.passed,
                    "details": a.details
                }
                for a in self.assertions
            ],
            "warnings": list(self.warnings),
            "error_message": self.error_message,
            "timestamp": self.timestamp
        }
