from __future__ import annotations

import asyncio
import contextlib
import logging
import pathlib
from collections.abc import Callable, Coroutine
from typing import Any

from sky_claw.antigravity.core.windows_interop import ModdingToolsAgent
from sky_claw.antigravity.security.path_validator import PathValidator, PathViolationError
from sky_claw.config import SystemPaths

# Standard 2026 Process Orchestration
logger = logging.getLogger("SkyClaw.ManagedExecutor")


class ManagedToolExecutor:
    """
    MANAGED TOOL EXECUTOR (STANDARD 2026)

    Orchestrates legacy Windows modding binaries from a WSL2 Linux environment.
    Handles dynamic path translation, real-time log streaming (telemetry),
    and strict process lifecycle management (Zombie Prevention).
    """

    def __init__(
        self,
        timeout: float = 300.0,
        path_validator: PathValidator | None = None,
        *,
        drain_timeout: float = 10.0,
    ) -> None:
        self.timeout: float = timeout
        # E-1: gracia máxima para drenar los pipes de telemetría DESPUÉS de que
        # el proceso principal murió. En condiciones normales el drenaje termina
        # en microsegundos (EOF inmediato); este tope solo muerde si un
        # proceso-nieto heredó el descriptor del pipe y nunca lo cierra.
        self._drain_timeout: float = drain_timeout
        self.proc: asyncio.subprocess.Process | None = None
        self._abort_event: asyncio.Event = asyncio.Event()
        if path_validator is not None:
            self._validator = path_validator
        else:
            try:
                self._validator = PathValidator([SystemPaths.modding_root()])
            except (OSError, ValueError):
                logger.exception("PathValidator initialization failed")
                self._validator = None

    @staticmethod
    async def _resolve_strict_false(path_str: str) -> pathlib.Path:
        """Audit A-1: ``pathlib.Path(path_str).resolve(strict=False)`` off the event loop.

        ``resolve()`` touches the filesystem (Windows reparse points / network
        mounts) and can stall the event loop for hundreds of milliseconds on a
        slow disk.  By dispatching through ``asyncio.to_thread`` we let other
        coroutines (telemetry, IPC, etc.) keep running while we wait for the
        FS.  Behavior is identical to the sync call — same Path object out.

        Kept as a ``@staticmethod`` so it can be unit-tested directly without
        constructing a full ``ManagedToolExecutor``.
        """
        return await asyncio.to_thread(pathlib.Path(path_str).resolve, strict=False)

    async def execute(
        self,
        binary_path: str,
        args: list[str],
        on_output_callback: Callable[[str], Coroutine[Any, Any, None]] | None = None,
    ) -> int:
        """
        Executes binary with WSL->Win interop. Captures output line-by-line.
        """
        self._abort_event.clear()

        # Interop Layer: Translate argument paths to ensure Windows binaries receive valid C:\... strings
        win_args: list[str] = []
        validator = self._validator
        if validator is None:
            logger.error("PathValidator not available")
            return -1

        for arg in args:
            if arg.startswith("/mnt/") or arg.startswith("/"):
                # WSL path — must translate AND pass validation. No fallback.
                try:
                    translated_path = await ModdingToolsAgent.translate_path_wsl_to_win(arg)
                except Exception as te:
                    logger.error(
                        "🚨 ABORT: WSL path translation failed for arg — rejecting. %s",
                        te,
                    )
                    return -1
                try:
                    validator.validate(translated_path)
                except PathViolationError as pv:
                    logger.error("🚨 ABORT (Fail-Safe): Path Traversal Detected! %s", pv)
                    return -1
                win_args.append(translated_path)
            elif pathlib.Path(arg).is_absolute():
                # Windows absolute path — apply base-directory jailing via pathlib.
                # Audit A-1: the two ``resolve(strict=False)`` calls below touch
                # the filesystem (Windows reparse points / mounted network
                # drives) and used to block the event loop. They are now
                # offloaded through ``asyncio.to_thread`` via the helper.
                try:
                    resolved = await self._resolve_strict_false(arg)
                    modding_root = await self._resolve_strict_false(str(SystemPaths.modding_root()))
                    if not resolved.is_relative_to(modding_root):
                        logger.error(
                            "🚨 ABORT: Base-dir jail violation — '%s' is outside '%s'",
                            resolved,
                            modding_root,
                        )
                        return -1
                except Exception as je:
                    logger.error("🚨 ABORT: Path resolution failed during jailing: %s", je)
                    return -1
                win_args.append(arg)
            else:
                # Non-path argument (flag, option, plain string) — pass through
                win_args.append(arg)

        logger.info(f"🚀 EXECUTOR [WSL2_INVOKE]: {binary_path}")

        monitor_task: asyncio.Task[None] | None = None
        try:
            # We must use binary_path (Linux path) to find the file in WSL,
            # but Windows arguments for its execution context.
            self.proc = await asyncio.create_subprocess_exec(
                binary_path,
                *win_args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            # Start monitoring tasks
            monitor_task = asyncio.create_task(self._stream_telemetry(on_output_callback))

            try:
                # Wait for completion OR timeout OR abort signal
                await asyncio.wait_for(self.proc.wait(), timeout=self.timeout)
            except TimeoutError:
                logger.error(f"⚠️ WATCHDOG: Timeout de {self.timeout}s alcanzado.")
                await self.abort()
                raise
            except asyncio.CancelledError:
                await self.abort()
                raise

            # E-1: el proceso principal terminó; drenamos la telemetría con una
            # espera ACOTADA. El ``await monitor_task`` previo era ilimitado: si
            # un proceso-nieto heredaba el descriptor del pipe, el ``readline()``
            # nunca veía EOF y el orquestador quedaba colgado para siempre.
            await self._drain_telemetry(monitor_task)

            if self._abort_event.is_set():
                return -1

            return self.proc.returncode if self.proc.returncode is not None else 0

        except Exception as e:
            logger.exception(f"❌ EXECUTOR ERROR: {e}")
            await self.abort()
            return -1
        finally:
            # Garantía anti-huérfano: el monitor de telemetría nunca sobrevive a
            # execute() (incluidos los caminos de timeout/abort/error, donde el
            # ``raise`` saltaba el drenaje y dejaba la tarea colgada).
            if monitor_task is not None and not monitor_task.done():
                monitor_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await monitor_task

    async def _drain_telemetry(self, monitor_task: asyncio.Task[None]) -> None:
        """E-1: espera ACOTADA al drenaje de los streams de telemetría.

        Tras la muerte del proceso, drenar los pipes debería ser instantáneo
        (solo resta leer lo buffereado hasta EOF). Pero si un proceso-nieto
        heredó el descriptor del pipe, ``readline()`` nunca recibe EOF y el
        drenaje colgaría para siempre. Acotamos con ``_drain_timeout``; al
        expirar, ``wait_for`` deja el monitor cancelado y el ``finally`` de
        ``execute`` lo recoge.
        """
        try:
            await asyncio.wait_for(monitor_task, timeout=self._drain_timeout)
        except TimeoutError:
            logger.warning(
                "⏱️ Drenaje de telemetría excedió %ss (¿pipe heredado por un proceso hijo?). "
                "Cancelando el monitor y continuando.",
                self._drain_timeout,
            )

    async def _stream_telemetry(self, callback: Callable[[str], Coroutine[Any, Any, None]] | None):
        """Streams stdout and stderr concurrently to the provided telemetry callback."""
        if not self.proc or not self.proc.stdout or not self.proc.stderr:
            return

        async def _read_stream(stream: asyncio.StreamReader, prefix: str):
            while True:
                line = await stream.readline()
                if not line:
                    break
                # Standardization: replace invalid chars from Windows pipes
                decoded = line.decode("utf-8", errors="replace").strip()
                if decoded and callback:
                    await callback(f"{prefix}: {decoded}")
                logger.debug(f"[PIPE-{prefix}] {decoded}")

        # H-01: return_exceptions=True para prevenir crashes del orquestador
        await asyncio.gather(
            _read_stream(self.proc.stdout, "OUT"),
            _read_stream(self.proc.stderr, "ERR"),
            return_exceptions=True,
        )

    def signal_abort(self):
        """Triggers the emergency stop from an external thread or task."""
        self._abort_event.set()
        # E-3: capturar la referencia UNA sola vez. ``signal_abort`` puede
        # invocarse desde otro hilo (ver docstring): si un ``abort()`` concurrente
        # nulea ``self.proc`` entre el check y el uso, releer ``self.proc`` daría
        # ``None.terminate()`` → AttributeError (no cubierto por el ``suppress``).
        proc = self.proc
        if proc is not None:
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()

    async def abort(self):
        """Forcefully terminates the managed sub-process and its family."""
        if not self.proc:
            return

        logger.warning("🛑 ABORT: Terminando proceso gerenciado para evitar zombies.")
        try:
            self.proc.terminate()
            # Wait for death
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=3.0)
            except TimeoutError:
                logger.warning("💀 ABORT: El proceso no responde a terminate(). Usando kill().")
                self.proc.kill()
                await self.proc.wait()
        except ProcessLookupError:
            pass
        finally:
            self.proc = None
            self._abort_event.set()

    @property
    def is_running(self) -> bool:
        return self.proc is not None and self.proc.returncode is None
