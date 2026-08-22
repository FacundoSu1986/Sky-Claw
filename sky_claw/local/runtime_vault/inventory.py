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
el mtime no participa de la identidad persistente ni del tree digest. El
tamaño sale de los bytes leídos junto al digest.

La observación de cada archivo queda SELLADA alrededor de su lectura
(PRE/READ/POST): se re-inspecciona la entrada después de leer y la
observación sólo se acepta si demuestra haber sido estable — misma identidad
de entrada, bytes leídos igual al tamaño final y mtime sin cambios durante la
captura. El mtime actúa acá como evidence gate de captura, NO como identidad.

Límites declarados: la inspección es best-effort ante mutación concurrente del
filesystem — se inspecciona entrada por entrada sin handles ``O_NOFOLLOW`` y se
revalida la identidad de cada directorio tras abrirlo, pero no se promete una
garantía TOCTOU-fuerte (un doble overwrite que restaure mtime dentro de la
misma granularidad no es distinguible). Cualquier señal de mutación que el
recorrido SÍ vea (entrada desaparecida, identidad cambiada, tamaño o mtime
distintos tras la lectura) es un error, no un inventario parcial.
"""

from __future__ import annotations

import hashlib
import os
import pathlib
import stat
from collections.abc import Callable

from sky_claw.app.security.links import link_kind_and_identity_or_raise, same_file_identity
from sky_claw.local.runtime_vault.models import FileIdentity, InventoryError, InventoryLinkError

#: Chunk de lectura: los árboles verificados pueden llegar a GBs; nunca se
#: sostiene un archivo completo en memoria.
_CHUNK = 1024 * 1024

#: Hooks de sincronización para los tests de estabilidad de captura (F1).
#: No-op por defecto; los tests los reemplazan con funciones que mutan el
#: archivo en el punto exacto de la ventana PRE/READ/POST. Son la única forma
#: determinista (sin sleeps) de ejercitar una carrera real del filesystem.
_HOOK_ANTES_DE_ABRIR: Callable[[pathlib.Path], None] | None = None
_HOOK_DURANTE_LA_LECTURA: Callable[[pathlib.Path], None] | None = None


def _digest_estable(ruta: pathlib.Path, identidad_pre: os.stat_result) -> tuple[int, str]:
    """(bytes leídos, sha256 hex) sellando la observación alrededor de la lectura.

    PRE: ``identidad_pre`` es el MISMO ``lstat`` con el que el recorrido decidió
    que ``ruta`` era un archivo regular propio. READ: streaming por chunks con
    conteo de bytes realmente leídos. POST: nuevo ``lstat`` (sin seguir
    enlaces) y tres gates, todos necesarios:

    - ``same_file_identity(pre, post)``: la entrada de directorio siguió siendo
      la misma (mismo tipo, mismo reparse tag, mismo file id) — un reemplazo
      por otro archivo o por un enlace rompe acá, y un ``post`` con tipo de
      enlace se rechaza con :class:`InventoryLinkError` ANTES de que la lectura
      del target pueda darse por válida (la ventana lstat → reemplazo por
      symlink/junction → open no sigue al nuevo target: la observación se
      descarta);
    - bytes leídos == ``post.st_size``: sin crecimiento ni truncamiento
      durante la lectura;
    - ``pre.st_mtime_ns == post.st_mtime_ns``: sin overwrite same-size ni
      modificación observada durante la lectura. El mtime es SOLO evidence
      gate de captura: no entra al ``FileIdentity`` ni al digest del árbol.

    Límite declarado: no es una garantía TOCTOU-fuerte — un doble overwrite
    que restaura el mtime dentro de la granularidad del filesystem no es
    distinguible. Todo lo que SÍ se observa falla cerrado.
    """
    acumulador = hashlib.sha256()
    total = 0
    if _HOOK_ANTES_DE_ABRIR is not None:
        _HOOK_ANTES_DE_ABRIR(ruta)
    with ruta.open("rb") as fh:
        while chunk := fh.read(_CHUNK):
            acumulador.update(chunk)
            total += len(chunk)
            if _HOOK_DURANTE_LA_LECTURA is not None:
                _HOOK_DURANTE_LA_LECTURA(ruta)
    tipo_post, identidad_post = _clasificar(ruta)
    if identidad_post is None:
        raise InventoryError(f"'{ruta}' desapareció durante la lectura: la observación no es estable")
    if tipo_post is not None:
        raise InventoryLinkError(
            f"'{ruta}' fue reemplazado por un enlace ({tipo_post}) durante la lectura: "
            "no se sigue el nuevo target — la observación se descarta"
        )
    if not same_file_identity(identidad_pre, identidad_post):
        raise InventoryError(f"'{ruta}' fue reemplazado durante la lectura: la observación no es estable")
    if total != identidad_post.st_size:
        raise InventoryError(
            f"'{ruta}' cambió de tamaño durante la lectura ({total} bytes leídos vs "
            f"{identidad_post.st_size} finales): la observación no es estable"
        )
    if identidad_pre.st_mtime_ns != identidad_post.st_mtime_ns:
        raise InventoryError(
            f"'{ruta}' fue modificado durante la lectura (mtime {identidad_pre.st_mtime_ns} → "
            f"{identidad_post.st_mtime_ns}): la observación no es estable"
        )
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
                size, digest = _digest_estable(actual, identidad)
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
