"""ProfileSandbox — clonado del perfil MO2 + overwrite compartido (T-27, ADR 0002).

Primer eslabón del flujo must-have de la "caja negra de vuelo":
``clonar perfil → … → aprobar → promover``. Los rituales mutantes operan sobre
una copia aislada y el estado real solo se toca al *promover* un diff aprobado.

Dos áreas se clonan, no una:

* **profile** — ``<mo2>/profiles/<nombre>`` (``plugins.txt``, ``modlist.txt``,
  settings). Copia byte-fiel: BOM UTF-8 y CRLF intactos (MO2 los preserva y el
  repo también — misma disciplina que el parsing de ``modlist.txt``).
* **overwrite** — ``<mo2>/overwrite``, el overwrite COMPARTIDO de MO2. Es
  crítico incluirlo: Synthesis (``synthesis_service.py``) y Pandora escriben
  ahí, fuera del árbol del perfil, así que clonar solo el profile no aislaría
  esas salidas (hallazgo del review de Codex en PR #241).

El clon vive fuera de ``profiles/`` (default: ``<mo2>/.skyclaw_sandbox``) para
que MO2 jamás lo liste como perfil — interacción con el VFS documentada en el
backlog. El cableado de los runners para que *apunten* al sandbox es el
follow-up T-27b; este módulo provee el núcleo clone/diff/promote.
"""

from __future__ import annotations

import asyncio
import contextlib
import filecmp
import logging
import os
import pathlib
import shutil
import stat
import uuid
from dataclasses import dataclass
from typing import Literal

from sky_claw.antigravity.security.path_validator import assert_safe_component

logger = logging.getLogger(__name__)

#: Área del árbol MO2 a la que pertenece un cambio.
Area = Literal["profile", "overwrite"]
#: Naturaleza del cambio detectado entre el clon y el estado real.
ChangeKind = Literal["added", "modified", "removed"]

#: Nombre del directorio de sandboxes dentro de la instancia MO2 (fuera de
#: ``profiles/``; MO2 no lo lista como perfil).
_DEFAULT_SANDBOX_DIRNAME = ".skyclaw_sandbox"


class ProfileSandboxError(Exception):
    """Error base del sandbox de perfiles."""


class ProfileNotFoundError(ProfileSandboxError):
    """El perfil MO2 pedido no existe en ``profiles/``."""


class SandboxLocationError(ProfileSandboxError):
    """La raíz del sandbox caería dentro de ``profiles/`` (MO2 la cargaría)."""


@dataclass(frozen=True, slots=True)
class SandboxClone:
    """Un clon materializado: rutas de origen y copia por área.

    Attributes:
        root: Directorio raíz del clon (borrar esto descarta todo el sandbox).
        profile_source: Perfil real en ``profiles/<nombre>``.
        profile_copy: Copia aislada del perfil.
        overwrite_source: Overwrite compartido real (puede no existir aún).
        overwrite_copy: Copia aislada del overwrite.
    """

    root: pathlib.Path
    profile_source: pathlib.Path
    profile_copy: pathlib.Path
    overwrite_source: pathlib.Path
    overwrite_copy: pathlib.Path


@dataclass(frozen=True, slots=True)
class FileChange:
    """Un cambio del clon respecto del estado real, explicable al usuario.

    Attributes:
        area: ``"profile"`` u ``"overwrite"``.
        relative_path: Ruta relativa al área, con ``/`` como separador
            (estable entre Windows y POSIX para mostrar y testear).
        kind: ``added`` (el ritual lo creó), ``modified`` (bytes distintos) o
            ``removed`` (el ritual lo borró).
    """

    area: Area
    relative_path: str
    kind: ChangeKind


@dataclass(frozen=True, slots=True)
class SandboxDiff:
    """Diff completo clon↔real, ordenado de forma determinista."""

    changes: tuple[FileChange, ...]

    @property
    def is_empty(self) -> bool:
        """True si el clon no difiere del estado real."""
        return not self.changes


