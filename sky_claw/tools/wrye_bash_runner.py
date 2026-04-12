"""Wrye Bash Runner for Sky-Claw.

Implements M-01 Wrye Bash Runner specifications.
"""

from __future__ import annotations

import asyncio
import logging
import pathlib
import sys
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)


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

        try:
            process = await asyncio.create_subprocess_exec(
                executable,
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self.config.game_path),
                **kwargs,
            )

            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=self.config.timeout_seconds
            )

            duration = time.monotonic() - start_time
            success = process.returncode == 0

            return WryeBashResult(
                success=success,
                return_code=process.returncode or 0,
                stdout=stdout.decode(errors="replace"),
                stderr=stderr.decode(errors="replace"),
                duration_seconds=duration,
            )

        except asyncio.TimeoutError:
            logger.error("Wrye Bash generation timed out.")
            return WryeBashResult(
                success=False,
                return_code=-1,
                stdout="",
                stderr="Timeout during Bashed Patch generation",
                duration_seconds=time.monotonic() - start_time,
            )
        except Exception as e:
            logger.error(f"Wrye Bash execution failed: {e}")
            raise WryeBashExecutionError(f"Failed to execute Wrye Bash: {e}")
