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
import stat

logger = logging.getLogger(__name__)

#: Los dos tipos que distingue :func:`link_kind`. ``"junction"`` sólo puede
#: aparecer en Windows.
SYMLINK = "symlink"
JUNCTION = "junction"

#: Valor de ``IO_REPARSE_TAG_MOUNT_POINT`` (constante pública de Windows, no de
#: Python). ``stat`` recién lo expone desde 3.12 —lo mismo que ``isjunction``,
#: que internamente compara CONTRA este valor exacto y no contra "no es cero"—
#: así que en 3.11 hace falta el literal como fallback.
_IO_REPARSE_TAG_MOUNT_POINT = getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", 0xA0000003)


def link_kind_or_raise(path: pathlib.Path) -> str | None:
    """Igual que :func:`link_kind`, pero deja propagar el ``OSError``.

    Existe para el caller que necesita DISTINGUIR "no es un enlace" de "no pude
    verlo" — ``link_kind`` conflate las dos en ``None`` (ver su docstring) y esa
    ambigüedad es correcta para la mayoría de los callers (fail-closed vía
    :func:`path_present`), pero no para uno que esté a punto de decidir entre
    ``rmtree`` y ``unlink``/``rmdir``: un bloqueo TRANSITORIO de AV/indexer en
    Windows (el mismo ``WinError 5/32`` que ``_dir_rollback._fs_op_with_retry``
    ya tolera en las mutaciones) hacía que ``link_kind`` devolviera ``None`` en
    plena inspección de un junction, y ese caller tomaba la rama de ``rmtree``
    creyendo que era un directorio real (review qodo-merge #404). Reintentar
    ESTA función con la misma política de backoff que las mutaciones cierra la
    ventana sin inventar una segunda implementación de la detección.
    """
    if path.is_symlink():
        return SYMLINK
    # ``is_junction`` es de Python 3.12+; en 3.11 se cae al reparse tag.
    es_junction = getattr(path, "is_junction", None)
    if es_junction is not None and es_junction():
        return JUNCTION
    # ``st_reparse_tag`` sólo existe en Windows. Se consulta sobre el lstat
    # (que no sigue el enlace) y NO se guarda con ``exists()``: ese guard
    # seguía el enlace para decidir si vale la pena mirarlo, así que un
    # junction roto (destino borrado) quedaba invisible — el mismo modo de
    # falla que este módulo existe para evitar, pero sin cubrir. La ruta
    # ausente ya la cubre el ``except OSError`` del caller.
    #
    # Comparación EXACTA contra IO_REPARSE_TAG_MOUNT_POINT, no "no es cero"
    # (review qodo-merge): un reparse tag distinto de cero no es sólo un
    # junction — OneDrive Files On-Demand usa IO_REPARSE_TAG_CLOUD, y NTFS
    # tiene dedup/HSM con sus propios tags. Con el ``bool()`` original,
    # cualquiera de esos hacía que ``DirectoryRollback``/``ProfileSandbox``
    # abortaran con "es un junction" sobre una carpeta que no lo es.
    if getattr(path.lstat(), "st_reparse_tag", 0) == _IO_REPARSE_TAG_MOUNT_POINT:
        return JUNCTION
    return None


def link_kind(path: pathlib.Path) -> str | None:
    """Devuelve ``"symlink"``, ``"junction"``, o ``None`` si es una ruta real.

    Mira el enlace mismo, nunca su destino: un enlace a directorio y un enlace
    **roto** son ambos enlaces, aunque el segundo no pase ``exists()``.

    Nunca lanza: un ``OSError`` al inspeccionar (permisos, ruta demasiado larga,
    unidad desconectada) se reporta como ``None``. Es deliberado y es la razón por
    la que los callers destructivos **no** pueden usar esto como única defensa —
    un ``None`` significa "no pude verlo", no "no es un enlace". Quien vaya a
    borrar algo decide fail-closed con :func:`path_present` además de esto, o usa
    :func:`link_kind_or_raise` con retry si necesita no confundir las dos.
    """
    try:
        return link_kind_or_raise(path)
    except OSError as exc:
        logger.debug("No se pudo inspeccionar el enlace %s: %s", path, exc)
    return None


def is_link(path: pathlib.Path) -> bool:
    """Devuelve ``True`` si *path* es un symlink o un junction (ver :func:`link_kind`)."""
    return link_kind(path) is not None


def path_present(path: pathlib.Path) -> bool:
    """Devuelve ``True`` si hay **algo** en *path*, incluido un enlace roto.

    ``Path.exists()`` no alcanza: para un symlink colgante devuelve ``False``
    mientras que ``mkdir()`` sobre esa misma ruta falla con ``FileExistsError``.
    Quien pregunta "¿está libre el camino?" necesita esta respuesta, no la otra.
    """
    return path.exists() or is_link(path)
