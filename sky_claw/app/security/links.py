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
import time
from collections.abc import Callable, Iterator

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
_LINK_INSPECTION_RETRIES = 5
_LINK_INSPECTION_BACKOFF_SECONDS = 0.1


def reparse_tag_or_zero(st: os.stat_result | None) -> int:
    """Devuelve el ``st_reparse_tag`` de *st* o 0 si no es reparse point o no está disponible."""
    if st is None:
        return 0
    return getattr(st, "st_reparse_tag", 0)


def link_kind_and_identity_or_raise(
    path: pathlib.Path,
) -> tuple[str | None, os.stat_result | None]:
    """Inspecciona *path* con un único ``lstat`` y devuelve también su identidad."""
    try:
        st = path.lstat()
    except FileNotFoundError:
        return None, None
    if stat.S_ISLNK(st.st_mode):
        return SYMLINK, st
    if getattr(st, "st_reparse_tag", 0) == _IO_REPARSE_TAG_MOUNT_POINT:
        return JUNCTION, st
    return None, st


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
    # UN solo ``lstat()`` (no sigue el enlace) para las dos preguntas, en vez de
    # ``is_symlink()``/``is_junction()`` (cada uno hace su propio lstat interno)
    # más un ``path.lstat()`` explícito para el reparse tag — este módulo está en
    # el camino recursivo de borrado (``_rmtree_link_aware_sync`` lo llama una
    # vez por entrada del árbol), así que duplicar el syscall en el caso común
    # (directorio real, ni symlink ni junction) se paga en cada archivo de un
    # output de mods potencialmente enorme (review CodeRabbit #404).
    #
    # NO se guarda con ``exists()``: ese guard seguía el enlace para decidir
    # si vale la pena mirarlo, así que un junction roto (destino borrado)
    # quedaba invisible — el mismo modo de falla que este módulo existe para
    # evitar, pero sin cubrir.
    tipo, _st = link_kind_and_identity_or_raise(path)
    return tipo


def link_kind_or_raise_with_retry(path: pathlib.Path) -> str | None:
    """Inspecciona fail-closed, reintentando bloqueos transitorios del filesystem.

    Los guards destructivos no pueden usar :func:`link_kind`: allí ``None``
    también significa "no pude inspeccionar". Esta variante conserva el
    ``OSError`` y aplica el mismo presupuesto de cinco intentos con backoff
    lineal que las mutaciones de ``DirectoryRollback``. Si el bloqueo persiste,
    propaga el último error y el caller aborta sin borrar.
    """
    last_exc: OSError | None = None
    for attempt in range(_LINK_INSPECTION_RETRIES):
        try:
            tipo, _identidad = link_kind_and_identity_or_raise(path)
            return tipo
        except OSError as exc:
            last_exc = exc
            if attempt < _LINK_INSPECTION_RETRIES - 1:
                time.sleep(_LINK_INSPECTION_BACKOFF_SECONDS * (attempt + 1))
    assert last_exc is not None
    raise last_exc


def link_kind_and_identity_or_raise_with_retry(
    path: pathlib.Path,
) -> tuple[str | None, os.stat_result | None]:
    """Clasifica e identifica *path* reintentando errores transitorios."""
    last_exc: OSError | None = None
    for attempt in range(_LINK_INSPECTION_RETRIES):
        try:
            return link_kind_and_identity_or_raise(path)
        except OSError as exc:
            last_exc = exc
            if attempt < _LINK_INSPECTION_RETRIES - 1:
                time.sleep(_LINK_INSPECTION_BACKOFF_SECONDS * (attempt + 1))
    assert last_exc is not None
    raise last_exc


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


def same_file_identity(antes: os.stat_result, despues: os.stat_result | None) -> bool:
    """True si dos ``lstat`` describen la misma entrada del directorio."""
    return (
        despues is not None
        and antes.st_dev > 0
        and antes.st_ino > 0
        and despues.st_dev > 0
        and despues.st_ino > 0
        and antes.st_dev == despues.st_dev
        and antes.st_ino == despues.st_ino
        and stat.S_IFMT(antes.st_mode) == stat.S_IFMT(despues.st_mode)
        and getattr(antes, "st_reparse_tag", 0) == getattr(despues, "st_reparse_tag", 0)
    )


