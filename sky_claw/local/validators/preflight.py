"""Agregador de preflight: el semáforo que gobierna a los rituales mutantes (T-15).

Compone los sensores del "Servicio de Preflight" — :class:`VfsHealthChecker`
(T-13) y la detección de versión de LOOT (T-14) — en un estado agregado
verde/amarillo/rojo con una regla de composición no trivial:

* **Rojo directo:** cualquier check crítico (ej. la ruta del juego es un
  symlink).
* **Rojo por composición:** symlinks presentes (aunque solo sean warning) +
  LOOT confirmado <0.29. Cada señal por separado es amarilla; juntas son
  exactamente el escenario documentado de LOOT ciego ante el VFS de MO2
  (informe mmodding §3): libloot resuelve el symlink y se sale de la
  virtualización.
* **Amarillo:** señales individuales degradadas (symlink no crítico, LOOT
  viejo sin symlinks, versión indetectable) — advierten sin bloquear.

Rojo bloquea a los mutantes: el primer consumidor cableado es
``LootSortingService`` (la herramienta vulnerable), con override explícito
para el flujo HITL. El contrato de datos (``to_dict``) es la API que
consumirá la GUI (T-16) y el journal.
"""

from __future__ import annotations

import logging
import pathlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from sky_claw.local.loot.version import (
    LOOT_MIN_SYMLINK_SAFE,
    detect_loot_version,
    symlink_advisory,
)
from sky_claw.local.validators.vfs_health import VfsHealthChecker

logger = logging.getLogger(__name__)

#: Detector de versión inyectable (facilita tests y desacopla del binario).
VersionDetector = Callable[[], Awaitable[tuple[int, int, int] | None]]


class PreflightStatus(StrEnum):
    """Semáforo agregado del preflight."""

    GREEN = "green"
    YELLOW = "yellow"
    RED = "red"


#: Orden de severidad para agregar (max() sobre este ranking).
_SEVERITY_RANK: dict[PreflightStatus, int] = {
    PreflightStatus.GREEN: 0,
    PreflightStatus.YELLOW: 1,
    PreflightStatus.RED: 2,
}


@dataclass(frozen=True, slots=True)
class PreflightCheck:
    """Resultado de un sensor individual.

    Attributes:
        name: Identificador estable (``"vfs"``, ``"loot_version"``,
            ``"composition"``).
        status: Semáforo del check.
        summary: Una línea para el usuario.
        details: Elementos individuales (issues, advisories).
    """

    name: str
    status: PreflightStatus
    summary: str
    details: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PreflightReport:
    """Estado agregado + checks individuales. Contrato de la GUI y el journal."""

    status: PreflightStatus
    checks: tuple[PreflightCheck, ...]

    @property
    def blocks_mutations(self) -> bool:
        """True si los rituales mutantes deben bloquearse (rojo)."""
        return self.status is PreflightStatus.RED

    def to_dict(self) -> dict[str, Any]:
        """Dict serializable y estable para GUI/journal/eventos."""
        return {
            "status": self.status.value,
            "blocks_mutations": self.blocks_mutations,
            "checks": [
                {
                    "name": c.name,
                    "status": c.status.value,
                    "summary": c.summary,
                    "details": list(c.details),
                }
                for c in self.checks
            ],
        }


