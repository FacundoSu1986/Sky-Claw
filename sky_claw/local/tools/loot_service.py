"""LootSortingService — LOOT load-order sorting under the distributed lock.

Audit #190 fast-follow. LOOT ``--sort`` rewrites the shared load order
(``plugins.txt`` / ``loadorder.txt``), exactly the serializable state the other
mutating runners (xEdit, Synthesis, DynDOLOD) already guard with
:class:`SnapshotTransactionLock`. The real-execution path previously called the
deprecated, lock-free ``ModdingToolsAgent.run_loot``; this service closes that
gap so a real sort serializes against:

* another concurrent LOOT sort, and
* the dry-run preview chain (which snapshots and force-reverts the same load
  order) — both share :data:`LOAD_ORDER_RESOURCE_ID`.

**Snapshot rollback (T-06):** los ``target_files`` se resuelven con
:class:`LoadOrderFileResolver` — la unión de ``plugins.txt``/``loadorder.txt``
existentes en LOCALAPPDATA (LOOT corre fuera del VFS con ``--game-path``), el
profile de MO2 y un override explícito. Un sort que lanza (timeout) o sale con
error restaura el snapshot; si no se encuentra ningún candidato (entorno no
configurado), se degrada a serialización-sola con warning, el comportamiento
previo al T-06.
"""

from __future__ import annotations

import asyncio
import logging
import pathlib
from typing import TYPE_CHECKING, Any

from sky_claw.antigravity.db.locks import (
    DistributedLockManager,
    LockAcquisitionError,
    SnapshotTransactionLock,
)
from sky_claw.local.loot.cli import (
    LOOTConfig,
    LOOTNotFoundError,
    LOOTRunner,
    LOOTTimeoutError,
)
from sky_claw.local.mo2.load_order import LoadOrderFileResolver

if TYPE_CHECKING:
    from sky_claw.antigravity.core.models import LootExecutionParams
    from sky_claw.antigravity.core.path_resolver import PathResolutionService
    from sky_claw.antigravity.db.journal import OperationJournal
    from sky_claw.antigravity.db.snapshot_manager import FileSnapshotManager
    from sky_claw.antigravity.security.path_validator import PathValidator
    from sky_claw.local.loot.parser import LOOTResult
    from sky_claw.local.validators.preflight import PreflightService

logger = logging.getLogger(__name__)

#: Shared lock resource id for the Skyrim load order (``plugins.txt`` /
#: ``loadorder.txt``). Used by this service AND the dry-run preview chain so a
#: real sort and a preview serialize on the load order instead of racing.
LOAD_ORDER_RESOURCE_ID = "load-order"

#: Default LOOT timeout in seconds. Preserves the prior ``run_loot`` allowance
#: (120s) rather than ``LOOTRunner``'s 60s default, so a slow masterlist update
#: or a large load order completing between 60 and 120s is not falsely timed out.
_DEFAULT_LOOT_TIMEOUT_SECONDS = 120

#: Prioridad al elegir el archivo cuyo orden refleja el load order: loadorder.txt
#: tiene el orden completo; plugins.txt lista los activos con marca ``*``.
_LOAD_ORDER_FILE_PRIORITY = ("loadorder.txt", "plugins.txt")


def _primary_load_order_file(paths: list[pathlib.Path]) -> pathlib.Path | None:
    """Elige el archivo de load order que mejor refleja el orden de plugins.

    ``loadorder.txt`` primero (orden completo), luego ``plugins.txt``, y como
    último recurso el primer candidato. ``None`` si no hay ninguno.
    """
    for preferido in _LOAD_ORDER_FILE_PRIORITY:
        for path in paths:
            if path.name.lower() == preferido:
                return path
    return paths[0] if paths else None


def _read_plugin_order(path: pathlib.Path | None) -> list[str]:
    """Lee el orden de plugins de un plugins.txt/loadorder.txt (best-effort).

    Ignora líneas vacías y comentarios (``#``) y quita la marca de activo
    (``*``), de modo que los nombres queden comparables con
    ``LOOTResult.sorted_plugins``. Devuelve ``[]`` ante cualquier problema de
    lectura/decodificación — el informe simplemente no llevará diff de orden.
    """
    if path is None:
        return []
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, ValueError):
        return []
    plugins: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        plugins.append(line.lstrip("*").strip())
    return plugins


