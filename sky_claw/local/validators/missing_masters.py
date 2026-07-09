"""Sensor de masters faltantes para el preflight (T-30·1, Oleada 7 / ADR 0002).

Un plugin cuyos masters no están instalados o no están habilitados en el load
order es la causa clásica de CTD al arrancar el juego — y de fallos a mitad de
un Ritual (xEdit no puede cargar el plugin). Este sensor lo detecta ANTES de
tocar nada, leyendo los subrecords ``MAST`` del header ``TES4`` con parsing
binario puro: sin xEdit ni LOOT (el preflight corre antes de cualquier
herramienta externa) y en milisegundos por plugin (solo se lee el header, no
el archivo completo).

Formato del record TES4 (Skyrim SE, .esp/.esm/.esl): header de 24 bytes
(firma ``TES4`` + dataSize u32 + flags/formID/vc + formVersion) seguido de
subrecords ``firma(4) + size(u16) + data``; cada master aparece como un
``MAST`` (zstring) seguido de su ``DATA`` (u64). El record TES4 nunca está
comprimido. Este parser es la semilla de T-17 (``PluginHeaderInspector``).

El puente :func:`masters_preflight_check` produce el :class:`PreflightCheck`
listo para componer en el semáforo — el cableado dentro de
``PreflightService`` es el follow-up de T-30 (no se toca ``preflight.py``
mientras haya trabajo en vuelo sobre ese archivo).
"""

from __future__ import annotations

import logging
import pathlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from sky_claw.local.validators.plugin_header import PluginHeaderError, read_plugin_header
from sky_claw.local.validators.preflight import PreflightCheck, PreflightStatus

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = logging.getLogger(__name__)

#: Extensiones de plugin que el juego carga.
_PLUGIN_SUFFIXES: frozenset[str] = frozenset({".esp", ".esm", ".esl"})

# ``PluginHeaderError`` se re-exporta (el parser TES4 vive ahora en
# ``plugin_header``): los consumidores históricos lo importan desde acá.
__all__ = [
    "IssueKind",
    "MasterIssue",
    "MissingMastersChecker",
    "PluginHeaderError",
    "Severity",
    "masters_preflight_check",
    "read_masters",
]

Severity = Literal["critical", "warning"]
IssueKind = Literal["missing", "disabled", "unreadable", "plugin_not_found"]


@dataclass(frozen=True, slots=True)
class MasterIssue:
    """Un problema de dependencias de masters, explicable al usuario.

    Attributes:
        plugin: Plugin del load order al que pertenece el issue.
        master: Master afectado (``None`` para issues del plugin en sí).
        kind: ``missing`` (master no instalado), ``disabled`` (instalado pero
            fuera del load order), ``unreadable`` (header ilegible) o
            ``plugin_not_found`` (el load order lista un plugin inexistente).
        severity: ``critical`` bloquea (el juego crashea al cargar);
            ``warning`` avisa.
        remediation: Qué hacer al respecto, en términos del usuario.
    """

    plugin: str
    master: str | None
    kind: IssueKind
    severity: Severity
    remediation: str


def read_masters(plugin: pathlib.Path) -> list[str]:
    """Lee los masters (subrecords ``MAST``) del header TES4 de ``plugin``.

    Delega en :func:`~sky_claw.local.validators.plugin_header.read_plugin_header`
    (parser TES4 canónico); se conserva por compatibilidad con los consumidores
    que solo necesitan los masters.

    Raises:
        PluginHeaderError: Si el archivo está truncado o no es un plugin TES4.
    """
    # El header expone los masters como tupla (inmutable); acá se conserva el
    # contrato histórico ``list[str]``.
    return list(read_plugin_header(plugin).masters)