@dataclass(frozen=True, slots=True)
class PromoteResult:
    """Resultado de promover el clon al estado real."""

    files_written: int
    files_deleted: int


class ProfileSandbox:
    """Clona el perfil MO2 activo + overwrite, diffea y promueve tras aprobación.

    Args:
        mo2_root: Raíz de la instancia portable de MO2.
        profile: Nombre del perfil (validado contra path traversal, igual que
            en :class:`~sky_claw.local.mo2.load_order.LoadOrderFileResolver`).
        sandbox_root: Dónde materializar los clones. Default:
            ``<mo2>/.skyclaw_sandbox``. Rechazado si cae dentro de
            ``profiles/`` (MO2 lo cargaría como perfil).

    Raises:
        SandboxLocationError: Si ``sandbox_root`` queda bajo ``profiles/``.
    """

    def __init__(
        self,
        *,
        mo2_root: pathlib.Path,
        profile: str = "Default",
        sandbox_root: pathlib.Path | None = None,
    ) -> None:
        assert_safe_component(profile, field="profile")
        self._mo2_root = mo2_root
        self._profile = profile
        root = sandbox_root if sandbox_root is not None else mo2_root / _DEFAULT_SANDBOX_DIRNAME

        profiles_dir = (mo2_root / "profiles").resolve()
        if root.resolve().is_relative_to(profiles_dir):
            raise SandboxLocationError(
                f"El sandbox no puede vivir dentro de {profiles_dir}: MO2 listaría el clon como un perfil real."
            )
        self._sandbox_root = root

    # ------------------------------------------------------------------
    # API pública
    # ------------------------------------------------------------------

    async def clone(self) -> SandboxClone:
        """Materializa una copia aislada y byte-fiel del perfil + overwrite.

        Returns:
            El :class:`SandboxClone` con las rutas de origen y copia.

        Raises:
            ProfileNotFoundError: Si el perfil no existe en ``profiles/``.
        """
        profile_source = self._mo2_root / "profiles" / self._profile
        if not profile_source.is_dir():
            raise ProfileNotFoundError(f"El perfil '{self._profile}' no existe en {self._mo2_root / 'profiles'}.")
        overwrite_source = self._mo2_root / "overwrite"

        clone_root = self._sandbox_root / f"{self._profile}-{uuid.uuid4().hex[:12]}"
        clone = SandboxClone(
            root=clone_root,
            profile_source=profile_source,
            profile_copy=clone_root / "profile",
            overwrite_source=overwrite_source,
            overwrite_copy=clone_root / "overwrite",
        )
        await asyncio.to_thread(self._materialize, clone)
        logger.info(
            "Perfil '%s' clonado en %s (overwrite incluido: %s)",
            self._profile,
            clone_root,
            overwrite_source.is_dir(),
        )
        return clone

    async def diff(self, clone: SandboxClone) -> SandboxDiff:
        """Compara el clon contra el estado real, área por área.

        La semántica es "qué hizo el ritual sobre la copia": ``added`` existe
        en el clon y no en el real; ``removed`` al revés; ``modified`` bytes
        distintos (comparación de contenido, no de mtime).
        """
        changes = await asyncio.to_thread(self._compute_diff, clone)
        return SandboxDiff(changes=changes)

    async def promote(self, clone: SandboxClone) -> PromoteResult:
        """Aplica al estado real los cambios del clon (llamar SOLO tras aprobar).

        Escribe added/modified y borra removed; el diff se recalcula acá mismo
        para promover exactamente lo que hay, no un diff viejo del caller.
        """
        changes = await asyncio.to_thread(self._compute_diff, clone)
        written, deleted = await asyncio.to_thread(self._apply_changes, clone, changes)
        logger.info(
            "Sandbox promovido al perfil '%s': %d archivo(s) escritos, %d borrado(s)",
            self._profile,
            written,
            deleted,
        )
        return PromoteResult(files_written=written, files_deleted=deleted)

    async def discard(self, clone: SandboxClone) -> None:
        """Descarta el clon (borra su árbol completo)."""
        await asyncio.to_thread(_rmtree_force, clone.root)
        logger.info("Sandbox descartado: %s", clone.root)

    # ------------------------------------------------------------------
    # Internos (sync; siempre invocados vía asyncio.to_thread)
    # ------------------------------------------------------------------

    def _materialize(self, clone: SandboxClone) -> None:
        """Copia byte-fiel de ambas áreas (``copy2`` preserva bytes y mtime)."""
        clone.root.mkdir(parents=True, exist_ok=True)
        shutil.copytree(clone.profile_source, clone.profile_copy, copy_function=shutil.copy2)
        if clone.overwrite_source.is_dir():
            shutil.copytree(clone.overwrite_source, clone.overwrite_copy, copy_function=shutil.copy2)
        else:
            # Sin overwrite real todavía: el clon arranca con el área vacía y
            # todo lo que el ritual escriba ahí saldrá como "added" en el diff.
            clone.overwrite_copy.mkdir(parents=True)

    def _compute_diff(self, clone: SandboxClone) -> tuple[FileChange, ...]:
        changes: list[FileChange] = []
        areas: tuple[tuple[Area, pathlib.Path, pathlib.Path], ...] = (
            ("profile", clone.profile_source, clone.profile_copy),
            ("overwrite", clone.overwrite_source, clone.overwrite_copy),
        )
        for area, source, copy in areas:
            source_files = self._relative_files(source)
            copy_files = self._relative_files(copy)

            for rel in sorted(copy_files - source_files):
                changes.append(FileChange(area=area, relative_path=rel, kind="added"))
            for rel in sorted(source_files - copy_files):
                changes.append(FileChange(area=area, relative_path=rel, kind="removed"))
            for rel in sorted(source_files & copy_files):
                # Contenido, no mtime: filecmp con shallow=False lee por chunks.
                if not filecmp.cmp(source / rel, copy / rel, shallow=False):
                    changes.append(FileChange(area=area, relative_path=rel, kind="modified"))
        return tuple(changes)

    def _apply_changes(self, clone: SandboxClone, changes: tuple[FileChange, ...]) -> tuple[int, int]:
        written = 0
        deleted = 0
        for change in changes:
            source, copy = (
                (clone.profile_source, clone.profile_copy)
                if change.area == "profile"
                else (clone.overwrite_source, clone.overwrite_copy)
            )
            target = source / change.relative_path
            if change.kind == "removed":
                target.unlink(missing_ok=True)
                deleted += 1
            else:  # added | modified
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(copy / change.relative_path, target)
                written += 1
        return written, deleted

    @staticmethod
    def _relative_files(root: pathlib.Path) -> set[str]:
        """Archivos bajo ``root`` como rutas relativas con ``/`` (set vacío si no existe)."""
        if not root.is_dir():
            return set()
        return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


def _rmtree_force(path: pathlib.Path) -> None:
    """Borra ``path`` recursivo, limpiando read-only de Windows (mods suelen
    traerlo). Mismo patrón que el helper de ``vfs.py`` (compatible con 3.11:
    intentar → limpiar bits de escritura → reintentar); no-op si no existe.
    """
    if not path.exists():
        return

    def _clear_readonly() -> None:
        for root, dirs, files in os.walk(path):
            for name in (*dirs, *files):
                p = os.path.join(root, name)
                with contextlib.suppress(OSError):
                    # Sumar el bit de escritura preservando el modo (clobberear
                    # el modo de un dir rompe rmtree en POSIX).
                    os.chmod(p, os.stat(p).st_mode | stat.S_IWRITE)

    try:
        shutil.rmtree(path)
    except OSError:
        _clear_readonly()
        shutil.rmtree(path)
