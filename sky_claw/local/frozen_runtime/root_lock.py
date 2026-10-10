"""Primitiva de exclusión interproceso e intraproceso por FrozenRuntimeRoot (P4-S1).

ADR 0012 §34.3 es la autoridad normativa:
- LOCK_RESOURCE = FrozenRuntimeRoot (exclusión única por root, no por destino).
- LOCK_IDENTITY = canonical root (normcase abspath) + session_id (UUID por adquisición).
- LOCK_EXCLUSION_SET = { PROMOTION, ROLLBACK, activation/rebinding,
  startup reconciliation, publish Generation, candidate build que escriba state/ }.
- LOCK_BUSY = fail-closed con timeout acotado; nunca espera infinita.
- LOCK_OWNER = { session_id, pid, process_create_time, acquired_at }.
- LOCK_LIVENESS = pid + process_create_time (nunca sólo PID ni os.kill(pid, 0)).
- LOCK_CRASH = el archivo de lock NO se borra al morir el proceso; persiste como
  evidencia forense de ownership para recuperación/clasificación.
"""

from __future__ import annotations

import dataclasses
import pathlib
from enum import StrEnum
from typing import Any

LOCK_FILE_NAME = "frozen-runtime.lock"
LOCK_SCHEMA_VERSION = 1
LOCK_METADATA_FIELDS = frozenset(
    {
        "schema_version",
        "canonical_root",
        "session_id",
        "pid",
        "process_create_time",
        "acquired_at",
    }
)


class OwnerLiveness(StrEnum):
    """Estado de vitalidad del dueño registrado en la metadata."""

    ALIVE = "ALIVE"
    DEAD = "DEAD"
    INDETERMINATE = "INDETERMINATE"


class LockDisposition(StrEnum):
    """Clasificación del estado observado del lock de un root."""

    FREE = "FREE"
    HELD_BY_LIVE_OWNER = "HELD_BY_LIVE_OWNER"
    ORPHANED = "ORPHANED"
    INDETERMINATE = "INDETERMINATE"
    CORRUPT_METADATA = "CORRUPT_METADATA"


@dataclasses.dataclass(frozen=True, slots=True)
class RootLockMetadata:
    """Metadata de ownership persistida en el lockfile.

    Esquema cerrado normativo con exactamente 6 campos (§34.3).
    """

    schema_version: int
    canonical_root: str
    session_id: str
    pid: int
    process_create_time: float
    acquired_at: float


@dataclasses.dataclass(frozen=True, slots=True)
class LockInspectionResult:
    """Resultado de la inspección no mutante de un root lock."""

    canonical_root: str
    lock_path: pathlib.Path
    disposition: LockDisposition
    is_os_locked: bool
    metadata: RootLockMetadata | None
    liveness: OwnerLiveness | None


class FrozenRuntimeRootLockHandle:
    """Handle de un lock adquirido por este proceso para un FrozenRuntimeRoot."""

    def __init__(
        self,
        *,
        root: pathlib.Path,
        canonical_root: str,
        lock_path: pathlib.Path,
        session_id: str,
        pid: int,
        process_create_time: float,
        acquired_at: float,
    ) -> None:
        self.root = root
        self.canonical_root = canonical_root
        self.lock_path = lock_path
        self.session_id = session_id
        self.pid = pid
        self.process_create_time = process_create_time
        self.acquired_at = acquired_at

    def assert_owned(self) -> None:
        """Verifica que este handle sigue siendo dueño legítimo del lock."""
        raise NotImplementedError("assert_owned pendiente de implementación")

    def release(self) -> None:
        """Libera la exclusión del lock de forma segura e idempotente."""
        raise NotImplementedError("release pendiente de implementación")

    def __enter__(self) -> FrozenRuntimeRootLockHandle:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.release()


def canonical_root_key(root: pathlib.Path | str) -> tuple[str, pathlib.Path]:
    """Obtiene la clave canónica y la ruta absoluta normalizada del root."""
    raise NotImplementedError("canonical_root_key pendiente de implementación")


def inspect_frozen_runtime_root_lock(root: pathlib.Path | str) -> LockInspectionResult:
    """Inspecciona el estado de exclusión y metadata del lock de un root sin mutarlo."""
    raise NotImplementedError("inspect_frozen_runtime_root_lock pendiente de implementación")


def acquire_frozen_runtime_root_lock(
    root: pathlib.Path | str,
    *,
    timeout: float = 0.0,
) -> FrozenRuntimeRootLockHandle:
    """Adquiere el lock exclusivo interproceso e intraproceso para FrozenRuntimeRoot."""
    raise NotImplementedError("acquire_frozen_runtime_root_lock pendiente de implementación")
