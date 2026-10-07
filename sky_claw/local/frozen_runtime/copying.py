"""Copia fisica independiente de la Managed Source hacia un Candidate (P3).

Es el UNICO modulo del paquete con escritura de contenido, y escribe SOLO
dentro del ``FrozenRuntimeRoot`` propio (nunca sobre la Managed Source).

Decisiones y su razon:

- **Copia por enumeracion SELLADA de PRE, no por rediscovering.** La copia
  consume exactamente el conjunto de archivos y el de directorios que la
  evidencia PRE autorizo a copiar. Rediscovering "lo que hay ahora" abriria una
  ventana en la que un archivo nuevo podria colarse en el Candidate sin que la
  evidencia PRE lo hubiera autorizado. Con la enumeracion sellada, un archivo
  que aparece durante la copia NO se copia y el POST lo detecta como drift de
  la fuente; un archivo que desaparece hace fallar la copia misma.

- **Copia de CONTENIDO, no de objeto.** Nada de ``os.link`` (hardlink),
  ``reflink`` ni alias: el Candidate debe ser un objeto de filesystem
  independiente, y la independencia se verifica despues con ``st_nlink == 1``.

- **Cross-volume por construccion.** Se escribe con ``open()`` +
  ``copyfileobj`` en vez de un primitive que asuma mismo volumen: la Managed
  Source puede estar en otra unidad y un rename/reflink alli no seria valido.

- **Sin borrado destructivo.** Si algo falla, el Candidate queda marcado
  INVALID con su motivo; P3 no introduce garbage collection.

- **Re-admision del destino.** El destino y sus ancestros se re-admiten
  (``exigir_namespace_escribible``) justo ANTES de mutar: si ``candidates/<id>``
  o ``payload/`` fueron reemplazados por un junction entre la preparacion y la
  escritura, la escritura seguiria el enlace y escaparia del root.
"""

from __future__ import annotations

import bisect
import os
import pathlib
import shutil
from typing import Final

from sky_claw.app.security.links import (
    ContencionFisicaVioladaError,
    exigir_contencion_fisica,
    link_kind_and_identity_or_raise,
    same_file_identity,
)
from sky_claw.local.frozen_runtime.errors import CandidateCopyError, FrozenRuntimeStorageError
from sky_claw.local.frozen_runtime.independence import exigir_namespace_escribible
from sky_claw.local.frozen_runtime.membership import (
    DirectoryMembershipError,
    canonicalizar_relpath_de_scope,
)
from sky_claw.local.runtime_vault.models import FileIdentity

#: Chunk de copia (los arboles verificados llegan a decenas de GB).
_CHUNK: Final[int] = 1024 * 1024

PAYLOAD_DIR_NAME: Final[str] = "payload"


def payload_dir(candidate_root: pathlib.Path) -> pathlib.Path:
    """Directorio de contenido del Candidate (lo que se sella y se compara)."""
    return pathlib.Path(candidate_root) / PAYLOAD_DIR_NAME


def exigir_payload_vacio(destino: pathlib.Path) -> None:
    """Fail-closed: el destino de la copia no existe o esta vacio.

    Nunca se sobrescribe ni se mezcla con contenido previo: un Candidate cuyo
    payload ya tiene archivos podria "heredar" bytes de otra corrida y sus
    digest no dirian nada sobre esta copia.
    """
    raiz = pathlib.Path(destino)
    if raiz.exists():
        raise CandidateCopyError(f"el destino de copia '{raiz}' ya existe: no se sobrescribe (fail-closed)")
    padre = raiz.parent
    if not padre.is_dir():
        raise FrozenRuntimeStorageError(f"el padre del destino '{padre}' no existe o no es un directorio")


