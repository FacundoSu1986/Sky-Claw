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

from sky_claw.local.validators.preflight import PreflightCheck, PreflightStatus

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = logging.getLogger(__name__)

#: Extensiones de plugin que el juego carga.
_PLUGIN_SUFFIXES: frozenset[str] = frozenset({".esp", ".esm", ".esl"})

#: Tamaño del header del record TES4 en Skyrim SE.
_TES4_HEADER_SIZE = 24

Severity = Literal["critical", "warning"]
IssueKind = Literal["missing", "disabled", "unreadable", "plugin_not_found"]


class PluginHeaderError(Exception):
    """El archivo no tiene un header TES4 legible (truncado o no es un plugin)."""


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

    Solo se lee el header (24 bytes + dataSize), nunca el archivo completo.

    Raises:
        PluginHeaderError: Si el archivo está truncado o no es un plugin TES4.
    """
    try:
        with plugin.open("rb") as fh:
            head = fh.read(_TES4_HEADER_SIZE)
            if len(head) < _TES4_HEADER_SIZE or head[:4] != b"TES4":
                raise PluginHeaderError(f"{plugin.name}: no tiene un header TES4 válido.")
            data_size = int.from_bytes(head[4:8], "little")
            data = fh.read(data_size)
    except OSError as exc:
        raise PluginHeaderError(f"{plugin.name}: no se pudo leer ({exc}).") from exc
    if len(data) < data_size:
        raise PluginHeaderError(f"{plugin.name}: header TES4 truncado.")

    masters: list[str] = []
    offset = 0
    xxxx_size: int | None = None
    while offset + 6 <= len(data):
        sig = data[offset : offset + 4]
        size = int.from_bytes(data[offset + 4 : offset + 6], "little")
        offset += 6
        if sig == b"XXXX" and size == 4:
            # XXXX extiende el tamaño del subrecord siguiente (raro en TES4,
            # pero el formato lo permite — manejo defensivo).
            xxxx_size = int.from_bytes(data[offset : offset + 4], "little")
            offset += 4
            continue
        real_size = xxxx_size if xxxx_size is not None else size
        xxxx_size = None
        field = data[offset : offset + real_size]
        offset += real_size
        if sig == b"MAST":
            # zstring en windows-1252 (encoding clásico de los plugins).
            masters.append(field.rstrip(b"\x00").decode("cp1252", errors="replace"))
    return masters


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
                if key in enabled:
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
            if not directory.is_dir():
                continue
            for entry in sorted(directory.iterdir()):
                if entry.is_file() and entry.suffix.lower() in _PLUGIN_SUFFIXES:
                    available.setdefault(entry.name.casefold(), entry)
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
