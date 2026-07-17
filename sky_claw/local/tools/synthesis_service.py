"""SynthesisPipelineService — servicio dedicado para el pipeline de Synthesis.

Extraído de ``supervisor.py`` como parte del Sprint 2 (Strangler Fig).
Reemplaza el manejo manual de snapshots/rollback con
:class:`SnapshotTransactionLock` para atomicidad y seguridad concurrente.

Diseño (LÓGICA, ARQUITECTURA, PREVENCIÓN):

LÓGICA:
    El pipeline de Synthesis genera un ESP (``Synthesis.esp``) combinando
    múltiples patchers. Si la ejecución falla o el ESP resultante está
    corrupto, se restaura automáticamente el snapshot previo.

ARQUITECTURA:
    Todas las dependencias se inyectan vía constructor (DI). El servicio
    no posee infraestructura propia — sólo coordina.

PREVENCIÓN:
    Las excepciones de dominio (``SynthesisExecutionError``,
    ``SynthesisValidationError``) burbujean hacia el ``__aexit__`` del
    ``SnapshotTransactionLock`` para activar rollback automático.
    No se captura ``Exception`` genérica dentro del context manager.
"""

from __future__ import annotations

import dataclasses
import logging
import pathlib
import time
from typing import TYPE_CHECKING, Any

from sky_claw.antigravity.core.event_bus import CoreEventBus, Event
from sky_claw.antigravity.core.event_payloads import (
    SynthesisPipelineCompletedPayload,
    SynthesisPipelineStartedPayload,
)
from sky_claw.antigravity.db.locks import (
    DistributedLockManager,
    LockAcquisitionError,
    SnapshotTransactionLock,
)
from sky_claw.local.tools.patcher_pipeline import PatcherPipeline
from sky_claw.local.tools.synthesis_runner import (
    SynthesisConfig,
    SynthesisExecutionError,
    SynthesisResult,
    SynthesisRunner,
    SynthesisValidationError,
)

if TYPE_CHECKING:
    from sky_claw.antigravity.core.path_resolver import PathResolutionService
    from sky_claw.antigravity.db.journal import OperationJournal
    from sky_claw.antigravity.db.snapshot_manager import FileSnapshotManager
    from sky_claw.local.validators.preflight import PreflightReport, PreflightService

logger = logging.getLogger(__name__)

# Directorio de staging de backups (compartido con supervisor)
_BACKUP_STAGING_DIR = ".skyclaw_backups/"


def _attach_preflight(result: dict[str, Any], report: PreflightReport | None) -> dict[str, Any]:
    """Adjunta el reporte de preflight al ``result`` cuando no está verde.

    Mismo criterio que ``loot_service``/``xedit_service`` (T-16b/T-16c·1): un
    semáforo verde no ensucia el dict; amarillo/rojo viajan como
    ``result["preflight"]`` para que el panel vivo lo renderice.
    """
    if report is not None and report.status.value != "green":
        result["preflight"] = report.to_dict()
    return result


class _ActionManifestError(Exception):
    """Interno (T-26): la emisión del manifiesto de vuelo falló. Se lanza DENTRO
    del lock (antes de mutar) para que el Ritual NO proceda sin manifiesto — la
    caja negra no es opcional cuando el journal está cableado (espejo de
    ``loot_service``/``xedit_service._ActionManifestError``)."""


