"""Wrye Bash Runner for Sky-Claw.

Implements M-01 Wrye Bash Runner specifications.
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

#: Nombre canónico del plugin que genera Wrye Bash. Única fuente para el
#: runner y sus callers (supervisor / tool del agente), que lo necesitan para
#: snapshotearlo antes de regenerarlo. Debe coincidir con
#: ``DelegateToBashedPatch.BASHED_PATCH_NAME`` (anclado por test).
BASHED_PATCH_NAME = "Bashed Patch, 0.esp"


class WryeBashExecutionError(Exception):
    pass


class WryeBashTimeoutError(WryeBashExecutionError):
    """Elevada cuando Wrye Bash excede su timeout (U-10).

    Dedicada (en vez de un ``WryeBashResult`` con ``success=False``) para que el
    timeout se PROPAGUE por el context manager del lock/rollback en lugar de salir
    limpio: es el prerequisito del rollback transaccional de salida (U-04). Deriva
    de :class:`WryeBashExecutionError`, así que los callers que ya capturan la base
    la traducen al contrato ``success=False`` sin cambios.
    """

    def __init__(self, timeout_seconds: float) -> None:
        self.timeout_seconds = timeout_seconds
        super().__init__(f"Wrye Bash excedió el timeout de {timeout_seconds:.0f}s generando el Bashed Patch")


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

        program = str(self.config.wrye_bash_path)
        # A .py entry point is launched through the interpreter; an .exe directly.
        if program.endswith(".py"):
            args = ["python", program, "-b", BASHED_PATCH_NAME]
        else:
            args = [program, "-b", BASHED_PATCH_NAME]

        try:
            stdout, stderr, return_code = await run_capture(
                args,
                timeout=self.config.timeout_seconds,
                cwd=str(self.config.game_path),
            )
        except TimeoutError as exc:
            logger.error("Wrye Bash generation timed out.")
            raise WryeBashTimeoutError(self.config.timeout_seconds) from exc
        except Exception as e:
            logger.error(f"Wrye Bash execution failed: {e}")
            raise WryeBashExecutionError(f"Failed to execute Wrye Bash: {e}") from e

        return WryeBashResult(
            success=return_code == 0,
            return_code=return_code,
            stdout=stdout.decode(errors="replace"),
            stderr=stderr.decode(errors="replace"),
            duration_seconds=time.monotonic() - start_time,
        )
