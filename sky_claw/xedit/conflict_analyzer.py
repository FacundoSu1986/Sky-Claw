"""ESP record-level conflict analyzer.

Uses xEdit headless (via :class:`XEditRunner`) to detect records that
are overridden by multiple plugins.  Conflicts are classified by
severity and grouped by plugin pair for easy presentation by the LLM.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from sky_claw.xedit.runner import XEditRunner

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Severity classification — configurable via constructor
# ---------------------------------------------------------------------------

#: Record signatures considered **critical** (can cause CTD / broken quests).
DEFAULT_CRITICAL_TYPES: frozenset[str] = frozenset(
    {
        "NPC_",
        "QUST",
        "SCPT",
        "PERK",
        "SPEL",
        "MGEF",
        "FACT",
        "DIAL",
        "PACK",
    }
)

#: Record signatures considered **warning** (visual glitches / gameplay).
DEFAULT_WARNING_TYPES: frozenset[str] = frozenset(
    {
        "CELL",
        "WRLD",
        "REFR",
        "ACHR",
        "NAVM",
        "LAND",
        "WEAP",
        "ARMO",
        "AMMO",
        "BOOK",
        "INGR",
        "ALCH",
        "MISC",
        "CONT",
        "DOOR",
        "LIGH",
        "STAT",
        "FLOR",
        "FURN",
        "LVLI",
        "LVLN",
        "LVSP",
        "ENCH",
        "OTFT",
        "RACE",
        "COBJ",
        "KYWD",
    }
)

#: Anything not in critical or warning is **info** (textures, strings, etc.).

_SCRIPT_NAME = "list_all_conflicts.pas"


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class RecordConflict:
    """A single record overridden by multiple plugins."""

    form_id: str
    editor_id: str
    record_type: str
    winner: str
    losers: list[str]
    severity: str  # "critical", "warning", "info"


@dataclass
class PluginConflictPair:
    """Aggregated conflicts between two specific plugins."""

    plugin_a: str
    plugin_b: str
    conflicts: list[RecordConflict] = field(default_factory=list)

    @property
    def critical_count(self) -> int:
        return sum(1 for c in self.conflicts if c.severity == "critical")

    @property
    def warning_count(self) -> int:
        return sum(1 for c in self.conflicts if c.severity == "warning")


@dataclass
class ConflictReport:
    """Full conflict analysis report."""

    total_conflicts: int
    critical_conflicts: int
    plugin_pairs: list[PluginConflictPair] = field(default_factory=list)
    summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialize for JSON output to the LLM."""
        return {
            "total_conflicts": self.total_conflicts,
            "critical_conflicts": self.critical_conflicts,
            "plugin_pairs": [
                {
                    "plugin_a": pp.plugin_a,
                    "plugin_b": pp.plugin_b,
                    "critical_count": pp.critical_count,
                    "warning_count": pp.warning_count,
                    "conflicts": [
                        {
                            "form_id": c.form_id,
                            "editor_id": c.editor_id,
                            "record_type": c.record_type,
                            "winner": c.winner,
                            "losers": c.losers,
                            "severity": c.severity,
                        }
                        for c in pp.conflicts
                    ],
                }
                for pp in self.plugin_pairs
            ],
            "summary": self.summary,
        }


# ---------------------------------------------------------------------------
# Analyzer
# ---------------------------------------------------------------------------