class SynthesisPipelineService:
    """Servicio dedicado para la ejecución del pipeline de Synthesis.

    Coordina ``SynthesisRunner``, ``PatcherPipeline``,
    ``SnapshotTransactionLock`` y ``CoreEventBus`` para ejecutar
    el pipeline con protección transaccional y observabilidad.
    """

    RESOURCE_ID: str = "Synthesis.esp"
    AGENT_ID: str = "synthesis-service"

    def __init__(
        self,
        *,
        lock_manager: DistributedLockManager,
        snapshot_manager: FileSnapshotManager,
        journal: OperationJournal,
        path_resolver: PathResolutionService,
        event_bus: CoreEventBus,
        pipeline_config_path: pathlib.Path | None = None,
        output_path: pathlib.Path | None = None,
        preflight: PreflightService | None = None,
    ) -> None:
        self._lock_manager = lock_manager
        self._snapshot_manager = snapshot_manager
        self._journal = journal
        self._path_resolver = path_resolver
        self._event_bus = event_bus
        # Preflight inyectable (tests) o construido perezosamente en el primer uso.
        self._preflight = preflight
        self._pipeline_config_path = pipeline_config_path or (
            pathlib.Path(_BACKUP_STAGING_DIR) / "synthesis_pipeline.json"
        )
        # T-27b: override del destino de salida. El sandbox de T-27 pasa
        # `SandboxClone.overwrite_copy` para que el run escriba en la copia y
        # no en el overwrite real (garantía de aislamiento). None = el cálculo
        # de siempre (overwrite real / "Synthesis Output"). Fijo por instancia:
        # un run sandboxeado construye su propio servicio (patrón ad-hoc de
        # system_tools.run_pandora).
        self._output_path = output_path

        # Lazy init — paths may not be available at construction time
        self._synthesis_runner: SynthesisRunner | None = None
        self._patcher_pipeline: PatcherPipeline | None = None

    # ------------------------------------------------------------------
    # Lazy initialization
    # ------------------------------------------------------------------

    def _ensure_synthesis_runner(self) -> SynthesisRunner:
        """Inicializa lazily el SynthesisRunner validando paths del entorno.

        Returns:
            SynthesisRunner inicializado.

        Raises:
            SynthesisExecutionError: Si las variables de entorno son inválidas.
        """
        if self._synthesis_runner is not None:
            return self._synthesis_runner

        game_path = self._path_resolver.get_skyrim_path()
        mo2_path = self._path_resolver.get_mo2_path()
        synthesis_exe = self._path_resolver.get_synthesis_exe()

        if not game_path or not mo2_path or not synthesis_exe:
            raise SynthesisExecutionError(
                "Cannot initialize SynthesisRunner: "
                "SKYRIM_PATH, MO2_PATH, and SYNTHESIS_EXE environment variables "
                "must be valid paths"
            )

        if not synthesis_exe.exists():
            raise SynthesisExecutionError(f"Synthesis executable not found: {synthesis_exe}")

        # T-27b: el override (sandbox) manda; sin él, el destino de siempre.
        output_path = self._output_path
        if output_path is None:
            output_path = mo2_path / "overwrite"
            if not output_path.exists():
                output_path = mo2_path / "mods" / "Synthesis Output"

        config = SynthesisConfig(
            game_path=game_path,
            mo2_path=mo2_path,
            output_path=output_path,
            synthesis_exe=synthesis_exe,
            timeout_seconds=300,
        )

        self._synthesis_runner = SynthesisRunner(config)
        logger.info(
            "SynthesisRunner inicializado: game=%s, output=%s",
            game_path,
            output_path,
        )
        return self._synthesis_runner

    def _ensure_patcher_pipeline(self) -> PatcherPipeline:
        """Inicializa lazily el PatcherPipeline.

        Returns:
            PatcherPipeline inicializado.
        """
        if self._patcher_pipeline is not None:
            return self._patcher_pipeline

        if self._pipeline_config_path.exists():
            self._patcher_pipeline = PatcherPipeline.from_json(self._pipeline_config_path)
            logger.info(
                "PatcherPipeline cargado desde %s: %d patchers",
                self._pipeline_config_path,
                len(self._patcher_pipeline),
            )
        else:
            self._patcher_pipeline = PatcherPipeline(pipeline_config_path=self._pipeline_config_path)
            logger.info("PatcherPipeline inicializado vacío")

        return self._patcher_pipeline

    def _ensure_preflight(self) -> PreflightService | None:
        """Construye perezosamente el preflight de Synthesis (T-16c·2, STAGE 7).

        Synthesis procesa TODO el modlist, así que los sensores relevantes son:
        **permisos de escritura sobre el output** (overwrite / clon del sandbox —
        si no es escribible, Synthesis muere generando ``Synthesis.esp``),
        **límites full/light del load order** (salud general del engine: un load
        order ya al/por encima del límite de slots es un problema real),
        **masters faltantes**, **overwrite sucio** y **symlinks/junctions**. NO
        cablea la versión de LOOT (irrelevante para Synthesis). Reusa las
        primitivas compartidas; no toca ``loot_service``. Sensores no resolubles →
        ``None`` (omitidos con ``omit_unconfigured``).

        NOTA (follow-up, review Codex #306): ``plugin_limits`` mide slots
        full/light del engine, NO el fan-in de masters del ``Synthesis.esp``
        generado — el ``Max Masters Exceeded`` del SOP §0.7 depende de cuántos
        plugins fuente lista el ESP de salida y de si el auto-split de Synthesis
        está activo, señales que este gate todavía no computa.
        """
        if self._preflight is not None:
            return self._preflight

        game = self._path_resolver.get_skyrim_path()
        mo2 = self._path_resolver.get_mo2_path()
        if not isinstance(game, pathlib.Path) or not isinstance(mo2, pathlib.Path):
            return None

        # Imports perezosos (anti-ciclo: validators.preflight llega a tools._process).
        from sky_claw.local.validators.preflight import PreflightService
        from sky_claw.local.validators.preflight_sensors import build_overwrite_sensor, build_vfs_sensor
        from sky_claw.local.validators.write_permissions import WritePermissionsChecker

        # vfs sobre rutas CRUDAS (las resueltas ya siguieron los symlinks) —
        # builder compartido (T-16d): coacciona no-Path y guarda "al menos una raíz".
        vfs_checker = build_vfs_sensor(
            raw_game=self._path_resolver.get_skyrim_path_raw(),
            raw_mo2=self._path_resolver.get_mo2_path_raw(),
            scan_mods_dir=False,
        )

        # Output real donde Synthesis escribe (el override del sandbox manda; si no,
        # overwrite / "Synthesis Output" — mismo cálculo que _ensure_synthesis_runner).
        output_dir = self._output_path
        if output_dir is None:
            output_dir = mo2 / "overwrite"
            if not output_dir.exists():
                output_dir = mo2 / "mods" / "Synthesis Output"

        def _permissions():
            # Re-probe por llamada (freshness): un cambio de permisos entre corridas se ve.
            return WritePermissionsChecker(targets=[output_dir]).check()

        overwrite_check = build_overwrite_sensor(mo2 / "overwrite")
        masters_check, limits_check = self._build_modlist_checks(game, mo2)

        self._preflight = PreflightService(
            vfs_checker=vfs_checker,
            permissions_check=_permissions,
            overwrite_check=overwrite_check,
            masters_check=masters_check,
            limits_check=limits_check,
            omit_unconfigured=True,
        )
        return self._preflight

    def _build_modlist_checks(self, game: pathlib.Path, mo2: pathlib.Path) -> tuple[Any, Any]:
        """Closures de masters/límites del **perfil MO2 activo**.

        Lee el load order de ``profiles/<perfil>/plugins.txt`` — NO el
        ``%LOCALAPPDATA%\\plugins.txt`` global que reescribe LOOT fuera del VFS
        (review Codex #306). Delega en los builders compartidos (T-16c·3): el
        resolver del perfil MO2 y el cableado de masters/límites son idénticos a
        los de DynDOLOD. Sin perfil/fuentes resolubles → ``(None, None)`` →
        checkpoints "no configurado" (omitidos). No miente verde (lección #250).
        """
        from sky_claw.local.validators.preflight_sensors import (
            build_mo2_profile_sources_resolver,
            build_modlist_sensors,
        )

        resolver = build_mo2_profile_sources_resolver(
            game=game, mo2=mo2, profile=self._path_resolver.get_active_profile()
        )
        if resolver is None:
            return None, None
        return build_modlist_sensors(resolver)

    # ------------------------------------------------------------------
    # Pipeline execution
    # ------------------------------------------------------------------

    async def execute_pipeline(
        self,
        patcher_ids: list[str] | None = None,
        create_snapshot: bool = True,
    ) -> dict[str, Any]:
        """Ejecuta el pipeline de Synthesis con protección transaccional.

        Usa ``SnapshotTransactionLock`` para garantizar atomicidad:
        si la ejecución falla o el ESP resultante está corrupto, el
        snapshot se restaura automáticamente.

        Args:
            patcher_ids: IDs de patchers a ejecutar. Si es ``None``,
                         usa los patchers habilitados del pipeline.
            create_snapshot: Si ``True``, crea snapshot antes de ejecutar.

        Returns:
            Diccionario con los campos de ``SynthesisResult``.
        """
        t0 = time.monotonic()

        # T-27b: dentro del sandbox el snapshot+rollback del servicio es
        # redundante (el clon ES el mecanismo de rollback: discard) y además
        # destructivo para la evidencia — ante un fallo restauraría el
        # Synthesis.esp previo del clon y el diff mostraría el estado pre-run
        # en vez de la salida parcial que el operador necesita ver
        # (review Codex #258). El lock se conserva (serialización).
        if self._output_path is not None and create_snapshot:
            logger.info("Run sandboxeado: snapshot del servicio deshabilitado (el clon es el rollback).")
            create_snapshot = False

        # Preflight brutal ANTES de tocar nada (T-16c·2, STAGE 7): un semáforo ROJO
        # (p. ej. >254 masters, u output sin permisos) cancela Synthesis sin correr el
        # pipeline ni abrir transacción. Amarillo/verde no bloquean; se surface al panel.
        preflight = self._ensure_preflight()
        preflight_report = None
        if preflight is not None:
            preflight_report = await preflight.run()
            if preflight_report.blocks_mutations:
                red = "; ".join(c.summary for c in preflight_report.checks if c.status.value == "red")
                logger.warning("Synthesis (stage 7) bloqueado por preflight en rojo: %s", red)
                blocked = self._error_dict(f"Preflight en rojo, Synthesis cancelado: {red}")
                blocked["reason"] = "PreflightBlocked"
                blocked["preflight"] = preflight_report.to_dict()
                return blocked

        # --- Early init ---
        try:
            runner = self._ensure_synthesis_runner()
            pipeline = self._ensure_patcher_pipeline()
        except SynthesisExecutionError as exc:
            logger.error("Error inicializando Synthesis (stage 7): %s", exc)
            return _attach_preflight(self._error_dict(str(exc)), preflight_report)

        if patcher_ids is None:
            enabled = pipeline.get_enabled_patchers()
            patcher_ids = [p.patcher_id for p in enabled]

        if not patcher_ids:
            logger.warning("No hay patchers para ejecutar")
            return _attach_preflight(self._error_dict("No patchers configured or enabled"), preflight_report)

        target_esp = runner._config.output_path / "Synthesis.esp"

        # --- Publish started event ---
        started_payload = SynthesisPipelineStartedPayload(
            patcher_ids=tuple(patcher_ids),
            target_esp=str(target_esp),
            snapshot_enabled=create_snapshot,
        )
        await self._event_bus.publish(
            Event(
                topic="synthesis.pipeline.started",
                payload=started_payload.to_log_dict(),
                source=self.AGENT_ID,
            )
        )

        # --- Transactional execution ---
        # Order: lock → snapshot → begin_transaction → execute → commit/abort
        target_files: list[pathlib.Path] = []
        if create_snapshot and target_esp.exists():
            target_files = [target_esp]

        result: SynthesisResult
        rolled_back = False
        manifest_failed = False
        tx_id: int | None = None
        # M-7 (misma lección que #295 en xedit_service): bindear el lock para
        # reportar el resultado REAL del rollback (tx_lock.rollback_completed)
        # en vez de inferirlo con flags — el flag anterior (in_lock_context)
        # declaraba rolled_back=True aunque la restauración del snapshot
        # hubiera fallado en __aexit__, dejando el .esp en estado parcial
        # mientras el caller creía que se había revertido.
        tx_lock: SnapshotTransactionLock | None = None

        try:
            async with SnapshotTransactionLock(
                lock_manager=self._lock_manager,
                snapshot_manager=self._snapshot_manager,
                resource_id=self.RESOURCE_ID,
                agent_id=self.AGENT_ID,
                target_files=target_files,
                metadata={"source": "synthesis_pipeline", "patchers": patcher_ids},
            ) as tx_lock:
                # Begin journal transaction AFTER lock+snapshot acquired
                tx_id = await self._journal.begin_transaction(
                    description="synthesis_pipeline",
                    agent_id=self.AGENT_ID,
                )

                # T-26 (ADR 0002): la caja negra ANTES de tocar el output
                # (fail-closed). Si el manifiesto no se puede emitir, se lanza
                # DENTRO del lock → __aexit__ restaura los snapshots y ningún
                # patcher corre (espejo de xedit_service).
                await self._emit_action_manifest(
                    tx_id=tx_id,
                    target_files=[target_esp],
                    snapshots=tx_lock.snapshots,
                    summary=f"Ejecutar {len(patcher_ids)} patcher(s) de Synthesis → {target_esp.name}.",
                )

                result = await runner.run_pipeline(patcher_ids)

                # Validar ESP DENTRO del context manager para activar rollback
                if result.success and result.output_esp:
                    is_valid = await runner.validate_synthesis_esp(result.output_esp)
                    if not is_valid:
                        raise SynthesisValidationError(
                            "ESP validation failed: corrupted or invalid",
                            esp_path=result.output_esp,
                        )

                # Pipeline failure → raise para activar rollback
                if not result.success:
                    raise SynthesisExecutionError(
                        "; ".join(result.errors) if result.errors else "Pipeline failed",
                        return_code=result.return_code,
                        stderr=result.stderr,
                    )

            # Commit journal. Un fallo del commit conserva el camino de "rollback
            # honesto" (#295): propaga a ``except Exception`` → resultado fallido
            # + TX pendiente (la mutación quedó aplicada sin restauración).
            #
            # T-28 (FlightReport) NO se emite acá: Synthesis corre SIEMPRE en
            # sandbox (tool_dispatcher: "corre SIEMPRE en sandbox") con un
            # StagingJournal cuyo commit está DIFERIDO hasta la promoción, así que
            # un informe compuesto en este punto reflejaría un estado pre-promoción
            # (pending) y quedaría stale. El cierre post-vuelo del informe con las
            # rutas reales pertenece al promotion flow (ExecuteSynthesisPipeline-
            # Strategy / SandboxPromotionFlow) — follow-up documentado (review #309).
            if tx_id is not None:
                await self._journal.commit_transaction(tx_id)

        except _ActionManifestError as exc:
            # La caja negra no se pudo emitir: ningún patcher corrió. __aexit__ ya
            # restauró los snapshots; marcar la TX para no dejarla PENDING. Como
            # la mutación nunca ocurrió, marcar rolled_back es siempre seguro acá.
            manifest_failed = True
            rolled_back = bool(tx_lock and tx_lock.rollback_completed)
            if tx_id is not None:
                # Guardado: el journal ya falló al persistir el manifiesto; si
                # vuelve a fallar acá no debe enmascarar el resultado
                # ActionManifestFailed ni saltarse el evento completed (review #309).
                await self._safe_mark_rolled_back(tx_id)
            logger.error("Synthesis: no se pudo emitir el ActionManifest; abortado: %s", exc)
            detail = f"Manifiesto de vuelo requerido no emitido: {exc}"
            result = SynthesisResult(
                success=False,
                output_esp=None,
                return_code=-1,
                stdout="",
                stderr=detail,
                patchers_executed=[],
                errors=[detail],
            )

        except (SynthesisExecutionError, SynthesisValidationError) as exc:
            # __aexit__ intentó restaurar los snapshots. M-7: reportar el
            # resultado REAL — rollback_completed es False si el restore falló
            # (o si no había snapshots que restaurar).
            rolled_back = bool(tx_lock and tx_lock.rollback_completed)
            if tx_id is not None and (rolled_back or not target_files):
                # Sin target_files no había nada que restaurar: la TX se marca
                # abortada como siempre. Con snapshots restaurados, ídem.
                await self._journal.mark_transaction_rolled_back(tx_id)
            elif tx_id is not None:
                logger.critical(
                    "Rollback Synthesis incompleto para TX %d; se mantiene pendiente para recuperación manual.",
                    tx_id,
                )
            logger.error("Pipeline Synthesis falló: %s", exc)
            _rc = getattr(exc, "return_code", None)
            result = SynthesisResult(
                success=False,
                output_esp=target_esp if target_esp.exists() else None,
                return_code=_rc if _rc is not None else -1,
                stdout="",
                stderr=str(exc),
                patchers_executed=[],
                errors=[str(exc)],
            )

        except LockAcquisitionError as exc:
            # Lock was never acquired — no journal TX started, no snapshots to restore
            logger.warning("Lock contention para %s: %s", self.RESOURCE_ID, exc)
            result = SynthesisResult(
                success=False,
                output_esp=None,
                return_code=-1,
                stdout="",
                stderr=str(exc),
                patchers_executed=[],
                errors=[f"Lock contention: {exc}"],
            )

        except Exception as exc:
            # Unexpected exception (OSError, asyncio.TimeoutError, etc.)
            # M-7: rolled_back sale de tx_lock.rollback_completed — True SOLO si
            # __aexit__ corrió y restauró todos los snapshots (≥1). Cubre ambos
            # casos que el flag anterior confundía: excepción fuera del lock
            # (commit_transaction falló → no hubo rollback) y restore fallido
            # dentro de __aexit__ (rollback_failures no vacío).
            # Note: asyncio.CancelledError is BaseException, not Exception —
            # it intentionally propagates through here uncaught.
            rolled_back = bool(tx_lock and tx_lock.rollback_completed)
            if tx_id is not None and (rolled_back or not target_files):
                try:
                    await self._journal.mark_transaction_rolled_back(tx_id)
                except Exception as rollback_exc:
                    logger.critical(
                        "Failed to mark journal TX %d rolled back after unexpected error: %s",
                        tx_id,
                        rollback_exc,
                        exc_info=True,
                    )
            elif tx_id is not None:
                logger.critical(
                    "Rollback Synthesis incompleto para TX %d; se mantiene pendiente para recuperación manual.",
                    tx_id,
                )
            logger.error("Unexpected exception in synthesis pipeline: %s", exc, exc_info=True)
            result = SynthesisResult(
                success=False,
                output_esp=None,
                return_code=-1,
                stdout="",
                stderr=str(exc),
                patchers_executed=[],
                errors=[f"Unexpected error: {exc}"],
            )

        duration = time.monotonic() - t0

        # --- Publish completed event ---
        completed_payload = SynthesisPipelineCompletedPayload(
            patcher_ids=tuple(patcher_ids),
            target_esp=str(target_esp),
            success=result.success,
            patchers_executed=tuple(result.patchers_executed),
            errors=tuple(result.errors),
            duration_seconds=round(duration, 3),
            rolled_back=rolled_back,
        )
        await self._event_bus.publish(
            Event(
                topic="synthesis.pipeline.completed",
                payload=completed_payload.to_log_dict(),
                source=self.AGENT_ID,
            )
        )

        if result.success:
            logger.info(
                "Pipeline Synthesis exitoso: %s (%d patchers, %.1fs)",
                result.output_esp,
                len(result.patchers_executed),
                duration,
            )

        out = self._result_to_dict(result)
        if manifest_failed:
            out["reason"] = "ActionManifestFailed"
        return _attach_preflight(out, preflight_report)

    # ------------------------------------------------------------------
    # Caja negra de vuelo (T-26/T-28, ADR 0002) — espejo de xedit_service
    # ------------------------------------------------------------------

    async def _emit_action_manifest(
        self,
        *,
        tx_id: int,
        target_files: list[pathlib.Path],
        snapshots: Any,
        summary: str,
    ) -> None:
        """Construye y persiste el ActionManifest ANTES de mutar (T-26).

        Fail-closed: cualquier fallo del builder o del journal se convierte en
        :class:`_ActionManifestError` para que el caller aborte el pipeline sin
        mutar (la caja negra no es opcional cuando el journal está cableado).

        NOTA (review #309): en el path de producción (Synthesis SIEMPRE en
        sandbox) ``target_files`` apunta al clon (``clone.overwrite_copy``), que
        es lo que ESTE run efectivamente escribe. La traducción a las rutas
        reales del overwrite tras la promoción pertenece al promotion flow
        (dueño del mapeo clon→real) — follow-up documentado.
        """
        from sky_claw.antigravity.orchestrator.preview.action_manifest import build_action_manifest

        try:
            manifest = build_action_manifest(
                ritual_id=f"synthesis-pipeline-{tx_id}",
                tool="Synthesis",
                tool_version=None,  # Synthesis no expone versión hoy (follow-up).
                target_files=[str(f) for f in target_files],
                snapshots=snapshots,
                summary=summary,
            )
            await self._journal.persist_action_manifest(
                manifest,
                agent_id=self.AGENT_ID,
                transaction_id=tx_id,
            )
        except Exception as exc:  # noqa: BLE001 — boundary: cualquier fallo del journal/builder
            raise _ActionManifestError(str(exc)) from exc

    async def _safe_mark_rolled_back(self, tx_id: int) -> None:
        """Marca la TX como rolled_back sin propagar (best-effort, review #309).

        Se usa en el path de fallo del manifiesto: el journal que ya reventó al
        persistir no debe, si vuelve a fallar acá, enmascarar el resultado
        ``ActionManifestFailed`` ni impedir el evento ``completed`` (espejo del
        ``_mark_journal_rolled_back`` guardado de ``xedit_service``).
        """
        try:
            await self._journal.mark_transaction_rolled_back(tx_id)
        except Exception:  # noqa: BLE001 — boundary best-effort del journal
            logger.error("Fallo al marcar la TX %d rolled_back tras fallo del manifiesto", tx_id, exc_info=True)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _result_to_dict(result: SynthesisResult) -> dict[str, Any]:
        """Convierte SynthesisResult a dict serializable.

        Añade el ``message`` canónico del contrato compartido (deuda #5): vacío
        en éxito, y el detalle de stderr/errors en fallo.
        """
        raw = dataclasses.asdict(result)
        # Path → str para serialización
        if raw.get("output_esp") is not None:
            raw["output_esp"] = str(raw["output_esp"])
        if raw.get("success"):
            raw["message"] = ""
        else:
            errors = raw.get("errors") or []
            raw["message"] = str(raw.get("stderr") or "; ".join(str(e) for e in errors) or "")
        return raw

    @staticmethod
    def _error_dict(message: str) -> dict[str, Any]:
        """Construye un dict de error para retornos tempranos."""
        return {
            "success": False,
            "message": message,
            "output_esp": None,
            "return_code": -1,
            "stdout": "",
            "stderr": message,
            "patchers_executed": [],
            "errors": [message],
        }
