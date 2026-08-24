"""
Capability Registry según ADR-001 (Secciones 6, 8, 9).
Refleja la realidad del software: para POC-002 solo INSPECT_HEADER está SUPPORTED.
"""
from dataclasses import dataclass
from enum import Enum
from typing import Dict, Optional

from exceptions import UnsupportedCapabilityError
from protocol import ClosedOperation, RiskLevel


class CapabilityStatus(Enum):
    SUPPORTED = "SUPPORTED"
    EXPERIMENTAL = "EXPERIMENTAL"
    UNSUPPORTED = "UNSUPPORTED"
    DISABLED = "DISABLED"


@dataclass(frozen=True)
class CapabilityEntry:
    operation: ClosedOperation
    backend: str
    status: CapabilityStatus
    risk: RiskLevel


class CapabilityRegistry:
    def __init__(self):
        self._registry: Dict[ClosedOperation, CapabilityEntry] = {}
        self._init_defaults()

    def _init_defaults(self):
        # ÚNICA capability soportada en POC-002
        self._register(
            ClosedOperation.INSPECT_HEADER,
            "plugin-worker",
            CapabilityStatus.SUPPORTED,
            RiskLevel.LEVEL_0_READONLY
        )

        # Todas las demás deshabilitadas explícitamente sin stubs ni workers ficticios
        disabled_ops = [
            (ClosedOperation.FIND_RECORD, "plugin-worker", RiskLevel.LEVEL_0_READONLY),
            (ClosedOperation.CREATE_MISC_ITEM, "plugin-worker", RiskLevel.LEVEL_1_SIMPLE_WRITE),
            (ClosedOperation.CREATE_WEAPON, "plugin-worker", RiskLevel.LEVEL_1_SIMPLE_WRITE),
            (ClosedOperation.CREATE_ARMOR, "plugin-worker", RiskLevel.LEVEL_1_SIMPLE_WRITE),
            (ClosedOperation.CREATE_RECIPE, "plugin-worker", RiskLevel.LEVEL_1_SIMPLE_WRITE),
            (ClosedOperation.EDIT_LEVELED_LIST, "plugin-worker", RiskLevel.LEVEL_2_INTERDEPENDENT),
            (ClosedOperation.COMPILE_PAPYRUS, "papyrus-worker", RiskLevel.LEVEL_2_INTERDEPENDENT),
            (ClosedOperation.ATTACH_SCRIPT, "plugin-worker", RiskLevel.LEVEL_2_INTERDEPENDENT),
            (ClosedOperation.VALIDATE_PLUGIN, "xedit-validator", RiskLevel.LEVEL_0_READONLY),
        ]

        for op, backend, risk in disabled_ops:
            self._register(op, backend, CapabilityStatus.DISABLED, risk)

    def _register(self, op: ClosedOperation, backend: str, status: CapabilityStatus, risk: RiskLevel):
        self._registry[op] = CapabilityEntry(op, backend, status, risk)

    def get(self, op: ClosedOperation) -> Optional[CapabilityEntry]:
        return self._registry.get(op)


class CapabilityRouter:
    """Enrutador determinista. Falla fail-closed si no está en estado SUPPORTED."""
    def __init__(self, registry: CapabilityRegistry):
        self.registry = registry

    def resolve_backend(self, op: ClosedOperation) -> str:
        entry = self.registry.get(op)
        if not entry:
            raise UnsupportedCapabilityError(f"Operación no registrada: {op}")
        if entry.status != CapabilityStatus.SUPPORTED:
            raise UnsupportedCapabilityError(
                f"Capacidad '{op.value}' no está en estado SUPPORTED (estado actual: {entry.status.value})."
            )
        return entry.backend
