"""Sensor de permisos de escritura para el preflight (T-30·4, Oleada 7).

El clásico "Skyrim/MO2 bajo Program Files sin permisos de admin": el Ritual
mutante arranca y muere a mitad de escritura, dejando el overwrite/perfil en un
estado intermedio. Este sensor lo detecta ANTES de tocar nada con un
**write-probe empírico**: crea y borra un archivo temporal único en cada ruta
que un Ritual va a escribir.

Por qué un probe real y no ``os.access(W_OK)``: en Windows ``os.access`` mira
los bits POSIX heredados y **ignora las ACLs**, así que miente en el escenario
exacto que importa (carpeta protegida por UAC). Un ``icacls`` respondería otra
pregunta ("¿es owner-only?") y es pesado. Escribir de verdad es la única señal
confiable cross-platform.

Un permiso denegado en una ruta que el Ritual va a escribir es **crítico/rojo**
(el fallo es seguro), a diferencia del overwrite sucio (T-30·3) que solo
advierte. :func:`permissions_preflight_check` compone el resultado en un
:class:`PreflightCheck`; el cableado al ``PreflightService`` es inyectable.
"""

from __future__ import annotations

import logging
import pathlib
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from sky_claw.local.validators.preflight import PreflightCheck, PreflightStatus

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = logging.getLogger(__name__)

Severity = Literal["critical", "warning"]
#: ``denied``: el probe fue rechazado (sin permiso de escritura) → crítico.
#: ``error``: otro OSError al escribir (ruta en red caída, disco lleno) → warning.
#: ``probe_residue``: se escribió el probe pero no se pudo borrar → warning.
IssueKind = Literal["denied", "error", "probe_residue"]

_DENIED_REMEDIATION = (
    "Sin permiso de escritura: corré Skyrim/MO2 fuera de 'Program Files' "
    "(o dale permiso a esta carpeta). El Ritual fallaría al escribir acá."
)
_ERROR_REMEDIATION = "No se pudo escribir (¿ruta en red/desmontada o disco lleno?). Revisá la carpeta antes del Ritual."
_RESIDUE_REMEDIATION = (
    "Se escribió un archivo de prueba pero no se pudo borrar; eliminá manualmente '.skyclaw_probe_*.tmp'."
)


@dataclass(frozen=True, slots=True)
class WriteAccessIssue:
    """Un problema de permisos de escritura, explicable al usuario."""

    path: str
    kind: IssueKind
    severity: Severity
    remediation: str


@dataclass(frozen=True, slots=True)
class WriteAccessReport:
    """Rutas probadas + issues detectados.

    Attributes:
        probed: Rutas donde se intentó escribir (existentes y dir).
        issues: Problemas detectados (vacío si todo escribible).
    """

    probed: tuple[str, ...]
    issues: tuple[WriteAccessIssue, ...]