def _clasificar_enlace(ruta: pathlib.Path) -> tuple[str | None, os.stat_result | None]:
    """Clasifica un enlace traduciendo el fallo de INSPECCION a la familia tipada.

    ``link_kind_and_identity_or_raise`` solo traduce ``FileNotFoundError``: un
    ``OSError`` transitorio (permiso, sharing, volumen desconectado) salia crudo
    de ``copiar_arbol_independiente`` -- fuera de sus handlers -- y llegaba hasta
    ``crear_candidate`` dejando metadata BUILDING huerfana en vez de un resultado
    tipado. La traduccion vive aca, no en cada caller (Codex sobre #682).
    """
    try:
        return link_kind_and_identity_or_raise(ruta)
    except OSError as exc:
        raise CandidateCopyError(f"no se pudo inspeccionar '{ruta}': {exc}") from exc


def _rechazar_si_es_enlace(ruta: pathlib.Path) -> None:
    tipo, _st = _clasificar_enlace(ruta)
    if tipo is not None:
        raise CandidateCopyError(f"'{ruta}' es un enlace ({tipo}) en el source o el destino: no se sigue ni se ignora")


def copiar_archivo(origen: pathlib.Path, destino: pathlib.Path) -> int:
    """Copia el CONTENIDO de un archivo, sin compartir el objeto de filesystem."""
    origen = pathlib.Path(origen)
    destino = pathlib.Path(destino)
    # La fuente se valida justo antes de abrir: un symlink/junction inyectado
    # despues del inventario no debe poder redirigir la lectura fuera de la
    # Managed Source.
    tipo, st = _clasificar_enlace(origen)
    if tipo is not None:
        raise CandidateCopyError(f"el origen '{origen}' es un enlace ({tipo}) en el momento de copiar")
    if st is None:
        raise CandidateCopyError(f"el origen '{origen}' desaparecio antes de copiar")
    try:
        with open(origen, "rb") as origen_fh:
            # Identidad DESPUES de abrir (P3-R): si un ANCESTRO del source fue
            # redirigido entre el `lstat` de arriba y el `open`, el handle apunta
            # a un archivo distinto -- posiblemente fuera de la Managed Source --
            # y esto lo detecta ANTES de copiar un solo byte. Cierra el tramo
            # inspeccion->apertura, que es lo maximo sin open relativo a handle.
            if not same_file_identity(st, os.fstat(origen_fh.fileno())):
                raise CandidateCopyError(
                    f"la identidad de '{origen}' cambio entre la inspeccion y la apertura: no se copia"
                )
            with open(destino, "xb") as destino_fh:
                shutil.copyfileobj(origen_fh, destino_fh, _CHUNK)
                destino_fh.flush()
                os.fsync(destino_fh.fileno())
    except OSError as exc:
        raise CandidateCopyError(f"no se pudo copiar '{origen}' -> '{destino}': {exc}") from exc
    return st.st_size


def _canonicalizar_lote(entradas: tuple[str, ...], *, tipo: str) -> tuple[str, ...]:
    """Canonicaliza el LOTE completo de relpaths de una vez.

    Se usa la MISMA primitive que la evidencia de membership de P3
    (``canonicalizar_relpath_de_scope``): si la copia repitiera estas reglas por su
    cuenta, un patron aceptado por la evidencia seria rechazado por la copia (o al
    reves), y esa diferencia seria justamente el hueco del traversal.

    Al hacerlo por lotes y antes de mutar, una entrada hostil hace fallar la
    operacion completa sin escritura parcial previa.
    """
    canonicos: list[str] = []
    for entrada in entradas:
        try:
            canonicos.append(canonicalizar_relpath_de_scope(entrada, tipo=tipo))
        except DirectoryMembershipError as exc:
            raise CandidateCopyError(f"relpath de {tipo} rechazado antes de copiar: {exc} (fail-closed)") from exc
    return tuple(canonicos)


