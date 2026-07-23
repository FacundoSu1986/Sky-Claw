"""VRAMrPipelineService — orquestador async de la herramienta VRAMr.

Copia la estructura de :class:`XEditPipelineService` con tres diferencias
explícitas pedidas por la misión:

* Validación Zero-Trust vía :class:`PathValidator` (no ``PathResolutionService``).
* Subprocess directo con :func:`asyncio.create_subprocess_exec` (no runner intermedio).
* Streaming línea-a-línea de ``stdout``/``stderr`` al logger en tiempo real.

VRAMr no muta archivos existentes; escribe a un ``output_dir`` fresco, así que
el lock se toma con ``target_files=[]`` (mismo patrón "serializar sin
snapshotear" que ``pandora_service``): no hay archivo previo que restaurar. El
rollback ante fallo es propio — limpiar los artefactos NUEVOS de ``output_dir``
(dejando intacto cualquier archivo preexistente) + marcar el journal-TX como
``rolled_back``.

Se usa :class:`SnapshotTransactionLock` (no ``acquire_lock`` crudo) porque un
run de VRAMr dura horas (default 1 h) y el lease del lock expira a los 10 min
(``DEFAULT_LOCK_TTL_SECONDS``): sin el heartbeat + auto-renew que trae el
context manager, la serialización desaparecía en silencio a mitad de run y otro
mutador podía entrar (§2.1 reporte de consistencia de la auditoría).

Regla T11: el servicio nunca propaga excepciones al caller — siempre
devuelve un ``dict[str, Any]`` serializable.
"""

from __future__ import annotations

import asyncio
import logging
import pathlib
import shutil
import sys
import time
from typing import TYPE_CHECKING, Any

from sky_claw.antigravity.core.event_bus import CoreEventBus, Event
from sky_claw.antigravity.db.locks import (
    DistributedLockManager,
    LockAcquisitionError,
    LockLeaseLostError,
    SnapshotTransactionLock,
)
from sky_claw.antigravity.security.path_validator import PathValidator, PathViolationError
from sky_claw.local.tools._process import kill_and_reap

if TYPE_CHECKING:
    from sky_claw.antigravity.db.journal import OperationJournal
    from sky_claw.antigravity.db.snapshot_manager import FileSnapshotManager

logger = logging.getLogger(__name__)

_CREATE_NO_WINDOW = 0x08000000
_TAIL_LINES = 20
_DEFAULT_TIMEOUT_SECONDS = 3600.0
# S-4: cota para drenar buffers residuales tras la salida normal del proceso. Sin
# ella, un nieto que heredó el pipe (write-end sin cerrar) dejaría el gather del
# path de éxito colgado indefinidamente.
_DRAIN_GRACE_SECONDS = 10.0


class VRAMrExecutionError(Exception):
    """Lanzada cuando VRAMr termina con un exit-code no-cero dentro del lock."""

    def __init__(self, exit_code: int, stderr_tail: str = "") -> None:
        self.exit_code = exit_code
        self.stderr_tail = stderr_tail
        super().__init__(f"VRAMr failed with exit code {exit_code}: {stderr_tail}")