class WritePermissionsChecker:
    """Prueba escritura real en cada ruta objetivo (crear + borrar un temporal).

    Args:
        targets: Rutas que los Rituales van a escribir. Un **directorio** se
            prueba creando/borrando un temporal (``Data``, ``overwrite``,
            ``mods``, perfil de MO2); un **archivo existente** se prueba abriéndolo
            en modo escritura sin modificarlo (p. ej. un master oficial que
            QuickAutoClean reescribe — un archivo read-only en un dir escribible
            no lo ve el probe de directorio). Las rutas inexistentes se saltean
            (otros sensores reportan rutas faltantes).
    """

    def __init__(self, *, targets: Sequence[pathlib.Path]) -> None:
        self._targets = tuple(targets)

    def check(self) -> WriteAccessReport:
        """Prueba escritura en cada target existente (directorio o archivo)."""
        probed: list[str] = []
        issues: list[WriteAccessIssue] = []
        for target in self._targets:
            try:
                is_dir = target.is_dir()
                is_file = target.is_file() if not is_dir else False
            except PermissionError as exc:
                # No se pudo ni stat-ear (ACL de Windows niega atributos, o el
                # padre es inaccesible): si el Ritual escribe acá va a fallar,
                # así que es tan crítico como una denegación de escritura — no
                # saltear en silencio (review Codex #256).
                logger.debug("Acceso denegado al inspeccionar %s: %s", target, exc)
                probed.append(str(target))
                issues.append(
                    WriteAccessIssue(
                        path=str(target), kind="denied", severity="critical", remediation=_DENIED_REMEDIATION
                    )
                )
                continue
            except OSError as exc:
                logger.debug("No se pudo inspeccionar %s: %s", target, exc)
                probed.append(str(target))
                issues.append(
                    WriteAccessIssue(path=str(target), kind="error", severity="warning", remediation=_ERROR_REMEDIATION)
                )
                continue
            if is_dir:
                probed.append(str(target))
                issue = self._probe_write(target)
            elif is_file:
                probed.append(str(target))
                issue = self._probe_write_file(target)
            else:
                # Inexistente (ni dir ni archivo): no es residuo — otros sensores
                # reportan rutas faltantes; is_dir()/is_file() no lanzan para inexistentes.
                continue
            if issue is not None:
                issues.append(issue)
        if issues:
            logger.warning("Permisos de escritura: %d problema(s) en %d ruta(s)", len(issues), len(probed))
        return WriteAccessReport(probed=tuple(probed), issues=tuple(issues))

    @staticmethod
    def _probe_write(directory: pathlib.Path) -> WriteAccessIssue | None:
        """Crea y borra un archivo único; devuelve un issue si algo falla.

        El nombre lleva un UUID para no colisionar con un run concurrente ni con
        un residuo previo (modo ``"x"``: creación exclusiva).
        """
        probe = directory / f".skyclaw_probe_{uuid.uuid4().hex}.tmp"
        try:
            with probe.open("x"):
                pass
        except PermissionError as exc:
            logger.debug("Escritura denegada en %s: %s", directory, exc)
            return WriteAccessIssue(
                path=str(directory), kind="denied", severity="critical", remediation=_DENIED_REMEDIATION
            )
        except OSError as exc:
            logger.debug("No se pudo escribir el probe en %s: %s", directory, exc)
            return WriteAccessIssue(
                path=str(directory), kind="error", severity="warning", remediation=_ERROR_REMEDIATION
            )
        try:
            probe.unlink()
        except OSError as exc:
            logger.debug("No se pudo borrar el probe %s: %s", probe, exc)
            return WriteAccessIssue(
                path=str(directory), kind="probe_residue", severity="warning", remediation=_RESIDUE_REMEDIATION
            )
        return None

    @staticmethod
    def _probe_write_file(path: pathlib.Path) -> WriteAccessIssue | None:
        """Prueba escritura sobre un archivo EXISTENTE sin modificarlo.

        Abre en modo ``"r+b"`` (lectura+escritura, sin truncar) y cierra sin
        escribir: si el archivo es read-only (atributo o ACL), el ``open`` lanza
        ``PermissionError`` → crítico. Cubre el caso que el probe de directorio no
        ve — ``Data`` escribible pero un master oficial read-only, donde
        QuickAutoClean fallaría al reescribirlo (review Codex #288).
        """
        try:
            with path.open("r+b"):
                pass
        except PermissionError as exc:
            logger.debug("Escritura denegada en el archivo %s: %s", path, exc)
            return WriteAccessIssue(path=str(path), kind="denied", severity="critical", remediation=_DENIED_REMEDIATION)
        except OSError as exc:
            logger.debug("No se pudo abrir %s para escritura: %s", path, exc)
            return WriteAccessIssue(path=str(path), kind="error", severity="warning", remediation=_ERROR_REMEDIATION)
        return None


def permissions_preflight_check(report: WriteAccessReport) -> PreflightCheck:
    """Compone el reporte en un :class:`PreflightCheck` para el semáforo.

    Rojo si alguna ruta niega la escritura (el Ritual fallaría seguro); amarillo
    si solo hubo warnings (ruta en red, residuo); verde reporta cuántas rutas se
    verificaron.
    """
    if not report.issues:
        summary = (
            f"Escritura verificada en {len(report.probed)} ruta(s)."
            if report.probed
            else "Sin rutas de escritura para verificar."
        )
        return PreflightCheck(name="write_permissions", status=PreflightStatus.GREEN, summary=summary)
    status = (
        PreflightStatus.RED if any(issue.severity == "critical" for issue in report.issues) else PreflightStatus.YELLOW
    )
    return PreflightCheck(
        name="write_permissions",
        status=status,
        summary=f"{len(report.issues)} problema(s) de permisos de escritura.",
        details=tuple(f"{i.path}: {i.remediation}" for i in report.issues),
    )
