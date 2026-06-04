"""Pandora Behavior Engine Runner for Sky-Claw.

Implements M-02 Pandora Runner specifications.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pathlib

logger = logging.getLogger(__name__)


async def _kill_and_reap(process: asyncio.subprocess.Process | None) -> None:
    """Kill *process* and reap it so no orphaned OS process survives.

    Safe with ``None`` (process never spawned) and tolerant of an already-exited
    process. Used on timeout, cancellation, and unexpected errors.
    """
    if process is None:
        return
    with contextlib.suppress(ProcessLookupError):
        process.kill()
    # Suppress only the reap timeout — never cancellation. If shutdown cancels us
    # mid-reap, the CancelledError must propagate (matches the canonical
    # windows_interop._kill_and_reap); the process was already killed above.
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(process.wait(), timeout=3.0)


class PandoraExecutionError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class PandoraConfig:
    pandora_exe: pathlib.Path
    game_path: pathlib.Path
    timeout_seconds: float = 300.0


@dataclass
class PandoraResult:
    success: bool
    return_code: int
    stdout: str
    stderr: str
    duration_seconds: float


class PandoraRunner:
    """Asynchronous runner for Pandora Behavior Engine."""

    def __init__(self, config: PandoraConfig):
        self.config = config

    async def run_pandora(self) -> PandoraResult:
        """Execute Pandora in auto mode for Skyrim Special Edition."""
        logger.info("[M-02] Executing Pandora Behavior Engine...")
        start_time = time.monotonic()

        args = ["--game", "Skyrim Special Edition", "--auto"]

        kwargs = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = 0x08000000

        process: asyncio.subprocess.Process | None = None
        try:
            process = await asyncio.create_subprocess_exec(
                str(self.config.pandora_exe),
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self.config.game_path),
                **kwargs,
            )

            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=self.config.timeout_seconds)

            duration = time.monotonic() - start_time
            success = process.returncode == 0

            return PandoraResult(
                success=success,
                return_code=process.returncode or 0,
                stdout=stdout.decode(errors="replace"),
                stderr=stderr.decode(errors="replace"),
                duration_seconds=duration,
            )

        except TimeoutError:
            logger.error("Pandora execution timed out.")
            await _kill_and_reap(process)
            return PandoraResult(
                success=False,
                return_code=-1,
                stdout="",
                stderr="Timeout during Pandora execution",
                duration_seconds=time.monotonic() - start_time,
            )
        except asyncio.CancelledError:
            # Graceful shutdown: never leave the GUI tool running after cancel.
            await _kill_and_reap(process)
            raise
        except Exception as e:
            logger.error(f"Pandora execution failed: {e}")
            await _kill_and_reap(process)
            raise PandoraExecutionError(f"Failed to execute Pandora: {e}") from e
