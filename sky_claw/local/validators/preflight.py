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

import asyncio
import logging
import pathlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from sky_claw.local.loot.version import (
    LOOT_MIN_SYMLINK_SAFE,
    detect_loot_version,
    symlink_advisory,
)
from sky_claw.local.validators.vfs_health import VfsHealthChecker

if TYPE_CHECKING:
    from sky_claw.local.validators.missing_masters import MasterIssue
    from sky_claw.local.validators.overwrite_health import OverwriteScan
    from sky_claw.local.validators.plugin_limits import LoadOrderLimits
    from sky_claw.local.validators.write_permissions import WriteAccessReport

logger = logging.getLogger(__name__)

#: Detector de versión inyectable (facilita tests y desacopla del binario).
VersionDetector = Callable[[], Awaitable[tuple[int, int, int] | None]]

#: Sensor de masters inyectable (T-30·1): el builder arma el closure con el
#: MissingMastersChecker + la fuente del load order; el servicio solo compone.
MastersCheck = Callable[[], "list[MasterIssue]"]

#: Sensor de límites de plugins inyectable (T-30·2): closure sobre
#: PluginLimitsChecker + el load order habilitado.
LimitsCheck = Callable[[], "LoadOrderLimits"]

#: Sensor de overwrite sucio inyectable (T-30·3): closure sobre
#: OverwriteHealthChecker (escanea el overwrite compartido de MO2).
OverwriteCheck = Callable[[], "OverwriteScan"]

