import asyncio
import contextlib
import logging
import pathlib
from collections.abc import Callable, Coroutine
from typing import Any

from sky_claw.config import SystemPaths
from sky_claw.core.windows_interop import ModdingToolsAgent
from sky_claw.security.path_validator import PathValidator, PathViolation

# Standard 2026 Process Orchestration
logger = logging.getLogger("SkyClaw.ManagedExecutor")


class ManagedToolExecutor:
    """
    MANAGED TOOL EXECUTOR (STANDARD 2026)

    Orchestrates legacy Windows modding binaries from a WSL2 Linux environment.
    Handles dynamic path translation, real-time log streaming (telemetry),
    and strict process lifecycle management (Zombie Prevention).
    """

    def __init__(self, timeout: float = 300.0):
        self.timeout: float = timeout
        self.proc: asyncio.subprocess.Process | None = None
        self._abort_event: asyncio.Event = asyncio.Event()

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
        # Validator setup based on SystemPaths modding root bounds for strict safety
        try:
            validator = PathValidator([SystemPaths.modding_root()])
        except Exception as ve:
            logger.error(f"🚨 PathValidator initialization failed: {ve}")
            return -1

        for arg in args:
            if arg.startswith("/mnt/") or arg.startswith("/"):
                # WSL path — must translate AND pass validation. No fallback.
                try:
                    translated_path = await ModdingToolsAgent.translate_path_wsl_to_win(
                        arg
                    )
                except Exception as te:
                    logger.error(
                        "🚨 ABORT: WSL path translation failed for arg — rejecting. %s",
                        te,
                    )
                    return -1
                try:
                    validator.validate(translated_path)
                except PathViolation as pv:
                    logger.error(
                        "🚨 ABORT (Fail-Safe): Path Traversal Detected! %s", pv
                    )
                    return -1
                win_args.append(translated_path)
            elif pathlib.Path(arg).is_absolute():
                # Windows absolute path — apply base-directory jailing via pathlib
                try:
                    resolved = pathlib.Path(arg).resolve(strict=False)
                    modding_root = pathlib.Path(SystemPaths.modding_root()).resolve(
                        strict=False
                    )
                    if not resolved.is_relative_to(modding_root):
                        logger.error(
                            "🚨 ABORT: Base-dir jail violation — '%s' is outside '%s'",
                            resolved,
                            modding_root,
                        )
                        return -1
                except Exception as je:
                    logger.error(
                        "🚨 ABORT: Path resolution failed during jailing: %s", je
                    )
                    return -1
                win_args.append(arg)
            else:
                # Non-path argument (flag, option, plain string) — pass through
                win_args.append(arg)

        logger.info(f"🚀 EXECUTOR [WSL2_INVOKE]: {binary_path}")

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
            monitor_task = asyncio.create_task(
                self._stream_telemetry(on_output_callback)
            )

            try:
                # Wait for completion OR timeout OR abort signal
                await asyncio.wait_for(self.proc.wait(), timeout=self.timeout)
                await monitor_task
            except TimeoutError:
                logger.error(f"⚠️ WATCHDOG: Timeout de {self.timeout}s alcanzado.")
                await self.abort()
                raise
            except asyncio.CancelledError:
                await self.abort()
                raise

            if self._abort_event.is_set():
                return -1

            return self.proc.returncode if self.proc.returncode is not None else 0

        except Exception as e:
            logger.exception(f"❌ EXECUTOR ERROR: {e}")
            await self.abort()
            return -1

    async def _stream_telemetry(
        self, callback: Callable[[str], Coroutine[Any, Any, None]] | None
    ):
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
        if self.proc:
            with contextlib.suppress(ProcessLookupError):
                self.proc.terminate()

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
                logger.warning(
                    "💀 ABORT: El proceso no responde a terminate(). Usando kill()."
                )
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