class PreflightService:
    """Corre los sensores y compone el semáforo.

    Args:
        vfs_checker: Sensor de symlinks/junctions (T-13). None = sin señal.
        loot_exe: Binario de LOOT para detectar la versión con
            :func:`detect_loot_version`. Ignorado si se inyecta
            ``loot_version_detector``.
        loot_version_detector: Detector asincrónico inyectable (tests/DI).
    """

    def __init__(
        self,
        *,
        vfs_checker: VfsHealthChecker | None = None,
        loot_exe: pathlib.Path | None = None,
        loot_version_detector: VersionDetector | None = None,
    ) -> None:
        self._vfs_checker = vfs_checker
        if loot_version_detector is None and loot_exe is not None:
            exe = loot_exe

            async def _detect() -> tuple[int, int, int] | None:
                return await detect_loot_version(exe)

            loot_version_detector = _detect
        self._detect_version = loot_version_detector

    async def run(self) -> PreflightReport:
        """Ejecuta los sensores configurados y agrega el semáforo."""
        checks: list[PreflightCheck] = []

        vfs_issues = self._vfs_checker.check() if self._vfs_checker is not None else []
        checks.append(self._vfs_check(vfs_issues, checker_configured=self._vfs_checker is not None))

        loot_version: tuple[int, int, int] | None = None
        loot_detected = self._detect_version is not None
        if self._detect_version is not None:
            loot_version = await self._detect_version()
        checks.append(self._loot_check(loot_version, detector_configured=loot_detected))

        composition = self._composition_check(vfs_issues, loot_version)
        if composition is not None:
            checks.append(composition)

        status = max((c.status for c in checks), key=_SEVERITY_RANK.__getitem__)
        report = PreflightReport(status=status, checks=tuple(checks))
        logger.info(
            "Preflight: %s (%s)",
            status.value,
            "; ".join(f"{c.name}={c.status.value}" for c in checks),
        )
        return report

    @staticmethod
    def _vfs_check(issues: list[Any], *, checker_configured: bool) -> PreflightCheck:
        if not checker_configured:
            # No mentir: "sin symlinks" implica que se verificó; acá no hubo sensor.
            return PreflightCheck(
                name="vfs",
                status=PreflightStatus.GREEN,
                summary="Sensor de VFS no configurado.",
            )
        if not issues:
            return PreflightCheck(
                name="vfs", status=PreflightStatus.GREEN, summary="Sin symlinks/junctions en rutas críticas."
            )
        status = PreflightStatus.RED if any(i.severity == "critical" for i in issues) else PreflightStatus.YELLOW
        return PreflightCheck(
            name="vfs",
            status=status,
            summary=f"{len(issues)} enlace(s) detectado(s) en la infraestructura.",
            details=tuple(f"{i.kind}: {i.path} — {i.remediation}" for i in issues),
        )

    @staticmethod
    def _loot_check(version: tuple[int, int, int] | None, *, detector_configured: bool) -> PreflightCheck:
        if not detector_configured:
            return PreflightCheck(
                name="loot_version",
                status=PreflightStatus.GREEN,
                summary="Detección de versión de LOOT no configurada.",
            )
        advisory = symlink_advisory(version)
        if advisory is None:
            assert version is not None  # symlink_advisory devuelve aviso para None
            return PreflightCheck(
                name="loot_version",
                status=PreflightStatus.GREEN,
                summary=f"LOOT {'.'.join(map(str, version))} (≥0.29: libloot no resuelve symlinks).",
            )
        return PreflightCheck(
            name="loot_version",
            status=PreflightStatus.YELLOW,
            summary=advisory,
        )

    @staticmethod
    def _composition_check(
        issues: list[Any],
        version: tuple[int, int, int] | None,
    ) -> PreflightCheck | None:
        """Rojo por composición: symlinks presentes + LOOT confirmado <0.29.

        Cada señal por separado es amarilla; la combinación reproduce el
        escenario de LOOT ciego, así que se promueve a rojo. Una versión
        DESCONOCIDA no promueve (se advierte, no se bloquea sin confirmación).
        """
        if not issues or version is None or version >= LOOT_MIN_SYMLINK_SAFE:
            return None
        return PreflightCheck(
            name="composition",
            status=PreflightStatus.RED,
            summary=(
                f"Enlaces (symlinks/junctions) presentes + LOOT {'.'.join(map(str, version))} (<0.29): "
                "libloot resuelve los enlaces y queda ciego ante el VFS de MO2. "
                "Actualizá LOOT a 0.29+ o eliminá los enlaces antes de ordenar."
            ),
        )
