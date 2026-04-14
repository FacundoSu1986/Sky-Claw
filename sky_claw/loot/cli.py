"""LOOT CLI wrapper — runs LOOT with correct game path.

The previous implementation incorrectly passed ``mo2_root`` as
``--game-path``.  LOOT requires the real Skyrim SE installation
directory (where ``SkyrimSE.exe`` lives).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sky_claw.loot.parser import LOOTOutputParser, LOOTResult

if TYPE_CHECKING:
    import pathlib

    from sky_claw.security.path_validator import PathValidator

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 60


@dataclass(frozen=True, slots=True)
class LOOTConfig:
    """Configuration for the LOOT CLI runner."""

    loot_exe: pathlib.Path
    game_path: pathlib.Path
    game: str = "SkyrimSE"
    timeout: int = DEFAULT_TIMEOUT


class LOOTNotFoundError(FileNotFoundError):
    """Raised when the LOOT executable is not found."""


class LOOTTimeoutError(RuntimeError):
    """Raised when LOOT execution exceeds the configured timeout."""

    def __init__(self, timeout: int) -> None:
        super().__init__(f"LOOT timed out after {timeout}s")
        self.timeout = timeout


class LOOTRunner:
    """Async wrapper for LOOT CLI with correct path handling.

    Args:
        config: LOOT CLI configuration.
        path_validator: Validator to check paths against sandbox.
    """

    def __init__(
        self,
        config: LOOTConfig,
        path_validator: PathValidator | None = None,
    ) -> None:
        self._config = config
        self._validator = path_validator

    async def sort(self) -> LOOTResult:
        """Run LOOT CLI to sort the load order.

        Returns:
            Parsed LOOT result with warnings, errors, and suggested order.

        Raises:
            LOOTNotFoundError: If the LOOT executable does not exist.
            RuntimeError: If LOOT times out or fails.
        """
        loot_path = self._config.loot_exe
        game_path = self._config.game_path

        if self._validator is not None:
            self._validator.validate(loot_path)

        if not loot_path.exists():
            raise LOOTNotFoundError(f"LOOT executable not found at {loot_path}")

        args = [
            str(loot_path),
            "--game",
            self._config.game,
            "--game-path",
            str(game_path),
            "--sort",
        ]

        logger.info("Running LOOT: %s", " ".join(args))

        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=self._config.timeout,
            )
        except FileNotFoundError:
            raise LOOTNotFoundError(f"LOOT executable not found at {loot_path}")
        except TimeoutError:
            proc.kill()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(proc.wait(), timeout=3.0)
            raise LOOTTimeoutError(self._config.timeout)

        stdout_text = stdout.decode(errors="replace")
        stderr_text = stderr.decode(errors="replace")

        if proc.returncode != 0:
            logger.warning("LOOT exited with code %d: %s", proc.returncode, stderr_text)

        return LOOTOutputParser.parse(
            stdout=stdout_text,
            stderr=stderr_text,
            return_code=proc.returncode or 0,
        )
