"""LOOT output parser — structured extraction from CLI stdout/stderr."""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class LOOTResult:
    """Structured result from a LOOT CLI run."""

    return_code: int = 0
    sorted_plugins: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    missing_patches: list[dict[str, str]] = field(default_factory=list)
    raw_stdout: str = ""
    raw_stderr: str = ""

    @property
    def success(self) -> bool:
        return self.return_code == 0 and not self.errors


# Patterns observed in LOOT CLI output.
_PLUGIN_LINE = re.compile(r"^\s*\d+\.\s+(.+\.es[pmlt])$", re.IGNORECASE)
_WARNING_LINE = re.compile(r"(?:warn(?:ing)?)\s*:\s*(.+)", re.IGNORECASE)
_ERROR_LINE = re.compile(r"(?:error)\s*:\s*(.+)", re.IGNORECASE)
_PATCH_REQ_LINE = re.compile(r"requires (?:a )?patch for\s+(.+?)(?:\.|$)", re.IGNORECASE)


class LOOTOutputParser:
    """Parses LOOT CLI stdout/stderr into structured data."""

    @staticmethod
    def parse(
        stdout: str,
        stderr: str,
        return_code: int,
    ) -> LOOTResult:
        """Parse raw LOOT output into a LOOTResult.

        Args:
            stdout: LOOT standard output.
            stderr: LOOT standard error.
            return_code: Process exit code.

        Returns:
            Structured LOOTResult.
        """
        plugins: list[str] = []
        warnings: list[str] = []
        errors: list[str] = []
        missing_patches: list[dict[str, str]] = []
        current_plugin = "Unknown"

        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue

            plugin_match = _PLUGIN_LINE.match(line)
            if plugin_match:
                current_plugin = plugin_match.group(1).strip()
                plugins.append(current_plugin)
                continue

            warn_match = _WARNING_LINE.match(line)
            if warn_match:
                warn_text = warn_match.group(1).strip()
                warnings.append(warn_text)

                patch_match = _PATCH_REQ_LINE.search(warn_text)
                if patch_match:
                    target_mod = patch_match.group(1).strip()
                    missing_patches.append(
                        {
                            "source_plugin": current_plugin,
                            "target_mod": target_mod,
                            "raw_message": warn_text,
                        }
                    )
                continue

            err_match = _ERROR_LINE.match(line)
            if err_match:
                errors.append(err_match.group(1).strip())

        # Also extract errors/warnings from stderr.
        for line in stderr.splitlines():
            line = line.strip()
            if not line:
                continue

            err_match = _ERROR_LINE.match(line)
            if err_match:
                errors.append(err_match.group(1).strip())
                continue

            warn_match = _WARNING_LINE.match(line)
            if warn_match:
                warnings.append(warn_match.group(1).strip())

        return LOOTResult(
            return_code=return_code,
            sorted_plugins=plugins,
            warnings=warnings,
            errors=errors,
            missing_patches=missing_patches,
            raw_stdout=stdout,
            raw_stderr=stderr,
        )
