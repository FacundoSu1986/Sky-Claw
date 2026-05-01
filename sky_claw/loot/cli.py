"""LOOT CLI wrapper — runs LOOT with correct game path.

The previous implementation incorrectly passed ``mo2_root`` as
``--game-path``.  LOOT requires the real Skyrim SE installation
directory (where ``SkyrimSE.exe`` lives).

TASK-011 enhancements:
- WSL2 conditional path translation via :func:`translate_path_if_wsl`.
- Full async subprocess with ``asyncio.wait_for`` timeout.
- Zombie process prevention: ``proc.kill()`` + ``proc.wait()`` on timeout.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import subprocess
from asyncio.exceptions import TimeoutError as AsyncTimeoutError
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sky_claw.core.windows_interop import translate_path_if_wsl
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

    TASK-011: Integrates WSL2 path translation so that ``game_path`` is
    automatically converted to Windows format (``C:\\...``) when the agent
    runs inside WSL2.

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
            LOOTTimeoutError: If LOOT exceeds the configured timeout.
            RuntimeError: If LOOT fails for other reasons.
        """
        loot_path = self._config.loot_exe
        game_path = self._config.game_path

        if self._validator is not None:
            self._validator.validate(loot_path)

        if not loot_path.exists():
            raise LOOTNotFoundError(f"LOOT executable not found at {loot_path}")

        # TASK-011: Translate game_path to Windows format when under WSL2.
        game_path_win = await translate_path_if_wsl(game_path)

        args = [
            str(loot_path),
            "--game",
            self._config.game,
            "--game-path",
            game_path_win,
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
            raise LOOTNotFoundError(f"LOOT executable not found at {loot_path}") from None
        except (AsyncTimeoutError, TimeoutError):
            # TASK-011: Zombie prevention -- kill + reap.
            with contextlib.suppress(ProcessLookupError):
                proc.kill()

            # Golden Master: WSL2/Windows native process annihilator (non-blocking).
            if "WSL_DISTRO_NAME" in os.environ:
                await asyncio.to_thread(
                    subprocess.run,
                    ["taskkill.exe", "/F", "/IM", "loot.exe", "/T"],
                    capture_output=True,
                    check=False,
                )

            with contextlib.suppress(AsyncTimeoutError, TimeoutError):
                await asyncio.wait_for(proc.wait(), timeout=3.0)
            raise LOOTTimeoutError(self._config.timeout) from None

        stdout_text = stdout.decode("utf-8", errors="replace")
        stderr_text = stderr.decode("utf-8", errors="replace")

        if proc.returncode != 0:
            logger.warning("LOOT exited with code %d: %s", proc.returncode, stderr_text)

        return LOOTOutputParser.parse(
            stdout=stdout_text,
            stderr=stderr_text,
            return_code=proc.returncode or 0,
        )
