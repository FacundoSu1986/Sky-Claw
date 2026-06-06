"""BodySlide Runner for Sky-Claw.

Implements M-03 BodySlide Runner specifications.
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

        args = [str(self.config.bodyslide_exe), "-b", group, "-o", output_path]

        try:
            stdout, stderr, return_code = await run_capture(
                args,
                timeout=self.config.timeout_seconds,
                cwd=str(self.config.game_path),
            )
        except TimeoutError:
            logger.error("BodySlide execution timed out.")
            return BodySlideResult(
                success=False,
                return_code=-1,
                stdout="",
                stderr="Timeout during BodySlide execution",
                duration_seconds=time.monotonic() - start_time,
            )
        except Exception as e:
            logger.error(f"BodySlide execution failed: {e}")
            raise BodySlideExecutionError(f"Failed to execute BodySlide: {e}") from e

        return BodySlideResult(
            success=return_code == 0,
            return_code=return_code,
            stdout=stdout.decode(errors="replace"),
            stderr=stderr.decode(errors="replace"),
            duration_seconds=time.monotonic() - start_time,
        )
