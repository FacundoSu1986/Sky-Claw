"""¿Qué archivo gana cada ruta virtual en el overlay de un perfil de Mod Organizer 2?

El generador nativo de parallax lee ``textures/.../x_n.dds`` y escribe ``x_p.dds``. Medir sobre el archivo de un mod que otro tapa
produce un mapa para una textura que el juego no va a ver; y no saber que el mod de control está por encima de nuestra salida
fue lo que enmascaró el producto en el rig real (#792 H12). Este módulo contesta **una sola vez** la pregunta de la que dependen
las dos: *dada una ruta virtual, ¿qué capa del perfil la provee?*

Reusa, sin reescribirlo, lo que el repo ya tiene para esto:

- ``vfs_attestation.read_enabled_mods``: la ÚNICA lectura del ``modlist.txt``. Devuelve los mods habilitados en orden de
  prioridad CRECIENTE (el último gana); este módulo recibe esa tupla y no parsea nada;
- ``links.iter_archivos_propios``: el recorrido compartido de un árbol, que no entra a symlinks ni a junctions;
- el criterio de ``mod_effectivity`` y de ``vfs_attestation``: ``overwrite`` gana a todo mod, y un ``*.mohidden`` no está en el
  overlay.

Falla cerrado: un mod habilitado sin carpeta, un nombre repetido o un árbol que pasa el tope no se resuelven "a ojo".

**Lo que NO hace:** no escribe, no elige entre capas con criterio propio, no considera el game data físico (un mod siempre lo
gana) y no prueba que USVFS mapee lo que acá se resuelve (eso lo aporta el canary del broker). Es una vista ESTRUCTURAL.
"""

from __future__ import annotations

import pathlib
import stat
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from sky_claw.app.security.links import (
    iter_archivos_propios,
    link_kind_and_identity_or_raise,
    reject_unclassified_reparse_point,
)

__all__ = [
    "TOPE_DE_ARCHIVOS_POR_DEFECTO",
    "GanadorDeRuta",
    "GanadoresDelPerfil",
    "GanadoresError",
    "indexar_ganadores",
]

#: Tope de archivos visitados entre TODAS las capas. Un perfil real de texturas pesa cientos de miles; pasarlo es un árbol que no
#: se debe resolver a medias (falla en vez de devolver un índice incompleto que parezca completo).
TOPE_DE_ARCHIVOS_POR_DEFECTO: Final[int] = 2_000_000

#: Etiqueta de la capa ``overwrite`` (un mod no puede llamarse así sin que MO2 lo confunda con la carpeta especial).
CAPA_OVERWRITE: Final = "overwrite"

_SUFIJO_OCULTO: Final = ".mohidden"


class GanadoresError(Exception):
    """El perfil no permite afirmar quién gana una ruta (perfil corrupto, ambiguo o demasiado grande)."""


@dataclass(frozen=True, slots=True)
class GanadorDeRuta:
    """La capa que provee una ruta virtual y el archivo físico que la entrega."""

    ruta_virtual: str  #: Data-relativa, con ``/`` y con las mayúsculas de la capa ganadora.
    capa: str  #: Nombre del mod, o ``overwrite``.
    archivo: pathlib.Path


def _clave(ruta: str) -> tuple[str, str]:
    """``(clave sin mayúsculas, ruta normalizada)`` de una ruta virtual; rechaza lo que sale del árbol o está mal formado."""
    normalizada = ruta.replace("\\", "/")
    partes = normalizada.split("/")
    if not normalizada or normalizada.startswith("/") or any(p in ("", ".", "..") for p in partes) or ":" in partes[0]:
        raise GanadoresError(f"ruta virtual inválida: {ruta!r}")
    return normalizada.lower(), normalizada


class GanadoresDelPerfil:
    """El índice resuelto: para cada ruta virtual, la capa de mayor prioridad que la provee."""

    def __init__(self, ganadores: dict[str, GanadorDeRuta]) -> None:
        self._ganadores = ganadores

    def __len__(self) -> int:
        return len(self._ganadores)

    def ganador(self, ruta_virtual: str) -> GanadorDeRuta | None:
        """El ganador de *ruta_virtual*, o ``None`` si ninguna capa la provee."""
        clave, _ = _clave(ruta_virtual)
        return self._ganadores.get(clave)

    def con_sufijo(self, sufijo: str) -> tuple[GanadorDeRuta, ...]:
        """Los ganadores cuya ruta termina en *sufijo* (sin distinguir mayúsculas), ordenados por ruta."""
        buscado = sufijo.lower()
        return tuple(self._ganadores[clave] for clave in sorted(self._ganadores) if clave.endswith(buscado))


def _exigir_tope(valor: object) -> int:
    if isinstance(valor, bool) or not isinstance(valor, int) or valor < 1:
        raise GanadoresError(f"el tope de archivos debe ser un entero positivo, no {valor!r}")
    return valor


def _exigir_nombre_de_mod(nombre: str) -> None:
    """Un mod es UNA carpeta de ``mods_dir``: ``read_enabled_mods`` valida la sintaxis de la línea, no que el nombre no se escape."""
    if not nombre or nombre in (".", "..") or any(c in nombre for c in "/\\:\x00"):
        raise GanadoresError(f"nombre de mod inválido: {nombre!r} (debe ser una sola carpeta de mods_dir)")


