"""Proveedor de estado de archivos para evaluar ``fileDependency`` de FOMOD.

Un ``fileDependency`` de ``ModuleConfig.xml`` condiciona una opción a que un
archivo (típicamente un plugin ``.esp``/``.esm``/``.esl``) esté ``Active``,
``Inactive`` o ``Missing`` en la instalación. El resolver FOMOD no tiene acceso
al filesystem: este módulo le da una vista del estado de MO2.

El estado se deriva de:
- ``modlist.txt`` del perfil (mods habilitados ``+`` / deshabilitados ``-``);
- el contenido de ``mods/<nombre>/`` (archivos sueltos; los BSA no se indexan).

El índice se construye una sola vez (lazy, al primer uso) y compara paths
case-insensitive (Windows). Una consulta sobre un archivo que no está en
ningún mod indexado responde ``Missing``.
"""

from __future__ import annotations

import logging
import pathlib
import threading
from typing import Protocol

from sky_claw.local.fomod.models import FileState

logger = logging.getLogger(__name__)

_DEFAULT_PROFILE = "Default"
_DATA_PREFIX = "data/"
_MODLIST_NAME = "modlist.txt"


class FileStateProvider(Protocol):
    """Vista del estado de un archivo en la instalación (relativo a ``Data/``)."""

    def file_state(self, path: str) -> FileState:
        """Estado del archivo *path*: Active, Inactive o Missing."""


def _normalize(path: str) -> str:
    """Normaliza un path relativo a Data para comparación case-insensitive."""
    normalized = path.replace("\\", "/").casefold()
    if normalized.startswith(_DATA_PREFIX):
        normalized = normalized[len(_DATA_PREFIX) :]
    return normalized


class MO2PluginStateProvider:
    """Estado de archivos derivado de una instancia portable de MO2."""

    def __init__(self, mo2_root: pathlib.Path, profile: str = _DEFAULT_PROFILE) -> None:
        self._mo2_root = mo2_root
        self._profile = profile
        self._active_files: frozenset[str] = frozenset()
        self._inactive_files: frozenset[str] = frozenset()
        self._indexed = False
        # El índice se construye una sola vez; el lock protege el doble-chequeo
        # cuando la resolución corre en un hilo (asyncio.to_thread).
        self._index_lock = threading.Lock()

    def file_state(self, path: str) -> FileState:
        """Estado del archivo *path* (relativo a ``Data/``)."""
        if not self._indexed:
            with self._index_lock:
                if not self._indexed:
                    self._build_index()
        key = _normalize(path)
        if key in self._active_files:
            return FileState.ACTIVE
        if key in self._inactive_files:
            return FileState.INACTIVE
        return FileState.MISSING

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _build_index(self) -> None:
        """Indexa ``mods/<nombre>/`` en dos conjuntos: habilitados/deshabilitados."""
        active: set[str] = set()
        inactive: set[str] = set()
        enabled_mods, disabled_mods = self._read_modlist()
        self._index_mods(enabled_mods, active)
        self._index_mods(disabled_mods, inactive)
        self._active_files = frozenset(active)
        self._inactive_files = frozenset(inactive)
        self._indexed = True

    def _read_modlist(self) -> tuple[set[str], set[str]]:
        """Lee ``profiles/<perfil>/modlist.txt`` → (habilitados, deshabilitados).

        Formato MO2: una línea por mod, prefijo ``+`` (activo) o ``-``
        (inactivo); los separadores ``==`` y los mods "overwrite" se ignoran.
        Los nombres pueden contener espacios: se toma la línea completa tras
        el prefijo.
        """
        enabled: set[str] = set()
        disabled: set[str] = set()
        modlist_path = self._mo2_root / "profiles" / self._profile / _MODLIST_NAME
        try:
            lines = modlist_path.read_text(encoding="utf-8-sig").splitlines()
        except OSError as exc:
            logger.warning("modlist.txt ilegible (%s) — estado tratado como vacío: %s", modlist_path, exc)
            return enabled, disabled
        for raw_line in lines:
            line = raw_line.strip()
            if not line or line.startswith("=="):
                continue
            prefix = line[0]
            if prefix not in ("+", "-"):
                continue
            name = line[1:].strip()
            if not name:
                continue
            (enabled if prefix == "+" else disabled).add(name)
        return enabled, disabled

    def _index_mods(self, mod_names: set[str], target: set[str]) -> None:
        """Agrega los paths relativos de cada mod a *target*."""
        for mod_name in mod_names:
            mod_dir = self._mo2_root / "mods" / mod_name
            if not mod_dir.is_dir():
                continue
            for file_path in mod_dir.rglob("*"):
                if file_path.is_file():
                    rel = file_path.relative_to(mod_dir)
                    target.add(_normalize(rel.as_posix()))
