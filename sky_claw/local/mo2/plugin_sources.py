"""Resolución de fuentes de plugins para los sensores del preflight (T-30w).

Traduce el entorno en las dos entradas que necesitan los sensores de masters
(T-30·1) y de límites full/light (T-30·2):

* ``plugin_dirs`` — dónde viven los archivos de plugin: cada carpeta de mod de
  MO2 (``<mo2>/mods/<mod>/``, los plugins van en su raíz) más la ``Data`` del
  juego (masters base: Skyrim.esm/Update.esm…). Los sensores no recorren
  recursivo, por eso se enumeran las carpetas de mods una a una.
* ``enabled_plugins`` — la lista de plugins habilitados del load order:
  de ``plugins.txt`` solo las líneas activas (marca ``*``); de ``loadorder.txt``
  todas. Formato viejo (listar == activar) cubierto como fallback.

Función pura y best-effort (un entorno a medio configurar produce fuentes
vacías, nunca una excepción) para que el cableado en
``LootSortingService._ensure_preflight`` sea trivial y esto sea testeable con
un fixture MO2 en tmp.
"""

from __future__ import annotations

import logging
import pathlib
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PluginSources:
    """Directorios de plugins + load order habilitado para el preflight."""

    plugin_dirs: tuple[pathlib.Path, ...]
    enabled_plugins: tuple[str, ...]


def resolve_plugin_sources(
    *,
    game_data_dir: pathlib.Path | None,
    mo2_mods_dir: pathlib.Path | None,
    load_order_file: pathlib.Path | None,
) -> PluginSources:
    """Arma las fuentes de plugins desde el entorno (best-effort).

    Args:
        game_data_dir: ``Data`` del juego (masters base). ``None`` si no se sabe.
        mo2_mods_dir: ``<mo2>/mods``; se enumeran sus subcarpetas. ``None`` si no
            hay instancia MO2.
        load_order_file: ``plugins.txt``/``loadorder.txt`` del que salen los
            plugins habilitados. ``None`` si no se resolvió ninguno.

    Returns:
        :class:`PluginSources` (tuplas vacías ante fuentes ausentes/ilegibles).
    """
    return PluginSources(
        plugin_dirs=_resolve_plugin_dirs(game_data_dir, mo2_mods_dir),
        enabled_plugins=_parse_enabled(load_order_file),
    )


def _resolve_plugin_dirs(
    game_data_dir: pathlib.Path | None,
    mo2_mods_dir: pathlib.Path | None,
) -> tuple[pathlib.Path, ...]:
    dirs: list[pathlib.Path] = []
    if mo2_mods_dir is not None:
        try:
            entries = sorted(mo2_mods_dir.iterdir())
        except OSError as exc:
            logger.debug("No se pudo enumerar %s: %s", mo2_mods_dir, exc)
            entries = []
        for entry in entries:
            try:
                if entry.is_dir():
                    dirs.append(entry)
            except OSError as exc:
                logger.debug("No se pudo inspeccionar %s: %s", entry, exc)
    if game_data_dir is not None:
        try:
            is_data_dir = game_data_dir.is_dir()
        except OSError:
            is_data_dir = False
        if is_data_dir:
            dirs.append(game_data_dir)
    return tuple(dirs)


def _parse_enabled(load_order_file: pathlib.Path | None) -> tuple[str, ...]:
    if load_order_file is None:
        return ()
    try:
        # utf-8-sig: MO2 escribe plugins.txt con BOM.
        text = load_order_file.read_text(encoding="utf-8-sig")
    except (OSError, ValueError) as exc:
        logger.debug("No se pudo leer el load order %s: %s", load_order_file, exc)
        return ()

    entries = [line.strip() for line in text.splitlines()]
    entries = [line for line in entries if line and not line.startswith("#")]

    if load_order_file.name.lower() == "plugins.txt":
        # Formato moderno: los activos llevan `*`. Si ninguno lo trae (formato
        # viejo donde listar == activar), se cae a considerarlos todos.
        starred = [line[1:].strip() for line in entries if line.startswith("*")]
        if starred:
            return tuple(starred)
    # loadorder.txt (orden completo, sin marca) o plugins.txt viejo.
    return tuple(line.lstrip("*").strip() for line in entries)