class VRAMrPipelineService:
    """Servicio async que orquesta el binario VRAMr bajo lock transaccional.

    Coordina :class:`DistributedLockManager` (serialización), journal
    (traza de transacciones) y :class:`CoreEventBus` (observabilidad)
    alrededor de una invocación de subprocess con streaming de salida.
    """

    AGENT_ID: str = "vramr-service"

    def __init__(
        self,
        *,
        lock_manager: DistributedLockManager,
        snapshot_manager: FileSnapshotManager,
        journal: OperationJournal,
        path_validator: PathValidator,
        event_bus: CoreEventBus,
        default_timeout: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._lock_manager = lock_manager
        # target_files=[] siempre (VRAMr no muta entrada), pero SnapshotTransactionLock
        # lo exige — es quien aporta el heartbeat/auto-renew que el run largo necesita.
        self._snapshot_manager = snapshot_manager
        self._journal = journal
        self._path_validator = path_validator
        self._event_bus = event_bus
        self._default_timeout = default_timeout

    async def execute_pipeline(
        self,
        *,
        vramr_exe: str | pathlib.Path,
        args: list[str],
        output_dir: str | pathlib.Path,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Ejecuta VRAMr con protección transaccional.

        Args:
            vramr_exe: Ruta al ejecutable de VRAMr. Se valida con
                ``self._path_validator.validate()``.
            args: Lista explícita de argumentos CLI para VRAMr.
            output_dir: Directorio de salida. Se valida y se usa como
                ``resource_id`` del lock. Ante fallo, solo se eliminan
                los artefactos nuevos creados durante la ejecución.
            timeout: Timeout en segundos; si es ``None`` se usa
                ``self._default_timeout``.

        Returns:
            ``dict`` serializable con ``success``, ``exit_code``,
            ``stdout_tail``, ``stderr_tail``, ``error``, ``rolled_back``
            y ``duration_seconds``.
        """
        t0 = time.monotonic()
        effective_timeout = timeout if timeout is not None else self._default_timeout

        # --- Validación Zero-Trust ANTES de publicar eventos ---
        try:
            validated_exe = self._path_validator.validate(vramr_exe)
            validated_output = self._path_validator.validate(output_dir)
        except PathViolationError as exc:
            logger.error("Path violation en VRAMr pipeline: %s", exc)
            return self._error_dict(f"Path violation: {exc}")

        # Snapshot de contenido preexistente para cleanup selectivo
        existed_before = self._snapshot_existing(validated_output)

        started_payload = {
            "vramr_exe": str(validated_exe),
            "output_dir": str(validated_output),
            "args": list(args),
            "started_at": time.time(),
        }
        await self._event_bus.publish(
            Event(
                topic="vramr.pipeline.started",
                payload=started_payload,
                source=self.AGENT_ID,
            )
        )

        exit_code: int = -1
        stdout_lines: list[str] = []
        stderr_lines: list[str] = []
        error: str | None = None
        rolled_back: bool = False
        tx_id: int | None = None

        # Usar el path completo como resource_id para unicidad.
        # (evita colisiones entre outputs en distintos paths con el mismo basename)
        lock_resource_id = str(validated_output)

        try:
            async with SnapshotTransactionLock(
                lock_manager=self._lock_manager,
                snapshot_manager=self._snapshot_manager,
                resource_id=lock_resource_id,
                agent_id=self.AGENT_ID,
                target_files=[],  # VRAMr no muta entrada — serializa, no snapshotea
                metadata={"source": "vramr_pipeline", "output_dir": lock_resource_id},
            ):
                # Todos los cambios + post-procesado ocurren dentro del lock
                # para mantener atomicidad y evitar ventanas de race. El heartbeat
                # del lock renueva el lease durante el run largo (horas).
                try:
                    tx_id = await self._journal.begin_transaction(
                        description="vramr_pipeline",
                        agent_id=self.AGENT_ID,
                    )
                    exit_code, stdout_lines, stderr_lines = await self._run_vramr(
                        validated_exe,
                        list(args),
                        effective_timeout,
                    )
                    if exit_code != 0:
                        tail = "\n".join(stderr_lines[-_TAIL_LINES:])
                        raise VRAMrExecutionError(exit_code, tail)

                    # Commit dentro del lock
                    await self._journal.commit_transaction(tx_id)

                except VRAMrExecutionError as exc:
                    rolled_back = True
                    self._cleanup_output_dir(validated_output, existed_before)
                    await self._safe_mark_rolled_back(tx_id)
                    logger.error("VRAMr falló: %s", exc)
                    error = f"VRAMr exit {exc.exit_code}: {exc.stderr_tail}"

                except TimeoutError:
                    rolled_back = tx_id is not None
                    self._cleanup_output_dir(validated_output, existed_before)
                    await self._safe_mark_rolled_back(tx_id)
                    logger.error(
                        "VRAMr excedió el timeout de %.1fs",
                        effective_timeout,
                    )
                    error = f"VRAMr timed out after {effective_timeout}s"

                except Exception as exc:  # Regla T11 — catch-all
                    rolled_back = tx_id is not None
                    self._cleanup_output_dir(validated_output, existed_before)
                    await self._safe_mark_rolled_back(tx_id)
                    logger.error(
                        "Error inesperado en VRAMr pipeline: %s",
                        exc,
                        exc_info=True,
                    )
                    error = f"Unexpected error: {exc}"

        except LockAcquisitionError as exc:
            logger.warning(
                "Lock contention para VRAMr (%s): %s",
                lock_resource_id,
                exc,
            )
            error = f"Lock contention: {exc}"

        except LockLeaseLostError as exc:
            # El heartbeat perdió el lease DURANTE/tras el run (renovación
            # fallida, otro agente pudo tomar el lock): __aexit__ NO revierte
            # ante lease loss (evita clobberear una mutación concurrente
            # ajena) — el journal ya pudo haberse commiteado (VRAMr completó
            # con exit 0) pero la exclusividad no estuvo garantizada durante
            # TODO el run largo, así que output_dir puede tener contenido de
            # OTRO proceso. No se limpia (limpiar podría borrar su trabajo);
            # se reporta como fallo con el estado incierto (review Codex #316).
            logger.error(
                "Lease del lock '%s' perdido durante/tras VRAMr: %s",
                lock_resource_id,
                exc,
            )
            error = f"Lock lease lost: {exc}"

        duration = time.monotonic() - t0
        success = error is None

        completed_payload = {
            "vramr_exe": str(validated_exe),
            "output_dir": str(validated_output),
            "success": success,
            "exit_code": exit_code,
            "stdout_line_count": len(stdout_lines),
            "stderr_line_count": len(stderr_lines),
            "duration_seconds": round(duration, 3),
            "rolled_back": rolled_back,
            "completed_at": time.time(),
        }
        await self._event_bus.publish(
            Event(
                topic="vramr.pipeline.completed",
                payload=completed_payload,
                source=self.AGENT_ID,
            )
        )

        if success:
            logger.info(
                "VRAMr completado: exe=%s exit=0 stdout_lines=%d stderr_lines=%d (%.2fs)",
                validated_exe.name,
                len(stdout_lines),
                len(stderr_lines),
                duration,
            )

        return self._result_to_dict(
            exit_code=exit_code,
            stdout_lines=stdout_lines,
            stderr_lines=stderr_lines,
            error=error,
            rolled_back=rolled_back,
            duration=duration,
        )

    # ------------------------------------------------------------------
    # Subprocess execution + streaming
    # ------------------------------------------------------------------

    async def _run_vramr(
        self,
        exe: pathlib.Path,
        args: list[str],
        timeout: float,
    ) -> tuple[int, list[str], list[str]]:
        """Lanza VRAMr y drena stdout/stderr línea-a-línea en tiempo real."""
        kwargs: dict[str, Any] = {
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.PIPE,
        }
        if sys.platform == "win32":
            kwargs["creationflags"] = _CREATE_NO_WINDOW

        proc = await asyncio.create_subprocess_exec(str(exe), *args, **kwargs)

        stdout_lines: list[str] = []
        stderr_lines: list[str] = []
        drain_out = asyncio.create_task(
            self._read_stream(proc.stdout, stdout_lines, logging.INFO),
            name="vramr-stdout-drain",
        )
        drain_err = asyncio.create_task(
            self._read_stream(proc.stderr, stderr_lines, logging.WARNING),
            name="vramr-stderr-drain",
        )

        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout)
        except asyncio.CancelledError:
            # La cancelación externa no puede dejar VRAMr escribiendo tras el
            # rollback/liberación del lock de la pipeline.
            await kill_and_reap(proc)
            drain_out.cancel()
            drain_err.cancel()
            await asyncio.gather(drain_out, drain_err, return_exceptions=True)
            raise
        except TimeoutError:
            # kill_and_reap mata el ÁRBOL (taskkill /F /T en Windows) ANTES del
            # proc.kill(): un proc.kill() pelado terminaba solo el hijo directo y
            # dejaba huérfanos los nietos (workers de compresión de texturas)
            # reteniendo file locks del output. Espeja el path de cancelación (arriba).
            await kill_and_reap(proc)
            drain_out.cancel()
            drain_err.cancel()
            # gather(return_exceptions=True) sobre tasks ya canceladas captura sus
            # CancelledError como resultados (no relanza); sin `suppress` una
            # cancelación externa del caller propaga como corresponde.
            await asyncio.gather(drain_out, drain_err, return_exceptions=True)
            raise

        # S-4: el proceso ya terminó; drenamos con una cota. Si un nieto heredó el
        # pipe y sigue vivo, el EOF nunca llega y sin timeout este gather colgaría
        # para siempre. Agotada la gracia, cancelamos los drains y seguimos con las
        # líneas parciales ya capturadas (el proceso ya reportó su return code).
        try:
            results = await asyncio.wait_for(
                asyncio.gather(drain_out, drain_err, return_exceptions=True),
                timeout=_DRAIN_GRACE_SECONDS,
            )
        except TimeoutError:
            logger.warning(
                "VRAMr: los drains no cerraron en %.1fs tras la salida del proceso "
                "(posible nieto con pipe heredado); se continúa con output parcial.",
                _DRAIN_GRACE_SECONDS,
            )
            drain_out.cancel()
            drain_err.cancel()
            # gather(return_exceptions=True) sobre tasks ya canceladas captura sus
            # CancelledError como resultados (no relanza); sin `suppress` una
            # cancelación externa del caller propaga como corresponde.
            await asyncio.gather(drain_out, drain_err, return_exceptions=True)
            results = []
        for r in results:
            if isinstance(r, BaseException):
                logger.warning("VRAMr stream drain lanzó excepción: %r", r)

        exit_code = proc.returncode if proc.returncode is not None else -1
        return exit_code, stdout_lines, stderr_lines

    @staticmethod
    async def _read_stream(
        stream: asyncio.StreamReader | None,
        bucket: list[str],
        log_level: int,
    ) -> None:
        """Drena ``stream`` línea por línea, logueando y capturando cada una."""
        if stream is None:
            return
        while True:
            line = await stream.readline()
            if not line:
                break
            decoded = line.decode("utf-8", errors="replace").rstrip()
            bucket.append(decoded)
            logger.log(log_level, "[VRAMr] %s", decoded)

    # ------------------------------------------------------------------
    # Cleanup helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _snapshot_existing(output_dir: pathlib.Path) -> set[pathlib.Path]:
        """Captura el conjunto de entradas ya presentes en ``output_dir``."""
        if not output_dir.exists() or not output_dir.is_dir():
            return set()
        return set(output_dir.iterdir())

    @staticmethod
    def _cleanup_output_dir(
        output_dir: pathlib.Path,
        existed_before: set[pathlib.Path],
    ) -> None:
        """Elimina solo las entradas nuevas creadas durante este run."""
        if not output_dir.exists() or not output_dir.is_dir():
            return
        for entry in list(output_dir.iterdir()):
            if entry in existed_before:
                continue
            try:
                if entry.is_dir() and not entry.is_symlink():
                    shutil.rmtree(entry, ignore_errors=True)
                else:
                    entry.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning("No pude limpiar %s: %s", entry, exc)

    async def _safe_mark_rolled_back(self, tx_id: int | None) -> None:
        """Marca el journal-TX como rolled-back sin propagar errores secundarios."""
        if tx_id is None:
            return
        try:
            await self._journal.mark_transaction_rolled_back(tx_id)
        except Exception as journal_exc:
            logger.critical(
                "Fallo al marcar journal TX %d como rolled_back: %s",
                tx_id,
                journal_exc,
                exc_info=True,
            )

    # ------------------------------------------------------------------
    # Result helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _result_to_dict(
        *,
        exit_code: int,
        stdout_lines: list[str],
        stderr_lines: list[str],
        error: str | None,
        rolled_back: bool,
        duration: float,
    ) -> dict[str, Any]:
        """Construye el dict de retorno serializable."""
        return {
            "success": error is None,
            "exit_code": exit_code,
            "stdout_line_count": len(stdout_lines),
            "stderr_line_count": len(stderr_lines),
            "stdout_tail": list(stdout_lines[-_TAIL_LINES:]),
            "stderr_tail": list(stderr_lines[-_TAIL_LINES:]),
            "error": error,
            "rolled_back": rolled_back,
            "duration_seconds": round(duration, 3),
        }

    @staticmethod
    def _error_dict(message: str) -> dict[str, Any]:
        """Dict de error para retornos tempranos (pre-lock, pre-evento)."""
        return {
            "success": False,
            "exit_code": -1,
            "stdout_line_count": 0,
            "stderr_line_count": 0,
            "stdout_tail": [],
            "stderr_tail": [],
            "error": message,
            "rolled_back": False,
            "duration_seconds": 0.0,
        }
