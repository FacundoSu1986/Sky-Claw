"""Inventario fail-closed de un árbol (RV-1).

Recorrido propio del Runtime Vault con esta política: un enlace inesperado
(symlink, junction o cualquier reparse point) dentro del scope NO se sigue NI
se ignora — el inventario falla cerrado con :class:`InventoryLinkError`. La
detección del enlace usa la primitiva canónica
:mod:`sky_claw.app.security.links` (módulo de ``main``), la única fuente de la
matriz symlink/junction del repo; acá NO se reimplementa esa detección (el
ancla ``tests/test_links.py`` rompe ante copias).

El inventario es COMPLETO (no muestreado) y determinista (orden lexicográfico
del relpath posix). La identidad de cada archivo es (relpath, size, sha256);
el mtime no participa. El tamaño sale de los bytes leídos junto al digest.

Límites declarados: la inspección es best-effort ante mutación concurrente del
filesystem — se inspecciona entrada por entrada sin handles ``O_NOFOLLOW`` y se
revalida la identidad de cada directorio tras abrirlo, pero no se promete una
garantía TOCTOU-fuerte. Cualquier señal de mutación que el recorrido SÍ vea
(entrada desaparecida, identidad cambiada) es un error, no un inventario
parcial.
"""

from __future__ import annotations

import hashlib
import os
import pathlib
import stat

from sky_claw.app.security.links import link_kind_and_identity_or_raise, same_file_identity
from sky_claw.local.runtime_vault.models import FileIdentity, InventoryError, InventoryLinkError

#: Chunk de lectura: los árboles verificados pueden llegar a GBs; nunca se
#: sostiene un archivo completo en memoria.
_CHUNK = 1024 * 1024


def _digest_de_archivo(ruta: pathlib.Path) -> tuple[int, str]:
    """(bytes leídos, sha256 hex) de un archivo regular, por chunks.

    El tamaño devuelto sale de la MISMA lectura que alimenta el digest, de modo
    que un ``FileIdentity`` nunca pueda combinar el tamaño de una versión del
    archivo con el digest de otra.
    """
    acumulador = hashlib.sha256()
    total = 0
    with ruta.open("rb") as fh:
        while chunk := fh.read(_CHUNK):
            acumulador.update(chunk)
            total += len(chunk)
    return total, acumulador.hexdigest()


def _clasificar(ruta: pathlib.Path) -> tuple[str | None, os.stat_result | None]:
    """Clasifica *ruta* con la primitiva canónica; los OSError se traducen a InventoryError."""
    try:
        tipo, identidad = link_kind_and_identity_or_raise(ruta)
    except OSError as exc:
        raise InventoryError(f"No se pudo inspeccionar '{ruta}' para el inventario: {exc}") from exc
    return tipo, identidad


def inventory_tree(root: pathlib.Path) -> tuple[FileIdentity, ...]:
    """Inventario completo del árbol bajo *root*, en orden lexicográfico del relpath.

    Args:
        root: raíz del árbol a inventariar. Si es un enlace, o no existe, o no
            es un directorio, no hay inventario que afirmar.

    Returns:
        Una tupla ordenada de :class:`FileIdentity`, una por archivo regular
        propio dentro del scope.

    Raises:
        InventoryError: raíz ausente/no-directorio/sin archivos propios,
            entrada ilegible o inesperada, o mutación concurrente detectada.
        InventoryLinkError: un enlace dentro del scope (fail closed).
    """
    tipo_raiz, identidad_raiz = _clasificar(root)
    if identidad_raiz is None:
        raise InventoryError(f"La raíz '{root}' no existe: no hay inventario que afirmar")
    if tipo_raiz is not None:
        raise InventoryLinkError(
            f"La raíz '{root}' es un enlace ({tipo_raiz}): no se sigue ni se ignora — no hay identidad que firmar"
        )
    if not stat.S_ISDIR(identidad_raiz.st_mode):
        raise InventoryError(f"La raíz '{root}' no es un directorio: no hay árbol que inventariar")

    encontrados: list[FileIdentity] = []
    pendientes: list[pathlib.Path] = [root]
    while pendientes:
        actual = pendientes.pop()
        tipo, identidad = _clasificar(actual)
        if identidad is None:
            raise InventoryError(f"'{actual}' desapareció durante el recorrido: el árbol está cambiando")
        if tipo is not None:
            raise InventoryLinkError(
                f"Enlace inesperado dentro del scope: '{actual}' ({tipo}) — no se sigue ni se ignora, el "
                "árbol deja de ser autorizable"
            )
        if stat.S_ISREG(identidad.st_mode):
            try:
                size, digest = _digest_de_archivo(actual)
            except OSError as exc:
                raise InventoryError(f"No se pudo leer '{actual}' para el inventario: {exc}") from exc
            encontrados.append(FileIdentity(rel_path=actual.relative_to(root).as_posix(), size=size, digest=digest))
            continue
        if not stat.S_ISDIR(identidad.st_mode):
            raise InventoryError(f"Entrada inesperada dentro del scope: '{actual}' no es archivo regular ni directorio")
        try:
            with os.scandir(actual) as entradas:
                hijos = [pathlib.Path(entrada.path) for entrada in entradas]
        except OSError as exc:
            raise InventoryError(f"No se pudo recorrer el directorio '{actual}': {exc}") from exc
        # Revalidar DESPUÉS de abrir: un directorio reemplazado por un enlace
        # entre su lstat y el scandir no debe meterse en el inventario. Es la
        # misma revalidación de identidad que usa el walker compartido de
        # sky_claw.app.security.links, pero con desenlace fail-closed acá.
        tipo_despues, identidad_despues = _clasificar(actual)
        if identidad_despues is None:
            raise InventoryError(f"'{actual}' desapareció mientras se abría: el árbol está cambiando")
        if tipo_despues is not None:
            raise InventoryLinkError(
                f"'{actual}' se convirtió en un enlace ({tipo_despues}) durante el recorrido: no se sigue ni se ignora"
            )
        if not same_file_identity(identidad, identidad_despues):
            raise InventoryError(f"'{actual}' cambió mientras se abría: el árbol está cambiando")
        pendientes.extend(hijos)

    if not encontrados:
        raise InventoryError(
            f"El árbol bajo '{root}' no tiene archivos propios que identificar: un árbol vacío no tiene identidad"
        )
    return tuple(sorted(encontrados, key=lambda f: f.rel_path))


__all__ = ["inventory_tree"]
