"""BodySlide Runner for Sky-Claw.

Implements M-03 BodySlide Runner specifications.
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


class BodySlideExecutionError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class BodySlideConfig:
    bodyslide_exe: pathlib.Path
    game_path: pathlib.Path
    timeout_seconds: float = 600.0


@dataclass
class BodySlideResult:
    success: bool
    return_code: int
    stdout: str
    stderr: str
    duration_seconds: float


class BodySlideRunner:
    """Asynchronous runner for BodySlide batch generation."""

    def __init__(self, config: BodySlideConfig):
        self.config = config

    async def run_batch(self, group: str, output_path: str) -> BodySlideResult:
        """Execute BodySlide in batch mode for a specific group and output path.

        Format: BodySlide.exe -b <Group> -o <Output>
        """
        logger.info(f"[M-03] Executing BodySlide batch for group {group}...")
        start_time = time.monotonic()

        args = ["-b", group, "-o", output_path]

        kwargs = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = 0x08000000

        process: asyncio.subprocess.Process | None = None
        try:
            process = await asyncio.create_subprocess_exec(
                str(self.config.bodyslide_exe),
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self.config.game_path),
                **kwargs,
            )

            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=self.config.timeout_seconds)

            duration = time.monotonic() - start_time
            success = process.returncode == 0

            return BodySlideResult(
                success=success,
                return_code=process.returncode or 0,
                stdout=stdout.decode(errors="replace"),
                stderr=stderr.decode(errors="replace"),
                duration_seconds=duration,
            )

        except TimeoutError:
            logger.error("BodySlide execution timed out.")
            await _kill_and_reap(process)
            return BodySlideResult(
                success=False,
                return_code=-1,
                stdout="",
                stderr="Timeout during BodySlide execution",
                duration_seconds=time.monotonic() - start_time,
            )
        except asyncio.CancelledError:
            # Graceful shutdown: never leave the GUI tool running after cancel.
            await _kill_and_reap(process)
            raise
        except Exception as e:
            logger.error(f"BodySlide execution failed: {e}")
            await _kill_and_reap(process)
            raise BodySlideExecutionError(f"Failed to execute BodySlide: {e}") from e
