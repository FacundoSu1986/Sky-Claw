"""GrassAnalyzer — diagnóstico xEdit para el pipeline de grass cache (PR-2).

Consume el output pipe-delimited de los dos scripts Pascal bundleados
(``list_grass_worldspaces.pas`` y ``list_zero_bound_grass.pas``) — mismo
contrato y patrón que ``ConflictAnalyzer`` ← ``list_all_conflicts.pas``:
parsers puros module-level + una clase async con el runner inyectado.

Disciplinas heredadas:
- **Lección #226**: si xEdit no terminó bien, se lanza — jamás se construye un
  reporte del stdout parcial ("sin worldspaces" falso haría que el precache
  saltee regiones enteras en silencio).
- **SUMMARY obligatorio y consistente**: exit 0 no garantiza que el script
  llegó a ``Finalize``; el conteo cruzado detecta stdout truncado/entrelazado.
- Los prefijos de línea y las claves del SUMMARY están anclados contra los
  ``.pas`` por ``tests/test_grass_scripts_sync.py`` (patrón T-08).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sky_claw.local.xedit.conflict_analyzer import parse_summary_line

if TYPE_CHECKING:
    from sky_claw.local.xedit.output_parser import XEditResult
    from sky_claw.local.xedit.runner import XEditRunner

logger = logging.getLogger(__name__)

#: Prefijos de línea emitidos por los scripts (anclados por el sync test).
WSGRASS_PREFIX = "WSGRASS|"
ZEROBOUND_PREFIX = "ZEROBOUND|"

#: Nombres de los scripts bundleados (en ``sky_claw/local/xedit/scripts/``).
SCRIPT_WORLDSPACES = "list_grass_worldspaces.pas"
SCRIPT_ZERO_BOUNDS = "list_zero_bound_grass.pas"

#: FormID de 8 hex exactos (local a propósito: no acoplarse a privados ajenos).
_FORMID_RE = re.compile(r"^[0-9A-Fa-f]{8}$")

#: Prefijo de timestamp que algunos builds de xEdit anteponen a AddMessage.
_TIMESTAMP_PREFIX_RE = re.compile(r"^\[\d{1,2}:\d{2}(?::\d{2})?\]\s+")

#: Razones válidas de un hallazgo ZEROBOUND (espejo del .pas).
_ZERO_BOUND_REASONS = frozenset({"zeros", "missing"})


@dataclass(frozen=True)
class GrassWorldspace:
    """Un worldspace con pasto reportado por el script."""

    form_id: str
    editor_id: str  # puede ser "" (WRLD sin EDID)
    plugin: str


@dataclass
class GrassWorldspaceReport:
    """Reporte completo del scan de worldspaces con pasto."""

    worldspaces: list[GrassWorldspace]
    summary: dict[str, int]

    @property
    def editor_ids(self) -> list[str]:
        """Payload para ``OnlyPregenerateWorldSpaces``: dedup, orden del script,
        sin vacíos. NO filtra test-worlds — eso es de capas superiores."""
        vistos: set[str] = set()
        resultado: list[str] = []
        for ws in self.worldspaces:
            if ws.editor_id and ws.editor_id not in vistos:
                vistos.add(ws.editor_id)
                resultado.append(ws.editor_id)
        return resultado

    def to_dict(self) -> dict[str, Any]:
        """Serialización para GUI/LLM (JSON-safe)."""
        return {
            "worldspaces": [
                {"form_id": ws.form_id, "editor_id": ws.editor_id, "plugin": ws.plugin} for ws in self.worldspaces
            ],
            "editor_ids": self.editor_ids,
            "summary": dict(self.summary),
        }


@dataclass(frozen=True)
class ZeroBoundGrass:
    """Un record GRAS cuya versión ganadora tiene bounds nulos o ausentes."""

    form_id: str
    editor_id: str
    winner_plugin: str  # la versión que NGIO ve
    source_plugin: str  # el master que introdujo el record (mod a purgar, SOP §2.8)
    reason: str  # "zeros" | "missing"


@dataclass
class ZeroBoundReport:
    """Reporte del scan de GRAS con Object Bounds inválidos."""

    findings: list[ZeroBoundGrass]
    summary: dict[str, int]

    @property
    def has_findings(self) -> bool:
        """``True`` explica el fallo silencioso de NGIO (cache vacío); si eso
        bloquea el ritual lo decide la capa superior."""
        return bool(self.findings)

    def to_dict(self) -> dict[str, Any]:
        """Serialización para GUI/LLM (JSON-safe)."""
        return {
            "findings": [
                {
                    "form_id": f.form_id,
                    "editor_id": f.editor_id,
                    "winner_plugin": f.winner_plugin,
                    "source_plugin": f.source_plugin,
                    "reason": f.reason,
                }
                for f in self.findings
            ],
            "summary": dict(self.summary),
        }


# ---------------------------------------------------------------------------
# Parsers puros
# ---------------------------------------------------------------------------


def _clean_lines(stdout: str) -> list[str]:
    """Líneas normalizadas: strip + prefijo ``[HH:MM]``/``[HH:MM:SS]`` removido."""
    return [_TIMESTAMP_PREFIX_RE.sub("", line.strip()) for line in stdout.splitlines()]


def parse_worldspace_lines(stdout: str) -> list[GrassWorldspace]:
    """Parsea las líneas ``WSGRASS|FormID|EditorID|Plugin`` del script.

    Conteo de campos EXACTO (4): campos de más = corrupción, de menos =
    truncado — ambas se saltean con warning (review #259). El FormID inválido
    también. ``'|'`` es inválido en filenames de Windows, así que el split es
    seguro para plugins y EditorIDs.
    """
    resultado: list[GrassWorldspace] = []
    for line in _clean_lines(stdout):
        if not line.startswith(WSGRASS_PREFIX):
            continue
        partes = line.split("|")
        if len(partes) != 4:
            logger.warning("Línea WSGRASS malformada (%d campos, esperados 4): %r", len(partes), line)
            continue
        _, form_id, editor_id, plugin = partes
        if not _FORMID_RE.match(form_id):
            logger.warning("Línea WSGRASS con FormID inválido: %r", line)
            continue
        resultado.append(GrassWorldspace(form_id=form_id, editor_id=editor_id, plugin=plugin))
    return resultado


def parse_zero_bound_lines(stdout: str) -> list[ZeroBoundGrass]:
    """Parsea las líneas ``ZEROBOUND|FormID|EditorID|Winner|Source|reason``."""
    resultado: list[ZeroBoundGrass] = []
    for line in _clean_lines(stdout):
        if not line.startswith(ZEROBOUND_PREFIX):
            continue
        partes = line.split("|")
        if len(partes) != 6:
            logger.warning("Línea ZEROBOUND malformada (%d campos, esperados 6): %r", len(partes), line)
            continue
        _, form_id, editor_id, winner, source, reason = partes
        if not _FORMID_RE.match(form_id):
            logger.warning("Línea ZEROBOUND con FormID inválido: %r", line)
            continue
        if reason not in _ZERO_BOUND_REASONS:
            logger.warning("Línea ZEROBOUND con reason desconocido %r: %r", reason, line)
            continue
        resultado.append(
            ZeroBoundGrass(
                form_id=form_id,
                editor_id=editor_id,
                winner_plugin=winner,
                source_plugin=source,
                reason=reason,
            )
        )
    return resultado


# ---------------------------------------------------------------------------
# Analyzer
# ---------------------------------------------------------------------------


class GrassAnalyzer:
    """Corre los scripts de diagnóstico de grass y devuelve reportes tipados."""

    async def list_grass_worldspaces(
        self,
        plugins: list[str],
        xedit_runner: XEditRunner,
        *,
        timeout: int | None = None,
    ) -> GrassWorldspaceReport:
        """Detecta los worldspaces con pasto del load order.

        Args:
            plugins: Plugins a cargar en xEdit.
            xedit_runner: Runner configurado (se stagea el script primero).
            timeout: Timeout por llamada en segundos (el scan de LAND excede
                los 120s default del runner en load orders reales).

        Returns:
            :class:`GrassWorldspaceReport` con ``editor_ids`` listo para
            ``OnlyPregenerateWorldSpaces``.

        Raises:
            RuntimeError: xEdit falló, la salida está truncada (sin SUMMARY) o
                el conteo no cierra (fail-closed — lección #226).
        """
        result = await self._run(SCRIPT_WORLDSPACES, plugins, xedit_runner, timeout)
        worldspaces = parse_worldspace_lines(result.raw_stdout)
        summary = self._require_consistent_summary(
            result.raw_stdout,
            key="grass_worldspaces",
            parsed_count=len(worldspaces),
            script=SCRIPT_WORLDSPACES,
        )
        return GrassWorldspaceReport(worldspaces=worldspaces, summary=summary)

    async def detect_zero_bound_grass(
        self,
        plugins: list[str],
        xedit_runner: XEditRunner,
        *,
        timeout: int | None = None,
    ) -> ZeroBoundReport:
        """Detecta records GRAS con Object Bounds nulos/ausentes (versión ganadora).

        Cero hallazgos con SUMMARY consistente es ÉXITO (reporte vacío); los
        hallazgos explican el fallo silencioso de NGIO y requieren Recalc
        Bounds manual en Creation Kit (delegación HITL en capas superiores).
        """
        result = await self._run(SCRIPT_ZERO_BOUNDS, plugins, xedit_runner, timeout)
        findings = parse_zero_bound_lines(result.raw_stdout)
        summary = self._require_consistent_summary(
            result.raw_stdout,
            key="zero_bounds",
            parsed_count=len(findings),
            script=SCRIPT_ZERO_BOUNDS,
        )
        return ZeroBoundReport(findings=findings, summary=summary)

    @staticmethod
    async def _run(
        script: str,
        plugins: list[str],
        xedit_runner: XEditRunner,
        timeout: int | None,
    ) -> XEditResult:
        """Stagea el script, lo corre y aplica el guard fail-closed de #226."""
        await xedit_runner.ensure_scripts_staged([script])
        result = await xedit_runner.run_script(script, plugins, timeout=timeout)
        if not result.success:
            detalle = "; ".join(result.errors) or result.raw_stderr.strip() or f"exit code {result.return_code}"
            raise RuntimeError(f"El análisis de xEdit falló ({detalle}).")
        return result

    @staticmethod
    def _require_consistent_summary(
        stdout: str,
        *,
        key: str,
        parsed_count: int,
        script: str,
    ) -> dict[str, int]:
        """SUMMARY presente y con conteo que cierra, o RuntimeError.

        El SUMMARY se busca sobre las líneas normalizadas (mismo strip de
        timestamp que los parsers) para no depender del build de xEdit.
        """
        summary = parse_summary_line("\n".join(_clean_lines(stdout)))
        if key not in summary:
            raise RuntimeError(f"Salida de {script} truncada: falta la línea SUMMARY (el script no llegó a Finalize).")
        if summary[key] != parsed_count:
            raise RuntimeError(
                f"Salida de {script} inconsistente: SUMMARY declara {key}={summary[key]} "
                f"pero se parsearon {parsed_count} líneas (stdout corrupto o entrelazado)."
            )
        return summary


__all__ = [
    "SCRIPT_WORLDSPACES",
    "SCRIPT_ZERO_BOUNDS",
    "WSGRASS_PREFIX",
    "ZEROBOUND_PREFIX",
    "GrassAnalyzer",
    "GrassWorldspace",
    "GrassWorldspaceReport",
    "ZeroBoundGrass",
    "ZeroBoundReport",
    "parse_worldspace_lines",
    "parse_zero_bound_lines",
]
