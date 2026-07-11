"""Validador post-run (T-21, ADR 0002) — el lazo `validate` del pipeline.

El preflight (T-15/T-30) frena ANTES de un Ritual mutante; este validador
verifica DESPUÉS qué dejó: re-corre los mismos sensores del
:class:`~sky_claw.local.validators.preflight.PreflightService` (masters,
límites, overwrite, permisos — los closures de #252 re-resuelven, así que ven
el estado post-mutación) y suma el check de **header 43**: un plugin con
formVersion de Skyrim LE activo en SE es un riesgo real que ninguna
herramienta reporta sola.

Su resultado viaja en el slot ``post_run_validation`` del FlightReport (T-28
lo dejó esperando explícitamente a T-21) y, cuando hay hallazgos, en la
respuesta del Ritual. Es **post-vuelo y best-effort**: un fallo del validador
jamás rompe un Ritual ya exitoso (misma disciplina que el flight report).

v1: cableado en ``LootSortingService`` (primer consumidor, patrón T-15).
Follow-ups documentados: el resto de los mutantes (xEdit/Synthesis/DynDOLOD) y
la visibilidad en GUI (con T-16/T-28 GUI).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sky_claw.local.validators.plugin_header import PluginHeaderError, read_plugin_header
from sky_claw.local.validators.preflight import PreflightReport, PreflightService, PreflightStatus

if TYPE_CHECKING:
    from collections.abc import Callable

    from sky_claw.local.mo2.plugin_sources import PluginSources

logger = logging.getLogger(__name__)

#: formVersion que Skyrim SE espera en el header TES4 (43 = LE sin portear).
_EXPECTED_FORM_VERSION = 44

_HEADER_REMEDIATION = (
    "header con formVersion {version} (Skyrim LE, se espera 44): resaveá el plugin en el "
    "Creation Kit de SE (o revisá el port) — los plugins 43 pueden corromper datos en SE."
)


@dataclass(frozen=True, slots=True)
class HeaderVersionIssue:
    """Un plugin habilitado cuyo header no es el que SE espera."""

    plugin: str
    form_version: int
    remediation: str


@dataclass(frozen=True, slots=True)
class PostRunReport:
    """Qué dejó el Ritual: el semáforo re-corrido + los headers sospechosos.

    Attributes:
        preflight: El reporte de los sensores re-corridos post-mutación.
        header_issues: Plugins habilitados con formVersion != 44.
        headers_checked: False si no hubo fuentes de plugins resolubles — el
            check no corrió (regla de honestidad: no se afirma "headers OK").
    """

    preflight: PreflightReport
    header_issues: tuple[HeaderVersionIssue, ...]
    headers_checked: bool

    @property
    def has_findings(self) -> bool:
        """True si hay algo que el operador debería mirar."""
        return self.preflight.status is not PreflightStatus.GREEN or bool(self.header_issues)

    def to_dict(self) -> dict[str, Any]:
        """Dict serializable para el FlightReport, el journal y la respuesta."""
        return {
            "kind": "post_run_validation",
            "has_findings": self.has_findings,
            "preflight": self.preflight.to_dict(),
            "headers_checked": self.headers_checked,
            "header_issues": [
                {"plugin": i.plugin, "form_version": i.form_version, "remediation": i.remediation}
                for i in self.header_issues
            ],
        }


class PostRunValidator:
    """Compone el semáforo re-corrido + el check de headers (T-21 v1).

    Args:
        preflight: El ``PreflightService`` del Ritual — el MISMO que corrió el
            gate previo (sus closures re-resuelven en cada run, así que acá
            ven el estado post-mutación sin reconstruir nada).
        plugin_sources: Resolver de fuentes de plugins (el de T-30w). ``None``
            = el check de headers se declara no corrido.
    """

    def __init__(
        self,
        *,
        preflight: PreflightService,
        plugin_sources: Callable[[], PluginSources] | None = None,
    ) -> None:
        self._preflight = preflight
        self._plugin_sources = plugin_sources

    async def run(self) -> PostRunReport:
        """Re-corre los sensores y chequea los headers de los habilitados."""
        report = await self._preflight.run()

        issues: tuple[HeaderVersionIssue, ...] = ()
        checked = False
        if self._plugin_sources is not None:
            sources = self._plugin_sources()
            if sources.plugin_dirs and sources.enabled_plugins:
                # Lee headers de plugins en disco: fuera del event loop.
                issues = await asyncio.to_thread(self._check_headers, sources)
                checked = True

        result = PostRunReport(preflight=report, header_issues=issues, headers_checked=checked)
        if result.has_findings:
            logger.warning(
                "Post-run con hallazgos: preflight=%s, headers sospechosos=%d",
                report.status.value,
                len(issues),
            )
        return result

    @staticmethod
    def _check_headers(sources: PluginSources) -> tuple[HeaderVersionIssue, ...]:
        """formVersion de cada plugin habilitado (best-effort, first-match)."""
        available: dict[str, Any] = {}
        for directory in sources.plugin_dirs:
            try:
                if not directory.is_dir():
                    continue
                for entry in sorted(directory.iterdir()):
                    available.setdefault(entry.name.casefold(), entry)
            except OSError as exc:
                logger.debug("No se pudo enumerar %s: %s", directory, exc)

        issues: list[HeaderVersionIssue] = []
        for plugin_name in sources.enabled_plugins:
            path = available.get(plugin_name.casefold())
            if path is None:
                continue  # ausente en disco: el sensor de masters ya lo reporta
            try:
                header = read_plugin_header(path)
            except PluginHeaderError as exc:
                logger.debug("Header ilegible en %s: %s", plugin_name, exc)
                continue
            if header.form_version != _EXPECTED_FORM_VERSION:
                issues.append(
                    HeaderVersionIssue(
                        plugin=plugin_name,
                        form_version=header.form_version,
                        remediation=_HEADER_REMEDIATION.format(version=header.form_version),
                    )
                )
        return tuple(issues)