def _exigir_contencion_destino(contenedor: pathlib.Path, ruta: pathlib.Path) -> None:
    """Revalida que *ruta* siga DENTRO del contenedor y sin enlaces intermedios.

    La contencion se verificaba UNA sola vez en la raiz del payload. Un junction
    que reemplace un directorio NIDO (`payload/Data`) despues de ese chequeo hace
    que cualquier mutacion posterior -- `mkdir`, `open` -- opere fuera del
    ``FrozenRuntimeRoot``. Se llama antes de cada mutacion y tambien sobre el
    resultado recien creado.

    ``HANDLE_GRADE_DESTINATION = NO``: esto es best-effort fail-closed, no una
    proteccion atomica contra un swap hostil concurrente. El lock cross-process
    es P4.
    """
    try:
        exigir_contencion_fisica(contenedor, ruta, exigir_existencia=True)
    except ContencionFisicaVioladaError as exc:
        raise CandidateCopyError(
            f"la ruta destino '{ruta}' no cuelga fisicamente de '{contenedor}' (posible redireccion por enlace): {exc}"
        ) from exc


def _materializar_directorio_del_destino(
    contenedor: pathlib.Path,
    raiz_destino: pathlib.Path,
    rel_dir: pathlib.PurePosixPath,
) -> pathlib.Path:
    """Materializa `rel_dir` componente por componente, revalidando en cada paso.

    El loop de directorios hacia un unico ``mkdir(parents=True, exist_ok=True)``, y
    eso SIGUE un prefijo ya creado que haya sido reemplazado por un symlink o
    junction: los niveles siguientes se creaban fuera del ``FrozenRuntimeRoot``. El
    guard por archivo (P3-P) no cubre ese tramo -- corre DESPUES de toda la
    materializacion -- y un directorio VACIO sellado (que P3 conserva a proposito)
    no tiene ningun archivo posterior que lo dispare, asi que el escape quedaba
    invisible (Codex sobre #682, P3-T).

    Cada paso hace: verificar el parent -> crear el hijo si falta -> verificar el
    hijo. Un directorio EXISTENTE se revalida antes de usarlo como parent del nivel
    siguiente, porque pudo ser reemplazado desde la observacion anterior.

    **La raiz del payload se revalida SIEMPRE, aunque `rel_dir` no tenga
    componentes.** Esa validacion vivia solo DENTRO del loop, y el padre de un
    archivo en la raiz del payload es `.` (`PurePosixPath(".").parts == ()`): el
    loop hacia CERO iteraciones, el guard NUNCA corria y el `open(..., "xb")`
    siguiente escribia en un payload que podia haber sido reemplazado por un
    junction -- sin error, y con los bytes FUERA del ``FrozenRuntimeRoot``.
    Verificacion ausente en un nivel, no una ventana residual (P4): P3-T/P3-P
    prometen revalidar antes de CADA mutacion, y para la raiz no habia ninguna.
    """
    actual = pathlib.Path(raiz_destino)
    # Incondicional y ANTES del loop: cubre el caso `parts == ()` y deja el loop
    # intacto (su revalidacion de `actual` en la primera iteracion queda
    # redundante a proposito -- un lstat extra por nivel es despreciable frente a
    # reescribir un loop que P3-P/P3-T ya cubren con sus propios tests).
    _exigir_contencion_destino(contenedor, actual)
    for componente in rel_dir.parts:
        _exigir_contencion_destino(contenedor, actual)
        hijo = actual / componente
        if not hijo.is_dir():
            try:
                hijo.mkdir()
            except OSError as exc:
                raise CandidateCopyError(f"no se pudo crear el directorio '{hijo}': {exc}") from exc
        _exigir_contencion_destino(contenedor, hijo)
        actual = hijo
    return actual


