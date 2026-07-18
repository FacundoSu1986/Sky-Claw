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
*serialización*: un lock **anidado** (``Bashed Patch, 0.esp`` externo + ``load-order``
interno, mismo patrón que ``grass_cache_service``) que serializa Wrye Bash tanto contra
otra corrida propia como contra un sort de LOOT — el Bashed Patch se arma del orden
activo que LOOT reescribe. El preflight brutal (PR B) corre PRIMERO y la caja negra de
vuelo (ActionManifest fail-closed + FlightReport best-effort, T-26/T-28, "PR C") se
emite con ``journal`` opcional, espejando ``loot_service``. Con ambos, Wrye Bash queda
al día con la disciplina de sus hermanos rituales (6/6 en preflight y caja negra).
"""

from __future__ import annotations

import asyncio
import logging
import pathlib
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from sky_claw.antigravity.db.locks import (
    DistributedLockManager,
    LockAcquisitionError,
    LockError,
    SnapshotTransactionLock,
)
from sky_claw.local.tools.loot_service import LOAD_ORDER_RESOURCE_ID
from sky_claw.local.tools.wrye_bash_runner import (
    BASHED_PATCH_NAME,
    WryeBashConfig,
    WryeBashExecutionError,
    WryeBashRunner,
)

if TYPE_CHECKING:
    from sky_claw.antigravity.core.path_resolver import PathResolutionService
    from sky_claw.antigravity.db.journal import OperationJournal
    from sky_claw.antigravity.db.snapshot_manager import FileSnapshotManager
    from sky_claw.local.validators.preflight import PreflightReport, PreflightService

logger = logging.getLogger(__name__)

#: Lock resource id (externo) para la generación del Bashed Patch. Espeja a Synthesis
#: (``Synthesis.esp``): serializa sobre el artefacto de salida contra otra corrida de
#: Wrye Bash. Además se anida el lock ``load-order`` (ver ``execute_pipeline``) porque
#: el Bashed Patch se arma del orden activo que LOOT reescribe.
BASHED_PATCH_RESOURCE_ID = "Bashed Patch, 0.esp"


class _ActionManifestError(Exception):
    """Interno (T-26): la emisión del manifiesto de vuelo falló. Se lanza DENTRO
    del lock (antes de mutar) para que Wrye Bash NO proceda sin manifiesto — la
    caja negra no es opcional cuando el journal está cableado (espejo de
    ``loot_service``/``pandora_service._ActionManifestError``)."""


def _attach_preflight(result: dict[str, Any], report: PreflightReport | None) -> dict[str, Any]:
    """Adjunta el reporte de preflight al ``result`` cuando no está verde.

    Mismo criterio que ``loot_service``/``xedit_service``/``dyndolod_service``/
    ``pandora_service`` (T-16b/T-16c): un semáforo verde no ensucia el dict;
    amarillo/rojo viajan como ``result["preflight"]`` para que el panel lo renderice.
    """
    if report is not None and report.status.value != "green":
        result["preflight"] = report.to_dict()
    return result


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
        preflight: PreflightService | None = None,
        journal: OperationJournal | None = None,
    ) -> None:
        self._lock_manager = lock_manager
        self._snapshot_manager = snapshot_manager
        self._path_resolver = path_resolver
        self._wrye_bash_runner = wrye_bash_runner
        self._plugin_limit_guard = plugin_limit_guard
        # Preflight inyectable (tests) o construido perezosamente en el primer uso.
        self._preflight = preflight
        # T-26/T-28 (ADR 0002, "PR C"): cuando el journal está cableado, la generación
        # del Bashed Patch emite la caja negra de vuelo. Se cablea en AMBOS paths de
        # producción vía app_context (GUI/dispatcher y agente), igual que loot_service.
        # Sin journal (callers legacy / tests) no se emite.
        self._journal = journal

    def _ensure_preflight(self) -> PreflightService | None:
        """Construye perezosamente el preflight de Wrye Bash (T-16c, PR B, FASE 6).

        Wrye Bash arma el Bashed Patch leyendo TODO el load order activo y escribe
        ``Bashed Patch, 0.esp`` (vía la VFS de MO2). Sensores relevantes — el mismo
        set que DynDOLOD/Synthesis, porque es un ritual plugin-based: **permisos de
        escritura** sobre el destino del Bashed Patch (``Data`` del juego y el
        ``overwrite`` de MO2 — el destino real es dependiente del entorno, así que se
        sondean ambos), **símbolos/junctions** en las rutas crudas, **masters
        faltantes** y **límites full/light** del perfil MO2 activo, y **overwrite
        sucio** (el Bashed Patch aterriza ahí; uno sucio hace el diff inatribuible).
        NO cablea la versión de LOOT (irrelevante). Reusa las primitivas compartidas
        (T-16d). Sensores no resolubles → ``None`` (omitidos con ``omit_unconfigured``).
        Sin game/MO2 resoluble → ``None`` (sin gate, mismo criterio que sus hermanos).
        """
        if self._preflight is not None:
            return self._preflight
        if self._path_resolver is None:
            return None

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

        # Permisos: targets recalculados POR CORRIDA dentro del closure (freshness,
        # patrón #252/#311) — un destino creado read-only entre corridas debe verse.
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
        """Rutas candidatas donde aterriza ``Bashed Patch, 0.esp``, por corrida.

        El destino real es dependiente del entorno: lanzado vía la VFS de MO2 (con
        ``cwd`` en el juego) el plugin se redirige al ``overwrite`` de MO2; en un
        setup sin VFS iría al ``Data`` del juego. Se sondean ambos; el
        ``WritePermissionsChecker`` se salta los inexistentes y se resuelve por
        corrida (freshness), así que incluir rutas aún ausentes es seguro.
        """
        import pathlib

        candidates: list[pathlib.Path] = []
        game = self._path_resolver.get_skyrim_path() if self._path_resolver is not None else None
        if isinstance(game, pathlib.Path):
            candidates.append(game / "Data")
        mo2 = self._path_resolver.get_mo2_path() if self._path_resolver is not None else None
        if isinstance(mo2, pathlib.Path):
            candidates.append(mo2 / "overwrite")
        seen: set[pathlib.Path] = set()
        return [p for p in candidates if not (p in seen or seen.add(p))]

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

    # ------------------------------------------------------------------
    # Caja negra de vuelo (T-26/T-28, ADR 0002) — espejo de loot_service
    # ------------------------------------------------------------------

    def _bashed_patch_target(self, runner: WryeBashRunner) -> str:
        """Ruta del Bashed Patch para el ``files_touched`` del manifiesto.

        Vive en el ``Data`` del juego (``bash.py`` corre con ``cwd=game_path``). Si
        el game path no es resoluble, cae al nombre canónico — el manifiesto igual
        registra QUÉ artefacto se tocó, aunque no la ruta absoluta.
        """
        game_path = getattr(runner.config, "game_path", None)
        if isinstance(game_path, pathlib.Path):
            return str(game_path / "Data" / BASHED_PATCH_NAME)
        return BASHED_PATCH_NAME

    async def _emit_action_manifest(self, target_file: str) -> int:
        """Construye y persiste el ActionManifest ANTES de mutar (T-26).

        Se llama DENTRO del lock, antes de ``generate_bashed_patch()``. Devuelve el id
        de la transacción del journal para commit/rollback posterior. Snapshot diferido
        (``target_files=[]`` en el lock) → ``snapshots=[]``: el rollback de Wrye Bash no
        es copy-based (la salida sale vía la VFS de MO2 con ``cwd``); el manifiesto
        registra el artefacto tocado para auditoría, sin plan de restore.

        Raises:
            _ActionManifestError: Si begin_transaction/persist falla — Wrye Bash no
                debe proceder sin la caja negra emitida. La TX recién abierta se marca
                rolled-back para no dejarla PENDING (espejo de loot_service).
        """
        from sky_claw.antigravity.orchestrator.preview.action_manifest import build_action_manifest

        assert self._journal is not None  # cableado verificado por el caller
        journal_tx_id: int | None = None
        try:
            journal_tx_id = await self._journal.begin_transaction(
                description="wrye_bash_bashed_patch",
                agent_id=self.AGENT_ID,
            )
            manifest = build_action_manifest(
                ritual_id=f"wrye-bash-{journal_tx_id}",
                tool="Wrye Bash",
                tool_version=None,  # Wrye Bash no expone versión hoy (follow-up menor).
                target_files=[target_file],
                snapshots=[],  # snapshot diferido — plan de rollback vacío por diseño.
                summary="Generar el Bashed Patch con Wrye Bash.",
            )
            await self._journal.persist_action_manifest(
                manifest,
                agent_id=self.AGENT_ID,
                transaction_id=journal_tx_id,
            )
            return journal_tx_id
        except Exception as exc:  # noqa: BLE001 — boundary: cualquier fallo del journal/builder
            await self._mark_journal_rolled_back(journal_tx_id)
            raise _ActionManifestError(str(exc)) from exc

    async def _emit_flight_report(self, journal_tx_id: int) -> None:
        """Compone y persiste el FlightReport del Ritual terminado (T-28).

        Post-vuelo y best-effort: lee la caja negra del journal (manifiesto + estado
        REAL de la TX). Un fallo se loguea y NO rompe un run ya exitoso.
        """
        from sky_claw.antigravity.orchestrator.preview.flight_report import (
            compose_flight_report_from_journal,
        )

        assert self._journal is not None  # cableado verificado por el caller
        try:
            report = await compose_flight_report_from_journal(self._journal, transaction_id=journal_tx_id)
            await self._journal.persist_flight_report(
                report,
                agent_id=self.AGENT_ID,
                transaction_id=journal_tx_id,
            )
        except Exception:  # noqa: BLE001 — boundary best-effort del journal
            logger.error("Fallo al persistir el informe de vuelo de la TX %d", journal_tx_id, exc_info=True)

    async def _mark_journal_rolled_back(self, journal_tx_id: int | None) -> None:
        """Marca la TX del journal como rolled-back (best-effort).

        Se llama en los caminos de excepción; si el journal falla acá NO debe
        enmascarar el error original ni romper el contrato de dict serializable.
        """
        if journal_tx_id is None or self._journal is None:
            return
        try:
            await self._journal.mark_transaction_rolled_back(journal_tx_id)
        except Exception:  # noqa: BLE001 — boundary best-effort del journal
            logger.error("Fallo al marcar la TX del journal %d como rolled-back", journal_tx_id, exc_info=True)

    async def execute_pipeline(
        self,
        *,
        profile: str,
        validate_limit: bool = True,
    ) -> dict[str, Any]:
        """Genera el Bashed Patch con Wrye Bash bajo el lock de behavior/load-order.

        Flujo:
        0. Preflight brutal (T-16c, PR B): un semáforo ROJO cancela sin tocar nada.
        1. [M-04] Validación de límite de plugins (guard compartido inyectado).
        2. Ejecutar ``WryeBashRunner.generate_bashed_patch()`` **bajo el lock**.
        3. Observabilidad vía logging estructurado.

        Siempre devuelve un ``dict`` serializable para los modos de fallo conocidos
        (preflight rojo, guard M-04, runner no disponible, contención de lock, error de
        ejecución) en vez de propagar la excepción, para que el dispatcher lo reenvíe
        verbatim.
        """
        logger.info(
            "[FASE-6] Iniciando generación de Bashed Patch para perfil '%s'.",
            profile,
        )

        # PASO 0: Preflight brutal ANTES de tocar nada (T-16c, PR B, FASE 6). Un
        # semáforo ROJO (p. ej. el destino del Bashed Patch sin permisos, un master
        # faltante o el límite de plugins excedido) cancela Wrye Bash sin correr el
        # subproceso ni tomar el lock. Amarillo/verde no bloquean; el reporte se
        # surface al panel en todos los retornos.
        preflight = self._ensure_preflight()
        preflight_report: PreflightReport | None = None
        if preflight is not None:
            preflight_report = await preflight.run()
            if preflight_report.blocks_mutations:
                red = "; ".join(c.summary for c in preflight_report.checks if c.status.value == "red")
                logger.warning("Wrye Bash (fase 6) bloqueado por preflight en rojo: %s", red)
                return {
                    "success": False,
                    "reason": "PreflightBlocked",
                    "message": f"Preflight en rojo, Bashed Patch cancelado: {red}",
                    "error": red,
                    "preflight": preflight_report.to_dict(),
                }

        # PASO 1: Gate preventivo M-04 — guard compartido inyectado por el supervisor.
        if validate_limit and self._plugin_limit_guard is not None:
            guard_result = await self._plugin_limit_guard(profile)
            if not guard_result.get("valid", True):
                logger.error(
                    "[FASE-6] Abortando Bashed Patch: validación M-04 falló. %s",
                    guard_result.get("error"),
                )
                return _attach_preflight(
                    {
                        "success": False,
                        "aborted_by": "plugin_limit_guard",
                        "plugin_count": guard_result.get("plugin_count"),
                        "error": guard_result.get("error"),
                        "message": guard_result.get("error") or "",
                    },
                    preflight_report,
                )

        # PASO 2: Asegurar runner inicializado.
        try:
            runner = self.ensure_runner()
        except WryeBashExecutionError as exc:
            logger.error("[FASE-6] Error inicializando WryeBashRunner: %s", exc)
            return _attach_preflight({"success": False, "error": str(exc), "message": str(exc)}, preflight_report)

        # PASO 3: Ejecutar la generación BAJO lock anidado — Wrye Bash era el único
        # ritual mutante sin serializar (hueco de concurrencia).
        #  - EXTERNO 'Bashed Patch, 0.esp': serializa contra otra corrida de Wrye Bash.
        #  - INTERNO 'load-order': el Bashed Patch se arma del orden ACTIVO que LOOT
        #    reescribe (plugins.txt/loadorder.txt). Sin este lock, un sort de LOOT
        #    concurrente (otro cliente/agente) reescribiría el orden a mitad de la
        #    corrida y el patch saldría de un orden inestable, violando el invariante
        #    "Bashed Patch después de LOOT" (review Codex #315; mismo patrón que grass).
        # Orden de adquisición FIJO (bashed-patch → load-order), consistente con grass
        # (grass-cache → load-order): nadie toma load-order primero, así que no hay
        # deadlock. Snapshot diferido en ambos: la salida sale vía la VFS de MO2 con cwd
        # y Wrye Bash solo LEE el load order (LOOT es quien lo snapshotea al mutarlo).
        journal_tx_id: int | None = None
        # Una vez commiteada la TX, ninguna ruta posterior debe re-marcarla rolled-back
        # (una cancelación post-commit corrompería el audit trail — review Codex #249/#318).
        journal_committed = False
        try:
            async with (
                SnapshotTransactionLock(
                    lock_manager=self._lock_manager,
                    snapshot_manager=self._snapshot_manager,
                    resource_id=self.RESOURCE_ID,
                    agent_id=self.AGENT_ID,
                    target_files=[],  # snapshot diferido — ver docstring del módulo
                    metadata={"source": "wrye_bash_bashed_patch", "profile": profile},
                ),
                SnapshotTransactionLock(
                    lock_manager=self._lock_manager,
                    snapshot_manager=self._snapshot_manager,
                    resource_id=LOAD_ORDER_RESOURCE_ID,
                    agent_id=self.AGENT_ID,
                    target_files=[],
                    metadata={"source": "wrye_bash_bashed_patch", "profile": profile},
                ),
            ):
                # T-26 (ADR 0002): emitir la caja negra ANTES de mutar. Si el journal
                # está cableado y la emisión falla, Wrye Bash NO corre (fail-closed:
                # se lanza dentro del lock, nada mutó). El path sin journal la salta.
                if self._journal is not None:
                    journal_tx_id = await self._emit_action_manifest(self._bashed_patch_target(runner))
                result = await runner.generate_bashed_patch()
            # Locks liberados. Cerrar la caja negra según el resultado real del run.
            if journal_tx_id is not None and self._journal is not None:
                if result.success:
                    try:
                        await self._journal.commit_transaction(journal_tx_id)
                        journal_committed = True
                    except Exception:  # noqa: BLE001 — boundary best-effort del journal
                        logger.error(
                            "Fallo al commitear la TX del journal %d tras el Bashed Patch exitoso",
                            journal_tx_id,
                            exc_info=True,
                        )
                    # T-28: cerrar la caja negra con el informe post-vuelo (best-effort).
                    await self._emit_flight_report(journal_tx_id)
                else:
                    # Run non-zero: no commitear, marcar rolled-back (no dejar PENDING).
                    await self._mark_journal_rolled_back(journal_tx_id)
        except LockAcquisitionError as exc:
            # Contención en __aenter__, antes del manifiesto: journal_tx_id sigue None.
            logger.warning("Lock contention (bashed-patch/load-order): %s", exc)
            detail = f"Could not acquire bashed-patch/load-order lock: {exc}"
            return _attach_preflight({"success": False, "error": detail, "message": detail}, preflight_report)
        except _ActionManifestError as exc:
            # La caja negra no se pudo emitir: Wrye Bash no corrió (fail-closed). La TX
            # recién abierta ya fue marcada rolled-back dentro de _emit_action_manifest.
            logger.error("[FASE-6] No se pudo emitir el ActionManifest; abortado: %s", exc)
            detail = f"Manifiesto de vuelo requerido no emitido: {exc}"
            return _attach_preflight(
                {"success": False, "reason": "ActionManifestFailed", "error": detail, "message": detail},
                preflight_report,
            )
        except LockError as exc:
            # __aexit__ del lock puede lanzar LockLeaseLostError (renovación fallida /
            # lease expirado durante una corrida larga) u otros errores de la capa de
            # lock en la salida limpia. La tool se despacha SOLO con el gate HITL (sin
            # el middleware que envuelve errores), así que sin este catch la excepción
            # burbujea como crash de dispatch. La pérdida de lease invalida la
            # exclusividad → reportar éxito mentiría: devolvemos success=False honesto.
            # Si no se commiteó, cerrar la TX del journal (no dejar PENDING).
            if not journal_committed:
                await self._mark_journal_rolled_back(journal_tx_id)
            logger.error("[FASE-6] Error de la capa de lock durante Wrye Bash: %s", exc)
            detail = f"Lock error durante la generación del Bashed Patch: {exc}"
            return _attach_preflight({"success": False, "error": detail, "message": detail}, preflight_report)
        except WryeBashExecutionError as exc:
            await self._mark_journal_rolled_back(journal_tx_id)
            logger.error("[FASE-6] WryeBashExecutionError: %s", exc)
            return _attach_preflight({"success": False, "error": str(exc), "message": str(exc)}, preflight_report)
        except asyncio.CancelledError:
            # Cancelación (shutdown/timeout del task): cerrar la TX del journal para no
            # dejarla PENDING (salvo que ya se commiteara, #249) y re-lanzar (review
            # Codex #318). Los __aexit__ de los locks ya liberaron.
            if not journal_committed:
                await self._mark_journal_rolled_back(journal_tx_id)
            raise
        except Exception as exc:  # noqa: BLE001 — T11: SIEMPRE devolver dict serializable
            # Red de seguridad final para cualquier error inesperado en la salida del
            # lock/journal (no Lock* ni WryeBashExecutionError): no dejar la TX PENDING
            # ni romper el contrato "siempre devolver dict".
            if not journal_committed:
                await self._mark_journal_rolled_back(journal_tx_id)
            logger.error("[FASE-6] Error inesperado durante Wrye Bash: %s", exc, exc_info=True)
            return _attach_preflight({"success": False, "error": str(exc), "message": str(exc)}, preflight_report)

        # PASO 4: Observabilidad vía logging estructurado (complementa la caja negra
        # del journal — el manifiesto/informe ya se persistieron arriba si está cableado).
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

        # Contrato de tool-result compartido (AGENTS.md): ``message`` canónico junto a
        # los campos estructurados; vacío en éxito, el detalle del fallo (stderr/stdout)
        # cuando el subproceso salió non-zero. Sin esto, StateGraphIntegration lee
        # error→reason→message y reportaría "falló sin detalle" (review Codex #315).
        message = "" if result.success else (result.stderr or result.stdout or "")
        return _attach_preflight(
            {
                "success": result.success,
                "message": message,
                "return_code": result.return_code,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "duration_seconds": result.duration_seconds,
            },
            preflight_report,
        )
