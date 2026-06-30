"""PandoraPipelineService — generación de animaciones (behavior graphs) bajo lock.

Follow-up A de la Fase 2. Pandora regenera los grafos de comportamiento del juego
(salida en el overwrite/MO2), estado serializable que el resto de los runners mutantes
(LOOT/xEdit/DynDOLOD) ya protegen con :class:`SnapshotTransactionLock`. Este servicio
expone la corrida real bajo el mismo lock distribuido.

Espeja deliberadamente a :class:`~sky_claw.local.tools.loot_service.LootSortingService`:
construcción perezosa del runner desde el ``PathResolutionService`` y **snapshot
diferido** (``target_files=[]``) porque el archivo concreto que reescribe Pandora es
dependiente del entorno (subproceso con ``cwd``) y no es resoluble con certeza hoy. La
protección que aplica con certeza ahora es la *serialización*.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from sky_claw.antigravity.db.locks import (
    DistributedLockManager,
    LockAcquisitionError,
    SnapshotTransactionLock,
)
from sky_claw.local.tools.pandora_runner import (
    PandoraConfig,
    PandoraExecutionError,
    PandoraRunner,
)

if TYPE_CHECKING:
    import pathlib

    from sky_claw.antigravity.core.path_resolver import PathResolutionService
    from sky_claw.antigravity.db.snapshot_manager import FileSnapshotManager

logger = logging.getLogger(__name__)

#: Lock resource id compartido para la regeneración de behavior graphs de Pandora.
BEHAVIOR_GRAPHS_RESOURCE_ID = "behavior-graphs"


class PandoraPipelineService:
    """Corre Pandora (generación de animaciones) bajo el lock distribuido compartido.

    Dependencias inyectadas (DI). ``pandora_runner`` se construye perezosamente desde
    ``path_resolver`` en el primer uso porque las rutas de tools pueden no estar
    configuradas en construcción; también puede inyectarse directo para tests.
    """

    RESOURCE_ID: str = BEHAVIOR_GRAPHS_RESOURCE_ID
    AGENT_ID: str = "pandora-pipeline-service"

    def __init__(
        self,
        *,
        lock_manager: DistributedLockManager,
        snapshot_manager: FileSnapshotManager,
        path_resolver: PathResolutionService | None = None,
        pandora_exe: pathlib.Path | None = None,
        pandora_runner: PandoraRunner | None = None,
    ) -> None:
        self._lock_manager = lock_manager
        self._snapshot_manager = snapshot_manager
        self._path_resolver = path_resolver
        self._pandora_exe = pandora_exe
        self._pandora_runner = pandora_runner

    def _ensure_runner(self) -> PandoraRunner:
        """Construye el :class:`PandoraRunner` resolviendo el exe + game path.

        El ejecutable se toma de (en orden) el ``pandora_exe`` inyectado o el resolver
        (``PANDORA_EXE``); el game path siempre del resolver (``SKYRIM_PATH``).

        Raises:
            PandoraExecutionError: Si faltan PANDORA_EXE o SKYRIM_PATH.
        """
        if self._pandora_runner is not None:
            return self._pandora_runner

        if self._path_resolver is None:
            raise PandoraExecutionError(
                "Cannot run Pandora: no pandora_runner injected and no path_resolver configured."
            )

        game_path = self._path_resolver.get_skyrim_path()
        if game_path is None:
            raise PandoraExecutionError("Cannot run Pandora: SKYRIM_PATH is not configured.")

        pandora_exe = self._pandora_exe or self._path_resolver.get_pandora_exe()
        if pandora_exe is None:
            raise PandoraExecutionError("Cannot run Pandora: PANDORA_EXE is not configured.")

        self._pandora_runner = PandoraRunner(
            PandoraConfig(pandora_exe=pandora_exe, game_path=game_path),
        )
        return self._pandora_runner

    async def generate_animations(self) -> dict[str, Any]:
        """Regenera las animaciones con Pandora bajo el lock de behavior-graphs.

        Siempre devuelve un ``dict`` serializable para los modos de fallo conocidos
        (runner no disponible, contención de lock, error de ejecución) en vez de
        propagar la excepción, para que el dispatcher lo reenvíe verbatim.
        """
        try:
            runner = self._ensure_runner()
        except PandoraExecutionError as exc:
            logger.error("Pandora runner unavailable: %s", exc)
            return {"status": "error", "success": False, "logs": str(exc)}

        try:
            async with SnapshotTransactionLock(
                lock_manager=self._lock_manager,
                snapshot_manager=self._snapshot_manager,
                resource_id=self.RESOURCE_ID,
                agent_id=self.AGENT_ID,
                target_files=[],  # snapshot diferido — ver docstring del módulo
                metadata={"source": "pandora_animations"},
            ):
                result = await runner.run_pandora()
        except LockAcquisitionError as exc:
            logger.warning("Lock contention on '%s': %s", self.RESOURCE_ID, exc)
            return {
                "status": "error",
                "success": False,
                "logs": f"Could not acquire behavior-graphs lock '{self.RESOURCE_ID}': {exc}",
            }
        except PandoraExecutionError as exc:
            logger.error("Pandora execution failed: %s", exc)
            return {"status": "error", "success": False, "logs": str(exc)}

        return {
            "status": "success" if result.success else "error",
            "success": result.success,
            "return_code": result.return_code,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "duration_seconds": result.duration_seconds,
        }