class _LootSortFailedError(Exception):
    """Interno: un sort con exit non-zero debe lanzar DENTRO del lock para que
    ``SnapshotTransactionLock.__aexit__`` restaure el load order; el resultado
    original viaja en la excepción para armar la respuesta al caller."""

    def __init__(self, result: LOOTResult) -> None:
        super().__init__(f"LOOT sort failed with return code {result.return_code}")
        self.result = result


class _ActionManifestError(Exception):
    """Interno (T-26): la emisión del manifiesto de vuelo falló. Se lanza DENTRO
    del lock (antes de mutar) para que el sort NO proceda sin manifiesto — la
    caja negra no es opcional cuando el journal está cableado."""


class LootSortingService:
    """Run LOOT's load-order sort under the shared distributed lock.

    Dependencies are injected (DI). ``loot_runner`` is built lazily from
    ``path_resolver`` on first use because tool paths may be unconfigured at
    construction time (mirrors ``SynthesisPipelineService._ensure_*``); it can
    also be injected directly for tests.
    """

    RESOURCE_ID: str = LOAD_ORDER_RESOURCE_ID
    AGENT_ID: str = "loot-sorting-service"

    def __init__(
        self,
        *,
        lock_manager: DistributedLockManager,
        snapshot_manager: FileSnapshotManager,
        path_resolver: PathResolutionService | None = None,
        path_validator: PathValidator | None = None,
        loot_exe: pathlib.Path | None = None,
        timeout: int = _DEFAULT_LOOT_TIMEOUT_SECONDS,
        loot_runner: LOOTRunner | None = None,
        load_order_resolver: LoadOrderFileResolver | None = None,
        preflight: PreflightService | None = None,
        mo2_root: pathlib.Path | None = None,
        journal: OperationJournal | None = None,
    ) -> None:
        self._lock_manager = lock_manager
        self._snapshot_manager = snapshot_manager
        self._path_resolver = path_resolver
        self._path_validator = path_validator
        self._loot_exe = loot_exe
        self._timeout = timeout
        self._loot_runner = loot_runner
        self._load_order_resolver = load_order_resolver
        self._preflight = preflight
        # Hint para el preflight en call sites sin path_resolver (agente):
        # la raíz de la instancia MO2 ya conocida por el caller.
        self._mo2_root = mo2_root
        # T-26 (ADR 0002): cuando el journal está cableado, el sort emite un
        # ActionManifest ANTES de mutar. Opcional para no romper callers legacy.
        self._journal = journal

    def _ensure_preflight(self) -> PreflightService | None:
        """Construye perezosamente el preflight con las piezas disponibles.

        Review Codex PR #239 (P1): los call sites de producción no inyectaban
        ``preflight``, así que el guard era un no-op fuera de los tests — la
        construcción perezosa protege a todos sin cambiarlos. Usa las rutas
        CRUDAS (P2): las resueltas siguen los symlinks y borran exactamente lo
        que el VfsHealthChecker necesita inspeccionar. Review PR #240: también
        cubre el call site del agente (sin path_resolver, con ``mo2_root``/
        ``loot_exe``), cae al auto-detect de MO2 cuando el env no está, espeja
        el fallback ``loot.exe`` del runner, y solo enumera ``mods/`` cuando
        la ruta tiene contraparte validada por el sandbox.
        """
        if self._preflight is not None:
            return self._preflight

        # Import perezoso: validators.preflight llega a tools._process vía
        # loot.version; importarlo a nivel módulo desde tools/ podría ciclar.
        from sky_claw.local.validators.preflight import PreflightService
        from sky_claw.local.validators.vfs_health import VfsHealthChecker

        raw_game: pathlib.Path | None = None
        raw_mo2: pathlib.Path | None = None
        mo2_validated = False
        loot_exe = self._loot_exe

        if self._path_resolver is not None:
            raw_game = self._path_resolver.get_skyrim_path_raw()
            raw_mo2 = self._path_resolver.get_mo2_path_raw()
            if raw_mo2 is not None:
                mo2_validated = self._path_resolver.get_mo2_path() is not None
            else:
                # Sin MO2_PATH, el Supervisor puede resolver la instancia por
                # auto-detección; ese candidato ya viene resuelto (pierde
                # symlinks de ancestros) pero sí permite ver mods enlazados.
                raw_mo2 = self._path_resolver.detect_mo2_path()
                mo2_validated = raw_mo2 is not None
            loot_exe = loot_exe or self._path_resolver.get_loot_exe()

        if raw_mo2 is None and self._mo2_root is not None:
            raw_mo2 = self._mo2_root
            mo2_validated = True  # raíz provista por el caller (instancia MO2 real)

        vfs_checker = None
        if raw_game is not None or raw_mo2 is not None:
            vfs_checker = VfsHealthChecker(
                game_path=raw_game,
                mo2_root=raw_mo2,
                scan_mods_dir=mo2_validated,
            )

        # Espejo del fallback de _ensure_loot_runner: el preflight debe medir
        # la versión del binario que efectivamente va a correr.
        loot_exe = loot_exe or pathlib.Path("loot.exe")

        self._preflight = PreflightService(vfs_checker=vfs_checker, loot_exe=loot_exe)
        return self._preflight

    def _ensure_load_order_resolver(self) -> LoadOrderFileResolver:
        """Construye perezosamente el resolver de load order (mismo patrón que
        ``_ensure_loot_runner``): MO2 root/profile del path resolver si están
        configurados; LOCALAPPDATA lo toma el resolver de su entorno."""
        if self._load_order_resolver is not None:
            return self._load_order_resolver

        mo2_root: pathlib.Path | None = None
        profile = "Default"
        if self._path_resolver is not None:
            mo2_root = self._path_resolver.get_mo2_path()
            if mo2_root is not None:
                profile = self._path_resolver.get_active_profile()

        self._load_order_resolver = LoadOrderFileResolver(mo2_root=mo2_root, profile=profile)
        return self._load_order_resolver

    def _ensure_loot_runner(self) -> LOOTRunner:
        """Lazily build the LOOTRunner, resolving the LOOT exe + game path on first use.

        The LOOT executable is taken from (in order) the injected ``loot_exe``,
        the path resolver (``LOOT_EXE``), then a bare ``loot.exe`` last resort —
        so a configured/discovered install is honored instead of always assuming
        ``loot.exe`` is on the cwd/PATH.
        """
        if self._loot_runner is not None:
            return self._loot_runner

        if self._path_resolver is None:
            raise LOOTNotFoundError("Cannot run LOOT: no loot_runner injected and no path_resolver configured.")

        game_path = self._path_resolver.get_skyrim_path()
        if game_path is None:
            raise LOOTNotFoundError("Cannot run LOOT: SKYRIM_PATH is not configured.")

        loot_exe = self._loot_exe or self._path_resolver.get_loot_exe() or pathlib.Path("loot.exe")

        self._loot_runner = LOOTRunner(
            LOOTConfig(loot_exe=loot_exe, game_path=game_path, timeout=self._timeout),
            path_validator=self._path_validator,
        )
        return self._loot_runner

    async def sort_load_order(
        self,
        params: LootExecutionParams | None = None,
        *,
        update_masterlist: bool | None = None,
        override_preflight: bool = False,
    ) -> dict[str, Any]:
        """Sort the load order under the load-order lock.

        Always returns a serializable ``dict`` for known failure modes (lock
        contention, missing LOOT, timeout) so the caller can forward it verbatim
        instead of propagating an exception.

        ``update_masterlist`` takes precedence when given (the agent tool passes
        ``False`` to preserve its no-network behavior); otherwise it falls back
        to ``params.update_masterlist`` (LootExecutionParams default is True).

        Un preflight en ROJO (T-15: p.ej. symlinks + LOOT <0.29, el escenario
        de LOOT ciego ante el VFS) bloquea el sort salvo ``override_preflight``
        explícito (flujo HITL); el reporte viaja en la respuesta.
        """
        if update_masterlist is None:
            update_masterlist = bool(getattr(params, "update_masterlist", True))

        # Versión de LOOT para el ActionManifest (T-26): la detecta el preflight
        # sin relanzar el binario (review Codex PR #243). None si no corrió.
        loot_version: tuple[int, int, int] | None = None
        preflight = None if override_preflight else self._ensure_preflight()
        if preflight is not None:
            preflight_report = await preflight.run()
            loot_version = preflight.loot_version
            if preflight_report.blocks_mutations:
                detail = "Preflight en rojo: el sort de LOOT quedó bloqueado. " + "; ".join(
                    c.summary for c in preflight_report.checks if c.status.value == "red"
                )
                logger.warning("%s", detail)
                return {
                    "status": "error",
                    "success": False,
                    "message": detail,
                    "logs": detail,
                    "preflight": preflight_report.to_dict(),
                }

        try:
            runner = self._ensure_loot_runner()
        except LOOTNotFoundError as exc:
            logger.error("LOOT runner unavailable: %s", exc)
            return {"status": "error", "success": False, "message": str(exc), "logs": str(exc)}

        # T-06: snapshotear lo que LOOT realmente puede reescribir. Sin
        # candidatos (entorno no configurado) se degrada a serialización-sola,
        # el comportamiento previo — el resolver ya lo dejó logueado.
        load_order = self._ensure_load_order_resolver().resolve()
        target_files = list(load_order.files)
        rolled_back = False

        # Orden ANTES del sort para el diff del informe post-vuelo (T-28): se
        # lee acá porque LOOT reescribe el archivo al ordenar. El "después" será
        # ``result.sorted_plugins``. Best-effort: si no se puede leer, el informe
        # simplemente no lleva load_order_diff (nunca rompe el sort).
        before_order = _read_plugin_order(_primary_load_order_file(target_files))

        # Referencia al lock fuera del with: rolled_back se deriva del resultado
        # REAL del rollback (tx.rollback_completed) — un restore fallido en la
        # ruta de excepción solo se loguea, así que bool(target_files) mentiría
        # (review Codex PR #238).
        tx = SnapshotTransactionLock(
            lock_manager=self._lock_manager,
            snapshot_manager=self._snapshot_manager,
            resource_id=self.RESOURCE_ID,
            agent_id=self.AGENT_ID,
            target_files=target_files,
            metadata={
                "source": "loot_sorting",
                "update_masterlist": update_masterlist,
                "load_order_sources": list(load_order.sources),
            },
        )
        journal_tx_id: int | None = None
        # Una vez commiteada la TX, ninguna ruta posterior debe re-marcarla
        # rolled-back: una cancelación mientras se compone/persiste el informe
        # (post-commit) corrompería el audit trail de una TX ya exitosa
        # (mark_transaction_rolled_back no valida el estado) — review Codex #249.
        journal_committed = False
        try:
            async with tx:
                # T-26 (ADR 0002): emitir la "caja negra de vuelo" ANTES de
                # mutar. Si el journal está cableado y la emisión falla, el sort
                # NO procede (se lanza dentro del lock → __aexit__ revierte).
                if self._journal is not None:
                    journal_tx_id = await self._emit_action_manifest(tx, target_files, loot_version)
                result = await runner.sort(update_masterlist=update_masterlist)
                if not result.success:
                    # Lanzar DENTRO del lock para que __aexit__ restaure el snapshot.
                    raise _LootSortFailedError(result)
            if journal_tx_id is not None and self._journal is not None:
                # El sort ya terminó y el lock se liberó; un fallo de commit del
                # journal es de estado (el manifiesto ya quedó persistido), no
                # debe romper el contrato "siempre devolver dict" (review
                # Copilot PR #243). Best-effort: se loguea con traceback.
                try:
                    await self._journal.commit_transaction(journal_tx_id)
                    journal_committed = True
                except Exception:  # noqa: BLE001 — boundary best-effort del journal
                    logger.error(
                        "Fallo al commitear la transacción del journal %d tras el sort exitoso",
                        journal_tx_id,
                        exc_info=True,
                    )
                # T-28 (ADR 0002): cerrar la caja negra con el informe
                # post-vuelo. Va DESPUÉS del commit para leer el estado real
                # de la TX; también best-effort — el sort ya fue exitoso.
                await self._emit_flight_report(
                    journal_tx_id,
                    before_order=before_order,
                    after_order=result.sorted_plugins,
                )
        except LockAcquisitionError as exc:
            logger.warning("Lock contention on '%s': %s", self.RESOURCE_ID, exc)
            detail = f"Could not acquire load-order lock '{self.RESOURCE_ID}': {exc}"
            return {"status": "error", "success": False, "message": detail, "logs": detail}
        except _ActionManifestError as exc:
            await self._mark_journal_rolled_back(journal_tx_id)
            logger.error("No se pudo emitir el ActionManifest; sort abortado: %s", exc)
            detail = f"Manifiesto de vuelo requerido no emitido: {exc}"
            return {
                "status": "error",
                "success": False,
                "message": detail,
                "logs": detail,
                "rolled_back": tx.rollback_completed,
            }
        except _LootSortFailedError as exc:
            await self._mark_journal_rolled_back(journal_tx_id)
            result = exc.result
            rolled_back = tx.rollback_completed
        except (LOOTNotFoundError, LOOTTimeoutError) as exc:
            await self._mark_journal_rolled_back(journal_tx_id)
            logger.error("LOOT sort failed: %s", exc)
            return {
                "status": "error",
                "success": False,
                "message": str(exc),
                "logs": str(exc),
                "rolled_back": tx.rollback_completed,
            }
        except asyncio.CancelledError:
            # La cancelación propaga; el snapshot ya se restauró en __aexit__.
            # Cerrar la TX del journal es best-effort (no debe tragar la cancelación).
            # Si ya se commiteó (cancelación durante el informe post-vuelo) NO se
            # revierte: la TX fue exitosa y el audit trail no debe mentir (#249).
            if not journal_committed:
                await self._mark_journal_rolled_back(journal_tx_id)
            raise
        except Exception as exc:  # noqa: BLE001 — contrato: sort_load_order SIEMPRE devuelve dict
            # runner.sort() u otra pieza puede lanzar algo fuera de las excepciones
            # LOOT-específicas (RuntimeError de subproceso, error de validador). El
            # snapshot ya se restauró en __aexit__; acá cerramos la TX del manifiesto
            # (no dejar PENDING) y devolvemos un dict serializable en vez de propagar
            # (review Codex PR #243). Si ya se commiteó, no revertir (#249).
            if not journal_committed:
                await self._mark_journal_rolled_back(journal_tx_id)
            logger.error("Error inesperado en el sort de LOOT: %s", exc, exc_info=True)
            return {
                "status": "error",
                "success": False,
                "message": f"Error inesperado durante el sort: {exc}",
                "logs": str(exc),
                "rolled_back": tx.rollback_completed,
            }

        # Contrato compartido (deuda #5): ``message`` canónico junto a los campos
        # estructurados; en éxito queda vacío (el consumidor arma su copy). En
        # fallo, incluir raw_stderr: LOOT puede salir non-zero con el error solo
        # en stderr no estructurado (errors=[] del parser) — review Codex #222.
        message = (
            ""
            if result.success
            else ("; ".join(str(e) for e in result.errors) or result.raw_stderr or result.raw_stdout or "")
        )
        return {
            "status": "success" if result.success else "error",
            "success": result.success,
            "message": message,
            "return_code": result.return_code,
            "sorted_plugins": result.sorted_plugins,
            "warnings": result.warnings,
            "errors": result.errors,
            "logs": result.raw_stdout or "",
            "rolled_back": rolled_back,
        }

    async def _emit_action_manifest(
        self,
        tx: SnapshotTransactionLock,
        target_files: list[pathlib.Path],
        loot_version: tuple[int, int, int] | None,
    ) -> int:
        """Construye y persiste el ActionManifest del sort dentro del lock (T-26).

        Se llama ANTES de ``runner.sort()`` con el journal ya cableado: los
        snapshots del lock (``tx.snapshots``) ya existen acá, así que el plan de
        rollback del manifiesto apunta a snapshots reales. Devuelve el id de la
        transacción del journal para poder commit/rollback después.

        Args:
            tx: El lock activo (sus ``snapshots`` alimentan el plan de rollback).
            target_files: Archivos que el sort tocará.
            loot_version: Versión de LOOT detectada por el preflight, o None.

        Raises:
            _ActionManifestError: Si begin_transaction/persist falla — el sort
                no debe proceder sin la caja negra emitida. La TX del journal
                recién abierta se marca rolled-back para no dejarla PENDING
                (review Codex PR #243).
        """
        from sky_claw.antigravity.orchestrator.preview.action_manifest import build_action_manifest

        assert self._journal is not None  # cableado verificado por el caller
        journal_tx_id: int | None = None
        try:
            journal_tx_id = await self._journal.begin_transaction(
                description="loot_sort",
                agent_id=self.AGENT_ID,
            )
            manifest = build_action_manifest(
                ritual_id=f"loot-sort-{journal_tx_id}",
                tool="LOOT",
                tool_version=".".join(map(str, loot_version)) if loot_version else None,
                target_files=[str(f) for f in target_files],
                snapshots=tx.snapshots,
                summary="Ordenar orden de carga con LOOT.",
            )
            await self._journal.persist_action_manifest(
                manifest,
                agent_id=self.AGENT_ID,
                transaction_id=journal_tx_id,
            )
            return journal_tx_id
        except Exception as exc:  # noqa: BLE001 — boundary: cualquier fallo del journal
            # El journal puede lanzar JournalTransactionError, sqlite3.Error, etc.
            # Todos deben convertirse a _ActionManifestError para que el
            # enforcement devuelva un dict serializable en vez de propagar y
            # romper el contrato de sort_load_order (review Copilot PR #243).
            # No dejar la TX recién abierta en PENDING (review Codex PR #243).
            await self._mark_journal_rolled_back(journal_tx_id)
            raise _ActionManifestError(str(exc)) from exc

    async def _emit_flight_report(
        self,
        journal_tx_id: int,
        *,
        before_order: list[str] | None = None,
        after_order: list[str] | None = None,
    ) -> None:
        """Compone y persiste el FlightReport del sort ya terminado (T-28).

        Lee la caja negra desde el journal — el manifiesto persistido en
        ``_emit_action_manifest`` y el estado REAL de la transacción (si el
        commit best-effort falló, el informe dirá ``pending``: verdad antes
        que optimismo). El manifiesto se emite ANTES del sort y es inmutable,
        así que no puede cargar el orden resultante; el diff real (orden antes
        vs ``result.sorted_plugins``) se calcula acá y se adjunta al informe
        (review Codex #249). Best-effort con la misma disciplina que el commit:
        un fallo se loguea y NO rompe el contrato "siempre devolver dict" ni
        revierte el sort exitoso.
        """
        from sky_claw.antigravity.orchestrator.preview.flight_report import (
            compose_flight_report_from_journal,
        )
        from sky_claw.antigravity.orchestrator.preview.manifest import LoadOrderDiff

        assert self._journal is not None  # cableado verificado por el caller
        try:
            report = await compose_flight_report_from_journal(self._journal, transaction_id=journal_tx_id)
            # Adjuntar el diff real solo si hay orden antes/después y cambió;
            # from_orders solo genera moves para plugins presentes en ambos, así
            # que un listado parcial de LOOT no puede fabricar movimientos falsos.
            if before_order and after_order:
                diff = LoadOrderDiff.from_orders(before_order, after_order)
                if diff.changed:
                    report = report.model_copy(update={"load_order_diff": diff})
            await self._journal.persist_flight_report(
                report,
                agent_id=self.AGENT_ID,
                transaction_id=journal_tx_id,
            )
        except Exception:  # noqa: BLE001 — boundary best-effort del journal
            logger.error(
                "Fallo al persistir el informe de vuelo de la transacción %d",
                journal_tx_id,
                exc_info=True,
            )

    async def _mark_journal_rolled_back(self, journal_tx_id: int | None) -> None:
        """Marca la transacción del journal como rolled-back (best-effort).

        Se llama en los caminos de excepción del sort; si el journal falla acá
        (sqlite/IO) NO debe enmascarar el error original ni romper el contrato
        de respuesta serializable — se suprime con log (review Copilot PR #243).
        """
        if journal_tx_id is None or self._journal is None:
            return
        try:
            await self._journal.mark_transaction_rolled_back(journal_tx_id)
        except Exception:  # noqa: BLE001 — boundary best-effort del journal
            logger.error(
                "Fallo al marcar la transacción del journal %d como rolled-back",
                journal_tx_id,
                exc_info=True,
            )
