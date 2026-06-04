"""Wrye Bash Runner for Sky-Claw.

Implements M-01 Wrye Bash Runner specifications.
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
    with contextlib.suppress(TimeoutError, asyncio.CancelledError):
        await asyncio.wait_for(process.wait(), timeout=3.0)


class WryeBashExecutionError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class WryeBashConfig:
    wrye_bash_path: pathlib.Path
    game_path: pathlib.Path
    mo2_path: pathlib.Path
    timeout_seconds: float = 600.0


@dataclass
class WryeBashResult:
    success: bool
    return_code: int
    stdout: str
    stderr: str
    duration_seconds: float


class WryeBashRunner:
    """Asynchronous runner for Wrye Bash (bash.py) for Bashed Patch generation."""

    def __init__(self, config: WryeBashConfig):
        self.config = config

    async def generate_bashed_patch(self) -> WryeBashResult:
        """Execute bash.py to generate 'Bashed Patch, 0.esp'."""
        logger.info("[M-01] Generating Bashed Patch, 0.esp using Wrye Bash...")
        start_time = time.monotonic()

        args = ["-b", "Bashed Patch, 0.esp"]
        executable = str(self.config.wrye_bash_path)

        # Determine if it's the python script or an exe.
        if executable.endswith(".py"):
            args.insert(0, executable)
            executable = "python"

        kwargs = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = 0x08000000

        process: asyncio.subprocess.Process | None = None
        try:
            process = await asyncio.create_subprocess_exec(
                executable,
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self.config.game_path),
                **kwargs,
            )

            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=self.config.timeout_seconds)

            duration = time.monotonic() - start_time
            success = process.returncode == 0

            return WryeBashResult(
                success=success,
                return_code=process.returncode or 0,
                stdout=stdout.decode(errors="replace"),
                stderr=stderr.decode(errors="replace"),
                duration_seconds=duration,
            )

        except TimeoutError:
            logger.error("Wrye Bash generation timed out.")
            await _kill_and_reap(process)
            return WryeBashResult(
                success=False,
                return_code=-1,
                stdout="",
                stderr="Timeout during Bashed Patch generation",
                duration_seconds=time.monotonic() - start_time,
            )
        except asyncio.CancelledError:
            # Graceful shutdown: never leave the GUI tool running after cancel.
            await _kill_and_reap(process)
            raise
        except Exception as e:
            logger.error(f"Wrye Bash execution failed: {e}")
            await _kill_and_reap(process)
            raise WryeBashExecutionError(f"Failed to execute Wrye Bash: {e}") from e
