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


class SandboxSymlinkError(ProfileSandboxError):
    """Se encontró un symlink en un árbol del sandbox — fail-closed.

    Un symlink puede sacar la copia, el diff o la promoción fuera del sandbox
    (leer contenido externo o escribir sobre targets inesperados). Misma
    política que ``file_permissions``/``vfs_health``: cortar, no seguir.
    """


class SandboxDriftError(ProfileSandboxError):
    """El árbol real cambió desde el clonado — promover a ciegas es inseguro.

    En la ventana de aprobación MO2, el usuario u otro proceso pueden escribir
    en el perfil/overwrite reales. Esos cambios vivos NO son del ritual:
    aplicarles el diff del clon los borraría o pisaría. Fail-closed: se
    reclona y se re-ejecuta, no se promueve sobre un real desconocido.
    """


class SandboxRollbackError(ProfileSandboxError):
    """El promote falló Y el rollback también: el perfil real puede haber
    quedado inconsistente.

    El mensaje incluye la ruta del directorio de backup (que se preserva) para
    restaurar a mano los archivos afectados; la excepción original del promote
    viaja encadenada (``__cause__``).
    """


@dataclass(frozen=True, slots=True)
class SandboxClone:
    """Un clon materializado: origen, copia mutable y baseline por área.

    El **baseline** es la foto intacta de clone-time: contra él se calcula el
    diff (qué hizo el ritual sobre la copia) y contra él se detecta drift del
    lado real antes de promover (review Codex PR #245).

    Attributes:
        root: Directorio raíz del clon (borrar esto descarta todo el sandbox).
        profile_source: Perfil real en ``profiles/<nombre>``.
        profile_copy: Copia mutable del perfil (acá opera el ritual).
        profile_baseline: Foto intacta del perfil al clonar.
        overwrite_source: Overwrite compartido real (puede no existir aún).
        overwrite_copy: Copia mutable del overwrite.
        overwrite_baseline: Foto intacta del overwrite al clonar.
    """

    root: pathlib.Path
    profile_source: pathlib.Path
    profile_copy: pathlib.Path
    profile_baseline: pathlib.Path
    overwrite_source: pathlib.Path
    overwrite_copy: pathlib.Path
    overwrite_baseline: pathlib.Path


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
        # Normalizar como hace MO2Controller: sin resolve(), un mo2_root
        # relativo o con symlinks dejaría el chequeo de ubicación y las rutas
        # del clon inconsistentes entre sí (review Copilot PR #245).
        self._mo2_root = mo2_root.resolve()
        self._profile = profile
        root = sandbox_root.resolve() if sandbox_root is not None else self._mo2_root / _DEFAULT_SANDBOX_DIRNAME

        # Prohibido dentro de CUALQUIER área clonada, no solo profiles/: un
        # sandbox bajo overwrite/ contaminaría el árbol que se está clonando
        # (recursión y artefactos propios en el diff — review Codex PR #245).
        profiles_dir = self._mo2_root / "profiles"
        overwrite_dir = self._mo2_root / "overwrite"
        if root.is_relative_to(profiles_dir) or root.is_relative_to(overwrite_dir):
            raise SandboxLocationError(
                f"El sandbox no puede vivir dentro de un área clonada ({profiles_dir} u {overwrite_dir}): "
                "MO2 listaría el clon como perfil o el clonado se contaminaría a sí mismo."
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
            profile_baseline=clone_root / "baseline" / "profile",
            overwrite_source=overwrite_source,
            overwrite_copy=clone_root / "overwrite",
            overwrite_baseline=clone_root / "baseline" / "overwrite",
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

        Primero verifica que el real no haya cambiado desde el clonado (drift
        gate, fail-closed) y que no aparezcan symlinks; después aplica el diff
        recalculado acá mismo — se promueve exactamente lo que hay, no un diff
        viejo del caller.

        La aplicación es transaccional (todo o nada): antes de mutar el árbol
        real se respalda cada target afectado y, si un cambio falla a mitad,
        los ya aplicados se revierten en orden inverso — el perfil real vuelve
        byte-exacto al estado previo al promote.

        Raises:
            SandboxDriftError: Si el perfil/overwrite reales cambiaron desde
                el clonado (promover pisaría cambios vivos).
            SandboxSymlinkError: Si hay symlinks en alguno de los árboles.
            SandboxRollbackError: Si el promote falló Y el rollback también;
                el backup se preserva para restauración manual.
        """
        written, deleted = await asyncio.to_thread(self._promote_sync, clone)
        logger.info(
            "Sandbox promovido al perfil '%s': %d archivo(s) escritos, %d borrado(s)",
            self._profile,
            written,
            deleted,
        )
        return PromoteResult(files_written=written, files_deleted=deleted)

    def _promote_sync(self, clone: SandboxClone) -> tuple[int, int]:
        """F5 (auditoría 2026-07-18): drift-gate + diff + apply en UN solo hilo.

        Antes ``promote`` corría ``_check_drift`` / ``_compute_diff`` /
        ``_apply_changes`` en tres ``asyncio.to_thread`` separados, con vueltas al
        event loop entre medio: una escritura de MO2/usuario en la ventana entre
        el gate y el apply se pisaba en silencio — justo lo que el drift-gate
        promete cortar. Fusionadas en una única función sync (un solo
        ``to_thread``), no hay scheduling del loop entre verificar y mutar, así
        que la ventana TOCTOU a nivel asyncio desaparece. Las tres siguen siendo
        sync e intactas; solo dejan de ser awaited por separado.
        """
        self._check_drift(clone)
        changes = self._compute_diff(clone)
        return self._apply_changes(clone, changes)

    async def discard(self, clone: SandboxClone) -> None:
        """Descarta el clon (borra su árbol completo)."""
        await asyncio.to_thread(_rmtree_force, clone.root)
        logger.info("Sandbox descartado: %s", clone.root)

    # ------------------------------------------------------------------
    # Internos (sync; siempre invocados vía asyncio.to_thread)
    # ------------------------------------------------------------------

    def _materialize(self, clone: SandboxClone) -> None:
        """Copia byte-fiel de ambas áreas + baseline (``copy2`` preserva bytes y mtime)."""
        # Fail-closed ANTES de copiar: copytree seguiría un symlink del árbol
        # real y materializaría contenido de fuera del sandbox.
        self._reject_symlinks(clone.profile_source)
        self._reject_symlinks(clone.overwrite_source)
        clone.root.mkdir(parents=True, exist_ok=True)
        shutil.copytree(clone.profile_source, clone.profile_copy, copy_function=shutil.copy2)
        if clone.overwrite_source.is_dir():
            shutil.copytree(clone.overwrite_source, clone.overwrite_copy, copy_function=shutil.copy2)
        else:
            # Sin overwrite real todavía: el clon arranca con el área vacía y
            # todo lo que el ritual escriba ahí saldrá como "added" en el diff.
            clone.overwrite_copy.mkdir(parents=True)
        # El baseline se copia DESDE las copias recién hechas para garantizar
        # identidad bit a bit en t0 (diff y drift se miden contra esta foto).
        shutil.copytree(clone.profile_copy, clone.profile_baseline, copy_function=shutil.copy2)
        shutil.copytree(clone.overwrite_copy, clone.overwrite_baseline, copy_function=shutil.copy2)

    @classmethod
    def _tree_changes(cls, area: Area, old: pathlib.Path, new: pathlib.Path) -> tuple[FileChange, ...]:
        """Cambios de ``old`` → ``new`` para un área (orden determinista)."""
        changes: list[FileChange] = []
        old_files = cls._relative_files(old)
        new_files = cls._relative_files(new)

        for rel in sorted(new_files - old_files):
            changes.append(FileChange(area=area, relative_path=rel, kind="added"))
        for rel in sorted(old_files - new_files):
            changes.append(FileChange(area=area, relative_path=rel, kind="removed"))
        for rel in sorted(old_files & new_files):
            # Contenido, no mtime: filecmp con shallow=False lee por chunks.
            if not filecmp.cmp(old / rel, new / rel, shallow=False):
                changes.append(FileChange(area=area, relative_path=rel, kind="modified"))
        return tuple(changes)

    def _compute_diff(self, clone: SandboxClone) -> tuple[FileChange, ...]:
        """Qué hizo el ritual: baseline (foto de clone-time) → copia mutada.

        Comparar contra el baseline y no contra el real vivo evita atribuirle
        al ritual los cambios que MO2/el usuario hicieron en la ventana de
        aprobación (review Codex PR #245).
        """
        return self._tree_changes("profile", clone.profile_baseline, clone.profile_copy) + self._tree_changes(
            "overwrite", clone.overwrite_baseline, clone.overwrite_copy
        )

    def _check_drift(self, clone: SandboxClone) -> None:
        """Corta si el real difiere del baseline (drift en la ventana de aprobación).

        El walk del árbol real también rechaza symlinks aparecidos después del
        clonado (destino inseguro para promote — review Codex PR #245).

        Raises:
            SandboxDriftError: Si el perfil/overwrite reales cambiaron.
            SandboxSymlinkError: Si apareció un symlink en el árbol real.
        """
        drift = self._tree_changes("profile", clone.profile_baseline, clone.profile_source) + self._tree_changes(
            "overwrite", clone.overwrite_baseline, clone.overwrite_source
        )
        if drift:
            detalle = "; ".join(f"{c.area}/{c.relative_path} ({c.kind})" for c in drift[:10])
            raise SandboxDriftError(
                f"El árbol real cambió desde el clonado ({len(drift)} cambio(s): {detalle}). "
                "Promover pisaría cambios vivos: reclonar y re-ejecutar el ritual."
            )

    def _apply_changes(self, clone: SandboxClone, changes: tuple[FileChange, ...]) -> tuple[int, int]:
        written = 0
        deleted = 0
        # removed se aplica ANTES que added/modified: destraba reemplazos
        # archivo→directorio y hace correcto el rename solo-de-mayúsculas en
        # filesystems case-insensitive (review Codex PR #245).
        ordered = sorted(changes, key=lambda c: 0 if c.kind == "removed" else 1)

        # Fase 0 — backup de todo target real afectado ANTES de mutar nada:
        # el tmp+os.replace protege cada archivo individual, no el conjunto.
        # Sin esto, un OSError en el cambio N dejaba los N-1 anteriores
        # aplicados (los removed van primero → borrados ya efectivos) y el
        # perfil MO2 quedaba mitad viejo/mitad nuevo. Un fallo en esta fase es
        # seguro: el árbol real sigue intacto. El nombre del directorio es
        # único por promote para no chocar al reintentar con el mismo clon.
        rollback_dir = clone.root / f"rollback-{uuid.uuid4().hex[:8]}"
        backups: dict[tuple[Area, str], pathlib.Path] = {}
        for change in ordered:
            target = self._real_target(clone, change)
            if target.is_file():
                backup = rollback_dir / change.area / change.relative_path
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, backup)
                backups[(change.area, change.relative_path)] = backup

        # Fase 1 — aplicar; cada cambio entra a `applied` solo tras completarse.
        applied: list[FileChange] = []
        try:
            for change in ordered:
                source, copy = (
                    (clone.profile_source, clone.profile_copy)
                    if change.area == "profile"
                    else (clone.overwrite_source, clone.overwrite_copy)
                )
                target = source / change.relative_path
                # Los árboles de mods suelen traer archivos read-only (Windows):
                # limpiar el bit de escritura antes de tocar el target para no
                # dejar la promoción a medias (review Copilot PR #245).
                _make_writable(target)
                if change.kind == "removed":
                    target.unlink(missing_ok=True)
                    deleted += 1
                else:  # added | modified
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if target.is_dir():
                        # Reemplazo directorio→archivo: tras las removals el dir
                        # solo conserva subdirectorios vacíos; despejarlo.
                        _rmtree_force(target)
                    # Escritura atómica: copiar a un tmp del MISMO directorio y
                    # os.replace — un fallo a mitad de copia no trunca el archivo
                    # real (mismo patrón que las escrituras de load order del repo;
                    # review Codex PR #245).
                    tmp = target.parent / f"{target.name}.{uuid.uuid4().hex[:8]}.skyclaw-tmp"
                    try:
                        shutil.copy2(copy / change.relative_path, tmp)
                        os.replace(tmp, target)
                    finally:
                        tmp.unlink(missing_ok=True)
                    written += 1
                applied.append(change)
        except Exception as original:
            try:
                self._rollback(clone, applied, backups)
            except Exception as rollback_exc:
                logger.exception("El rollback del promote también falló; backup preservado en %s", rollback_dir)
                raise SandboxRollbackError(
                    f"El promote falló ({original!r}) y el rollback también ({rollback_exc!r}): "
                    f"el perfil real puede haber quedado inconsistente. "
                    f"Backup para restauración manual en: {rollback_dir}"
                ) from original
            logger.warning(
                "El promote falló a mitad (%s); el perfil real fue restaurado al estado previo.",
                original,
            )
            raise
        # Éxito: el backup ya no hace falta (best-effort; el clon entero se
        # descarta después de todos modos).
        _rmtree_force(rollback_dir)
        return written, deleted

    @staticmethod
    def _real_target(clone: SandboxClone, change: FileChange) -> pathlib.Path:
        """Ruta del target de un cambio en el árbol REAL (profile u overwrite)."""
        source = clone.profile_source if change.area == "profile" else clone.overwrite_source
        return source / change.relative_path

    def _rollback(
        self,
        clone: SandboxClone,
        applied: list[FileChange],
        backups: dict[tuple[Area, str], pathlib.Path],
    ) -> None:
        """Revierte en orden inverso los cambios ya aplicados de un promote fallido.

        Deja el árbol real byte-exacto al estado previo al promote. Caveat: los
        directorios vacíos creados por el apply no se limpian ni se restauran
        (el diff trackea archivos — ``_relative_files`` ignora dirs vacíos —
        e inocuo para MO2).
        """
        for change in reversed(applied):
            target = self._real_target(clone, change)
            if change.kind == "added":
                _make_writable(target)
                target.unlink(missing_ok=True)
                continue
            backup = backups.get((change.area, change.relative_path))
            if backup is None:
                # Imposible por construcción (modified/removed implican target
                # preexistente ya respaldado en fase 0); fail-loud para que el
                # caller lo reporte como SandboxRollbackError.
                raise ProfileSandboxError(f"Sin backup para revertir {change.area}/{change.relative_path}")
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.is_dir():
                # Caso archivo→directorio: deshacer los added ya dejó el dir
                # vacío donde antes vivía el archivo borrado; despejarlo.
                _rmtree_force(target)
            _make_writable(target)
            if change.kind == "modified":
                # Mismo patrón atómico del apply: tmp en el MISMO directorio.
                tmp = target.parent / f"{target.name}.{uuid.uuid4().hex[:8]}.skyclaw-tmp"
                try:
                    shutil.copy2(backup, tmp)
                    os.replace(tmp, target)
                finally:
                    tmp.unlink(missing_ok=True)
            else:  # removed → reponer el archivo borrado
                shutil.copy2(backup, target)

    @staticmethod
    def _relative_files(root: pathlib.Path) -> set[str]:
        """Archivos bajo ``root`` como rutas relativas con ``/`` (set vacío si no existe).

        Raises:
            SandboxSymlinkError: Si el árbol contiene symlinks (fail-closed:
                diff/promote los leerían o escribirían a través de ellos).
        """
        if not root.is_dir():
            return set()
        files: set[str] = set()
        for p in root.rglob("*"):
            if p.is_symlink():
                raise SandboxSymlinkError(
                    f"Symlink detectado en el sandbox: {p}. No se sigue (podría apuntar fuera del árbol)."
                )
            if p.is_file():
                files.add(p.relative_to(root).as_posix())
        return files

    @classmethod
    def _reject_symlinks(cls, root: pathlib.Path) -> None:
        """Corta con :class:`SandboxSymlinkError` si hay symlinks bajo ``root``."""
        cls._relative_files(root)


def _make_writable(path: pathlib.Path) -> None:
    """Suma el bit de escritura a ``path`` si existe (no-op ante cualquier OSError)."""
    with contextlib.suppress(OSError):
        path.chmod(path.stat().st_mode | stat.S_IWRITE)


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