class ContencionFisicaVioladaError(OSError):
    """El candidato no es un descendiente FÍSICO real de la raíz administrada.

    La contención LÓGICA (``resolve().is_relative_to``) no alcanza: si un
    componente intermedio fue reemplazado por un junction/symlink después de
    resolver el workspace, el candidato y su raíz pueden resolver AMBOS al mismo
    árbol externo y la relación sigue siendo verdadera aunque la mutación
    aterrice fuera del workspace. Esta excepción representa el veredicto de
    :func:`exigir_contencion_fisica`, que inspecciona cada componente con
    ``lstat`` sin seguir enlaces.

    Hereda de ``OSError`` para que los callers que ya tratan fallos de
    inspección de filesystem como fail-closed la cubran naturalmente, sin
    confundirla con un fallo de dominio.
    """


def _componentes_absolutos(ruta: pathlib.Path) -> tuple[str, ...]:
    """Componentes de *ruta* colapsando ``.``/``..`` SOLO léxicamente.

    ``os.path.abspath`` no toca el filesystem: no resuelve enlaces ni consulta
    la existencia. Un ``..`` que sobreviva al colapso (ruta relativa al volumen)
    se rechaza fail-closed: aceptarlo obligaría a decidir su significado
    atravesando componentes, que es exactamente lo que esta primitiva prohíbe.
    """
    absoluta = pathlib.Path(os.path.abspath(os.fspath(ruta)))
    partes = absoluta.parts
    if ".." in partes:
        raise ContencionFisicaVioladaError(f"La ruta no se puede normalizar sin ambigüedad: {ruta}")
    return partes


def exigir_contencion_fisica(
    raiz: pathlib.Path,
    candidato: pathlib.Path,
    *,
    permitir_raiz: bool = False,
    exigir_existencia: bool = False,
) -> pathlib.Path:
    """Exige que *candidato* cuelgue FÍSICAMENTE de *raiz*, sin reparse intermedios.

    **Qué problema cierra.** Los guards que sólo miran el path final (o que
    comparan rutas resueltas) no ven un symlink/junction/reparse introducido en
    un ANCESTRO después de que el workspace quedó resuelto: con
    ``E:\\Work\\DynDOLOD`` reemplazado por un junction a ``D:\\Outside``, tanto
    ``E:\\Work\\DynDOLOD\\TexGen`` como su raíz resuelven al mismo árbol externo y
    la relación lógica sigue siendo verdadera. La mutación administrada
    aterrizaría fuera del ``external_work_root`` admitido.

    **Cómo lo decide.** Recorre la cadena de componentes desde *raiz* hasta
    *candidato* y hace ``lstat`` de cada uno —nunca ``stat``/``resolve``, que
    seguirían el enlace—: la raíz y todo ancestro existente deben ser
    directorios REALES, sin reparse tag ni symlink. Después de recorrer,
    revalida la identidad (``st_dev``/``st_ino``/modo/reparse tag) de cada
    componente capturado, en orden inverso, para acotar la ventana de un
    reemplazo durante la propia inspección.

    **Componentes inexistentes.** Un componente que no existe no puede ser un
    enlace; si la cadena se corta (primer run, root todavía no creado) el
    recorrido se detiene ahí y valida los ancestros existentes. Con
    ``exigir_existencia=True`` cualquier componente ausente es un fallo; es el
    modo del packaging, donde copiar una fuente que desapareció no es un caso
    normal. La ventana entre este guard y el ``mkdir``/``rename``/``copytree``
    queda del lado del caller: se llama lo más cerca posible de la mutación y,
    cuando aplica, la identidad final se vuelve a verificar después.

    **Fail-closed.** *candidato* fuera de *raiz*, *candidato* igual a *raiz* sin
    ``permitir_raiz``, un componente que no se puede inspeccionar, que no es
    directorio, que es symlink/junction/reparse, o cuya identidad cambia durante
    la inspección, terminan todos en :class:`ContencionFisicaVioladaError`.
    No sigue ningún enlace para decidir ownership.

    Devuelve el path absoluto normalizado (léxicamente) del candidato, para que
    el caller mute exactamente el path que se validó.
    """
    partes_raiz = _componentes_absolutos(raiz)
    partes_cand = _componentes_absolutos(candidato)
    if os.path.normcase(partes_raiz[0]) != os.path.normcase(partes_cand[0]):
        raise ContencionFisicaVioladaError(f"'{candidato}' está en otro volumen que la raíz administrada '{raiz}'")
    if len(partes_cand) < len(partes_raiz):
        raise ContencionFisicaVioladaError(f"'{candidato}' está fuera de la raíz administrada '{raiz}'")
    prefijo = tuple(os.path.normcase(parte) for parte in partes_cand[: len(partes_raiz)])
    if prefijo != tuple(os.path.normcase(parte) for parte in partes_raiz):
        raise ContencionFisicaVioladaError(f"'{candidato}' está fuera de la raíz administrada '{raiz}'")
    if len(partes_cand) == len(partes_raiz):
        if not permitir_raiz:
            raise ContencionFisicaVioladaError(
                f"'{candidato}' ES la raíz administrada: esta mutación exige un descendiente real"
            )
        return pathlib.Path(*partes_cand)

    capturadas: list[tuple[pathlib.Path, os.stat_result]] = []
    actual = pathlib.Path(*partes_cand[: len(partes_raiz)])
    for indice in range(len(partes_raiz), len(partes_cand) + 1):
        if indice > len(partes_raiz):
            actual = actual / partes_cand[indice - 1]
        tipo_de_enlace, identidad = link_kind_and_identity_or_raise(actual)
        if identidad is None:
            # La RAÍZ administrada existe por contrato (P0 publicó el binding
            # ahí): si no está, no hay cadena física que demostrar.
            if indice == len(partes_raiz) or exigir_existencia:
                raise ContencionFisicaVioladaError(
                    f"El componente '{actual}' de la cadena física no existe o no se pudo inspeccionar"
                )
            # Un componente inexistente no puede ser un reparse point, y nada que
            # cuelgue de él existe todavía: se valida lo existente y el caller
            # revalida después de crear (born-empty) o antes de mutar de nuevo.
            break
        if tipo_de_enlace is not None:
            raise ContencionFisicaVioladaError(
                f"El componente '{actual}' de la cadena física es un {tipo_de_enlace}: "
                f"la mutación administrada no puede atravesarlo"
            )
        if not stat.S_ISDIR(identidad.st_mode):
            raise ContencionFisicaVioladaError(f"El componente '{actual}' de la cadena física no es un directorio")
        capturadas.append((actual, identidad))

    for ruta, identidad_capturada in reversed(capturadas):
        _, identidad_actual = link_kind_and_identity_or_raise(ruta)
        if not same_file_identity(identidad_capturada, identidad_actual):
            raise ContencionFisicaVioladaError(
                f"La identidad de '{ruta}' cambió durante la inspección de la cadena física"
            )
    return pathlib.Path(*partes_cand)


