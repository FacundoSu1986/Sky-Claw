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


def _mtime_ns(path: pathlib.Path) -> int | None:
    """mtime en nanosegundos de *path*, o None si no existe/ilegible."""
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return None


class MO2PluginStateProvider:
    """Estado de archivos derivado de una instancia portable de MO2."""

    def __init__(self, mo2_root: pathlib.Path, profile: str = _DEFAULT_PROFILE) -> None:
        self._mo2_root = mo2_root
        self._profile = profile
        self._active_files: frozenset[str] = frozenset()
        self._inactive_files: frozenset[str] = frozenset()
        self._indexed = False
        # Snapshot del CONTENIDO de modlist.txt: la fuente de verdad de
        # activación. Comparar contenido (no solo mtime) hace la invalidación
        # determinista — el mtime de archivos/directorios puede no refrescar
        # inmediatamente en NTFS (caché de metadatos, visto en CI de Windows).
        self._modlist_snapshot: str | None = None
        self._mods_root_mtime: int | None = None
        # Cache por mod: paths ya escaneados y mtime del directorio. Permite
        # reindexar SOLO los mods afectados (nuevos o mutados) en vez de
        # recorrer todo mods/ en cada cambio.
        self._mod_paths: dict[str, frozenset[str]] = {}
        self._mod_mtimes: dict[str, int | None] = {}
        # El índice se construye una sola vez; el lock protege el doble-chequeo
        # cuando la resolución corre en un hilo (asyncio.to_thread).
        self._index_lock = threading.Lock()

    def file_state(self, path: str) -> FileState:
        """Estado del archivo *path* (relativo a ``Data/``)."""
        with self._index_lock:
            if self._estado_cambio():
                self._rebuild()
            key = _normalize(path)
            if key in self._active_files:
                return FileState.ACTIVE
            if key in self._inactive_files:
                return FileState.INACTIVE
            return FileState.MISSING

    def _estado_cambio(self) -> bool:
        """Devuelve True si el estado indexado cambió desde el último índice.

        El modlist se compara por CONTENIDO (la activación es la fuente de
        verdad; inmune a mtimes no refrescados). También se comparan los mtimes
        de cada mod conocido para detectar altas, bajas y mutaciones internas.
        """
        if not self._indexed:
            return True
        if self._modlist_snapshot != self._leer_modlist_crudo():
            return True
        if self._mods_root_mtime != _mtime_ns(self._mo2_root / "mods"):
            return True
        enabled_mods, disabled_mods = self._read_modlist()
        conocidos = enabled_mods | disabled_mods
        if conocidos != set(self._mod_mtimes):
            return True
        return any(self._mod_mtimes[mod_name] != _mtime_ns(self._mod_dir(mod_name)) for mod_name in conocidos)

    def _leer_modlist_crudo(self) -> str:
        """Contenido crudo de modlist.txt ("" si ilegible/ausente)."""
        try:
            return (self._mo2_root / "profiles" / self._profile / _MODLIST_NAME).read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError):
            return ""

    # ------------------------------------------------------------------
    # Implementación interna
    # ------------------------------------------------------------------

    def _rebuild(self) -> None:
        """Reconstruye los conjuntos activo/inactivo.

        Escanea SOLO los mods cuyo directorio cambió (nuevos o mutados) y
        reutiliza el cache de paths para el resto: activar/desactivar un mod
        re-clasifica sin recorrer archivos.
        """
        enabled_mods, disabled_mods = self._read_modlist()
        conocidos = enabled_mods | disabled_mods
        for mod_name in conocidos:
            mod_dir = self._mod_dir(mod_name)
            mtime = _mtime_ns(mod_dir)
            if mtime is None:
                self._mod_paths.pop(mod_name, None)
                self._mod_mtimes[mod_name] = None
                continue
            if mod_name not in self._mod_mtimes or self._mod_mtimes[mod_name] != mtime:
                self._mod_paths[mod_name] = self._scan_mod(mod_name)
                self._mod_mtimes[mod_name] = mtime
        # Mods removidos del modlist: fuera del estado de MO2.
        for mod_name in [n for n in self._mod_mtimes if n not in conocidos]:
            self._mod_mtimes.pop(mod_name, None)
            self._mod_paths.pop(mod_name, None)

        active: set[str] = set()
        inactive: set[str] = set()
        for mod_name in enabled_mods:
            active.update(self._mod_paths.get(mod_name, ()))
        for mod_name in disabled_mods:
            inactive.update(self._mod_paths.get(mod_name, ()))
        self._active_files = frozenset(active)
        self._inactive_files = frozenset(inactive)
        self._modlist_snapshot = self._leer_modlist_crudo()
        self._mods_root_mtime = _mtime_ns(self._mo2_root / "mods")
        self._indexed = True

    def _mod_dir(self, mod_name: str) -> pathlib.Path:
        return self._mo2_root / "mods" / mod_name

    def _scan_mod(self, mod_name: str) -> frozenset[str]:
        """Paths relativos (normalizados) de un solo mod."""
        mod_dir = self._mod_dir(mod_name)
        paths: set[str] = set()
        if not mod_dir.is_dir():
            return frozenset()
        for file_path in mod_dir.rglob("*"):
            if file_path.is_file():
                rel = file_path.relative_to(mod_dir)
                paths.add(_normalize(rel.as_posix()))
        return frozenset(paths)

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
        except (OSError, UnicodeDecodeError) as exc:
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
