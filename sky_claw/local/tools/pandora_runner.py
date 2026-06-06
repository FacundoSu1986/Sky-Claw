"""Pandora Behavior Engine Runner for Sky-Claw.

Implements M-02 Pandora Runner specifications.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sky_claw.local.tools._process import run_capture

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

        args = [str(self.config.pandora_exe), "--game", "Skyrim Special Edition", "--auto"]

        try:
            stdout, stderr, return_code = await run_capture(
                args,
                timeout=self.config.timeout_seconds,
                cwd=str(self.config.game_path),
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
            raise PandoraExecutionError(f"Failed to execute Pandora: {e}") from e

        return PandoraResult(
            success=return_code == 0,
            return_code=return_code,
            stdout=stdout.decode(errors="replace"),
            stderr=stderr.decode(errors="replace"),
            duration_seconds=time.monotonic() - start_time,
        )
