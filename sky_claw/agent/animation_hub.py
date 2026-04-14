"""Animation and Physics engine orchestrator.

Handles execution of Pandora Behavior Engine and BodySlide.
These tools require the MO2 VFS context to see installed mods.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pathlib

    from sky_claw.mo2.vfs import MO2Controller
    from sky_claw.security.path_validator import PathValidator

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class EngineConfig:
    """Paths to the behavior and physics engines."""

    pandora_exe: pathlib.Path | None = None
    bodyslide_exe: pathlib.Path | None = None


class AnimationHub:
    """Orchestrates Bodyslide and Pandora Engine executions.

    Both engines must run via MO2's VFS (using MO2's proxy) to access
    mod files properly, similar to how SKSE or LOOT runs.
    """

    def __init__(
        self,
        mo2: MO2Controller,
        config: EngineConfig,
        path_validator: PathValidator | None = None,
    ) -> None:
        self._mo2 = mo2
        self._config = config
        self._validator = path_validator

    async def run_pandora(self) -> dict:
        """Execute Pandora Behavior Engine to update animations.

        Pandora replaces Nemesis/FNIS. It reads animation files and generates
        behavior graphs in the MO2 Overwrite folder.
        """
        exe = self._config.pandora_exe
        if not exe or not exe.exists():
            return {
                "status": "error",
                "message": "Error: Pandora Behavior Engine executable not found or not configured.",
            }

        if self._validator:
            try:
                self._validator.validate(exe)
            except Exception as exc:
                return {"status": "error", "message": f"Path validation failed: {exc}"}

        logger.info("Executing Pandora Behavior Engine via CLI...")

        args = [
            str(exe),
            "--launch",  # Assuming headless/CLI flags for Pandora
            "--headless",
        ]

        kwargs: dict = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW

        try:
            # Note: In a real implementation we would wrap this via MO2's virtual execution
            # using something like nxm-handler or ModOrganizer.exe -p Profile "path/to/Pandora.exe"
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **kwargs,
            )
            _stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
        except TimeoutError:
            proc.kill()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(proc.wait(), timeout=3.0)
            return {
                "status": "error",
                "message": "Error: Pandora timed out after 5 minutes.",
            }
        except Exception as exc:
            return {"status": "error", "message": f"Error executing Pandora: {exc}"}

        if proc.returncode != 0:
            err_text = stderr.decode(errors="replace").strip() if stderr else ""
            logger.error("Pandora exited with code %d: %s", proc.returncode, err_text)
            return {
                "status": "error",
                "message": f"Pandora exited with code {proc.returncode}.",
            }

        return {
            "status": "success",
            "message": "Pandora Behavior Engine completed successfully. Behaviors generated.",
        }

    async def run_bodyslide_batch(self) -> dict:
        """Execute BodySlide in batch mode to generate meshes based on presets."""
        exe = self._config.bodyslide_exe
        if not exe or not exe.exists():
            return {
                "status": "error",
                "message": "Error: BodySlide executable not found or not configured.",
            }

        if self._validator:
            try:
                self._validator.validate(exe)
            except Exception as exc:
                return {"status": "error", "message": f"Path validation failed: {exc}"}

        logger.info("Executing BodySlide Batch Build...")

        # BodySlide CLI flags typical for headless batch building.
        args = [
            str(exe),
            "-g",
            "Build",
            "-p",
            "CBBE Body Physics",  # Default preset
            "-t",
            "CBBE",
            # Outpath normally defaults to the overwrite or data folder
        ]

        kwargs: dict = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW

        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **kwargs,
            )
            _stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=600)
        except TimeoutError:
            proc.kill()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(proc.wait(), timeout=3.0)
            return {
                "status": "error",
                "message": "Error: BodySlide timed out after 10 minutes.",
            }
        except Exception as exc:
            return {"status": "error", "message": f"Error executing BodySlide: {exc}"}

        if proc.returncode != 0:
            err_text = stderr.decode(errors="replace").strip() if stderr else ""
            logger.error("BodySlide exited with code %d: %s", proc.returncode, err_text)
            return {
                "status": "error",
                "message": f"BodySlide exited with code {proc.returncode}.",
            }

        return {
            "status": "success",
            "message": "BodySlide batch build completed successfully.",
        }
