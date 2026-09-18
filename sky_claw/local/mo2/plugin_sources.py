"""Resolución de fuentes de plugins y load order para el preflight (T-30w).

Traduce el entorno en el **snapshot** que necesitan los sensores de masters
(T-30·1), de orden (T-31) y de límites full/light (T-30·2):

* ``plugin_dirs`` — dónde viven los archivos de plugin: cada carpeta de mod de
  MO2 (``<mo2>/mods/<mod>/``, los plugins van en su raíz) más la ``Data`` del
  juego (masters base: Skyrim.esm/Update.esm…). Los sensores no recorren
  recursivo, por eso se enumeran las carpetas de mods una a una.
* ``explicit_enabled_plugins`` — lo que MO2 activa explícitamente en
  ``plugins.txt``: solo las líneas activas (marca ``*``), con el fallback
  histórico "listar == activar" cuando ninguna línea lleva ``*``.
* ``implicit_official_masters`` — masters oficiales del juego
  (:data:`OFFICIAL_MASTERS`) presentes en la ``Data`` del juego: el motor los
  carga siempre y MO2 no los marca en ``plugins.txt``. La disponibilidad
  física manda (y solo ``Data`` es la autoridad): un oficial ausente del disco
  no se transforma en presente, y una copia dentro de un mod deshabilitado no
  lo vuelve implícito.
* ``ordered_plugins`` — el orden conocido del perfil (``loadorder.txt``, o el
  orden de líneas de ``plugins.txt`` si aquel no existe). Estar listado ahí no
  implica estar habilitado.
* ``effective_enabled_plugins`` — la unión semántica de
  ``explicit_enabled_plugins`` + ``implicit_official_masters``, deduplicada
  case-insensitive y ordenada de forma determinista. Es el universo sobre el
  que razonan los tres sensores del preflight.

Modelar los conceptos por separado es el fix de #585: el modelo viejo
colapsaba "habilitado explícitamente por MO2" con "carga en el load order
efectivo", así que un master oficial instalado y ausente de ``plugins.txt``
terminaba reportado como ``disabled``.

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

__all__ = ["OFFICIAL_MASTERS", "PluginSources", "resolve_plugin_sources"]

#: Masters oficiales de Skyrim SE/AE que el motor carga implícitamente cuando
#: el archivo está presente en la ``Data`` del juego. NO incluye Creation Club
#: (``cc*``): su activación depende de fuentes explícitas y un prefijo no
#: alcanza como evidencia (política congelada por test).
OFFICIAL_MASTERS: tuple[str, ...] = (
    "Skyrim.esm",
    "Update.esm",
    "Dawnguard.esm",
    "HearthFires.esm",
    "Dragonborn.esm",
)


@dataclass(frozen=True, slots=True)
class PluginSources:
    """Snapshot del load order del perfil para los sensores del preflight.

    Attributes:
        plugin_dirs: Directorios donde viven los plugins, en orden de
            precedencia del VFS (overwrite → mods → Data).
        explicit_enabled_plugins: Activación explícita de MO2 (``plugins.txt``;
            ``loadorder.txt`` como fallback histórico "listar == activar").
        implicit_official_masters: Masters oficiales presentes en la ``Data``
            del juego que el motor carga sin que ``plugins.txt`` los marque.
        ordered_plugins: Orden conocido del perfil (``loadorder.txt``); vacío
            sin archivos de load order. Orden no es habilitación.
    """

    plugin_dirs: tuple[pathlib.Path, ...]
    explicit_enabled_plugins: tuple[str, ...]
    implicit_official_masters: tuple[str, ...] = ()
    ordered_plugins: tuple[str, ...] = ()

    @property
    def effective_enabled_plugins(self) -> tuple[str, ...]:
        """Explícitos + oficiales implícitos, deduplicados y ordenados.

        Orden determinista: primero los oficiales implícitos en orden canónico
        (el motor los carga siempre antes que todo, sin importar dónde los
        liste el perfil); después ``ordered_plugins`` filtrado al conjunto
        efectivo (el orden del perfil manda para el resto); al final lo
        explícito que no aparezca en ninguna de las dos fuentes, en orden de
        activación. La deduplicación es case-insensitive (semántica Windows) y
        conserva la primera grafía vista.
        """
        efectivos = {nombre.casefold() for nombre in self.explicit_enabled_plugins}
        efectivos.update(nombre.casefold() for nombre in self.implicit_official_masters)

        resultado: list[str] = []
        vistos: set[str] = set()

        # 1. Oficiales implícitos: el motor los carga primero, en orden canónico.
        for nombre in self.implicit_official_masters:
            clave = nombre.casefold()
            if clave in efectivos and clave not in vistos:
                vistos.add(clave)
                resultado.append(nombre)
        # 2. Orden del perfil, filtrado al conjunto efectivo.
        for nombre in self.ordered_plugins:
            clave = nombre.casefold()
            if clave in efectivos and clave not in vistos:
                vistos.add(clave)
                resultado.append(nombre)
        # 3. Explícitos que el orden del perfil no listaba.
        for nombre in self.explicit_enabled_plugins:
            clave = nombre.casefold()
            if clave in efectivos and clave not in vistos:
                vistos.add(clave)
                resultado.append(nombre)
        return tuple(resultado)


def resolve_plugin_sources(
    *,
    game_data_dir: pathlib.Path | None,
    mo2_mods_dir: pathlib.Path | None,
    mo2_overwrite_dir: pathlib.Path | None = None,
    plugins_file: pathlib.Path | None = None,
    order_file: pathlib.Path | None = None,
) -> PluginSources:
    """Arma el snapshot de plugins/load order desde el entorno (best-effort).

    Los dos archivos aportan información distinta y se leen por separado:
    ``plugins.txt`` da la **activación** explícita y ``loadorder.txt`` el
    **orden** del perfil. Preferir uno y descartar el otro pierde semántica
    (bug de #585).

    Args:
        game_data_dir: ``Data`` del juego (masters base). ``None`` si no se sabe.
        mo2_mods_dir: ``<mo2>/mods``; se enumeran sus subcarpetas. ``None`` si no
            hay instancia MO2.
        mo2_overwrite_dir: ``<mo2>/overwrite``, donde caen los plugins generados
            (bashed patch, DynDOLOD…). Máxima precedencia en el VFS de MO2.
        plugins_file: ``plugins.txt`` del perfil (activación con ``*``). ``None``
            si no existe; en ese caso ``order_file`` cae al fallback histórico
            "listar == activar".
        order_file: ``loadorder.txt`` del perfil (orden). ``None`` si no existe;
            el orden queda vacío y el efectivo se deriva con oficiales primero.

    Returns:
        :class:`PluginSources` (tuplas vacías ante fuentes ausentes/ilegibles).
    """
    plugin_dirs = _resolve_plugin_dirs(game_data_dir, mo2_mods_dir, mo2_overwrite_dir)
    explicit = _parse_activation(plugins_file) if plugins_file is not None else _parse_activation(order_file)
    ordered = _parse_order(order_file) if order_file is not None else _parse_order(plugins_file)
    return PluginSources(
        plugin_dirs=plugin_dirs,
        explicit_enabled_plugins=explicit,
        implicit_official_masters=_implicit_official_masters(game_data_dir),
        ordered_plugins=ordered,
    )


def _resolve_plugin_dirs(
    game_data_dir: pathlib.Path | None,
    mo2_mods_dir: pathlib.Path | None,
    mo2_overwrite_dir: pathlib.Path | None,
) -> tuple[pathlib.Path, ...]:
    # Orden = precedencia del VFS de MO2 (los checkers hacen first-match):
    # overwrite gana sobre los mods, y los mods sobre la Data base. La
    # ordenación por prioridad de modlist.txt entre mods es una mejora futura;
    # acá el conjunto activo lo determina plugins.txt, no la enumeración.
    dirs: list[pathlib.Path] = []
    if _is_dir(mo2_overwrite_dir):
        assert mo2_overwrite_dir is not None
        dirs.append(mo2_overwrite_dir)
    if mo2_mods_dir is not None:
        try:
            entries = sorted(mo2_mods_dir.iterdir())
        except OSError as exc:
            logger.debug("No se pudo enumerar %s: %s", mo2_mods_dir, exc)
            entries = []
        for entry in entries:
            if _is_dir(entry):
                dirs.append(entry)
    if _is_dir(game_data_dir):
        assert game_data_dir is not None
        dirs.append(game_data_dir)
    return tuple(dirs)


def _implicit_official_masters(game_data_dir: pathlib.Path | None) -> tuple[str, ...]:
    """Oficiales de :data:`OFFICIAL_MASTERS` presentes en la ``Data`` del juego.

    Solo ``Data`` es la autoridad: el motor carga esos masters desde ahí, no
    desde ``mods/``. Buscar en todas las carpetas de mods (incluidas las
    deshabilitadas) convertiría un oficial ausente en un falso activo y además
    recorrería árboles que el caller todavía no validó (reviews del PR #595).
    El matching es case-insensitive (semántica Windows) y se devuelve la grafía
    canónica, en orden oficial.
    """
    if game_data_dir is None:
        return ()
    try:
        if not game_data_dir.is_dir():
            return ()
        entries = sorted(game_data_dir.iterdir())
    except OSError as exc:
        logger.debug("No se pudo inspeccionar %s: %s", game_data_dir, exc)
        return ()

    buscados = {nombre.casefold() for nombre in OFFICIAL_MASTERS}
    presentes: set[str] = set()
    for entry in entries:
        clave = entry.name.casefold()
        if clave not in buscados:
            continue
        try:
            if entry.is_file():
                presentes.add(clave)
        except OSError as exc:
            logger.debug("No se pudo inspeccionar %s: %s", entry, exc)
    return tuple(nombre for nombre in OFFICIAL_MASTERS if nombre.casefold() in presentes)


def _is_dir(path: pathlib.Path | None) -> bool:
    if path is None:
        return False
    try:
        return path.is_dir()
    except OSError as exc:
        logger.debug("No se pudo inspeccionar %s: %s", path, exc)
        return False


def _read_entries(path: pathlib.Path | None) -> tuple[str, ...]:
    """Líneas útiles del archivo (sin vacías ni comentarios); vacío si ilegible."""
    if path is None:
        return ()
    try:
        # utf-8-sig: MO2 escribe plugins.txt con BOM. errors="replace": un byte
        # suelto no debe tirar la decodificación y borrar el load order entero
        # (best-effort real; precedente en chain_preview_service — review #252).
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as exc:
        logger.debug("No se pudo leer el load order %s: %s", path, exc)
        return ()

    entries = [line.strip() for line in text.splitlines()]
    return tuple(line for line in entries if line and not line.startswith("#"))


def _parse_activation(path: pathlib.Path | None) -> tuple[str, ...]:
    """Plugins habilitados explícitamente según ``plugins.txt``/``loadorder.txt``.

    En ``plugins.txt`` moderno solo las líneas con ``*`` están activas; si
    ninguna lo trae (formato viejo donde listar == activar), se caen a
    considerarlas todas. Para ``loadorder.txt`` (o cualquier otro nombre),
    listar == activar es el fallback histórico.
    """
    entries = _read_entries(path)
    if not entries:
        return ()
    if path is not None and path.name.lower() == "plugins.txt":
        starred = tuple(line[1:].strip() for line in entries if line.startswith("*"))
        if starred:
            return starred
    return tuple(line.lstrip("*").strip() for line in entries)


def _parse_order(path: pathlib.Path | None) -> tuple[str, ...]:
    """Orden del perfil: todas las líneas, sin ``*`` y en el orden del archivo."""
    return tuple(line.lstrip("*").strip() for line in _read_entries(path))