class MissingMastersChecker:
    """Detecta masters faltantes/deshabilitados en el load order habilitado.

    Args:
        plugin_dirs: Directorios donde viven los plugins (``Data`` del juego
            y/o carpetas de mods de MO2 — fuera del VFS los plugins están
            repartidos entre mods, por eso se aceptan varios). No se recorre
            recursivo: los plugins van en la raíz de cada carpeta.
    """

    def __init__(self, *, plugin_dirs: Sequence[pathlib.Path]) -> None:
        self._plugin_dirs = tuple(plugin_dirs)

    def check(self, enabled_plugins: Sequence[str]) -> list[MasterIssue]:
        """Devuelve los issues de masters del load order, en orden estable."""
        available = self._index_available()
        enabled = {name.casefold() for name in enabled_plugins}
        issues: list[MasterIssue] = []

        for plugin_name in enabled_plugins:
            path = available.get(plugin_name.casefold())
            if path is None:
                issues.append(
                    MasterIssue(
                        plugin=plugin_name,
                        master=None,
                        kind="plugin_not_found",
                        severity="warning",
                        remediation=(
                            f"'{plugin_name}' está en el load order pero no existe en disco: "
                            "load order desactualizado — quitalo o reinstalá el mod."
                        ),
                    )
                )
                continue

            try:
                masters = read_masters(path)
            except PluginHeaderError as exc:
                issues.append(
                    MasterIssue(
                        plugin=plugin_name,
                        master=None,
                        kind="unreadable",
                        severity="warning",
                        remediation=f"No se pudo leer el header de '{plugin_name}' ({exc}); verificá el archivo.",
                    )
                )
                continue

            for master in masters:
                key = master.casefold()
                if key in enabled and key in available:
                    continue
                if key in available:
                    issues.append(
                        MasterIssue(
                            plugin=plugin_name,
                            master=master,
                            kind="disabled",
                            severity="critical",
                            remediation=(
                                f"'{plugin_name}' necesita '{master}', que está instalado pero "
                                "deshabilitado: habilitalo en el load order antes que su dependiente."
                            ),
                        )
                    )
                else:
                    issues.append(
                        MasterIssue(
                            plugin=plugin_name,
                            master=master,
                            kind="missing",
                            severity="critical",
                            remediation=(
                                f"'{plugin_name}' necesita '{master}', que no está instalado: "
                                "instalá el mod que lo provee (o desinstalá el dependiente)."
                            ),
                        )
                    )

        if issues:
            logger.warning(
                "Masters check: %d issue(s) en el load order: %s",
                len(issues),
                [f"{i.plugin}→{i.master} ({i.kind})" for i in issues],
            )
        return issues

    def _index_available(self) -> dict[str, pathlib.Path]:
        """Nombre casefold → ruta del plugin (primer directorio gana)."""
        available: dict[str, pathlib.Path] = {}
        for directory in self._plugin_dirs:
            try:
                if not directory.is_dir():
                    continue
                entries = sorted(directory.iterdir())
            except OSError as exc:
                logger.debug("No se pudo inspeccionar %s: %s", directory, exc)
                continue
            for entry in entries:
                try:
                    if entry.is_file() and entry.suffix.lower() in _PLUGIN_SUFFIXES:
                        available.setdefault(entry.name.casefold(), entry)
                except OSError as exc:
                    logger.debug("No se pudo inspeccionar %s: %s", entry, exc)
        return available


def masters_preflight_check(issues: Sequence[MasterIssue]) -> PreflightCheck:
    """Compone los issues en un :class:`PreflightCheck` para el semáforo.

    Rojo si algún master falta o está deshabilitado (el juego crashea al
    cargar); amarillo con solo warnings (plugin ilegible / load order stale).
    """
    if not issues:
        return PreflightCheck(
            name="masters",
            status=PreflightStatus.GREEN,
            summary="Todos los masters presentes y habilitados.",
        )
    status = PreflightStatus.RED if any(issue.severity == "critical" for issue in issues) else PreflightStatus.YELLOW
    return PreflightCheck(
        name="masters",
        status=status,
        summary=f"{len(issues)} problema(s) de masters en el load order.",
        details=tuple(f"{i.plugin} → {i.master or '(header)'} [{i.kind}]: {i.remediation}" for i in issues),
    )
