"""Detección de enlaces del filesystem: symlinks y junctions de Windows.

Existe porque decidir sobre un enlace mirando su **destino** es una forma de
pérdida de datos, y las APIs de la stdlib invitan a ese error:

* ``Path.exists()`` **sigue** el enlace. Para un enlace roto devuelve ``False``,
  así que "no existe" y "no puedo crear nada acá" son las dos ciertas a la vez.
* ``shutil.rmtree`` se niega a borrar un **symlink** (``os.path.islink`` lo
  detecta) pero **atraviesa un junction** y borra el contenido del destino: su
  guard interno es ``os.path.islink()``, que reporta ``False`` para un
  ``IO_REPARSE_TAG_MOUNT_POINT``.
* ``Path.is_junction`` / ``os.path.isjunction`` existen recién en **Python 3.12**.
  Este paquete soporta 3.11, donde el junction sólo se ve leyendo el
  ``st_reparse_tag`` del ``lstat``.

Toda esa matriz de plataforma y versión vive **solo acá**: estaba duplicada en
``local/validators/vfs_health`` y ``local/validators/overwrite_health`` (el
docstring del segundo ya declaraba que espejaba al primero) y estaba por
triplicarse en el rollback de directorios. El ancla
``tests/test_links.py::test_la_deteccion_de_junctions_vive_en_un_solo_modulo``
enumera los módulos que la implementan y rompe ante una copia nueva.

Sigue el criterio que ya usa :mod:`sky_claw.app.security.file_permissions`:
preguntar por el enlace **antes** que por la existencia, porque el orden inverso
deja pasar los enlaces rotos en silencio.
"""

from __future__ import annotations

import logging
import pathlib

logger = logging.getLogger(__name__)

#: Los dos tipos que distingue :func:`link_kind`. ``"junction"`` sólo puede
#: aparecer en Windows.
SYMLINK = "symlink"
JUNCTION = "junction"


def link_kind(path: pathlib.Path) -> str | None:
    """Devuelve ``"symlink"``, ``"junction"``, o ``None`` si es una ruta real.

    Mira el enlace mismo, nunca su destino: un enlace a directorio y un enlace
    **roto** son ambos enlaces, aunque el segundo no pase ``exists()``.

    Nunca lanza: un ``OSError`` al inspeccionar (permisos, ruta demasiado larga,
    unidad desconectada) se reporta como ``None``. Es deliberado y es la razón por
    la que los callers destructivos **no** pueden usar esto como única defensa —
    un ``None`` significa "no pude verlo", no "no es un enlace". Quien vaya a
    borrar algo decide fail-closed con :func:`path_present` además de esto.
    """
    try:
        if path.is_symlink():
            return SYMLINK
        # ``is_junction`` es de Python 3.12+; en 3.11 se cae al reparse tag.
        es_junction = getattr(path, "is_junction", None)
        if es_junction is not None and es_junction():
            return JUNCTION
        # ``st_reparse_tag`` sólo existe en Windows. Se consulta sobre el lstat
        # (que no sigue el enlace) y se guarda con ``exists()`` para no pagar un
        # FileNotFoundError en el camino más común: la ruta que simplemente no está.
        if path.exists() and bool(getattr(path.lstat(), "st_reparse_tag", 0)):
            return JUNCTION
    except OSError as exc:
        logger.debug("No se pudo inspeccionar el enlace %s: %s", path, exc)
    return None


def is_link(path: pathlib.Path) -> bool:
    """True si *path* es un symlink o un junction (ver :func:`link_kind`)."""
    return link_kind(path) is not None


def path_present(path: pathlib.Path) -> bool:
    """True si hay **algo** en *path*, incluido un enlace roto.

    ``Path.exists()`` no alcanza: para un symlink colgante devuelve ``False``
    mientras que ``mkdir()`` sobre esa misma ruta falla con ``FileExistsError``.
    Quien pregunta "¿está libre el camino?" necesita esta respuesta, no la otra.
    """
    return path.exists() or is_link(path)
