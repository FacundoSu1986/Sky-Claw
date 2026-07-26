"""Productor puro del :class:`ActionManifest` (T-26, ADR 0002).

Ensambla el manifiesto de un Ritual mutante a partir de datos que el servicio
ya tiene en mano — nombre + versión de herramienta, los ``target_files``
resueltos, y los snapshots capturados por ``SnapshotTransactionLock`` — sin
ningún I/O. Se aísla de la persistencia (journal) y del wiring del servicio
para poder testearlo sin subprocesos ni DB.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sky_claw.app.orchestrator.preview.manifest import (
    ActionManifest,
    ConflictPair,
    LoadOrderDiff,
    RollbackStep,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sky_claw.app.db.snapshot_manager import SnapshotInfo

__all__ = ["ActionManifest", "RollbackStep", "build_action_manifest"]


def build_action_manifest(
    *,
    ritual_id: str,
    tool: str,
    tool_version: str | None,
    target_files: Sequence[str],
    snapshots: Sequence[SnapshotInfo],
    records_forwarded: Sequence[ConflictPair] | None = None,
    load_order_diff: LoadOrderDiff | None = None,
    summary: str | None = None,
) -> ActionManifest:
    """Arma un :class:`ActionManifest` desde los datos de un Ritual mutante.

    Args:
        ritual_id: Identificador del Ritual (ej. ``"loot-sort-<tx>"``).
        tool: Nombre de la herramienta que muta (ej. ``"LOOT"``).
        tool_version: Versión detectada, o ``None`` si no se pudo determinar.
        target_files: Archivos que el Ritual tocará (los mismos del snapshot).
        snapshots: ``SnapshotInfo`` capturados por el lock; la función traduce
            cada uno a un :class:`RollbackStep` del plan de rollback (qué
            snapshot restaura qué archivo). Puede ir vacío (entorno no
            resoluble): el manifiesto sigue siendo válido, con rollback vacío.
        records_forwarded: Records/plugins forwardeados (opcional).
        load_order_diff: Diff de orden de carga si aplica (opcional).
        summary: Resumen legible opcional.

    Returns:
        Un ``ActionManifest`` listo para persistir y para el approval gate.
    """
    rollback_plan = [
        RollbackStep(
            original_path=snap.original_path,
            snapshot_path=snap.snapshot_path,
            snapshot_id=snap.snapshot_id,
        )
        for snap in snapshots
    ]
    return ActionManifest(
        ritual_id=ritual_id,
        tool=tool,
        tool_version=tool_version,
        files_touched=list(target_files),
        records_forwarded=list(records_forwarded) if records_forwarded else [],
        load_order_diff=load_order_diff,
        rollback_plan=rollback_plan,
        summary=summary,
    )