class ConflictAnalyzer:
    """Analyze ESP record-level conflicts via xEdit.

    Parameters
    ----------
    critical_types:
        Record signatures classified as critical.
    warning_types:
        Record signatures classified as warning.
    """

    def __init__(
        self,
        critical_types: frozenset[str] | None = None,
        warning_types: frozenset[str] | None = None,
    ) -> None:
        self._critical = critical_types or DEFAULT_CRITICAL_TYPES
        self._warning = warning_types or DEFAULT_WARNING_TYPES

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def validate_load_order_limit(self, plugins: list[str]) -> None:
        """Validates the strict engine plugin limit of 254 plugins.

        Args:
            plugins: List of plugin filenames.

        Raises:
            RuntimeError: If the count exceeds 254.
        """
        active_plugins = [p for p in plugins if p.lower().endswith((".esp", ".esm"))]
        if len(active_plugins) > 254:
            logger.critical(
                f"CRITICAL ALERT: Plugin limit exceeded! ({len(active_plugins)} > 254)"
            )
            raise RuntimeError(
                f"Load order limit of 254 plugins exceeded: found {len(active_plugins)} active plugins."
            )

    async def verify_masters(
        self, plugins: list[str], xedit_runner: XEditRunner
    ) -> list[str]:
        """Verify master dependencies for all active plugins.

        Args:
            plugins: List of active plugin filenames.
            xedit_runner: Configured xEdit runner.

        Returns:
            List of error strings regarding missing masters.
        """
        # Note: A headless script like check_masters.pas might be run here.
        # This implementation delegates to the runner script checking.
        logger.info("[M-05] Verifying plugin master dependencies...")
        try:
            result = await xedit_runner.run_script("check_masters.pas", plugins)
            if not result.success:
                return [f"Error verifying masters: {err}" for err in result.errors]
            return []  # No missing masters detected
        except Exception as exc:
            logger.error(f"Failed to verify masters: {exc}")
            return [str(exc)]

    async def analyze(
        self,
        plugins: list[str],
        xedit_runner: XEditRunner,
    ) -> ConflictReport:
        """Run the conflict detection script and return a structured report.

        Args:
            plugins: Plugin filenames to load.
            xedit_runner: Configured xEdit runner.

        Returns:
            :class:`ConflictReport` with classified and grouped conflicts.
        """
        result = await xedit_runner.run_script(_SCRIPT_NAME, plugins)

        raw_conflicts = parse_conflict_lines(result.raw_stdout)

        # Classify severity.
        classified: list[RecordConflict] = []
        for rc in raw_conflicts:
            rc.severity = self._classify(rc.record_type)
            classified.append(rc)

        # Group by plugin pairs.
        pairs = self._group_by_pair(classified)

        critical_total = sum(1 for c in classified if c.severity == "critical")

        report = ConflictReport(
            total_conflicts=len(classified),
            critical_conflicts=critical_total,
            plugin_pairs=pairs,
        )
        report.summary = self._build_summary(report)
        return report

    def suggest_resolution(self, report: ConflictReport) -> list[str]:
        """Generate human-readable resolution suggestions.

        Args:
            report: A completed conflict report.

        Returns:
            List of suggestion strings.
        """
        suggestions: list[str] = []

        if report.total_conflicts == 0:
            suggestions.append("No conflicts detected — load order looks clean.")
            return suggestions

        # Analyze by record type across all pairs.
        type_counts: dict[str, int] = defaultdict(int)
        heavy_pairs: list[PluginConflictPair] = []

        for pair in report.plugin_pairs:
            if len(pair.conflicts) >= 10:
                heavy_pairs.append(pair)
            for c in pair.conflicts:
                type_counts[c.record_type] += 1

        # NPC conflicts → patch.
        if type_counts.get("NPC_", 0) > 0:
            suggestions.append(
                f"{type_counts['NPC_']} NPC conflict(s) detected — "
                "these can cause CTDs. Look for a compatibility patch on Nexus, "
                "or create one in xEdit by forwarding the desired changes."
            )

        # Quest/script conflicts.
        quest_count = type_counts.get("QUST", 0) + type_counts.get("SCPT", 0)
        if quest_count > 0:
            suggestions.append(
                f"{quest_count} quest/script conflict(s) — "
                "these are high-risk. Check mod pages for known incompatibilities "
                "and required load order patches."
            )

        # Cell/world conflicts → reorder.
        cell_count = type_counts.get("CELL", 0) + type_counts.get("WRLD", 0)
        if cell_count > 0:
            suggestions.append(
                f"{cell_count} cell/worldspace conflict(s) — "
                "try reordering the load order so the preferred visual mod wins, "
                "or use a merged patch."
            )

        # Leveled list conflicts.
        ll_count = (
            type_counts.get("LVLI", 0)
            + type_counts.get("LVLN", 0)
            + type_counts.get("LVSP", 0)
        )
        if ll_count > 0:
            suggestions.append(
                f"{ll_count} leveled list conflict(s) — "
                "use a Bashed Patch or Smashed Patch to merge leveled lists "
                "so all mods' additions are preserved."
            )

        # Heavy pairs → dedicated patch.
        for pair in heavy_pairs:
            suggestions.append(
                f"{pair.plugin_a} and {pair.plugin_b} have "
                f"{len(pair.conflicts)} conflicts ({pair.critical_count} critical) — "
                "search Nexus for a dedicated compatibility patch between these two mods."
            )

        # Info-only conflicts.
        info_count = sum(1 for c in _flat_conflicts(report) if c.severity == "info")
        if info_count > 0 and not suggestions:
            suggestions.append(
                f"{info_count} minor conflict(s) (textures, strings) — "
                "generally safe to ignore."
            )

        return suggestions

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _classify(self, record_type: str) -> str:
        """Classify a record type into a severity bucket."""
        sig = record_type.upper().strip()
        if sig in self._critical:
            return "critical"
        if sig in self._warning:
            return "warning"
        return "info"

    def _group_by_pair(
        self, conflicts: list[RecordConflict]
    ) -> list[PluginConflictPair]:
        """Group conflicts by (winner, loser) plugin pairs."""
        pair_map: dict[tuple[str, str], list[RecordConflict]] = defaultdict(list)

        for c in conflicts:
            for loser in c.losers:
                # Normalize pair key so (A,B) == (B,A).
                key = tuple(sorted([c.winner, loser]))
                pair_map[key].append(c)

        pairs: list[PluginConflictPair] = []
        for (a, b), pair_conflicts in sorted(
            pair_map.items(), key=lambda x: -len(x[1])
        ):
            pairs.append(
                PluginConflictPair(
                    plugin_a=a,
                    plugin_b=b,
                    conflicts=pair_conflicts,
                )
            )
        return pairs

    def _build_summary(self, report: ConflictReport) -> str:
        """Build a human-readable summary for the LLM."""
        if report.total_conflicts == 0:
            return "No record-level conflicts detected between loaded plugins."

        lines = [
            f"Found {report.total_conflicts} record-level conflict(s) "
            f"({report.critical_conflicts} critical).",
        ]

        if report.plugin_pairs:
            top = report.plugin_pairs[0]
            lines.append(
                f"Most conflicting pair: {top.plugin_a} vs {top.plugin_b} "
                f"({len(top.conflicts)} conflicts, {top.critical_count} critical)."
            )

        if report.critical_conflicts > 0:
            lines.append(
                "Critical conflicts (NPC, quests, scripts) should be resolved "
                "with compatibility patches to avoid crashes."
            )

        return " ".join(lines)


