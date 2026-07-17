"""DynDOLODPipelineService — servicio transaccional para generación de LODs.

Extrae la lógica de ``execute_dyndolod_pipeline`` desde ``supervisor.py``
hacia un servicio con inyección de dependencias, locking multi-recurso
y eventos de ciclo de vida.

Sprint 2, Fase 3: Strangler Fig — desacoplamiento de ``supervisor.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import pathlib
import time
from typing import TYPE_CHECKING, Any

from sky_claw.antigravity.core.event_bus import CoreEventBus, Event
from sky_claw.antigravity.core.event_payloads import (
    DynDOLODPipelineCompletedPayload,
    DynDOLODPipelineStartedPayload,
)
from sky_claw.antigravity.core.path_resolver import PathResolutionService
from sky_claw.antigravity.db.journal import OperationJournal
from sky_claw.antigravity.db.locks import (
    DistributedLockManager,
    LockAcquisitionError,
    SnapshotTransactionLock,
)
from sky_claw.antigravity.db.snapshot_manager import FileSnapshotManager
from sky_claw.local.tools._dir_rollback import DirectoryRollback
from sky_claw.local.tools.dyndolod_runner import (
    DynDOLODConfig,
    DynDOLODExecutionError,
    DynDOLODPipelineResult,
    DynDOLODRunner,
    DynDOLODTimeoutError,
)

if TYPE_CHECKING:
    from sky_claw.local.validators.preflight import PreflightReport, PreflightService

logger = logging.getLogger("SkyClaw.DynDOLODPipelineService")


def _attach_preflight(result: dict[str, Any], report: PreflightReport | None) -> dict[str, Any]:
    """Adjunta el reporte de preflight al ``result`` cuando no está verde.

    Mismo criterio que ``loot_service``/``xedit_service``/``synthesis_service``
    (T-16b/T-16c): un semáforo verde no ensucia el dict; amarillo/rojo viajan
    como ``result["preflight"]`` para que el panel vivo lo renderice.
    """
    if report is not None and report.status.value != "green":
        result["preflight"] = report.to_dict()
    return result


class _ActionManifestError(Exception):
    """Interno (T-26): la emisión del manifiesto de vuelo falló. Se lanza DENTRO
    del lock (antes de mutar) para que el Ritual NO proceda sin manifiesto — la
    caja negra no es opcional cuando el journal está cableado (espejo de
    ``loot_service``/``xedit_service``/``synthesis_service._ActionManifestError``)."""


class DynDOLODPipelineService:
    """Servicio transaccional para el pipeline DynDOLOD (TexGen + DynDOLOD).

    Encapsula la lógica de ejecución con:
    - Locking multi-recurso vía :class:`SnapshotTransactionLock`
    - Snapshots automáticos para rollback
    - Registro de operaciones en :class:`OperationJournal`
    - Eventos de ciclo de vida en :class:`CoreEventBus`

    Args:
        lock_manager: Gestor de locks distribuidos.
        snapshot_manager: Gestor de snapshots de archivos.
        journal: Journal de operaciones para trazabilidad.
        path_resolver: Servicio de resolución de rutas validadas.
        event_bus: Bus de eventos para publicación de ciclo de vida.
    """

    def __init__(
        self,
        *,
        lock_manager: DistributedLockManager,
        snapshot_manager: FileSnapshotManager,
        journal: OperationJournal,
        path_resolver: PathResolutionService,
        event_bus: CoreEventBus,
        preflight: PreflightService | None = None,
    ) -> None:
        self._lock_manager = lock_manager
        self._snapshot_manager = snapshot_manager
        self._journal = journal
        self._path_resolver = path_resolver
        self._event_bus = event_bus
        # Preflight inyectable (tests) o construido perezosamente en el primer uso.
        self._preflight = preflight

        # Lazy init — runner requiere env vars que pueden no existir aún.
        self._runner: DynDOLODRunner | None = None

    # ------------------------------------------------------------------
    # Lazy initialization
    # ------------------------------------------------------------------

    def _ensure_runner(self) -> DynDOLODRunner:
        """Inicializa el :class:`DynDOLODRunner` bajo demanda.

        Variables de entorno requeridas:
        - ``DYNDLOD_EXE``: Ruta a DynDOLODx64.exe
        - ``TEXGEN_EXE``: Ruta a TexGenx64.exe (opcional)
        - ``SKYRIM_PATH``: Ruta al directorio de Skyrim SE/AE
        - ``MO2_PATH``: Ruta al directorio de MO2
        - ``MO2_MODS_PATH``: Ruta a la carpeta mods de MO2

        Returns:
            DynDOLODRunner inicializado.

        Raises:
            DynDOLODExecutionError: Si faltan variables de entorno requeridas.
        """
        if self._runner is not None:
            return self._runner

        game_path = self._path_resolver.get_skyrim_path()
        mo2_path = self._path_resolver.get_mo2_path()
        mo2_mods_path = self._path_resolver.get_mo2_mods_path()
        dyndolod_exe = self._path_resolver.get_dyndolod_exe()
        texgen_exe = self._path_resolver.get_texgen_exe()

        if not game_path or not mo2_path or not mo2_mods_path or not dyndolod_exe:
            raise DynDOLODExecutionError(
                "Cannot initialize DynDOLODRunner: "
                "SKYRIM_PATH, MO2_PATH, MO2_MODS_PATH, and DYNDLOD_EXE "
                "environment variables must be valid paths"
            )

        if not dyndolod_exe.exists():
            raise DynDOLODExecutionError(f"DynDOLOD executable not found: {dyndolod_exe}")

        config = DynDOLODConfig(
            game_path=game_path,
            mo2_path=mo2_path,
            mo2_mods_path=mo2_mods_path,
            dyndolod_exe=dyndolod_exe,
            texgen_exe=texgen_exe,
        )

        self._runner = DynDOLODRunner(config)
        logger.info(
            "DynDOLODRunner inicializado: game=%s, dyndolod=%s",
            game_path,
            dyndolod_exe,
        )
        return self._runner

    def _ensure_preflight(self) -> PreflightService | None:
        """Construye perezosamente el preflight de DynDOLOD (T-16c·3).

        DynDOLOD regenera GBs de LODs bajo ``mods/`` en un run de 30+ min, así que
        los sensores relevantes son: **permisos de escritura** sobre los dirs de
        salida (``mods/`` y los ``*/Output`` existentes — el clásico "output
        read-only mata el run a mitad"), **símbolos/junctions** en las rutas
        crudas, **masters faltantes** y **límites full/light** del perfil MO2
        activo (DynDOLOD lee todo el load order), y **overwrite sucio**. NO cablea
        la versión de LOOT (irrelevante). Reusa las primitivas compartidas
        (T-16d/T-16c·3); no toca los otros servicios. Sensores no resolubles →
        ``None`` (omitidos con ``omit_unconfigured``). Sin game/MO2 → ``None``
        (sin gate, mismo criterio que loot/Synthesis).
        """
        if self._preflight is not None:
            return self._preflight

        game = self._path_resolver.get_skyrim_path()
        mo2 = self._path_resolver.get_mo2_path()
        if not isinstance(game, pathlib.Path) or not isinstance(mo2, pathlib.Path):
            return None

        # Imports perezosos (anti-ciclo: validators.preflight llega a tools._process).
        from sky_claw.local.validators.preflight import PreflightService
        from sky_claw.local.validators.preflight_sensors import (
            build_mo2_profile_sources_resolver,
            build_modlist_sensors,
            build_overwrite_sensor,
            build_vfs_sensor,
        )
        from sky_claw.local.validators.write_permissions import WritePermissionsChecker

        # vfs sobre rutas CRUDAS (las resueltas ya siguieron los symlinks).
        vfs_checker = build_vfs_sensor(
            raw_game=self._path_resolver.get_skyrim_path_raw(),
            raw_mo2=self._path_resolver.get_mo2_path_raw(),
            scan_mods_dir=False,
        )

        # Permisos: los targets se recalculan POR CORRIDA dentro del closure
        # (freshness, review Codex #311) — un dir de salida creado read-only
        # después de construir el preflight cacheado debe verse igual.
        def _permissions() -> Any:
            return WritePermissionsChecker(targets=self._permission_targets()).check()

        overwrite_check = build_overwrite_sensor(mo2 / "overwrite")

        resolver = build_mo2_profile_sources_resolver(
            game=game, mo2=mo2, profile=self._path_resolver.get_active_profile()
        )
        masters_check, limits_check = build_modlist_sensors(resolver) if resolver is not None else (None, None)

        self._preflight = PreflightService(
            vfs_checker=vfs_checker,
            permissions_check=_permissions,
            overwrite_check=overwrite_check,
            masters_check=masters_check,
            limits_check=limits_check,
            omit_unconfigured=True,
        )
        return self._preflight

    def _permission_targets(self) -> list[pathlib.Path]:
        """Rutas que DynDOLOD reescribe, resueltas EN CADA corrida (review #311).

        - ``mods/`` (padre donde crea los mods) + los mod dirs empaquetados
          (``DynDOLOD Output``/``TexGen Output``).
        - El **staging crudo** (``DynDOLOD_Output``/``TexGen_Output``) que la
          herramienta crea/reusa bajo la raíz MO2 y el dir del exe
          (``dyndolod_runner._find_texgen_output``/``_find_dyndolod_output``): su
          padre debe ser escribible para crearlo, y un staging existente
          read-only mata el run tras la generación. No se sondea el ``cwd`` del
          agente: no es el cwd del subproceso de DynDOLOD.

        ``WritePermissionsChecker`` sondea solo los dirs existentes, así que
        incluir rutas aún inexistentes es seguro (se saltan) y las que aparezcan
        en runs futuros se sondean sin reconstruir el preflight cacheado.
        """
        candidates: list[pathlib.Path] = []
        mods = self._path_resolver.get_mo2_mods_path()
        if isinstance(mods, pathlib.Path):
            candidates += [mods, mods / DynDOLODRunner.DYNDOLLOD_MOD_NAME, mods / DynDOLODRunner.TEXGEN_MOD_NAME]

        roots: list[pathlib.Path] = []
        mo2 = self._path_resolver.get_mo2_path()
        if isinstance(mo2, pathlib.Path):
            roots.append(mo2)
        exe = self._path_resolver.get_dyndolod_exe()
        if isinstance(exe, pathlib.Path):
            roots.append(exe.parent)
        for root in roots:
            candidates += [root, root / DynDOLODRunner.DYNDOLLOD_OUTPUT_NAME, root / DynDOLODRunner.TEXGEN_OUTPUT_NAME]

        seen: set[pathlib.Path] = set()
        return [p for p in candidates if not (p in seen or seen.add(p))]

    # ------------------------------------------------------------------
    # Caja negra de vuelo (T-26/T-28, ADR 0002) — espejo de xedit_service
    # ------------------------------------------------------------------

    async def _emit_action_manifest(
        self,
        *,
        tx_id: int,
        target_files: list[pathlib.Path],
        summary: str,
    ) -> None:
        """Construye y persiste el ActionManifest ANTES de mutar (T-26).

        Fail-closed: cualquier fallo del builder o del journal se convierte en
        :class:`_ActionManifestError` para que el caller aborte el pipeline sin
        mutar (la caja negra no es opcional cuando el journal está cableado).

        NOTA: el rollback de DynDOLOD es el move-aside de ``DirectoryRollback``
        (los ``Output/`` pesan GBs; snapshot copy-based sería carísimo), NO el
        snapshot manager — por eso el lock usa ``target_files=[]`` y el
        ``rollback_plan`` del manifiesto queda vacío por diseño (``snapshots=[]``).
        Un plan de rollback consciente del move-aside es follow-up.
        """
        from sky_claw.antigravity.orchestrator.preview.action_manifest import build_action_manifest

        try:
            manifest = build_action_manifest(
                ritual_id=f"dyndolod-pipeline-{tx_id}",
                tool="DynDOLOD",
                tool_version=None,  # DynDOLOD no expone versión hoy (follow-up).
                target_files=[str(f) for f in target_files],
                snapshots=[],  # rollback = DirectoryRollback move-aside, no snapshots.
                summary=summary,
            )
            await self._journal.persist_action_manifest(
                manifest,
                agent_id="dyndolod-pipeline-service",
                transaction_id=tx_id,
            )
        except Exception as exc:  # noqa: BLE001 — boundary: cualquier fallo del journal/builder
            raise _ActionManifestError(str(exc)) from exc

    async def _emit_flight_report(self, tx_id: int) -> None:
        """Compone y persiste el FlightReport del Ritual terminado (T-28).

        Post-vuelo y best-effort: lee la caja negra desde el journal (el
        manifiesto persistido + el estado REAL de la TX) y la persiste. Un fallo
        se loguea y NO rompe un pipeline ya exitoso (misma disciplina que LOOT/xEdit).
        """
        from sky_claw.antigravity.orchestrator.preview.flight_report import (
            compose_flight_report_from_journal,
        )

        try:
            report = await compose_flight_report_from_journal(self._journal, transaction_id=tx_id)
            await self._journal.persist_flight_report(
                report,
                agent_id="dyndolod-pipeline-service",
                transaction_id=tx_id,
            )
        except Exception:  # noqa: BLE001 — boundary best-effort del journal
            logger.error("Fallo al persistir el informe de vuelo de la TX %d", tx_id, exc_info=True)

    # ------------------------------------------------------------------
    # Pipeline principal
    # ------------------------------------------------------------------

    async def execute(
        self,
        preset: str = "Medium",
        run_texgen: bool = True,
        create_snapshot: bool = True,
        texgen_args: list[str] | None = None,
        dyndolod_args: list[str] | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Ejecuta el pipeline completo de generación de LODs.

        Flujo transaccional:
        1. Publicar evento ``pipeline.dyndolod.started``
        2. Adquirir lock + snapshot vía :class:`SnapshotTransactionLock`
        3. Comenzar transacción en journal
        4. Ejecutar TexGen (si ``run_texgen=True``) → DynDOLOD
        5. Validar salida de DynDOLOD
        6. Commit en journal y publicar evento ``pipeline.dyndolod.completed``

        Args:
            preset: Nivel de calidad (Low, Medium, High).
            run_texgen: Si True, ejecuta TexGen antes de DynDOLOD.
            create_snapshot: Si True, crea snapshot para rollback.
            texgen_args: Argumentos adicionales para TexGen.
            dyndolod_args: Argumentos adicionales para DynDOLOD.
            dry_run: Si True, NO ejecuta TexGen/DynDOLOD; devuelve una estimación
                plan-only (``status="dry_run_preview"`` + ``change_set``) sin
                lock, journal ni eventos. DynDOLOD es la etapa más cara (GBs,
                30+ min), por eso el preview nunca la ejecuta.

        Returns:
            Diccionario con resultado del pipeline, o el preview plan-only
            cuando ``dry_run=True``.
        """
        if dry_run:
            return await self._preview(preset=preset, run_texgen=run_texgen)

        # Preflight brutal ANTES de tocar nada (T-16c·3): un semáforo ROJO (p. ej.
        # el dir de salida sin permisos) cancela el run de 30+ min / GBs sin adquirir
        # el lock, abrir transacción, ni publicar el evento de inicio. Amarillo/verde
        # no bloquean; el reporte se surface al panel en todos los retornos.
        preflight = self._ensure_preflight()
        preflight_report: PreflightReport | None = None
        if preflight is not None:
            preflight_report = await preflight.run()
            if preflight_report.blocks_mutations:
                red = "; ".join(c.summary for c in preflight_report.checks if c.status.value == "red")
                logger.warning("DynDOLOD (stage 9) bloqueado por preflight en rojo: %s", red)
                return {
                    "status": "error",
                    "success": False,
                    "reason": "PreflightBlocked",
                    "message": f"Preflight en rojo, DynDOLOD cancelado: {red}",
                    "errors": [red],
                    "preflight": preflight_report.to_dict(),
                }

        start_time = time.monotonic()
        rolled_back = False
        tx_id: int | None = None
        # M-7: rastrear los DirectoryRollback para reportar el resultado REAL del
        # rollback (dr.rollback_completed) en vez de hardcodear rolled_back=True.
        # El AsyncExitStack ejecuta los __aexit__ (restore) ANTES de que corran los
        # except handlers, así que el flag ya está seteado cuando se leen.
        dir_rollbacks: list[DirectoryRollback] = []

        logger.info(
            "Iniciando pipeline DynDOLOD: preset=%s, texgen=%s, snapshot=%s",
            preset,
            run_texgen,
            create_snapshot,
        )

        # 1. Publicar evento de inicio
        await self._publish_started(preset=preset, run_texgen=run_texgen)

        # 2. Inicializar runner
        try:
            runner = self._ensure_runner()
        except DynDOLODExecutionError as exc:
            logger.error("Error inicializando DynDOLOD: %s", exc)
            duration = time.monotonic() - start_time
            await self._publish_completed(
                preset=preset,
                run_texgen=run_texgen,
                success=False,
                texgen_success=False,
                dyndolod_success=False,
                errors=(str(exc),),
                duration_seconds=duration,
                rolled_back=False,
            )
            return _attach_preflight(
                {
                    "success": False,
                    "message": str(exc),
                    "errors": [str(exc)],
                    "duration_seconds": duration,
                },
                preflight_report,
            )

        # DD-1: Directorios regenerados a proteger con rollback move-aside.
        # El backend de snapshots es copy-based/solo-archivos y ``Output/`` puede
        # pesar varios GB; renombrar el dir aparte es O(1) y da rollback real
        # byte-a-byte (evita que un fallo deje texturas/meshes parciales). El
        # move-aside del dir subsume el ``.esp``, así que el lock transaccional ya
        # no snapshotea archivos (``target_files=[]``).
        mods_path = runner._config.mo2_mods_path
        rollback_dirs: list[pathlib.Path] = []
        if create_snapshot:
            rollback_dirs.append(mods_path / runner.DYNDOLLOD_MOD_NAME)
            if run_texgen:
                rollback_dirs.append(mods_path / runner.TEXGEN_MOD_NAME)

        # T-26: los mods de salida que el ritual reescribe (independiente del
        # snapshot) — el files_touched del ActionManifest.
        manifest_targets: list[pathlib.Path] = [mods_path / DynDOLODRunner.DYNDOLLOD_MOD_NAME]
        if run_texgen:
            manifest_targets.append(mods_path / DynDOLODRunner.TEXGEN_MOD_NAME)

        # 3. Ejecutar bajo lock transaccional + rollback de directorios.
        # AsyncExitStack: el lock se adquiere primero y se libera último; los
        # DirectoryRollback se restauran ANTES de soltar el lock.
        try:
            async with contextlib.AsyncExitStack() as tx_stack:
                await tx_stack.enter_async_context(
                    SnapshotTransactionLock(
                        lock_manager=self._lock_manager,
                        snapshot_manager=self._snapshot_manager,
                        resource_id="dyndolod-pipeline",
                        agent_id="dyndolod-pipeline-service",
                        target_files=[],
                        metadata={"preset": preset, "run_texgen": run_texgen},
                    )
                )
                for output_dir in rollback_dirs:
                    dr = DirectoryRollback(output_dir)
                    await tx_stack.enter_async_context(dr)
                    dir_rollbacks.append(dr)

                # Comenzar transacción en journal DENTRO del lock
                tx_id = await self._journal.begin_transaction(
                    description=f"DynDOLOD pipeline (preset={preset}, texgen={run_texgen})",
                    agent_id="dyndolod-pipeline-service",
                )

                # T-26 (ADR 0002): la caja negra ANTES de generar LODs
                # (fail-closed). Si el manifiesto no se puede emitir, se lanza
                # DENTRO del lock → los DirectoryRollback restauran y el pipeline
                # NO corre (espejo de xedit_service).
                await self._emit_action_manifest(
                    tx_id=tx_id,
                    target_files=manifest_targets,
                    summary=f"Generar LODs (preset={preset}, texgen={run_texgen}) → {len(manifest_targets)} mod(s).",
                )

                # Ejecutar pipeline
                result = await runner.run_full_pipeline(
                    run_texgen=run_texgen,
                    preset=preset,
                    texgen_args=texgen_args,
                    dyndolod_args=dyndolod_args,
                )

                # Validar salida de DynDOLOD si fue exitoso
                if result.success and result.dyndolod_result and result.dyndolod_result.output_path:
                    is_valid = await runner.validate_dyndolod_output(result.dyndolod_result.output_path)
                    if not is_valid:
                        msg = "DynDOLOD output validation failed"
                        logger.error(msg)
                        raise DynDOLODExecutionError(msg)

                if not result.success:
                    errors_str = "; ".join(result.errors) if result.errors else "Unknown error"
                    raise DynDOLODExecutionError(f"DynDOLOD pipeline failed: {errors_str}")

                # Commit en journal
                await self._journal.commit_transaction(tx_id)

                # T-28: cerrar la caja negra tras el commit (best-effort — los
                # LODs ya se generaron; un fallo del informe no tumba el run).
                await self._emit_flight_report(tx_id)

                duration = time.monotonic() - start_time
                await self._log_result(result, preset, success=True)
                texgen_ok = result.texgen_result.success if result.texgen_result else False
                dyndolod_ok = result.dyndolod_result.success if result.dyndolod_result else False
                await self._publish_completed(
                    preset=preset,
                    run_texgen=run_texgen,
                    success=True,
                    texgen_success=texgen_ok,
                    dyndolod_success=dyndolod_ok,
                    errors=(),
                    duration_seconds=duration,
                    rolled_back=False,
                )

                logger.info(
                    "Pipeline DynDOLOD exitoso: texgen=%s, dyndolod=%s (%.1fs)",
                    result.texgen_mod_path,
                    result.dyndolod_mod_path,
                    duration,
                )

                # Normalizar pathlib.Path → str de forma recursiva para compatibilidad JSON/WS.
                def normalize_for_serialization(obj: Any) -> Any:
                    if isinstance(obj, pathlib.Path):
                        return str(obj)
                    if isinstance(obj, dict):
                        return {k: normalize_for_serialization(v) for k, v in obj.items()}
                    if isinstance(obj, list):
                        return [normalize_for_serialization(v) for v in obj]
                    return obj

                result_dict = normalize_for_serialization(dataclasses.asdict(result))

                return _attach_preflight(
                    {
                        "success": True,
                        "message": "",
                        **result_dict,
                        "duration_seconds": duration,
                    },
                    preflight_report,
                )

        except _ActionManifestError as exc:
            # La caja negra no se pudo emitir: ningún LOD se generó. Los
            # DirectoryRollback ya restauraron (nada mutó); marcar la TX para no
            # dejarla PENDING (guardado — el journal que ya reventó podría fallar
            # de nuevo). rolled_back real vía dir_rollbacks (M-7).
            rolled_back = all(dr.rollback_completed for dr in dir_rollbacks)
            duration = time.monotonic() - start_time
            if tx_id is not None:
                try:
                    await self._journal.mark_transaction_rolled_back(tx_id)
                except Exception as journal_exc:  # noqa: BLE001 — boundary best-effort del journal
                    logger.error(
                        "Failed to mark TX %d rolled back after manifest failure: %s",
                        tx_id,
                        journal_exc,
                        exc_info=True,
                    )
            logger.error("DynDOLOD: no se pudo emitir el ActionManifest; abortado: %s", exc)
            await self._publish_completed(
                preset=preset,
                run_texgen=run_texgen,
                success=False,
                texgen_success=False,
                dyndolod_success=False,
                errors=(str(exc),),
                duration_seconds=duration,
                rolled_back=rolled_back,
            )
            detail = f"Manifiesto de vuelo requerido no emitido: {exc}"
            return _attach_preflight(
                {
                    "success": False,
                    "reason": "ActionManifestFailed",
                    "message": detail,
                    "errors": [detail],
                    "duration_seconds": duration,
                    "rolled_back": rolled_back,
                },
                preflight_report,
            )

        except LockAcquisitionError as exc:
            duration = time.monotonic() - start_time
            logger.error("No se pudo adquirir lock para DynDOLOD pipeline: %s", exc)
            await self._publish_completed(
                preset=preset,
                run_texgen=run_texgen,
                success=False,
                texgen_success=False,
                dyndolod_success=False,
                errors=(f"Lock acquisition failed: {exc}",),
                duration_seconds=duration,
                rolled_back=False,
            )
            return _attach_preflight(
                {
                    "success": False,
                    "message": f"Lock acquisition failed: {exc}",
                    "errors": [f"Lock acquisition failed: {exc}"],
                    "duration_seconds": duration,
                },
                preflight_report,
            )

        except (DynDOLODExecutionError, DynDOLODTimeoutError) as exc:
            # M-7: reportar el resultado REAL del rollback. Los __aexit__ de los
            # DirectoryRollback ya corrieron (restore best-effort); rolled_back es
            # True sólo si TODOS completaron. Un rmtree/rename fallido deja el output
            # parcial en disco y debe reflejarse como rolled_back=False.
            rolled_back = all(dr.rollback_completed for dr in dir_rollbacks)
            duration = time.monotonic() - start_time
            logger.error("DynDOLOD pipeline domain error: %s (rolled_back=%s)", exc, rolled_back)

            if tx_id is not None:
                try:
                    await self._journal.mark_transaction_rolled_back(tx_id)
                except Exception as journal_exc:
                    logger.error(
                        "Failed to mark TX %d as rolled back: %s",
                        tx_id,
                        journal_exc,
                        exc_info=True,
                    )

            await self._log_result_error(preset, str(exc))
            await self._publish_completed(
                preset=preset,
                run_texgen=run_texgen,
                success=False,
                texgen_success=False,
                dyndolod_success=False,
                errors=(str(exc),),
                duration_seconds=duration,
                rolled_back=rolled_back,
            )
            return _attach_preflight(
                {
                    "success": False,
                    "message": str(exc),
                    "errors": [str(exc)],
                    "duration_seconds": duration,
                    "rolled_back": rolled_back,
                },
                preflight_report,
            )

        except asyncio.CancelledError:
            # Cancelación de task — hacer cleanup mínimo y re-lanzar.
            duration = time.monotonic() - start_time
            logger.warning("DynDOLOD pipeline cancelled after %.1fs", duration)
            if tx_id is not None:
                try:
                    await self._journal.mark_transaction_rolled_back(tx_id)
                except Exception as journal_exc:
                    # Aislar el fallo secundario para no enmascarar la
                    # cancelación; traceback al log como sus handlers hermanos.
                    logger.error(
                        "Failed to mark TX %d as rolled back on cancel: %s",
                        tx_id,
                        journal_exc,
                        exc_info=True,
                    )
            raise

        except Exception as exc:
            # PREVENCIÓN T11: Red de seguridad final — NUNCA dejar TX en PENDING
            # M-7: resultado real del rollback (ver handler de dominio arriba).
            rolled_back = all(dr.rollback_completed for dr in dir_rollbacks)
            duration = time.monotonic() - start_time
            logger.error(
                "Unexpected error in DynDOLOD pipeline: %s",
                exc,
                exc_info=True,
            )

            if tx_id is not None:
                try:
                    await self._journal.mark_transaction_rolled_back(tx_id)
                except Exception as journal_exc:
                    logger.error(
                        "Failed to mark TX %d as rolled back after unexpected error: %s",
                        tx_id,
                        journal_exc,
                        exc_info=True,
                    )

            await self._log_result_error(preset, str(exc))
            await self._publish_completed(
                preset=preset,
                run_texgen=run_texgen,
                success=False,
                texgen_success=False,
                dyndolod_success=False,
                errors=(str(exc),),
                duration_seconds=duration,
                rolled_back=rolled_back,
            )
            return _attach_preflight(
                {
                    "success": False,
                    "message": str(exc),
                    "errors": [str(exc)],
                    "duration_seconds": duration,
                    "rolled_back": rolled_back,
                },
                preflight_report,
            )

    # ------------------------------------------------------------------
    # Dry-run / preview (plan-only estimate)
    # ------------------------------------------------------------------

    async def _preview(self, *, preset: str, run_texgen: bool) -> dict[str, Any]:
        """Plan-only dry-run: estimate the LODs DynDOLOD WOULD generate.

        The TexGen/DynDOLOD executables are never launched (matrix: DynDOLOD is
        plan-only — it is the most expensive stage), so nothing is locked,
        journaled, or written.  Output directories are derived from the path
        resolver alone, so no DynDOLOD binary is required to preview.
        """
        # Local import to avoid an import-time cycle (local.tools -> orchestrator).
        from sky_claw.antigravity.orchestrator.preview.manifest import LODPlan, StageChangeSet

        mo2_mods_path = self._path_resolver.get_mo2_mods_path()
        dyndolod_dir = str(mo2_mods_path / "DynDOLOD Output") if mo2_mods_path else "DynDOLOD Output"

        would_generate = ["DynDOLOD.esp"]
        output_dirs = [dyndolod_dir]
        if run_texgen:
            texgen_dir = str(mo2_mods_path / "TexGen Output") if mo2_mods_path else "TexGen Output"
            would_generate.append("TexGen textures")
            output_dirs.append(texgen_dir)

        lod_plan = LODPlan(
            preset=preset,
            would_generate=would_generate,
            # The exact asset count is unknowable without running DynDOLOD; the
            # estimate is intentionally 0 and flagged as such in the warnings.
            estimated_assets=0,
            output_dirs=output_dirs,
        )
        change_set = StageChangeSet(
            stage="dyndolod",
            executed_for_real=False,
            files_touched=output_dirs,
            lod_plan=lod_plan,
            warnings=["LOD asset count is an estimate; TexGen/DynDOLOD are not run in preview."],
            summary=(
                f"Would generate LODs (preset={preset}, texgen={run_texgen}) into {dyndolod_dir} — DynDOLOD not run."
            ),
        )
        logger.info("DynDOLOD dry-run preview: %s", change_set.summary)
        return {
            "status": "dry_run_preview",
            "message": change_set.summary,
            "change_set": change_set.model_dump(mode="json"),
        }

    # ------------------------------------------------------------------
    # Eventos
    # ------------------------------------------------------------------

    async def _publish_started(self, *, preset: str, run_texgen: bool) -> None:
        """Publica evento de inicio del pipeline."""
        payload = DynDOLODPipelineStartedPayload(
            preset=preset,
            run_texgen=run_texgen,
        )
        await self._event_bus.publish(
            Event(
                topic="pipeline.dyndolod.started",
                payload=payload.to_log_dict(),
                source="dyndolod-pipeline-service",
            )
        )

    async def _publish_completed(
        self,
        *,
        preset: str,
        run_texgen: bool,
        success: bool,
        texgen_success: bool,
        dyndolod_success: bool,
        errors: tuple[str, ...],
        duration_seconds: float,
        rolled_back: bool,
    ) -> None:
        """Publica evento de finalización del pipeline."""
        payload = DynDOLODPipelineCompletedPayload(
            preset=preset,
            run_texgen=run_texgen,
            success=success,
            texgen_success=texgen_success,
            dyndolod_success=dyndolod_success,
            errors=errors,
            duration_seconds=duration_seconds,
            rolled_back=rolled_back,
        )
        await self._event_bus.publish(
            Event(
                topic="pipeline.dyndolod.completed",
                payload=payload.to_log_dict(),
                source="dyndolod-pipeline-service",
            )
        )

    # ------------------------------------------------------------------
    # Journal helpers
    # ------------------------------------------------------------------

    async def _log_result(
        self,
        result: DynDOLODPipelineResult,
        preset: str,
        *,
        success: bool,
    ) -> None:
        """Registra el resultado del pipeline mediante logging estructurado.

        El outcome transaccional ya queda persistido por
        ``commit_transaction``/``mark_transaction_rolled_back``; este helper
        añade un log estructurado con detalles del pipeline para observabilidad.
        """
        logger.info(
            "DynDOLOD pipeline result",
            extra={
                "agent_id": "dyndolod-pipeline-service",
                "operation_type": ("dyndolod_pipeline_complete" if success else "dyndolod_pipeline_failed"),
                "file_path": (str(result.dyndolod_mod_path) if result.dyndolod_mod_path else ""),
                "success": success,
                "preset": preset,
                "texgen_success": (result.texgen_result.success if result.texgen_result else False),
                "dyndolod_success": (result.dyndolod_result.success if result.dyndolod_result else False),
                "errors": result.errors,
            },
        )

    async def _log_result_error(self, preset: str, error_msg: str) -> None:
        """Registra un error del pipeline mediante logging estructurado."""
        logger.error(
            "DynDOLOD pipeline failed",
            extra={
                "agent_id": "dyndolod-pipeline-service",
                "operation_type": "dyndolod_pipeline_failed",
                "file_path": "",
                "success": False,
                "preset": preset,
                "error": error_msg,
            },
        )