def _exigir_prefijo(prefijo: str) -> tuple[str, ...]:
    """Los componentes de un prefijo Data relativo y contenido en la capa (``textures``, ``textures/architecture``)."""
    partes = tuple(prefijo.replace("\\", "/").split("/"))
    if not prefijo or any(p in ("", ".", "..") for p in partes) or ":" in partes[0] or "\x00" in prefijo:
        raise GanadoresError(f"prefijo inválido: {prefijo!r} (debe ser relativo y quedar dentro de la capa)")
    return partes


def _exigir_enabled(enabled: Sequence[str]) -> tuple[str, ...]:
    vistos: set[str] = set()
    for nombre in enabled:
        _exigir_nombre_de_mod(nombre)
        if nombre.lower() in vistos:
            raise GanadoresError(
                f"el modlist repite el mod {nombre!r}: la prioridad efectiva es ambigua y no se puede afirmar"
            )
        vistos.add(nombre.lower())
    return tuple(enabled)


def _es_carpeta_propia(raiz: pathlib.Path, *, etiqueta: str) -> bool:
    """``True`` si *raiz* es una carpeta propia; ``False`` si no existe; ``GanadoresError`` si es un enlace o no es carpeta.

    ``is_dir()`` sigue enlaces: sin esto, un mod que es un symlink o un junction hace que el recorrido arranque DENTRO del árbol
    ajeno (``iter_archivos_propios`` solo mira lo que cuelga de la ruta que se le da) e indexe archivos que el overlay no entrega.
    """
    try:
        tipo, identidad = link_kind_and_identity_or_raise(raiz)
        if identidad is not None and tipo is None:
            reject_unclassified_reparse_point(raiz, identidad)
    except OSError as exc:
        raise GanadoresError(f"no se pudo inspeccionar {etiqueta}: {exc}") from exc
    if identidad is None:
        return False
    if tipo is not None:
        raise GanadoresError(f"{etiqueta} es un enlace ({tipo}): no se recorre")
    if not stat.S_ISDIR(identidad.st_mode):
        raise GanadoresError(f"{etiqueta} no es una carpeta")
    return True


def indexar_ganadores(
    *,
    mods_dir: pathlib.Path,
    data_root: pathlib.Path,
    enabled: Sequence[str],
    prefijo: str = "textures",
    maximo_de_archivos: int = TOPE_DE_ARCHIVOS_POR_DEFECTO,
) -> GanadoresDelPerfil:
    """Resuelve el ganador de cada ruta bajo ``<capa>/<prefijo>`` para los mods *enabled* y ``overwrite``.

    Args:
        mods_dir: ``<raíz MO2>/mods``.
        data_root: raíz de datos MO2 (donde vive ``overwrite``).
        enabled: mods habilitados en orden de prioridad CRECIENTE (``vfs_attestation.read_enabled_mods``).
        prefijo: subárbol Data a indexar (por defecto ``textures``).
        maximo_de_archivos: tope de archivos visitados entre todas las capas.

    Raises:
        GanadoresError: mod habilitado sin carpeta, nombre repetido, tope inválido o superado, o árbol ilegible.
    """
    tope = _exigir_tope(maximo_de_archivos)
    partes_del_prefijo = _exigir_prefijo(prefijo)
    for etiqueta, raiz in (("mods_dir", mods_dir), ("data_root", data_root)):
        if not raiz.is_dir():
            raise GanadoresError(f"{etiqueta} no existe o no es una carpeta: {raiz}: no es la instancia que se cree")
    mods = _exigir_enabled(enabled)
    capas: list[tuple[str, pathlib.Path]] = []
    for nombre in mods:
        raiz = mods_dir / nombre
        if not _es_carpeta_propia(raiz, etiqueta=f"el mod {nombre!r}"):
            raise GanadoresError(f"el mod habilitado {nombre!r} no existe en {mods_dir}")
        capas.append((nombre, raiz))
    overwrite = data_root / CAPA_OVERWRITE
    if _es_carpeta_propia(overwrite, etiqueta=CAPA_OVERWRITE):
        capas.append((CAPA_OVERWRITE, overwrite))

    ganadores: dict[str, GanadorDeRuta] = {}
    visitados = 0
    try:
        for capa, raiz in capas:  # de menor a mayor prioridad: la capa siguiente pisa a la anterior
            base = raiz.joinpath(*partes_del_prefijo)
            for archivo, _identidad in iter_archivos_propios(base):
                visitados += 1
                if visitados > tope:
                    raise GanadoresError(
                        f"el árbol de {prefijo!r} supera el tope de {tope} archivos: no se resuelve a medias"
                    )
                partes = archivo.relative_to(base).parts
                if any(parte.lower().endswith(_SUFIJO_OCULTO) for parte in partes):
                    continue  # un archivo o una carpeta ocultos: nada de lo que cuelga de ahí está en el overlay
                relativa = "/".join((*partes_del_prefijo, *partes))
                ganadores[relativa.lower()] = GanadorDeRuta(ruta_virtual=relativa, capa=capa, archivo=archivo)
    except OSError as exc:
        raise GanadoresError(f"no se pudo recorrer el perfil: {exc}") from exc
    return GanadoresDelPerfil(ganadores)
