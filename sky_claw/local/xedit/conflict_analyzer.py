"""ESP record-level conflict analyzer.

Uses xEdit headless (via :class:`XEditRunner`) to detect records that
are overridden by multiple plugins.  Conflicts are classified by
severity and grouped by plugin pair for easy presentation by the LLM.
"""

from __future__ import annotations

import contextlib
import logging
import pathlib
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

# Sin ciclo: flag_rules solo importa de este módulo bajo TYPE_CHECKING.
from sky_claw.local.xedit.flag_rules import FlagAlert, evaluate_flag_rules

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sky_claw.local.xedit.runner import XEditRunner

logger = logging.getLogger(__name__)

# SCA-004: Regex for validating FormID format (8 hex digits)
_FORMID_RE = re.compile(r"^[0-9A-Fa-f]{8}$")

# ---------------------------------------------------------------------------
# Severity classification — configurable via constructor
# ---------------------------------------------------------------------------

#: Record signatures considered **critical** (can cause CTD / broken quests).
DEFAULT_CRITICAL_TYPES: frozenset[str] = frozenset(
    {
        "NPC_",
        "QUST",
        "SCEN",  # SCA-001: Replaced obsolete SCPT (Oblivion) with Skyrim SE/AE relevant types
        "INFO",
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

#: Flags críticos exportados por firma (T-19a). Fuente única (patrón T-08):
#: ``list_all_conflicts.pas`` duplica a mano el guard por firma y el literal
#: del nombre del flag — anclado por tests/test_conflict_signatures_sync.py.
#: ``Manual Cost Calc`` es el bit 0x1 del campo Flags del subrecord SPIT: un
#: override que lo define y un ganador que no lo preserva rompe el coste del
#: hechizo silenciosamente (el caso canónico del review §5).
CRITICAL_FLAGS: dict[str, tuple[str, ...]] = {
    "SPEL": ("Manual Cost Calc",),
}

_SCRIPT_NAME = "list_all_conflicts.pas"

# ---------------------------------------------------------------------------
# Plugin pool limits (Skyrim SSE/AE)
# ---------------------------------------------------------------------------

#: Maximum number of full plugins (.esp, .esm) allowed simultaneously.
FULL_PLUGIN_LIMIT: int = 254

#: Maximum number of light plugins (.esl) allowed simultaneously.
LIGHT_PLUGIN_LIMIT: int = 4096


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OverrideFlagState:
    """Estado de un flag crítico en UNA versión del record (master u override).

    T-19a: el dato por-versión que la regla de T-19b compara (¿el ganador
    preserva el flag que otro override define?).

    Attributes:
        plugin: Plugin que aporta esta versión del record.
        flag: Nombre canónico del flag (p. ej. ``"Manual Cost Calc"``).
        value: Estado del flag en esta versión.
    """

    plugin: str
    flag: str
    value: bool


@dataclass
class RecordConflict:
    """A single record overridden by multiple plugins."""

    form_id: str
    editor_id: str
    record_type: str
    winner: str
    losers: list[str]
    severity: str  # "critical", "warning", "info"
    #: T-19a: estado de los flags críticos por versión del record (vacío si el
    #: export no emitió FLAG para este FormID — compat con salidas viejas).
    flag_states: tuple[OverrideFlagState, ...] = ()
    #: T-19b: alertas del motor de reglas de flags (las asigna analyze(), como
    #: hace con severity). Vacío = ninguna regla disparó o faltan datos.
    flag_alerts: tuple[FlagAlert, ...] = ()


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
                            "flag_states": [
                                {"plugin": f.plugin, "flag": f.flag, "value": f.value} for f in c.flag_states
                            ],
                            "flag_alerts": [
                                {
                                    "flag": a.flag,
                                    "winner": a.winner,
                                    "defined_by": list(a.defined_by),
                                    "severity": a.severity,
                                    "explanation": a.explanation,
                                }
                                for a in c.flag_alerts
                            ],
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

    def validate_load_order_limit(
        self,
        plugins: list[str],
        *,
        plugin_dirs: Sequence[pathlib.Path] | None = None,
    ) -> None:
        """Validates the Skyrim SSE/AE plugin limits for both full and light pools.

        Skyrim SE/AE has two independent plugin pools:
        - Full plugins: max 254
        - Light plugins (FE): max 4096

        T-18: con ``plugin_dirs`` el conteo usa los **flags reales** del header
        TES4 (vía :class:`~sky_claw.local.validators.plugin_limits.PluginLimitsChecker`):
        un ESPFE (``.esp`` con flag ESL) consume slot *light*, exactamente el
        caso que la heurística por extensión contaba mal. Sin ``plugin_dirs``
        se degrada a la heurística con un warning explícito (no mentir
        precisión).

        Args:
            plugins: List of plugin filenames (basename or full path accepted).
            plugin_dirs: Directorios donde viven los plugins (``Data`` y/o
                carpetas de mods de MO2). ``None`` = conteo por extensión.

        Raises:
            RuntimeError: If either pool exceeds its respective limit.
        """
        if plugin_dirs is not None:
            # Import perezoso: plugin_limits importa PreflightCheck; mantener a
            # conflict_analyzer liviano en el camino sin dirs (mismo patrón que
            # los checkpoints del preflight).
            from sky_claw.local.validators.plugin_limits import PluginLimitsChecker

            # El checker indexa por basename; normalizar para no subcontar si
            # el caller pasó paths completos (el contrato acepta ambos —
            # review Copilot #267).
            names = [pathlib.PurePath(p).name for p in plugins]
            limits = PluginLimitsChecker(plugin_dirs=plugin_dirs).check(names)
            # Cobertura: el checker SKIPEA los plugins que no encuentra en los
            # dirs. Si los dirs no cubren todos los plugins activos (p. ej. MO2
            # no resuelto → solo Data), delegar subcontaría y podría dejar
            # pasar un overflow real — peor que la heurística. En ese caso se
            # cae a la heurística por extensión, que cuenta todo (review Codex
            # #267).
            # located = plugins ÚNICOS ubicados en disco. unreadable es un
            # subconjunto de full/light (se cuenta por extensión igual), así que
            # NO se suma aparte o sobre-contaría.
            located = limits.full_count + limits.light_count
            unique = len({n.casefold() for n in names})
            if located >= unique:
                criticals = [i for i in limits.issues if i.severity == "critical"]
                for issue in limits.issues:
                    if issue.severity == "critical":
                        logger.critical("CRITICAL ALERT [%s]: %s", issue.kind, issue.detail)
                    else:
                        logger.warning("[%s] %s", issue.kind, issue.detail)
                if criticals:
                    raise RuntimeError("; ".join(i.detail for i in criticals))
                return
            logger.warning(
                "Cobertura de headers incompleta (%d/%d plugins ubicados en disco): se cae al conteo "
                "por extensión para no subcontar los plugins ausentes de los dirs resueltos.",
                located,
                unique,
            )

        else:
            logger.warning(
                "Límites full/light contados por extensión (aproximado): un ESPFE contaría "
                "contra el pool equivocado — pasá plugin_dirs para usar los flags reales del header."
            )
        full_plugins = [p for p in plugins if p.lower().endswith((".esp", ".esm"))]
        light_plugins = [p for p in plugins if p.lower().endswith(".esl")]

        if len(full_plugins) > FULL_PLUGIN_LIMIT:
            logger.critical(
                "CRITICAL ALERT: Full plugin limit exceeded! (%d > %d)",
                len(full_plugins),
                FULL_PLUGIN_LIMIT,
            )
            raise RuntimeError(
                f"Full plugin limit exceeded: {len(full_plugins)}/{FULL_PLUGIN_LIMIT}. "
                "Consider converting small mods (<2048 new records) to ESL format in xEdit."
            )

        if len(light_plugins) > LIGHT_PLUGIN_LIMIT:
            logger.critical(
                "CRITICAL ALERT: Light plugin limit exceeded! (%d > %d)",
                len(light_plugins),
                LIGHT_PLUGIN_LIMIT,
            )
            raise RuntimeError(f"Light plugin limit exceeded: {len(light_plugins)}/{LIGHT_PLUGIN_LIMIT}.")

    async def verify_masters(self, plugins: list[str], xedit_runner: XEditRunner) -> list[str]:
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
        # Acotado (T-11): solo los fallos del runner se degradan a mensajes;
        # un bug inesperado propaga en vez de volverse un string silencioso.
        from sky_claw.local.xedit.runner import XEditError

        try:
            result = await xedit_runner.run_script("check_masters.pas", plugins)
            if not result.success:
                return [f"Error verifying masters: {err}" for err in result.errors]
            return []  # No missing masters detected
        except (XEditError, OSError) as exc:
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
        # PR-2 grass cache: run_script pasa -script:<nombre> y xEdit lo resuelve
        # contra SU "Edit Scripts/" — sin staging, el .pas bundleado solo
        # funcionaba si el usuario lo copiaba a mano.
        await xedit_runner.ensure_scripts_staged([_SCRIPT_NAME])
        result = await xedit_runner.run_script(_SCRIPT_NAME, plugins)

        # xEdit puede salir con código != 0 (script/master/load fallido) y
        # run_script solo loguea un warning; construir el reporte del stdout
        # parcial lo haría pasar por "sin conflictos" y ocultaría disputas
        # reales (review Codex #226). Surfacear el fallo en vez de mentir.
        if not result.success:
            detalle = "; ".join(result.errors) or result.raw_stderr.strip() or f"exit code {result.return_code}"
            raise RuntimeError(f"El análisis de xEdit falló ({detalle}).")

        raw_conflicts = parse_conflict_lines(result.raw_stdout)

        # Classify severity.
        classified: list[RecordConflict] = []
        for rc in raw_conflicts:
            rc.severity = self._classify(rc.record_type)
            # T-19b: el motor de reglas convierte el dato por-versión (T-19a)
            # en alertas explicadas, adjuntas al conflicto para GUI/LLM.
            rc.flag_alerts = evaluate_flag_rules(rc)
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

        # T-19b: las alertas del motor de reglas van PRIMERO — son lo más
        # accionable (un flag concreto que el ganador pierde, con su porqué).
        seen_alerts: set[tuple[str, str]] = set()
        for c in _flat_conflicts(report):
            for alert in c.flag_alerts:
                key = (alert.form_id, alert.flag)
                if key in seen_alerts:
                    continue  # _group_by_pair duplica el conflicto por par
                seen_alerts.add(key)
                suggestions.append(
                    f"{alert.editor_id} ({alert.form_id}): el ganador {alert.winner} no preserva "
                    f"'{alert.flag}' definido por {', '.join(alert.defined_by)} — {alert.explanation}. "
                    "Forwardear el flag en un parche xEdit."
                )

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
        quest_count = type_counts.get("QUST", 0) + type_counts.get("SCEN", 0) + type_counts.get("INFO", 0)
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
        ll_count = type_counts.get("LVLI", 0) + type_counts.get("LVLN", 0) + type_counts.get("LVSP", 0)
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
            suggestions.append(f"{info_count} minor conflict(s) (textures, strings) — generally safe to ignore.")

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

    def _group_by_pair(self, conflicts: list[RecordConflict]) -> list[PluginConflictPair]:
        """Group conflicts by (winner, loser) plugin pairs."""
        pair_map: dict[tuple[str, str], list[RecordConflict]] = defaultdict(list)

        for c in conflicts:
            for loser in c.losers:
                # Normalize pair key so (A,B) == (B,A).
                key = (c.winner, loser) if c.winner < loser else (loser, c.winner)
                pair_map[key].append(c)

        pairs: list[PluginConflictPair] = []
        for (a, b), pair_conflicts in sorted(pair_map.items(), key=lambda x: -len(x[1])):
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
            f"Found {report.total_conflicts} record-level conflict(s) ({report.critical_conflicts} critical).",
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


def parse_flag_lines(stdout: str) -> dict[str, list[OverrideFlagState]]:
    """Parse FLAG lines from xEdit script output (T-19a).

    Expected format (one line per record version — master and each override)::

        FLAG|FormID|Plugin|FlagName|0/1

    ``|`` es un separador robusto: es inválido en nombres de archivo Windows,
    a diferencia de ``,``/``:`` que aparecen en plugins reales ("Bashed
    Patch, 0.esp"). Malformed lines are skipped with a warning, like CONFLICT.
    """
    flags: dict[str, list[OverrideFlagState]] = {}
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("FLAG|"):
            continue
        parts = line.split("|")
        # Exactamente 5: el script controla el formato, así que un `|` de más
        # es corrupción — parsearlo correría plugin/flag/value en silencio
        # (review Copilot #259).
        if len(parts) != 5:
            logger.warning("Malformed FLAG line (expected exactly 5 fields): %s", line)
            continue
        form_id = parts[1].strip()
        if not _FORMID_RE.match(form_id):
            logger.warning("Invalid FormID '%s' in FLAG line: %s", form_id, line)
            continue
        value = parts[4].strip()
        if value not in ("0", "1"):
            logger.warning("Invalid flag value '%s' in FLAG line: %s", value, line)
            continue
        flags.setdefault(form_id, []).append(
            OverrideFlagState(plugin=parts[2].strip(), flag=parts[3].strip(), value=value == "1")
        )
    return flags


def parse_conflict_lines(stdout: str) -> list[RecordConflict]:
    """Parse CONFLICT lines from xEdit script output.

    Expected format::

        CONFLICT|FormID|EditorID|RecordType|WinnerPlugin|LoserPlugin1,LoserPlugin2

    Las líneas ``FLAG|`` (T-19a) se adjuntan al conflict de su FormID en
    ``flag_states``; el join por FormID es robusto al orden de las líneas.
    Lines that don't match are skipped with a warning log.
    """
    # T-19a: primera pasada — flags por FormID (el script puede emitirlas
    # antes o después de su CONFLICT).
    flag_states = parse_flag_lines(stdout)

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
            form_id = parts[1].strip()
            # SCA-004: Validate FormID format (8 hex digits)
            if not _FORMID_RE.match(form_id):
                logger.warning("Invalid FormID '%s' in line: %s", form_id, line)
                continue
            losers = [entry.strip() for entry in parts[5].split(",") if entry.strip()]
            conflicts.append(
                RecordConflict(
                    form_id=form_id,
                    editor_id=parts[2].strip(),
                    record_type=parts[3].strip(),
                    winner=parts[4].strip(),
                    losers=losers,
                    severity="info",  # classified later by the analyzer
                    flag_states=tuple(flag_states.get(form_id, [])),
                )
            )
        # Acotado (T-11): una línea malformada es un problema de parseo, no
        # una razón para tragar cualquier bug del bloque.
        except (ValueError, IndexError):
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
                with contextlib.suppress(ValueError):
                    result[key.strip()] = int(val.strip())
        return result
    return {}


def _flat_conflicts(report: ConflictReport) -> list[RecordConflict]:
    """Flatten all conflicts from a report."""
    out: list[RecordConflict] = []
    for pair in report.plugin_pairs:
        out.extend(pair.conflicts)
    return out
