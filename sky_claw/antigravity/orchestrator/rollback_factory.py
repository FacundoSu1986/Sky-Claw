"""SUP-04: Factory para componentes de rollback.

Extrae la creación de ``OperationJournal``, ``FileSnapshotManager``,
``RollbackManager``, ``DistributedLockManager`` y ``PathValidator``
a un factory method para desacoplar ``SupervisorAgent`` de la
construcción concreta de componentes de resiliencia.

Principio aplicado: Single Responsibility — el supervisor delega
la construcción de dependencias al factory.
"""

from __future__ import annotations

import logging
import pathlib
from dataclasses import dataclass

from sky_claw.antigravity.db.journal import OperationJournal
from sky_claw.antigravity.db.locks import DistributedLockManager
from sky_claw.antigravity.db.rollback_manager import RollbackManager
from sky_claw.antigravity.db.snapshot_manager import FileSnapshotManager
from sky_claw.antigravity.orchestrator.maintenance_daemon import get_max_backup_size_mb
from sky_claw.antigravity.security.path_validator import PathValidator

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RollbackComponents:
    """Contenedor inmutable para los componentes de resiliencia."""

    journal: OperationJournal
    snapshot_manager: FileSnapshotManager
    rollback_manager: RollbackManager
    lock_manager: DistributedLockManager
    path_validator: PathValidator


def create_rollback_components(
    backup_dir: str | pathlib.Path,
) -> RollbackComponents:
    """Factory que crea y retorna todos los componentes de rollback.

    Args:
        backup_dir: Directorio base para backups y snapshots.

    Returns:
        ``RollbackComponents`` con todas las instancias inicializadas.
    """
    backup_path = pathlib.Path(backup_dir)
    backup_path.mkdir(parents=True, exist_ok=True)

    journal = OperationJournal(db_path=backup_path / "journal.db")
    snapshot_manager = FileSnapshotManager(
        snapshot_dir=backup_path / "snapshots",
        max_size_mb=get_max_backup_size_mb(),
    )
    rollback_manager = RollbackManager(
        journal=journal,
        snapshot_manager=snapshot_manager,
    )
    lock_manager = DistributedLockManager(
        db_path=backup_path / "locks.db",
    )
    path_validator = PathValidator(roots=[backup_path])

    logger.info(
        "Componentes de rollback inicializados (factory): backup_dir=%s, max_size_mb=%d",
        backup_path,
        get_max_backup_size_mb(),
    )

    return RollbackComponents(
        journal=journal,
        snapshot_manager=snapshot_manager,
        rollback_manager=rollback_manager,
        lock_manager=lock_manager,
        path_validator=path_validator,
    )