def same_direntry_identity(
    capturada: os.stat_result,
    inode_capturado: int,
    observada: os.stat_result | None,
) -> bool:
    """Compara una captura de ``DirEntry`` con el ``lstat`` canónico.

    Python 3.11 en Windows devuelve ceros en ``DirEntry.stat().st_ino/st_dev``,
    pero ``DirEntry.inode()`` sí expone el file ID real.
    """
    return (
        observada is not None
        and inode_capturado > 0
        and observada.st_dev > 0
        and observada.st_ino > 0
        and (capturada.st_dev == 0 or capturada.st_dev == observada.st_dev)
        and inode_capturado == observada.st_ino
        and stat.S_IFMT(capturada.st_mode) == stat.S_IFMT(observada.st_mode)
    )


def _borrar_recursivo(ruta: pathlib.Path, *, limpiar_readonly: bool) -> int:
    """Recorrido postorden iterativo de :func:`rmtree_link_aware`.

    Devuelve los bytes de los archivos regulares que efectivamente desenlazó.
    """
    bytes_borrados = 0
    pendientes: list[tuple[pathlib.Path, os.stat_result | None]] = [(ruta, None)]
    while pendientes:
        actual, identidad_para_rmdir = pendientes.pop()
        if identidad_para_rmdir is not None:
            tipo_final, identidad_final = link_kind_and_identity_or_raise(actual)
            if tipo_final is not None or not same_file_identity(identidad_para_rmdir, identidad_final):
                raise OSError(f"La entrada cambió antes de quitar el directorio: {actual}")
            _borrar_con_reintento_de_readonly(actual.rmdir, actual, limpiar_readonly=limpiar_readonly)
            continue

        # UN solo lstat por entrada. Antes había dos seguidos —``link_kind_or_raise``
        # y después ``link_kind_and_identity_or_raise``— y se comparaban entre sí.
        # Esa comparación no compraba nada: la ventana que importa es la que va
        # del ÚLTIMO lstat al ``unlink``/``rmdir``, y contra eso protegen las
        # revalidaciones de identidad de más abajo. Duplicar el syscall por
        # entrada sí se paga en un output de mods de decenas de miles de
        # archivos, y ahora este recorrido además sostiene la medición.
        tipo_revalidado, identidad_antes = link_kind_and_identity_or_raise(actual)
        if identidad_antes is None:
            continue
        if tipo_revalidado is not None:
            # Un enlace aporta CERO: se quita el reparse point y el destino queda
            # intacto. Contar su `st_size` —o peor, el del árbol al que apunta—
            # es justo la divergencia que esta función existe para cerrar.
            borrar_enlace(actual, tipo_revalidado)
            continue
        if not stat.S_ISDIR(identidad_antes.st_mode):
            _borrar_con_reintento_de_readonly(actual.unlink, actual, limpiar_readonly=limpiar_readonly)
            # El tamaño sale del lstat que ya se hizo para el chequeo de
            # identidad: medir no cuesta un syscall extra, y sale del MISMO
            # stat con el que se decidió borrar.
            bytes_borrados += identidad_antes.st_size
            continue

        try:
            with os.scandir(actual) as entradas:
                tipo_despues, identidad_despues = link_kind_and_identity_or_raise(actual)
                if identidad_despues is None:
                    # Igual que en el recorrido de medición: desaparecer después
                    # del scandir es lo mismo que desaparecer antes, y para un
                    # BORRADO además cumplió el objetivo.
                    continue
                if tipo_despues is not None or not same_file_identity(identidad_antes, identidad_despues):
                    raise OSError(f"La entrada cambió mientras se abría para borrar: {actual}")
                hijos = [pathlib.Path(entrada.path) for entrada in entradas]
        except FileNotFoundError:
            # El directorio desaparecio entre su lstat y el scandir. Para un
            # BORRADO eso cumplio el objetivo, que es lo que ya declara el
            # docstring de `rmtree_link_aware` para su raiz: "borrar lo que ya no
            # existe cumplio el objetivo". Hacerlo valer solo en la raiz y no en
            # los niveles de adentro seria el defecto #1 del repo.
            continue

        pendientes.append((actual, identidad_despues))
        pendientes.extend((hijo, None) for hijo in reversed(hijos))
    return bytes_borrados