# ---------------------------------------------------------------------------
# Parsing helpers (used by output_parser and directly)
# ---------------------------------------------------------------------------


def parse_conflict_lines(stdout: str) -> list[RecordConflict]:
    """Parse CONFLICT lines from xEdit script output.

    Expected format::

        CONFLICT|FormID|EditorID|RecordType|WinnerPlugin|LoserPlugin1,LoserPlugin2

    Lines that don't match are silently skipped with a debug log.
    """
    conflicts: list[RecordConflict] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("CONFLICT|"):
            continue
        parts = line.split("|")
        if len(parts) < 6:
            logger.warning("Malformed CONFLICT line (expected 6 fields): %s", line)
            continue
        try:
            losers = [entry.strip() for entry in parts[5].split(",") if entry.strip()]
            conflicts.append(
                RecordConflict(
                    form_id=parts[1].strip(),
                    editor_id=parts[2].strip(),
                    record_type=parts[3].strip(),
                    winner=parts[4].strip(),
                    losers=losers,
                    severity="info",  # classified later by the analyzer
                )
            )
        except Exception:
            logger.warning("Failed to parse CONFLICT line: %s", line, exc_info=True)
    return conflicts


def parse_summary_line(stdout: str) -> dict[str, int]:
    """Parse the SUMMARY line from xEdit script output.

    Returns a dict like ``{"total_conflicts": 5, "critical": 2, "minor": 3}``.
    """
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("SUMMARY|"):
            continue
        result: dict[str, int] = {}
        for part in line.split("|")[1:]:
            if "=" in part:
                key, val = part.split("=", 1)
                try:
                    result[key.strip()] = int(val.strip())
                except ValueError:
                    pass
        return result
    return {}


def _flat_conflicts(report: ConflictReport) -> list[RecordConflict]:
    """Flatten all conflicts from a report."""
    out: list[RecordConflict] = []
    for pair in report.plugin_pairs:
        out.extend(pair.conflicts)
    return out
