"""Sensor de presupuesto de disco de la etapa 9 (TexGen + DynDOLOD) para el preflight.

TexGen y DynDOLOD generan GBs de LODs y de temporales durante 30+ minutos. Antes
de este sensor el espacio libre sólo se medía al EMPAQUETAR
(``DynDOLODRunner._package_output_as_mod``), o sea al final: con el disco justo, la
corrida moría después de horas de cómputo. Acá se mide ANTES de lanzar nada.

**Tres roles, un volumen puede serlo de varios.** El ``work root`` recibe el
staging crudo de cada herramienta, ``-t:`` (por defecto ``%TEMP%``, normalmente
``C:``) recibe los temporales y ``mods/`` recibe el mod empaquetado. Si dos roles
comparten volumen sus requisitos se SUMAN: durante el packaging el staging y el mod
nuevo coexisten, así que medirlos por separado daría un verde falso.

**El umbral es una elección, no una derivación.** Los rigs commiteados midieron
~0,14 GB (TexGen) y ~0,72 GB (DynDOLOD) con un load order mínimo; un perfil real
produce órdenes de magnitud más y no hay una medición que fije el número. Por eso es
un piso conservador y está nombrado en un solo lugar
(:data:`DYNDOLOD_MIN_FREE_BYTES_POR_ROL`).

Sensor de SOLO LECTURA (``shutil.disk_usage`` sobre el ancestro existente): corre en
un hilo, igual que los demás sensores del preflight.
"""

from __future__ import annotations

import logging
import pathlib
import shutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal

from sky_claw.local.validators.preflight import PreflightCheck, PreflightStatus

logger = logging.getLogger(__name__)

_GIB = 1024**3

#: Piso de espacio libre por rol, en bytes. Valor ELEGIDO (ver el docstring del módulo).
DYNDOLOD_MIN_FREE_BYTES_POR_ROL: dict[str, int] = {
    "work_root": 10 * _GIB,
    "temp": 10 * _GIB,
    "mods": 10 * _GIB,
}

IssueKind = Literal["insufficient", "unmeasurable"]
FreeBytes = Callable[[pathlib.Path], int]


@dataclass(frozen=True, slots=True)
class DiskVolume:
    """Un rol de escritura de la corrida: etiqueta, ruta (puede no existir aún) y piso requerido."""

    label: str
    path: pathlib.Path
    required_bytes: int


@dataclass(frozen=True, slots=True)
class DiskBudgetIssue:
    """Un volumen que no alcanza (o que no se pudo medir). ``label`` une los roles que lo comparten."""

    label: str
    path: str
    kind: IssueKind
    required_bytes: int
    free_bytes: int | None
    remediation: str


@dataclass(frozen=True, slots=True)
class DiskBudgetScan:
    """Volúmenes medidos (uno por volumen físico, no por rol) + issues."""

    measured: tuple[str, ...]
    issues: tuple[DiskBudgetIssue, ...]


def _disk_usage_free(path: pathlib.Path) -> int:
    return shutil.disk_usage(path).free


def _ancestro_existente(path: pathlib.Path) -> pathlib.Path:
    """El primer ancestro (o la propia ruta) que existe: ``disk_usage`` exige una ruta real."""
    actual = path
    while not actual.exists():
        padre = actual.parent
        if padre == actual:
            return actual
        actual = padre
    return actual


def _identidad_de_volumen(probe: pathlib.Path) -> object:
    """Clave del volumen físico de ``probe``. ``st_dev`` si se puede leer; si no, la propia ruta."""
    try:
        return probe.stat().st_dev
    except OSError:
        return probe


def _gib(valor: int) -> str:
    return f"{valor / _GIB:.1f} GiB"


class DiskBudgetChecker:
    """Mide el espacio libre de cada volumen que la corrida va a escribir.

    Args:
        volumes: Roles de escritura. Los que caen en el mismo volumen físico se suman.
        free_bytes: Medición inyectable (tests). Por defecto ``shutil.disk_usage``.
    """

    def __init__(self, *, volumes: Sequence[DiskVolume], free_bytes: FreeBytes | None = None) -> None:
        self._volumes = tuple(volumes)
        self._free_bytes = free_bytes or _disk_usage_free

    def check(self) -> DiskBudgetScan:
        grupos: dict[object, list[tuple[DiskVolume, pathlib.Path]]] = {}
        for volume in self._volumes:
            probe = _ancestro_existente(volume.path)
            grupos.setdefault(_identidad_de_volumen(probe), []).append((volume, probe))

        medidos: list[str] = []
        issues: list[DiskBudgetIssue] = []
        for miembros in grupos.values():
            etiqueta = "+".join(v.label for v, _ in miembros)
            probe = miembros[0][1]
            requerido = sum(v.required_bytes for v, _ in miembros)
            try:
                libre = self._free_bytes(probe)
            except OSError as exc:
                logger.warning("No se pudo medir el espacio libre de %s (%s): %s", probe, etiqueta, exc)
                issues.append(
                    DiskBudgetIssue(
                        label=etiqueta,
                        path=str(probe),
                        kind="unmeasurable",
                        required_bytes=requerido,
                        free_bytes=None,
                        remediation="No se pudo medir el volumen; verificá a mano que haya espacio antes de lanzar.",
                    )
                )
                continue
            medidos.append(f"{etiqueta}: {_gib(libre)} libres en {probe}")
            if libre < requerido:
                issues.append(
                    DiskBudgetIssue(
                        label=etiqueta,
                        path=str(probe),
                        kind="insufficient",
                        required_bytes=requerido,
                        free_bytes=libre,
                        remediation=(
                            f"Liberá espacio en {probe} o movelo a otro volumen: la etapa 9 necesita "
                            f"al menos {_gib(requerido)} y hay {_gib(libre)}."
                        ),
                    )
                )
        return DiskBudgetScan(measured=tuple(medidos), issues=tuple(issues))


def disk_budget_preflight_check(scan: DiskBudgetScan) -> PreflightCheck:
    """Compone el scan en un :class:`PreflightCheck`: rojo si falta espacio, amarillo si no se pudo medir."""
    if not scan.measured and not scan.issues:
        return PreflightCheck(
            name="disk_budget",
            status=PreflightStatus.GREEN,
            summary="Sin volúmenes de disco para verificar.",
        )
    if not scan.issues:
        return PreflightCheck(
            name="disk_budget",
            status=PreflightStatus.GREEN,
            summary=f"Espacio libre suficiente en {len(scan.measured)} volumen(es).",
            details=scan.measured,
        )
    insuficientes = [i for i in scan.issues if i.kind == "insufficient"]
    status = PreflightStatus.RED if insuficientes else PreflightStatus.YELLOW
    summary = (
        f"Espacio insuficiente en {len(insuficientes)} volumen(es) para la etapa 9."
        if insuficientes
        else "No se pudo medir el espacio libre de algún volumen de la etapa 9."
    )
    return PreflightCheck(
        name="disk_budget",
        status=status,
        summary=summary,
        details=tuple(f"{i.label}: {i.remediation}" for i in scan.issues),
    )