def tamano_de_arbol_propio(ruta: pathlib.Path) -> tuple[int, int]:
    """Bytes y cantidad de archivos de *ruta*, SIN atravesar enlaces.

    Es la mitad "medir" de :func:`rmtree_link_aware`, para los callers que
    necesitan el número sin borrar —un ``dry_run``, un reporte de estadísticas—.

    **Por qué existe en vez de usar ``rglob``.** ``Path.rglob`` decide si
    desciende con ``entry.is_dir(follow_symlinks=False)``: eso frena en un
    symlink, pero **no en un junction de Windows**, porque el reparse point
    ``IO_REPARSE_TAG_MOUNT_POINT`` no es un symlink para ``os.path.islink`` —la
    misma ceguera que hizo falta cerrar en el borrado (#404, #405), y la razón
    de que ``tests/_symlink_guard.py`` verifique justamente esa propiedad—. Un
    ``rglob`` que suma tamaños **entra** al junction y cuenta bytes ajenos; el
    borrado link-aware no los toca. Medir con uno y borrar con el otro hace que
    la cuenta y el efecto diverjan, y quien resta esos bytes de un presupuesto
    cree haber liberado espacio que sigue ocupado.

    Un enlace aporta cero y no se recursa: se cuenta lo que un borrado sobre
    este mismo árbol se llevaría, ni más ni menos. Esa igualdad es el invariante
    y está anclada en ``tests/test_borrado_recursivo.py``.
    """
    total = 0
    archivos = 0
    for _, identidad in iter_archivos_propios(ruta):
        total += identidad.st_size
        archivos += 1
    return total, archivos


