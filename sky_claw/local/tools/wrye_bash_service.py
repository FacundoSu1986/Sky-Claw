"""WryeBashPipelineService — generación del Bashed Patch bajo lock.

Extracción Strangler-Fig (PR A de la caja negra de Wrye Bash). La lógica del ritual
vivía en :meth:`SupervisorAgent.execute_wrye_bash_pipeline`: Wrye Bash era el **único**
ritual mutante que NO estaba serializado (sin :class:`SnapshotTransactionLock`) ni tenía
un servicio propio como sus hermanos (LOOT/xEdit/Synthesis/DynDOLOD/Pandora). Este
servicio cierra ese hueco de concurrencia: expone la corrida real bajo el lock
distribuido compartido, mientras el guard M-04 (compartido, expuesto también por la tool
``validate_plugin_limit``) se **inyecta** desde el supervisor en vez de vivir acá.

Espeja a :class:`~sky_claw.local.tools.pandora_service.PandoraPipelineService`:
construcción perezosa del runner desde el ``PathResolutionService`` y **snapshot
diferido** (``target_files=[]``) porque el archivo concreto que escribe Wrye Bash
(``Bashed Patch, 0.esp``) sale vía la VFS de MO2 (subproceso con ``cwd``) y su ubicación
real es dependiente del entorno. La protección que aplica con certeza ahora es la
*serialización*; el preflight brutal (PR B) y el ActionManifest/FlightReport (PR C, que
cablea el journal) quedan como follow-ups documentados.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from sky_claw.antigravity.db.locks import (
    DistributedLockManager,
    LockAcquisitionError,
    SnapshotTransactionLock,
)
from sky_claw.local.tools.wrye_bash_runner import (
    WryeBashConfig,
    WryeBashExecutionError,
    WryeBashRunner,
)

if TYPE_CHECKING:
    from sky_claw.antigravity.core.path_resolver import PathResolutionService
    from sky_claw.antigravity.db.snapshot_manager import FileSnapshotManager

logger = logging.getLogger(__name__)

#: Lock resource id para la generación del Bashed Patch. Espeja a Synthesis
#: (``Synthesis.esp``): cada ritual mutante serializa sobre su artefacto de salida.
BASHED_PATCH_RESOURCE_ID = "Bashed Patch, 0.esp"


class WryeBashPipelineService:
    """Corre Wrye Bash (generación del Bashed Patch) bajo el lock distribuido.

    Dependencias inyectadas (DI). ``wrye_bash_runner`` se construye perezosamente
    desde ``path_resolver`` en el primer uso porque las rutas de tools pueden no estar
    configuradas en construcción; también puede inyectarse directo para tests. El
    ``plugin_limit_guard`` (guard M-04 compartido) es opcional: si no se inyecta, no se
    valida el límite de plugins (comportamiento honesto — no hay gate que mienta verde).
    """

    RESOURCE_ID: str = BASHED_PATCH_RESOURCE_ID
    AGENT_ID: str = "wrye-bash-pipeline-service"

    def __init__(
        self,
        *,
        lock_manager: DistributedLockManager,
        snapshot_manager: FileSnapshotManager,
        path_resolver: PathResolutionService | None = None,
        wrye_bash_runner: WryeBashRunner | None = None,
        plugin_limit_guard: Callable[[str], Awaitable[dict[str, Any]]] | None = None,
    ) -> None:
        self._lock_manager = lock_manager
        self._snapshot_manager = snapshot_manager
        self._path_resolver = path_resolver
        self._wrye_bash_runner = wrye_bash_runner
        self._plugin_limit_guard = plugin_limit_guard

    def ensure_runner(self) -> WryeBashRunner:
        """Asegura el ``WryeBashRunner`` (construcción perezosa desde el resolver).

        Variables de entorno requeridas (vía el ``PathResolutionService``):
        ``SKYRIM_PATH``, ``MO2_PATH`` y ``WRYE_BASH_PATH``.

        Raises:
            WryeBashExecutionError: si faltan rutas o el ejecutable no existe.
        """
        if self._wrye_bash_runner is not None:
            return self._wrye_bash_runner

        if self._path_resolver is None:
            raise WryeBashExecutionError("Cannot initialize WryeBashRunner: no path_resolver configured")

        game_path = self._path_resolver.get_skyrim_path()
        mo2_path = self._path_resolver.get_mo2_path()
        wrye_bash_path = self._path_resolver.get_wrye_bash_path()

        if not game_path or not mo2_path or not wrye_bash_path:
            raise WryeBashExecutionError(
                "Cannot initialize WryeBashRunner: "
                "SKYRIM_PATH, MO2_PATH, and WRYE_BASH_PATH environment variables must be valid paths"
            )

        if not wrye_bash_path.exists():
            raise WryeBashExecutionError(f"Wrye Bash executable not found: {wrye_bash_path}")

        config = WryeBashConfig(
            wrye_bash_path=wrye_bash_path,
            game_path=game_path,
            mo2_path=mo2_path,
        )
        self._wrye_bash_runner = WryeBashRunner(config)

        logger.info(
            "WryeBashRunner inicializado: game=%s, bash=%s",
            game_path,
            wrye_bash_path,
        )
        return self._wrye_bash_runner

    async def execute_pipeline(
        self,
        *,
        profile: str,
        validate_limit: bool = True,
    ) -> dict[str, Any]:
        """Genera el Bashed Patch con Wrye Bash bajo el lock de behavior/load-order.

        Flujo:
        1. [M-04] Validación de límite de plugins (guard compartido inyectado).
        2. Ejecutar ``WryeBashRunner.generate_bashed_patch()`` **bajo el lock**.
        3. Observabilidad vía logging estructurado.

        Siempre devuelve un ``dict`` serializable para los modos de fallo conocidos
        (guard M-04, runner no disponible, contención de lock, error de ejecución) en
        vez de propagar la excepción, para que el dispatcher lo reenvíe verbatim.
        """
        logger.info(
            "[FASE-6] Iniciando generación de Bashed Patch para perfil '%s'.",
            profile,
        )

        # PASO 0: Gate preventivo M-04 — guard compartido inyectado por el supervisor.
        if validate_limit and self._plugin_limit_guard is not None:
            guard_result = await self._plugin_limit_guard(profile)
            if not guard_result.get("valid", True):
                logger.error(
                    "[FASE-6] Abortando Bashed Patch: validación M-04 falló. %s",
                    guard_result.get("error"),
                )
                return {
                    "success": False,
                    "aborted_by": "plugin_limit_guard",
                    "plugin_count": guard_result.get("plugin_count"),
                    "error": guard_result.get("error"),
                }

        # PASO 1: Asegurar runner inicializado.
        try:
            runner = self.ensure_runner()
        except WryeBashExecutionError as exc:
            logger.error("[FASE-6] Error inicializando WryeBashRunner: %s", exc)
            return {"success": False, "error": str(exc)}

        # PASO 2: Ejecutar la generación BAJO el lock distribuido — Wrye Bash era el
        # único ritual mutante sin serializar (hueco de concurrencia). Snapshot
        # diferido: la salida ('Bashed Patch, 0.esp') sale vía la VFS de MO2 con cwd,
        # env-dependiente; la garantía que aplica con certeza ahora es la serialización.
        try:
            async with SnapshotTransactionLock(
                lock_manager=self._lock_manager,
                snapshot_manager=self._snapshot_manager,
                resource_id=self.RESOURCE_ID,
                agent_id=self.AGENT_ID,
                target_files=[],  # snapshot diferido — ver docstring del módulo
                metadata={"source": "wrye_bash_bashed_patch", "profile": profile},
            ):
                result = await runner.generate_bashed_patch()
        except LockAcquisitionError as exc:
            logger.warning("Lock contention on '%s': %s", self.RESOURCE_ID, exc)
            detail = f"Could not acquire bashed-patch lock '{self.RESOURCE_ID}': {exc}"
            return {"success": False, "error": detail}
        except WryeBashExecutionError as exc:
            logger.error("[FASE-6] WryeBashExecutionError: %s", exc)
            return {"success": False, "error": str(exc)}

        # PASO 3: Observabilidad vía logging estructurado. Este flujo AÚN no usa
        # OperationJournal; el ActionManifest/FlightReport llega en el PR C.
        logger.info(
            "[FASE-6] Bashed Patch result logged",
            extra={
                "agent_id": "wrye_bash_runner",
                "operation_type": "bashed_patch_generation",
                "file_path": "Bashed Patch, 0.esp",
                "success": result.success,
                "return_code": result.return_code,
                "duration_seconds": result.duration_seconds,
                "profile": profile,
            },
        )

        if result.success:
            logger.info(
                "[FASE-6] Bashed Patch generado exitosamente en %.1fs.",
                result.duration_seconds,
            )
        else:
            logger.error(
                "[FASE-6] Wrye Bash retornó código %d. stderr: %s",
                result.return_code,
                result.stderr[:500],
            )

        return {
            "success": result.success,
            "return_code": result.return_code,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "duration_seconds": result.duration_seconds,
        }