#: Sensor de permisos de escritura inyectable (T-30·4): closure sobre
#: WritePermissionsChecker (write-probe en las rutas que el Ritual escribe).
PermissionsCheck = Callable[[], "WriteAccessReport"]


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
        masters_check: Sensor de masters faltantes (T-30·1): callable que
            devuelve los :class:`MasterIssue` del load order habilitado
            (típicamente un closure sobre ``MissingMastersChecker.check``).
            Corre en un thread (lee headers de plugins en disco).
        limits_check: Sensor de límites full/light (T-30·2): callable que
            devuelve el :class:`LoadOrderLimits` (closure sobre
            ``PluginLimitsChecker.check``). Corre en un thread.
        overwrite_check: Sensor de overwrite sucio (T-30·3): callable que
            devuelve el :class:`OverwriteScan` (closure sobre
            ``OverwriteHealthChecker.check``). Corre en un thread (escanea
            disco).
        permissions_check: Sensor de permisos de escritura (T-30·4): callable
            que devuelve el :class:`WriteAccessReport` (closure sobre
            ``WritePermissionsChecker.check``). Corre en un thread (escribe un
            probe temporal en disco).
    """

    def __init__(
        self,
        *,
        vfs_checker: VfsHealthChecker | None = None,
        loot_exe: pathlib.Path | None = None,
        loot_version_detector: VersionDetector | None = None,
        masters_check: MastersCheck | None = None,
        limits_check: LimitsCheck | None = None,
        overwrite_check: OverwriteCheck | None = None,
        permissions_check: PermissionsCheck | None = None,
    ) -> None:
        self._vfs_checker = vfs_checker
        self._masters_check = masters_check
        self._limits_check = limits_check
        self._overwrite_check = overwrite_check
        self._permissions_check = permissions_check
        if loot_version_detector is None and loot_exe is not None:
            exe = loot_exe

            async def _detect() -> tuple[int, int, int] | None:
                return await detect_loot_version(exe)

            loot_version_detector = _detect
        self._detect_version = loot_version_detector
        # Caché de la detección: correr `loot --version` en cada ritual sería
        # relanzar el binario innecesariamente (y en el peor caso, pagar el
        # timeout completo por corrida). La versión instalada no cambia
        # durante la vida del servicio. El lock evita el doble disparo ante
        # run() concurrentes (review Copilot PR #240).
        self._version_cache: tuple[int, int, int] | None = None
        self._version_checked = False
        self._version_lock = asyncio.Lock()

    @property
    def loot_version(self) -> tuple[int, int, int] | None:
        """Versión de LOOT detectada (o None si aún no se corrió/​no se pudo).

        Expuesta para que el consumidor la registre — p. ej. el ActionManifest
        del sort (T-26) — sin relanzar el binario (review Codex PR #243)."""
        return self._version_cache

    async def run(self) -> PreflightReport:
        """Ejecuta los sensores configurados y agrega el semáforo."""
        checks: list[PreflightCheck] = []

        vfs_issues = self._vfs_checker.check() if self._vfs_checker is not None else []
        checks.append(self._vfs_check(vfs_issues, checker_configured=self._vfs_checker is not None))

        loot_version: tuple[int, int, int] | None = None
        loot_detected = self._detect_version is not None
        if self._detect_version is not None:
            async with self._version_lock:
                if not self._version_checked:
                    self._version_cache = await self._detect_version()
                    self._version_checked = True
            loot_version = self._version_cache
        checks.append(self._loot_check(loot_version, detector_configured=loot_detected))

        masters_issues: list[MasterIssue] = []
        if self._masters_check is not None:
            # Lee headers de plugins en disco: fuera del event loop.
            masters_issues = await asyncio.to_thread(self._masters_check)
        checks.append(self._masters_checkpoint(masters_issues, checker_configured=self._masters_check is not None))

        limits: LoadOrderLimits | None = None
        if self._limits_check is not None:
            limits = await asyncio.to_thread(self._limits_check)
        checks.append(self._limits_checkpoint(limits))

        overwrite: OverwriteScan | None = None
        if self._overwrite_check is not None:
            # Escanea el overwrite en disco: fuera del event loop.
            overwrite = await asyncio.to_thread(self._overwrite_check)
        checks.append(self._overwrite_checkpoint(overwrite))

        permissions: WriteAccessReport | None = None
        if self._permissions_check is not None:
            # Escribe un probe temporal en disco: fuera del event loop.
            permissions = await asyncio.to_thread(self._permissions_check)
        checks.append(self._permissions_checkpoint(permissions))

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
    def _masters_checkpoint(issues: list[MasterIssue], *, checker_configured: bool) -> PreflightCheck:
        if not checker_configured:
            # No mentir: "masters OK" implica que se verificó; acá no hubo sensor.
            return PreflightCheck(
                name="masters",
                status=PreflightStatus.GREEN,
                summary="Sensor de masters no configurado.",
            )
        # Import a nivel función: missing_masters importa PreflightCheck de este
        # módulo, así que el import a nivel módulo sería un ciclo.
        from sky_claw.local.validators.missing_masters import masters_preflight_check

        return masters_preflight_check(issues)

    @staticmethod
    def _limits_checkpoint(limits: LoadOrderLimits | None) -> PreflightCheck:
        if limits is None:
            return PreflightCheck(
                name="plugin_limits",
                status=PreflightStatus.GREEN,
                summary="Sensor de límites de plugins no configurado.",
            )
        # Import a nivel función: mismo ciclo que masters (plugin_limits importa
        # PreflightCheck de este módulo).
        from sky_claw.local.validators.plugin_limits import limits_preflight_check

        return limits_preflight_check(limits)

    @staticmethod
    def _overwrite_checkpoint(scan: OverwriteScan | None) -> PreflightCheck:
        if scan is None:
            # No mentir: "overwrite limpio" implica que se escaneó; acá no hubo sensor.
            return PreflightCheck(
                name="overwrite",
                status=PreflightStatus.GREEN,
                summary="Sensor de overwrite no configurado.",
            )
        # Import a nivel función: mismo ciclo que masters/límites (overwrite_health
        # importa PreflightCheck de este módulo).
        from sky_claw.local.validators.overwrite_health import overwrite_preflight_check

        return overwrite_preflight_check(scan)

    @staticmethod
    def _permissions_checkpoint(report: WriteAccessReport | None) -> PreflightCheck:
        if report is None:
            # No mentir: "escritura OK" implica que se probó; acá no hubo sensor.
            return PreflightCheck(
                name="write_permissions",
                status=PreflightStatus.GREEN,
                summary="Sensor de permisos no configurado.",
            )
        # Import a nivel función: mismo ciclo (write_permissions importa
        # PreflightCheck de este módulo).
        from sky_claw.local.validators.write_permissions import permissions_preflight_check

        return permissions_preflight_check(report)

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