def iter_archivos_propios(ruta: pathlib.Path) -> Iterator[tuple[pathlib.Path, os.stat_result]]:
    """Itera los archivos regulares bajo *ruta* SIN atravesar enlaces.

    Es el recorrido que comparten los que necesitan mirar un árbol sin entrar a
    lo ajeno. Existe como generador —y no como una función que sólo suma— porque
    los consumidores quieren cosas distintas del mismo camino: bytes, cantidad,
    fechas, extensiones. Que cada uno rehiciera el recorrido es exactamente cómo
    dos caminos con la misma intención terminan con políticas distintas.

    Rinde el ``lstat`` ya hecho junto a la ruta: quien necesita el tamaño no
    tiene que volver a llamar a ``stat()``, y sobre todo **no puede** obtenerlo
    de un stat distinto del que decidió que la entrada era un archivo propio.

    Reemplaza a ``Path.rglob("*")`` para este uso. ``rglob`` decide si desciende
    con ``entry.is_dir(follow_symlinks=False)``: frena en un symlink pero **no
    en un junction de Windows**, porque ``IO_REPARSE_TAG_MOUNT_POINT`` no es un
    symlink para ``os.path.islink`` — la misma ceguera que hubo que cerrar en el
    borrado (#404, #405).
    """
    if not path_present(ruta):
        return

    pendientes = [ruta]
    while pendientes:
        actual = pendientes.pop()
        tipo, identidad_antes = link_kind_and_identity_or_raise(actual)
        if identidad_antes is None or tipo is not None:
            continue
        if not stat.S_ISDIR(identidad_antes.st_mode):
            yield actual, identidad_antes
            continue
        try:
            with os.scandir(actual) as entradas:
                # Revalidar DESPUÉS de abrir, igual que ``_borrar_recursivo``.
                # Sin esto, un directorio reemplazado por un enlace entre su
                # ``lstat`` y este ``scandir`` hace que el recorrido entre al
                # árbol externo y devuelva rutas de afuera del store — y
                # ``cleanup_by_pattern`` se las pasa a ``unlink``, así que la
                # carrera borra archivos ajenos. Que el hermano destructivo ya
                # revalidara y éste no era el defecto #1 del repo, cometido
                # dentro del cambio que lo denuncia (review Codex #416).
                tipo_despues, identidad_despues = link_kind_and_identity_or_raise(actual)
                if identidad_despues is None:
                    # Se fue DESPUÉS del scandir. Es la misma desaparición
                    # concurrente y benigna que el ``except`` de abajo tolera
                    # cuando ocurre ANTES; distinguirlas por milisegundos daría
                    # dos desenlaces para la misma causa (review qodo-merge #416).
                    continue
                if tipo_despues is not None or not same_file_identity(identidad_antes, identidad_despues):
                    raise OSError(f"La entrada cambió mientras se abría para medir: {actual}")
                pendientes.extend(pathlib.Path(entrada.path) for entrada in entradas)
        except FileNotFoundError:
            # Un directorio que se fue entre su lstat y el scandir no aporta
            # bytes. Medir NO toma el lock del store —`_calculate_total_size` no
            # lo hace— asi que convivir con una limpieza concurrente es el caso
            # esperado, no la excepcion: reventar la medicion porque otra tarea
            # hizo su trabajo seria peor que contar de menos algo que ya no esta.
            continue


def rmtree_link_aware(ruta: pathlib.Path, *, limpiar_readonly: bool = False) -> int:
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

    **Devuelve los bytes que realmente borró**, medidos DENTRO del mismo
    recorrido que decidió qué borrar. Ése es el punto: quien necesita el número
    ya no puede obtenerlo por un camino con otra política de enlaces. La
    alternativa que había —medir con ``rglob`` y borrar con esta función—
    divergía en Windows, porque ``rglob`` entra a un junction y esto no; el
    caller restaba de su presupuesto bytes que seguían ocupados. Un enlace suma
    cero: se quitó el reparse point y el destino sigue entero.

    :func:`tamano_de_arbol_propio` es la mitad "medir sin borrar", para los
    ``dry_run``. Que las dos den el mismo número sobre el mismo árbol está
    anclado en ``tests/test_borrado_recursivo.py``.
    """
    if not path_present(ruta):
        return 0
    return _borrar_recursivo(ruta, limpiar_readonly=limpiar_readonly)
