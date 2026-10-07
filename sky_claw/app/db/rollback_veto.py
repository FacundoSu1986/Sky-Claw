"""Módulo centralizado para evaluar el veto de rollback por terminalidad desconocida.

Contrato operativo (issue #623 / ADR 0007 / ADR 0011):
Si un worker VFS o proceso mutante no confirmó salida terminal (timeout con bridge vivo,
falla de terminación de Job Object o teardown deadline vencido), la terminalidad es
desconocida. Revertir snapshots o restaurar directorios en ese estado puede pisar
mutaciones concurrentes de un worker que sigue en ejecución. Por tanto, el rollback
debe ser vetado fail-closed (el archivo/directorio permanece como quedó y el backup
queda en disco para recovery manual).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

TERMINALITY_UNKNOWN_ATTR = "terminality_unknown"
TEARDOWN_ERROR_ATTR = "teardown_error"


def mark_unknown_terminality(exc: BaseException, *, teardown_error: BaseException | None = None) -> None:
    """Marca una excepción con terminalidad desconocida y error de teardown opcional."""
    setattr(exc, TERMINALITY_UNKNOWN_ATTR, True)
    if teardown_error is not None:
        setattr(exc, TEARDOWN_ERROR_ATTR, teardown_error)


def exception_forbids_rollback(exc: BaseException | None) -> bool:
    """Determina si una excepción (o su cadena causal) veta el rollback."""
    if exc is None:
        return False

    # Evitamos import circular en módulo de db
    from sky_claw.local.mo2.vfs_broker import VfsTeardownError

    curr: BaseException | None = exc
    visited: set[int] = set()
    depth = 0

    while curr is not None and depth < 32 and id(curr) not in visited:
        visited.add(id(curr))
        depth += 1

        if getattr(curr, TERMINALITY_UNKNOWN_ATTR, False) is True:
            return True

        if isinstance(curr, VfsTeardownError):
            return True

        if getattr(curr, TEARDOWN_ERROR_ATTR, None) is not None:
            return True

        curr = curr.__cause__ or curr.__context__

    return False