def _exigir_ancestros_del_source(raiz_origen: pathlib.Path, archivo: pathlib.Path) -> None:
    """Revalida la cadena de ANCESTROS del source antes de leer el archivo.

    El probe de fuente miraba SOLO el ultimo componente: ``lstat`` no sigue al
    leaf, pero SI sigue a los ancestros, asi que con ``Source/Data`` reemplazado
    por un junction a ``External/`` el probe veia ``External/a.bin`` como un
    archivo REGULAR de la Managed Source y copiaba bytes externos al payload.

    ``HANDLE_GRADE_SOURCE_TRAVERSAL = NO``: no hay ``open`` relativo a handle, asi
    que la garantia es la cadena validada + la identidad re-verificada DESPUES de
    abrir (ver :func:`copiar_archivo`).

    ``permitir_raiz=True`` porque un archivo en la RAIZ del source (``SkyrimSE.exe``)
    tiene como padre al propio root administrado, que es la frontera legitima y no
    un escape: la cadena a demostrar es "root -> ... -> padre", y el root ya es
    parte de ella.

    P3-AB: ``exigir_contencion_fisica`` con ``permitir_raiz=True`` retorna por
    igualdad cuando ``archivo.parent == raiz_origen`` sin inspeccionar el root.
    Revalidar fisicamente ``raiz_origen`` aqui garantiza que un junction que
    reemplace el root de la fuente no escape a la comprobacion de ancestros.
    """
    _rechazar_si_es_enlace(raiz_origen)
    try:
        exigir_contencion_fisica(raiz_origen, archivo.parent, permitir_raiz=True, exigir_existencia=True)
    except ContencionFisicaVioladaError as exc:
        raise CandidateCopyError(
            f"un ancestro del origen '{archivo}' no cuelga fisicamente de '{raiz_origen}' "
            f"(posible redireccion por enlace): {exc}"
        ) from exc


def _rechazar_solapamiento_origen_destino(raiz_origen: pathlib.Path, raiz_destino: pathlib.Path) -> None:
    """P3-W: source tree ∩ destination tree = ∅, ANTES de la primera mutacion.

    ``copiar_arbol_independiente`` es una primitive PUBLICA y reutilizable: la
    frontera no puede vivir en ``crear_candidate`` (que ya valida la suya),
    porque un caller directo puede pasar origen = contenedor = Managed Source
    con destino = ``<source>/candidates/<id>/payload``. Los guards actuales
    exigen ``destino ∈ contenedor`` y eso PASA, asi que la copia ejecutaba
    ``mkdir``/``open("xb")`` DENTRO de la Managed Source (MANAGED_SOURCE_WRITES=NO).

    Se decide con :func:`exigir_contencion_fisica` en AMBAS direcciones con
    ``permitir_raiz=True``: es la primitive canonica de contencion del repo
    (``lstat`` componente a componente, NUNCA ``resolve`` — que seguiria un
    enlace antes de clasificarlo), asi que no se crea un detector paralelo.

    * Si ``destino`` cuelga del ``origen`` (o es el ``origen``), hay solape.
    * Si ``origen`` cuelga del ``destino`` (o es el ``destino``), tambien: un
      arbol copiado hacia un ancestro suyo es self-copy que muta el namespace
      del source mientras lo lee.
    * ``ContencionFisicaVioladaError`` en una direccion significa "no cuelga"
      — o "la cadena contiene un enlace", que los guards de contencion de la
      copia rechazan despues de todas formas — y se pasa a la otra direccion.

    Igualdad y contencion en cualquier direccion se rechazan AQUI, sin haber
    creado un solo directorio ni abierto un solo archivo.
    """
    try:
        exigir_contencion_fisica(raiz_origen, raiz_destino, permitir_raiz=True)
    except ContencionFisicaVioladaError:
        pass  # el destino NO cuelga del origen: sin solape en esta direccion
    else:
        raise CandidateCopyError(
            f"el destino '{raiz_destino}' esta dentro del origen '{raiz_origen}' (o es el origen): "
            "la copia exige arboles disjuntos (MANAGED_SOURCE_WRITES=NO)"
        ) from None
    try:
        exigir_contencion_fisica(raiz_destino, raiz_origen, permitir_raiz=True)
    except ContencionFisicaVioladaError:
        return  # el origen tampoco cuelga del destino: arboles disjuntos
    raise CandidateCopyError(
        f"el origen '{raiz_origen}' esta dentro del destino '{raiz_destino}' (o es el destino): "
        "la copia exige arboles disjuntos (MANAGED_SOURCE_WRITES=NO)"
    ) from None


