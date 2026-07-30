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

Además de *detectar*, este módulo **borra**: :func:`rmtree_link_aware` es el
único borrado recursivo del paquete que no atraviesa enlaces. Vive acá y no en
el caller destructivo de turno porque la decisión que toma —"esto es un enlace,
borro el enlace y no lo que apunta"— es exactamente la que implementan las
funciones de arriba, y separarlas fue lo que dejó ``shutil.rmtree`` crudo en 9
módulos. El ancla ``tests/test_borrado_recursivo.py`` enumera a todo el que
borre árboles y exige que declare con qué mecanismo.
"""

from __future__ import annotations

import logging
import os
import pathlib
import stat
from collections.abc import Callable

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

    ``FileNotFoundError`` es la ÚNICA excepción que NO propaga: "la ruta no
    existe" no es ambiguo ni transitorio como un lock de AV/indexer —
    reintentarlo no lo cambia, y es el caso NORMAL de un primer run (target
    todavía no creado). Dejarlo escapar rompía exactamente eso: un
    ``_fs_op_with_retry`` alrededor de esta función quemaba los 5 reintentos
    con backoff y terminaba fallando ``__aenter__`` sobre la entrada más común
    y correcta que hay.
    """
    # ``is_junction`` es de Python 3.12+; en 3.11 se cae al reparse tag.
    es_junction = getattr(path, "is_junction", None)
    if es_junction is not None and es_junction():
        return JUNCTION
    # UN solo ``lstat()`` (no sigue el enlace) para las dos preguntas que
    # quedan, en vez de ``is_symlink()`` (que hace su propio lstat interno) más
    # un ``path.lstat()`` explícito para el reparse tag — este módulo está en
    # el camino recursivo de borrado (``_rmtree_link_aware_sync`` lo llama una
    # vez por entrada del árbol), así que duplicar el syscall en el caso común
    # (directorio real, ni symlink ni junction) se paga en cada archivo de un
    # output de mods potencialmente enorme (review CodeRabbit #404).
    #
    # NO se guarda con ``exists()``: ese guard seguía el enlace para decidir
    # si vale la pena mirarlo, así que un junction roto (destino borrado)
    # quedaba invisible — el mismo modo de falla que este módulo existe para
    # evitar, pero sin cubrir.
    try:
        st = path.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(st.st_mode):
        return SYMLINK
    # Comparación EXACTA contra IO_REPARSE_TAG_MOUNT_POINT, no "no es cero"
    # (review qodo-merge): un reparse tag distinto de cero no es sólo un
    # junction — OneDrive Files On-Demand usa IO_REPARSE_TAG_CLOUD, y NTFS
    # tiene dedup/HSM con sus propios tags. Con el ``bool()`` original,
    # cualquiera de esos hacía que ``DirectoryRollback``/``ProfileSandbox``
    # abortaran con "es un junction" sobre una carpeta que no lo es.
    if getattr(st, "st_reparse_tag", 0) == _IO_REPARSE_TAG_MOUNT_POINT:
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


def borrar_enlace(ruta: pathlib.Path, tipo: str) -> None:
    """Borra el ENLACE *ruta*, nunca el árbol al que ``tipo`` dice que apunta.

    ``Path.unlink`` borra un symlink en POSIX, y en Windows un symlink de
    ARCHIVO — pero un symlink de DIRECTORIO en Windows necesita ``rmdir`` igual
    que un junction: ``DeleteFileW`` (lo que ``unlink`` invoca) rechaza
    cualquier ruta con ``FILE_ATTRIBUTE_DIRECTORY``, que un symlink de
    directorio SÍ tiene, y falla con ``PermissionError`` (review qodo-merge
    #404). Por eso la rama no-junction prueba ``unlink`` primero (cubre POSIX
    sin costo) y cae a ``rmdir`` si falla, en vez de asumir ``unlink``
    incondicional.

    Para el junction se va directo a ``rmdir``, sin probar ``unlink``: ahí
    ``unlink`` falla SIEMPRE (no es transitorio), así que probarlo primero sólo
    quema tiempo antes de caer igual a ``rmdir``.
    """
    if tipo == JUNCTION:
        ruta.rmdir()
        return
    try:
        ruta.unlink()
    except OSError:
        ruta.rmdir()


