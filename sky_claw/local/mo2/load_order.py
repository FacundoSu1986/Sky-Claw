"""Resolución de los archivos de load order para snapshot/rollback (T-05).

LOOT corre como subproceso con ``--game-path``, FUERA del VFS de MO2, así que
reescribe ``plugins.txt``/``loadorder.txt`` en ``%LOCALAPPDATA%\\<juego>`` —
no en el profile de MO2. Cuál de los dos existe (y bajo qué nombre de carpeta:
Steam, GOG, Epic, MS Store) depende del entorno; ese era el motivo del
deferral documentado en ``loot_service`` (``target_files=[]``).

Este resolver devuelve la UNIÓN de todos los candidatos existentes. Restaurar
un archivo que la herramienta no tocó es un no-op (mismo contenido), así que
sobre-cubrir es seguro; sub-cubrir deja al usuario sin rollback justo en el
archivo que LOOT mutó.
"""

from __future__ import annotations

import logging
import os
import pathlib
from dataclasses import dataclass

from sky_claw.antigravity.security.path_validator import assert_safe_component

logger = logging.getLogger(__name__)

#: Nombres de carpeta del juego bajo LOCALAPPDATA según la tienda. libloot usa
#: la carpeta correspondiente a la edición instalada; cubrimos todas las
#: variantes conocidas porque el resolver no sabe cuál edición corre el usuario.
_LOCALAPPDATA_GAME_DIRS: tuple[str, ...] = (
    "Skyrim Special Edition",
    "Skyrim Special Edition GOG",
    "Skyrim Special Edition EPIC",
    "Skyrim Special Edition MS",
)

#: Archivos de load order que LOOT (y el juego) reescriben.
_LOAD_ORDER_FILENAMES: tuple[str, ...] = ("plugins.txt", "loadorder.txt")


@dataclass(frozen=True, slots=True)
class LoadOrderPaths:
    """Candidatos de load order existentes y el origen de cada grupo.

    Attributes:
        files: Rutas absolutas existentes, sin duplicados, en orden estable.
        sources: Orígenes que aportaron al menos un archivo
            (``"override"``, ``"localappdata"``, ``"mo2_profile"``).
    """

    files: tuple[pathlib.Path, ...]
    sources: tuple[str, ...]


class LoadOrderFileResolver:
    """Resuelve los archivos de load order que una herramienta externa puede mutar.

    Args:
        explicit_dir: Directorio configurado explícitamente por el usuario que
            contiene ``plugins.txt``/``loadorder.txt`` (prioridad de origen
            ``"override"``).
        local_app_data: Base tipo ``%LOCALAPPDATA%``. Por defecto se toma de la
            variable de entorno ``LOCALAPPDATA`` (ausente en POSIX/CI: se omite
            el origen sin fallar).
        mo2_root: Raíz de la instancia portable de MO2 (cubre LOOT-vía-VFS).
        profile: Nombre del profile de MO2; se valida contra path traversal.
    """

    def __init__(
        self,
        *,
        explicit_dir: pathlib.Path | None = None,
        local_app_data: pathlib.Path | None = None,
        mo2_root: pathlib.Path | None = None,
        profile: str = "Default",
    ) -> None:
        if mo2_root is not None:
            assert_safe_component(profile, field="profile")
        self._explicit_dir = explicit_dir
        self._local_app_data = local_app_data
        self._mo2_root = mo2_root
        self._profile = profile

    def resolve(self) -> LoadOrderPaths:
        """Devuelve la unión de archivos de load order existentes por origen."""
        files: list[pathlib.Path] = []
        sources: list[str] = []
        seen: set[pathlib.Path] = set()

        for source, directory in self._candidate_dirs():
            found = [
                candidate
                for name in _LOAD_ORDER_FILENAMES
                if (candidate := (directory / name).resolve()) not in seen and candidate.is_file()
            ]
            if found:
                files.extend(found)
                seen.update(found)
                if source not in sources:
                    sources.append(source)

        if not files:
            logger.warning(
                "No se encontró ningún plugins.txt/loadorder.txt (override=%s, localappdata=%s, mo2=%s): "
                "el snapshot de load order quedará vacío.",
                self._explicit_dir,
                self._local_app_data or os.environ.get("LOCALAPPDATA"),
                self._mo2_root,
            )
        return LoadOrderPaths(files=tuple(files), sources=tuple(sources))

    def _candidate_dirs(self) -> list[tuple[str, pathlib.Path]]:
        """Directorios candidatos en orden estable: override, LOCALAPPDATA, MO2."""
        candidates: list[tuple[str, pathlib.Path]] = []

        if self._explicit_dir is not None:
            candidates.append(("override", self._explicit_dir))

        local_app_data = self._local_app_data
        if local_app_data is None:
            env_value = os.environ.get("LOCALAPPDATA")
            local_app_data = pathlib.Path(env_value) if env_value else None
        if local_app_data is not None:
            candidates.extend(("localappdata", local_app_data / game_dir) for game_dir in _LOCALAPPDATA_GAME_DIRS)

        if self._mo2_root is not None:
            candidates.append(("mo2_profile", self._mo2_root / "profiles" / self._profile))

        return candidates