def _validar_coherencia_del_lote(
    directorios: tuple[str, ...],
    archivos: tuple[str, ...],
) -> None:
    """P3-AE: Valida la coherencia estructural de todo el lote antes de mutar.

    Rechaza ANTES de crear un solo directorio o archivo:
    - Duplicados canonicos entre archivos (ej. 'Data/a.bin' y 'Data/./a.bin').
    - Duplicados canonicos entre directorios (ej. 'Data/Meshes' y 'Data/./Meshes').
    - Colision exacta entre un archivo y un directorio con la misma ruta.
    - Un archivo que sea ancestro de otro archivo o de un directorio
      (ej. archivo 'Data/Foo' y archivo 'Data/Foo/bar.bin', o directorio 'Data/Foo/Bar').

    Acepta:
    - Jerarquias legitimas de directorios ('Data', 'Data/Meshes', 'Data/Meshes/Armor').
    - Archivos hermanos en el mismo directorio.

    Costo (finding post-merge F1). La version original comparaba cada archivo
    contra TODOS los demas archivos y contra todos los directorios:
    ``N² + N×D`` comparaciones de prefijo. El scan corre ANTES de la primera
    mutacion, asi que en un arbol de modding real (decenas de miles de archivos)
    el preflight dominaba el costo de la operacion entera: medido con entradas
    sinteticas, 20k archivos / 2k directorios tardaban ~21.7 s.

    El veredicto es IDENTICO, pero el chequeo de ancestro pasa a apoyarse en una
    propiedad del orden lexicografico: los strings que comparten un prefijo son
    CONTIGUOS, asi que el primer elemento ``>= f + "/"`` (``bisect_left``) es el
    primero con ese prefijo si es que existe alguno. Queda
    ``O((N+D) log (N+D))`` en vez de cuadratico, sin trie ni estructura nueva.
    """
    vistos_archivos_cf: set[str] = set()
    for a in archivos:
        cf = a.casefold()
        if cf in vistos_archivos_cf:
            raise CandidateCopyError(f"lote con archivos canonicos duplicados: '{a}' (fail-closed)")
        vistos_archivos_cf.add(cf)

    vistos_dirs_cf: set[str] = set()
    for d in directorios:
        cf = d.casefold()
        if cf in vistos_dirs_cf:
            raise CandidateCopyError(f"lote con directorios canonicos duplicados: '{d}' (fail-closed)")
        vistos_dirs_cf.add(cf)

    colisiones = vistos_archivos_cf & vistos_dirs_cf
    if colisiones:
        raise CandidateCopyError(
            f"colision entre archivo y directorio con la misma ruta canonica: {sorted(colisiones)} (fail-closed)"
        )

    # Un ARCHIVO no puede contener nada: si su ruta canonica es prefijo de otra
    # (con separador), el lote es incoherente. Se conserva el texto original de
    # cada ruta para el mensaje, y se distingue archivo-ancestro-de-archivo de
    # archivo-ancestro-de-directorio consultando a que conjunto pertenece el
    # elemento encontrado.
    originales: dict[str, str] = {}
    for a in archivos:
        originales.setdefault(a.casefold(), a)
    for d in directorios:
        originales.setdefault(d.casefold(), d)

    todos_ordenados = sorted(vistos_archivos_cf | vistos_dirs_cf)
    for a in archivos:
        prefijo = a.casefold() + "/"
        indice = bisect.bisect_left(todos_ordenados, prefijo)
        if indice >= len(todos_ordenados):
            continue
        canonico = todos_ordenados[indice]
        if not canonico.startswith(prefijo):
            continue
        if canonico in vistos_archivos_cf:
            raise CandidateCopyError(
                f"el archivo '{a}' no puede ser ancestro del archivo '{originales[canonico]}' (fail-closed)"
            )
        raise CandidateCopyError(
            f"el archivo '{a}' no puede ser ancestro del directorio '{originales[canonico]}' (fail-closed)"
        )


