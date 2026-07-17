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
import pathlib
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
    from sky_claw.antigravity.core.path_resolver import PathResolutionService
    from sky_claw.antigravity.db.snapshot_manager import FileSnapshotManager
    from sky_claw.local.validators.preflight import PreflightReport, PreflightService

logger = logging.getLogger(__name__)

#: Lock resource id compartido para la regeneración de behavior graphs de Pandora.
BEHAVIOR_GRAPHS_RESOURCE_ID = "behavior-graphs"

#: Nombre del dir de salida de Pandora en setups standalone (junto al exe).
_PANDORA_OUTPUT_DIR = "Pandora_Output"


def _attach_preflight(result: dict[str, Any], report: PreflightReport | None) -> dict[str, Any]:
    """Adjunta el reporte de preflight al ``result`` cuando no está verde.

    Mismo criterio que ``loot_service``/``xedit_service``/``synthesis_service``/
    ``dyndolod_service`` (T-16b/T-16c): un semáforo verde no ensucia el dict;
    amarillo/rojo viajan como ``result["preflight"]`` para que el panel lo renderice.
    """
    if report is not None and report.status.value != "green":
        result["preflight"] = report.to_dict()
    return result


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
        preflight: PreflightService | None = None,
    ) -> None:
        self._lock_manager = lock_manager
        self._snapshot_manager = snapshot_manager
        self._path_resolver = path_resolver
        self._pandora_exe = pandora_exe
        self._pandora_runner = pandora_runner
        # Preflight inyectable (tests) o construido perezosamente en el primer uso.
        self._preflight = preflight

    def _resolve_pandora_paths(
        self,
    ) -> tuple[pathlib.Path | None, pathlib.Path | None, pathlib.Path | None, pathlib.Path | None, pathlib.Path | None]:
        """Reúne ``(game, mo2, exe, raw_game, raw_mo2)`` desde el ``path_resolver``
        **O** el config del runner inyectado (review Codex #314).

        El agent tool (``system_tools.run_pandora``) construye el servicio con un
        ``PandoraRunner`` pero SIN resolver — el gate no debe desactivarse por eso,
        así que si falta el resolver se deriva ``game``/``exe`` del ``config`` del
        runner. MO2 solo lo conoce el resolver (el config de Pandora no lo tiene).
        """
        game = mo2 = exe = raw_game = raw_mo2 = None
        resolver = self._path_resolver
        if resolver is not None:
            g = resolver.get_skyrim_path()
            game = g if isinstance(g, pathlib.Path) else None
            m = resolver.get_mo2_path()
            mo2 = m if isinstance(m, pathlib.Path) else None
            rg = resolver.get_skyrim_path_raw()
            raw_game = rg if isinstance(rg, pathlib.Path) else None
            rm = resolver.get_mo2_path_raw()
            raw_mo2 = rm if isinstance(rm, pathlib.Path) else None
            e = resolver.get_pandora_exe()
            exe = e if isinstance(e, pathlib.Path) else None
        runner = self._pandora_runner
        if runner is not None:
            cfg = getattr(runner, "config", None)
            cfg_game = getattr(cfg, "game_path", None)
            cfg_exe = getattr(cfg, "pandora_exe", None)
            if game is None and isinstance(cfg_game, pathlib.Path):
                game = cfg_game
            if exe is None and isinstance(cfg_exe, pathlib.Path):
                exe = cfg_exe
        if self._pandora_exe is not None:
            exe = self._pandora_exe
        # Fallback de raw: sin resolver (runner inyectado) no hay rutas crudas; usar
        # las resueltas es mejor que no cablear vfs (detecta symlinks en la ruta).
        raw_game = raw_game or game
        raw_mo2 = raw_mo2 or mo2
        return game, mo2, exe, raw_game, raw_mo2

    def _ensure_preflight(self) -> PreflightService | None:
        """Construye perezosamente el preflight de Pandora (T-16c·4, STAGE 4).

        Pandora regenera los behavior graphs (animaciones/IA). Sensores relevantes:
        **permisos de escritura** sobre los dirs candidatos de salida (el destino
        exacto es dependiente del entorno — VFS de MO2 vs standalone — así que se
        sondean todos los resolubles), **symlinks/junctions** en las rutas crudas,
        y **overwrite sucio** (Pandora lee/escribe el overwrite; uno sucio hace el
        diff inatribuible). NO cablea masters/límites: Pandora procesa mods de
        ANIMACIÓN (estilo FNIS/Nemesis), no el load order de plugins.

        Se construye con lo que HAYA (review #314): basta game **o** exe **o** MO2
        resoluble (desde el resolver o el config del runner). El overwrite solo se
        cablea con MO2; en standalone (SKYRIM_PATH/PANDORA_EXE sin MO2_PATH) se
        omite ese sensor pero el gate igual protege ``Data`` y el output del exe.
        Sin NINGUNA raíz → ``None`` (sin gate, honesto).
        """
        if self._preflight is not None:
            return self._preflight

        game, mo2, exe, raw_game, raw_mo2 = self._resolve_pandora_paths()
        if game is None and mo2 is None and exe is None:
            return None

        # Imports perezosos (anti-ciclo: validators.preflight llega a tools._process).
        from sky_claw.local.validators.preflight import PreflightService
        from sky_claw.local.validators.preflight_sensors import build_overwrite_sensor, build_vfs_sensor
        from sky_claw.local.validators.write_permissions import WritePermissionsChecker

        # vfs sobre rutas CRUDAS (las resueltas ya siguieron los symlinks).
        vfs_checker = build_vfs_sensor(raw_game=raw_game, raw_mo2=raw_mo2, scan_mods_dir=False)

        # Permisos: targets recalculados POR CORRIDA dentro del closure (freshness).
        def _permissions() -> Any:
            return WritePermissionsChecker(targets=self._permission_targets()).check()

        # El overwrite solo aplica con MO2; sin MO2 se omite (no se inventa un
        # sensor sin fuente — review #314 F3).
        overwrite_check = build_overwrite_sensor(mo2 / "overwrite") if mo2 is not None else None

        self._preflight = PreflightService(
            vfs_checker=vfs_checker,
            permissions_check=_permissions,
            overwrite_check=overwrite_check,
            omit_unconfigured=True,
        )
        return self._preflight

    def _permission_targets(self) -> list[pathlib.Path]:
        """Rutas candidatas donde Pandora escribe los behavior graphs (review-hardened).

        El destino exacto es dependiente del entorno (lanzado vía el VFS de MO2 →
        el ``overwrite``; standalone → ``Data`` o un ``Pandora_Output`` junto al
        exe), así que se sondean todos los candidatos resolubles: el ``Data`` del
        juego, el ``overwrite`` de MO2, el dir del exe **y el ``Pandora_Output``
        concreto** (un output hijo read-only con el padre escribible pasaría
        inadvertido — review #314 F2). El ``WritePermissionsChecker`` se salta los
        inexistentes; se resuelve por corrida (freshness).
        """
        game, mo2, exe, _, _ = self._resolve_pandora_paths()
        candidates: list[pathlib.Path] = []
        if game is not None:
            candidates.append(game / "Data")
        if mo2 is not None:
            candidates.append(mo2 / "overwrite")
        if exe is not None:
            candidates.append(exe.parent)
            candidates.append(exe.parent / _PANDORA_OUTPUT_DIR)
        seen: set[pathlib.Path] = set()
        return [p for p in candidates if not (p in seen or seen.add(p))]

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
        # Preflight brutal ANTES de tocar nada (T-16c·4, STAGE 4): un semáforo ROJO
        # (p. ej. el dir de salida de behaviors sin permisos) cancela Pandora sin
        # correr el subproceso ni tomar el lock. Amarillo/verde no bloquean; el
        # reporte se surface al panel en todos los retornos.
        preflight = self._ensure_preflight()
        preflight_report: PreflightReport | None = None
        if preflight is not None:
            preflight_report = await preflight.run()
            if preflight_report.blocks_mutations:
                red = "; ".join(c.summary for c in preflight_report.checks if c.status.value == "red")
                logger.warning("Pandora (stage 4) bloqueado por preflight en rojo: %s", red)
                return {
                    "status": "error",
                    "success": False,
                    "reason": "PreflightBlocked",
                    "message": f"Preflight en rojo, Pandora cancelado: {red}",
                    "logs": red,
                    "preflight": preflight_report.to_dict(),
                }

        try:
            runner = self._ensure_runner()
        except PandoraExecutionError as exc:
            logger.error("Pandora runner unavailable: %s", exc)
            return _attach_preflight(
                {"status": "error", "success": False, "message": str(exc), "logs": str(exc)}, preflight_report
            )

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
            detail = f"Could not acquire behavior-graphs lock '{self.RESOURCE_ID}': {exc}"
            return _attach_preflight(
                {"status": "error", "success": False, "message": detail, "logs": detail}, preflight_report
            )
        except PandoraExecutionError as exc:
            logger.error("Pandora execution failed: %s", exc)
            return _attach_preflight(
                {"status": "error", "success": False, "message": str(exc), "logs": str(exc)}, preflight_report
            )

        # Contrato compartido (deuda #5): ``message`` canónico junto a los campos
        # estructurados; en éxito queda vacío (el consumidor arma su copy).
        message = "" if result.success else (result.stderr or result.stdout or "")
        return _attach_preflight(
            {
                "status": "success" if result.success else "error",
                "success": result.success,
                "message": message,
                "return_code": result.return_code,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "duration_seconds": result.duration_seconds,
            },
            preflight_report,
        )
