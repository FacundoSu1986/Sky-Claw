"""xEdit output parser — structured extraction from CLI stdout.

Parses the stdout of xEdit headless runs to extract processed
plugins, detected conflicts, and error messages.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


@dataclass
class XEditConflict:
    """A single conflict detected by xEdit."""

    plugin: str
    record: str
    detail: str = ""


@dataclass
class XEditResult:
    """Structured result from an xEdit headless run."""

    return_code: int = 0
    processed_plugins: list[str] = field(default_factory=list)
    conflicts: list[XEditConflict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    raw_stdout: str = ""
    raw_stderr: str = ""

    @property
    def success(self) -> bool:
        return self.return_code == 0 and not self.errors


# Patterns observed in xEdit output.
_PROCESSING_LINE = re.compile(
    r"\[\d{2}:\d{2}\]\s+Processing:\s+(.+\.es[pmlt])",
    re.IGNORECASE,
)
_CONFLICT_LINE = re.compile(
    r"Conflict\s+found:\s+(.+)",
    re.IGNORECASE,
)
_ERROR_LINE = re.compile(
    r"(?:Error|Fatal)\s*:\s*(.+)",
    re.IGNORECASE,
)
# More specific conflict pattern: "[HH:MM] <plugin> -> <record>: <detail>"
_DETAILED_CONFLICT = re.compile(
    r"\[\d{2}:\d{2}\]\s+(.+\.es[pmlt])\s*->\s*([^:]+):\s*(.*)",
    re.IGNORECASE,
)


class XEditOutputParser:
    """Parses xEdit CLI stdout/stderr into structured data."""

    @staticmethod
    def parse(
        stdout: str,
        stderr: str,
        return_code: int,
    ) -> XEditResult:
        """Parse raw xEdit output into an XEditResult.

        Args:
            stdout: xEdit standard output.
            stderr: xEdit standard error.
            return_code: Process exit code.

        Returns:
            Structured XEditResult.
        """
        plugins: list[str] = []
        conflicts: list[XEditConflict] = []
        errors: list[str] = []

        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue

            proc_match = _PROCESSING_LINE.match(line)
            if proc_match:
                plugins.append(proc_match.group(1).strip())
                continue

            detail_match = _DETAILED_CONFLICT.match(line)
            if detail_match:
                conflicts.append(
                    XEditConflict(
                        plugin=detail_match.group(1).strip(),
                        record=detail_match.group(2).strip(),
                        detail=detail_match.group(3).strip(),
                    )
                )
                continue

            conflict_match = _CONFLICT_LINE.match(line)
            if conflict_match:
                conflicts.append(
                    XEditConflict(
                        plugin="",
                        record="",
                        detail=conflict_match.group(1).strip(),
                    )
                )
                continue

            err_match = _ERROR_LINE.match(line)
            if err_match:
                errors.append(err_match.group(1).strip())

        # Also extract errors from stderr.
        for line in stderr.splitlines():
            line = line.strip()
            if not line:
                continue
            err_match = _ERROR_LINE.match(line)
            if err_match:
                errors.append(err_match.group(1).strip())

        return XEditResult(
            return_code=return_code,
            processed_plugins=plugins,
            conflicts=conflicts,
            errors=errors,
            raw_stdout=stdout,
            raw_stderr=stderr,
        )

    @staticmethod
    def parse_conflict_report(stdout: str) -> list[dict[str, Any]]:
        """Parse CONFLICT lines from the ``list_all_conflicts.pas`` output.

        Returns a list of dicts with keys:
        ``form_id``, ``editor_id``, ``record_type``, ``winner``, ``losers``.

        Lines that don't match the expected format are silently skipped.
        """
        from sky_claw.xedit.conflict_analyzer import parse_conflict_lines

        raw = parse_conflict_lines(stdout)
        return [
            {
                "form_id": c.form_id,
                "editor_id": c.editor_id,
                "record_type": c.record_type,
                "winner": c.winner,
                "losers": c.losers,
            }
            for c in raw
        ]