def copiar_arbol_independiente(
    origen: pathlib.Path,
    destino: pathlib.Path,
    files: tuple[FileIdentity, ...],
    directories: tuple[str, ...],
    *,
    contenedor: pathlib.Path,
) -> int:
    """Copia el arbol SELLADO de la Managed Source a un Candidate.

    Crea primero toda la estructura de directorios (incluidos los vacios, que
    son parte de la identidad P3) y despues los archivos, en orden determinista.
    Devuelve la cantidad de archivos copiados.

    ``contenedor`` es el ``FrozenRuntimeRoot`` del que el destino NO puede
    escaparse. Se verifica con :func:`exigir_contencion_fisica`, que hace
    ``lstat`` de cada ancestro -- no ``resolve``, que seguiria el enlace.

    Cuatro fronteras, todas fail-closed:

    0. el SOLAPE origen/destino (P3-W): un caller directo de esta primitive
       PUBLICA puede pasar origen = contenedor = Managed Source con el destino
       dentro de el; como los guards exigen ``destino ∈ contenedor`` y eso
       PASA, sin este guard la copia ejecutaria ``mkdir``/``open("xb")``
       DENTRO de la Managed Source. Se decide ANTES de la primera mutacion.
    * la raiz del payload, antes y despues de su ``mkdir`` (cierra la ventana que
      Qodo senalo sobre #682: entre la validacion del padre y el ``mkdir`` un
      junction podia reemplazarlo);
    * la MATERIALIZACION de directorios, componente por componente, con la
      contencion revalidada antes de crear cada nivel y sobre el nivel creado
      (P3-T: un solo ``mkdir(parents=True)`` seguia un prefijo redirigido y creaba
      los niveles siguientes fuera del root; un directorio VACIO sellado no tiene
      ningun archivo posterior que dispare el guard de archivo);
    * cada ESCRITURA de archivo, con el padre revalidado inmediatamente antes del
      ``open`` y la cadena de ancestros del source validada (P3-P / P3-R), mas la
      identidad del source re-verificada despues de abrir.

    Garantia honesta (no se afirma mas de lo que hay): esto NO es proteccion
    HANDLE-grade -- no hay un handle al directorio padre que impida el swap
    atomicamente -- la revalidacion es best-effort fail-closed y la ventana
    residual entre la verificacion y la mutacion la cierra el lock cross-process,
    que es P4 (``HANDLE_GRADE_DESTINATION = NO``).
    """
    raiz_origen = pathlib.Path(origen)
    raiz_destino = pathlib.Path(destino)
    raiz_contenedora = pathlib.Path(contenedor)

    # ── P3-W) SOLAPE ORIGEN/DESTINO, ANTES DE LA PRIMERA MUTACION ──────────
    # La frontera source ∩ destination = ∅ vive DENTRO de la primitive, no en
    # el caller: un guard de `crear_candidate` no protege a los callers
    # directos de esta primitive PUBLICA (el ataque del finding usaba
    # contenedor == Managed Source y destino dentro de el).
    _rechazar_solapamiento_origen_destino(raiz_origen, raiz_destino)

    # Orden determinista ANTES de canonicalizar, para que cada `FileIdentity` quede
    # emparejada con SU relpath canonico: canonicalizar y ordenar por separado
    # desalinearia el par y escribiria un archivo con la ruta de otro.
    archivos_ordenados = sorted(files, key=lambda f: f.rel_path)

    # ── 0) VALIDAR TODO EL LOTE ANTES DE LA PRIMERA MUTACION ──────────────
    # `copiar_arbol_independiente` es una primitive reusable: no puede asumir que
    # la evidencia que recibe sea perfecta, y unir un `rel_path` crudo a una raiz
    # es peligroso de tres formas en Windows: `../../x` sale del payload,
    # `C:/x` RESETEA LA UNIDAD y `/x` reinicia en la raiz del volumen.
    #
    # La validacion es de LOTO y ocurre antes de crear un solo directorio: si se
    # canonicalizara entrada por entrada mientras se copia, una entrada hostil que
    # llegue al final dejaria las sanas ya escritas en disco (mutacion parcial).
    canonicos_directorios = _canonicalizar_lote(directories, tipo="directorio")
    canonicos_archivos = _canonicalizar_lote(
        tuple(entrada.rel_path for entrada in archivos_ordenados),
        tipo="archivo",
    )
    # P3-AE: Validar la coherencia estructural de todo el lote antes de mutar
    _validar_coherencia_del_lote(canonicos_directorios, canonicos_archivos)
    pares = tuple(zip(archivos_ordenados, canonicos_archivos, strict=True))

    # 1) ANTES de crear nada: el padre debe colgar fisicamente del root, exista o no.
    # P3-AD: si el padre ya existe fuera del contenedor, debe rechazarse antes de
    # crear el payload; no asumir contencion por el hecho de que ya exista.
    padre = raiz_destino.parent
    try:
        exigir_contencion_fisica(
            raiz_contenedora,
            padre,
            permitir_raiz=True,
            exigir_existencia=padre.exists(),
        )
    except ContencionFisicaVioladaError as exc:
        raise CandidateCopyError(
            f"el padre del destino '{padre}' no cuelga fisicamente de '{raiz_contenedora}': {exc}"
        ) from exc

    if not padre.exists():
        exigir_namespace_escribible(padre.parent)
        try:
            padre.mkdir(parents=False, exist_ok=False)
        except OSError as exc:
            raise CandidateCopyError(f"no se pudo crear el directorio del Candidate '{padre}': {exc}") from exc
        try:
            exigir_contencion_fisica(raiz_contenedora, padre, permitir_raiz=True, exigir_existencia=True)
        except ContencionFisicaVioladaError as exc:
            raise CandidateCopyError(
                f"el directorio del Candidate recien creado '{padre}' no cuelga fisicamente de '{raiz_contenedora}': {exc}"
            ) from exc
    exigir_namespace_escribible(padre)
    exigir_payload_vacio(raiz_destino)
    _rechazar_si_es_enlace(raiz_origen)

    try:
        raiz_destino.mkdir(parents=False, exist_ok=False)
    except OSError as exc:
        raise CandidateCopyError(f"no se pudo crear el payload '{raiz_destino}': {exc}") from exc

    # 2) DESPUES del mkdir y ANTES de escribir contenido: re-verificar que el
    #    arbol recien creado cuelga fisicamente del root. Si `padre` fue
    #    reemplazado por un junction en la ventana, esto lo detecta aca.
    try:
        exigir_contencion_fisica(raiz_contenedora, raiz_destino, exigir_existencia=True)
    except ContencionFisicaVioladaError as exc:
        raise CandidateCopyError(
            f"el payload '{raiz_destino}' no cuelga fisicamente de '{raiz_contenedora}' "
            f"(posible redireccion por enlace): {exc}"
        ) from exc

    try:
        for directorio in canonicos_directorios:
            # P3-T: componente por componente. Un solo `mkdir(parents=True)` sigue un
            # prefijo que haya sido reemplazado por un junction y crea los niveles
            # siguientes fuera del root; un directorio VACIO sellado no tiene ningun
            # archivo posterior que dispare el guard de P3-P.
            _materializar_directorio_del_destino(raiz_contenedora, raiz_destino, pathlib.PurePosixPath(directorio))
        copiados = 0
        for _entrada, rel_path in pares:
            # `rel_path` ya es canonico: relativo y sin `..`, unirlo no sale del payload.
            origen_archivo = raiz_origen / pathlib.PurePosixPath(rel_path)
            destino_archivo = raiz_destino / pathlib.PurePosixPath(rel_path)
            # P3-T / P3-P / P3-R: materializar el padre con la contencion revalidada en
            # cada componente (P3-T), y cerrar con la verificacion del parent del
            # destino inmediatamente antes de la escritura (P3-P) mas la cadena de
            # ancestros del source (P3-R).
            _materializar_directorio_del_destino(raiz_contenedora, raiz_destino, pathlib.PurePosixPath(rel_path).parent)
            _exigir_ancestros_del_source(raiz_origen, origen_archivo)
            copiar_archivo(origen_archivo, destino_archivo)
            copiados += 1
    except CandidateCopyError:
        raise
    except OSError as exc:
        raise CandidateCopyError(f"fallo la copia de '{raiz_origen}' -> '{raiz_destino}': {exc}") from exc
    return copiados
