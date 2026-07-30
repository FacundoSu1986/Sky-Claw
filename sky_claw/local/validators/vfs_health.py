"""Health-check de VFS: symlinks/junctions que rompen la virtualización (T-13).

libloot <0.29 resuelve la ruta real de los archivos; si la ruta del juego (o
un ancestro) es un symlink/junction, la resolución "sale" del paraguas del VFS
de MO2 y LOOT queda ciego ante los mods virtualizados (informe mmodding §3).
Los mods symlinkeados dentro de ``mods/`` u ``overwrite/`` tienen el mismo
riesgo para cualquier herramienta externa.

A diferencia del sandboxing de ``security/path_validator`` — que protege al
PROCESO de escaparse — este checker protege al USUARIO: es un chequeo de
preflight (T-15) que reporta la infraestructura problemática con remediación,
antes de que un Ritual mute nada.
"""

from __future__ import annotations

import logging
import pathlib
from dataclasses import dataclass

# La detección de enlaces NO se reimplementa acá: estaba duplicada con
# ``overwrite_health`` (cuyo helper declaraba en su docstring que "espejaba" a este)
# y el rollback de directorios iba a ser la tercera copia. Conocer que ``islink()``
# no ve los junctions y que ``is_junction`` recién existe en 3.12 es exactamente la
# clase de detalle que no puede estar escrito en tres lados. Anclado en
# ``tests/test_links.py::test_la_deteccion_de_junctions_vive_en_un_solo_modulo``.
from sky_claw.app.security.links import link_kind

logger = logging.getLogger(__name__)

#: Subdirectorios de MO2 cuya virtualización importa a las herramientas.
_MO2_SUBDIRS: tuple[str, ...] = ("mods", "profiles", "overwrite")

_REMEDIATION_GAME = (
    "La ruta del juego pasa por un enlace: LOOT <0.29 (libloot) resuelve la "
    "ruta real y queda ciego ante los mods del VFS de MO2. Reemplazá el "
    "enlace por la carpeta real (o renombrado físico) o actualizá LOOT a "
    "0.29+."
)
_REMEDIATION_MO2 = (
    "Elemento de MO2 detrás de un enlace: las herramientas externas pueden "
    "resolver la ruta real y salirse del VFS. Preferí carpetas reales dentro "
    "de la instancia de MO2."
)


@dataclass(frozen=True, slots=True)
class VfsIssue:
    """Un enlace problemático encontrado en la infraestructura.

    Attributes:
        path: Ruta del enlace (no su destino).
        kind: ``"symlink"`` o ``"junction"`` (reparse point de Windows).
        severity: ``"critical"`` (ruta del juego: LOOT ciego) o ``"warning"``.
        remediation: Qué hacer al respecto, en términos del usuario.
    """

    path: pathlib.Path
    kind: str
    severity: str
    remediation: str


class VfsHealthChecker:
    """Detecta symlinks/junctions en las rutas del juego y de MO2.

    IMPORTANTE: pasar las rutas CONFIGURADAS sin resolver
    (``PathResolutionService.get_*_path_raw()``). Las rutas resueltas por el
    validator ya siguieron los symlinks — inspeccionarlas reporta verde sobre
    el destino real y oculta el enlace que este checker existe para encontrar
    (review Codex PR #239).

    Args:
        game_path: Instalación de Skyrim (ella y sus ancestros se inspeccionan;
            un enlace acá es ``critical`` por el caso LOOT/libloot).
        mo2_root: Instancia portable de MO2 (raíz, ancestros, ``mods/``,
            ``profiles/``, ``overwrite/`` y el primer nivel de ``mods/``).
    """

    def __init__(
        self,
        *,
        game_path: pathlib.Path | None = None,
        mo2_root: pathlib.Path | None = None,
        scan_mods_dir: bool = True,
    ) -> None:
        self._game_path = game_path
        self._mo2_root = mo2_root
        # Enumerar mods/ (iterdir) sobre una ruta NO validada por el sandbox
        # permitiría listar directorios arbitrarios (review Codex PR #240):
        # el caller lo deshabilita cuando la ruta cruda no tiene contraparte
        # validada. Los lstat de rutas fijas (root/ancestros/subdirs con
        # nombre conocido) no enumeran nada y siempre corren.
        self._scan_mods_dir = scan_mods_dir

    def check(self) -> list[VfsIssue]:
        """Devuelve los enlaces encontrados, sin duplicados, en orden estable."""
        issues: list[VfsIssue] = []
        seen: set[pathlib.Path] = set()

        def add(path: pathlib.Path, severity: str, remediation: str) -> None:
            if path in seen:
                return
            kind = link_kind(path)
            if kind is None:
                return
            seen.add(path)
            issues.append(VfsIssue(path=path, kind=kind, severity=severity, remediation=remediation))

        def add_with_ancestors(path: pathlib.Path, severity: str, remediation: str) -> None:
            add(path, severity, remediation)
            for ancestor in path.parents:
                add(ancestor, severity, remediation)

        if self._game_path is not None:
            add_with_ancestors(self._game_path, "critical", _REMEDIATION_GAME)

        if self._mo2_root is not None:
            add_with_ancestors(self._mo2_root, "warning", _REMEDIATION_MO2)
            for subdir in _MO2_SUBDIRS:
                sub_path = self._mo2_root / subdir
                add(sub_path, "warning", _REMEDIATION_MO2)
            mods_dir = self._mo2_root / "mods"
            if self._scan_mods_dir and mods_dir.is_dir():
                for mod_dir in sorted(mods_dir.iterdir()):
                    add(mod_dir, "warning", _REMEDIATION_MO2)

        if issues:
            logger.warning(
                "VFS health-check: %d enlace(s) detectado(s): %s",
                len(issues),
                [str(i.path) for i in issues],
            )
        return issues
