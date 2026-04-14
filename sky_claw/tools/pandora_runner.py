"""Pandora Behavior Engine Runner for Sky-Claw.

Implements M-02 Pandora Runner specifications.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pathlib

logger = logging.getLogger(__name__)


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

        try:
            process = await asyncio.create_subprocess_exec(
                str(self.config.pandora_exe),
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

            return PandoraResult(
                success=success,
                return_code=process.returncode or 0,
                stdout=stdout.decode(errors="replace"),
                stderr=stderr.decode(errors="replace"),
                duration_seconds=duration,
            )

        except TimeoutError:
            logger.error("Pandora execution timed out.")
            return PandoraResult(
                success=False,
                return_code=-1,
                stdout="",
                stderr="Timeout during Pandora execution",
                duration_seconds=time.monotonic() - start_time,
            )
        except Exception as e:
            logger.error(f"Pandora execution failed: {e}")
            raise PandoraExecutionError(f"Failed to execute Pandora: {e}")