def _sumar_bit_de_escritura(ruta: pathlib.Path) -> None:
    """Agrega ``S_IWRITE`` preservando el resto del modo.

    Clobberear el modo completo de un directorio rompe el borrado en POSIX (se
    pierde el bit de ejecución y deja de poder recorrerse), así que se suma en
    vez de asignar.
    """
    try:
        os.chmod(ruta, ruta.stat().st_mode | stat.S_IWRITE)
    except OSError as exc:  # noqa: BLE001 — best-effort: el borrado de abajo re-lanza si igual falla
        logger.debug("No se pudo limpiar el read-only de %s: %s", ruta, exc)


def _borrar_con_reintento_de_readonly(
    operacion: Callable[[], None],
    ruta: pathlib.Path,
    *,
    limpiar_readonly: bool,
) -> None:
    """Ejecuta *operacion*; si falla y se pidió, limpia el read-only y reintenta.

    El read-only se limpia **por entrada y sólo ante el fallo**, no con un
    ``os.walk`` previo sobre todo el árbol como hacían las dos copias de
    ``_rmtree_force``: ese walk desciende por junctions (``followlinks=False``
    decide con ``islink()``, ciego al reparse point) y terminaba **chmodeando el
    árbol ajeno** antes de que ``rmtree`` lo borrara. Acá el chmod no puede
    alcanzar nada que el recorrido no vaya a borrar.
    """
    try:
        operacion()
    except OSError:
        if not limpiar_readonly:
            raise
        _sumar_bit_de_escritura(ruta)
        operacion()


def _borrar_recursivo(ruta: pathlib.Path, *, limpiar_readonly: bool) -> None:
    """Recorrido interno de :func:`rmtree_link_aware`; asume que *ruta* existe."""
    tipo = link_kind_or_raise(ruta)
    if tipo is not None:
        borrar_enlace(ruta, tipo)
        return
    if not ruta.is_dir():
        _borrar_con_reintento_de_readonly(ruta.unlink, ruta, limpiar_readonly=limpiar_readonly)
        return
    with os.scandir(ruta) as entradas:
        for entrada in entradas:
            _borrar_recursivo(pathlib.Path(entrada.path), limpiar_readonly=limpiar_readonly)
    _borrar_con_reintento_de_readonly(ruta.rmdir, ruta, limpiar_readonly=limpiar_readonly)


def rmtree_link_aware(ruta: pathlib.Path, *, limpiar_readonly: bool = False) -> None:
    """Borra *ruta* recursivamente SIN atravesar enlaces, ni siquiera anidados.

    Reemplaza a ``shutil.rmtree`` en todo el paquete. ``rmtree`` sólo protege su
    propia RAÍZ contra symlinks (ahí lanza ``OSError``) y es ciego a junctions
    en la raíz **y en cualquier nivel más profundo**: su recorrido interno usa
    ``os.path.islink()``/``DirEntry.is_dir()``, que no distinguen un
    ``IO_REPARSE_TAG_MOUNT_POINT`` de un directorio real. Un árbol por lo demás
    propio con un junction en un subdirectorio —una sola carpeta de mods llevada
    a otro disco, práctica corriente en MO2— se atravesaba igual y borraba el
    destino ajeno, sin excepción que lo delatara. Acá cada entrada se inspecciona
    con :func:`link_kind_or_raise` ANTES de decidir si recursar o borrar sólo el
    enlace.

    *limpiar_readonly* activa el reintento tras sumar el bit de escritura, que
    los mods extraídos y los ``.git`` necesitan en Windows. Va apagado por
    defecto: es una mutación de permisos, y el caller que no la pidió merece ver
    el ``OSError`` en vez de que se le toquen modos en silencio.

    **No lanza si *ruta* no está.** Borrar lo que ya no existe cumplió el
    objetivo, y la ausencia se mide con :func:`path_present` —no con
    ``exists()``— para que un enlace ROTO cuente como presente y se borre el
    enlace en vez de tomarse por "no hay nada acá".
    """
    if not path_present(ruta):
        return
    _borrar_recursivo(ruta, limpiar_readonly=limpiar_readonly)
