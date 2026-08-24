"""
Jerarquía de excepciones controladas y tipadas (ADR-001 / Threat Model).
"""

class SkyrimAgentError(Exception):
    """Excepción base del sistema."""
    pass


class PathContainmentError(SkyrimAgentError):
    """Intento de escape del workspace o path no seguro (traversal, drive, UNC)."""
    pass


class WorkspaceViolationError(SkyrimAgentError):
    """Violación de invariante de workspace (ej: sobreescritura de receipt, mutación de original)."""
    pass


class MalformedRecordError(SkyrimAgentError):
    """Error de parsing binario: truncamiento, overflow, trailing bytes o firma inválida."""
    pass


class UnsupportedCapabilityError(SkyrimAgentError):
    """Operación solicitada no soportada o deshabilitada en el CapabilityRegistry."""
    pass


class PolicyViolationError(SkyrimAgentError):
    """ModPlan viola las restricciones estáticas de seguridad o unicidad de identificadores."""
    pass


class InvariantViolationError(SkyrimAgentError):
    """Violación crítica de invariante (ej: SHA del original alterado)."""
    pass
