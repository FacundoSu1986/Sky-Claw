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


def _exigir_contencion_del_padre_destino(contenedor: pathlib.Path, padre: pathlib.Path) -> None:
    """Revalida, JUSTO antes de escribir, que el padre del destino siga DENTRO.

    La contencion se verificaba UNA sola vez en la raiz del payload. Un junction
    que reemplace un directorio NIDO (`payload/Data`) despues de ese chequeo hace
    que `mkdir(exist_ok=True)` acepte el directorio redirigido y que el `open`
    destino escriba fuera del ``FrozenRuntimeRoot``. Revalidar por archivo acota
    la ventana al tramo ``lstat -> open``, que es lo maximo que se puede cerrar
    sin un handle al directorio padre.

    ``HANDLE_GRADE_DESTINATION = NO``: esto es best-effort fail-closed, no una
    proteccion atomica contra un swap hostil concurrente. El lock cross-process
    es P4.
    """
    try:
        exigir_contencion_fisica(contenedor, padre, exigir_existencia=True)
    except ContencionFisicaVioladaError as exc:
        raise CandidateCopyError(
            f"el padre del destino '{padre}' no cuelga fisicamente de '{contenedor}' "
            f"(posible redireccion por enlace): {exc}"
        ) from exc


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
    """
    try:
        exigir_contencion_fisica(raiz_origen, archivo.parent, permitir_raiz=True, exigir_existencia=True)
    except ContencionFisicaVioladaError as exc:
        raise CandidateCopyError(
            f"un ancestro del origen '{archivo}' no cuelga fisicamente de '{raiz_origen}' "
            f"(posible redireccion por enlace): {exc}"
        ) from exc


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
    ``lstat`` de cada ancestro -- no ``resolve``, que seguiria el enlace -- antes
    de crear nada y otra vez DESPUES del ``mkdir`` del payload. La segunda
    comprobacion es la que cierra la ventana que Qodo senalo sobre #682: entre la
    validacion del padre y el ``mkdir`` un junction podia reemplazarlo, y sin esa
    re-verificacion la copia escribia TODOS los bytes fuera del root. Con ella, el
    caso se detecta antes de escribir un solo byte de contenido.

    Garantia honesta (no se afirma mas de lo que hay): esto NO es proteccion
    HANDLE-grade -- no hay un handle al directorio padre que impida el swap
    atomicamente -- pero la mutacion fuera del root queda acotada a la creacion
    del directorio del payload y se detecta fail-closed antes del contenido.
    """
    raiz_origen = pathlib.Path(origen)
    raiz_destino = pathlib.Path(destino)
    raiz_contenedora = pathlib.Path(contenedor)

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
    pares = tuple(zip(archivos_ordenados, canonicos_archivos, strict=True))

    # 1) ANTES de crear nada: el padre debe colgar fisicamente del root.
    padre = raiz_destino.parent
    if not padre.exists():
        try:
            exigir_contencion_fisica(raiz_contenedora, padre.parent, permitir_raiz=True)
        except ContencionFisicaVioladaError as exc:
            raise CandidateCopyError(
                f"el ancestro del destino '{padre.parent}' no cuelga fisicamente de '{raiz_contenedora}': {exc}"
            ) from exc
        exigir_namespace_escribible(padre.parent)
        try:
            padre.mkdir(parents=False, exist_ok=False)
        except OSError as exc:
            raise CandidateCopyError(f"no se pudo crear el directorio del Candidate '{padre}': {exc}") from exc
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
            (raiz_destino / pathlib.PurePosixPath(directorio)).mkdir(parents=True, exist_ok=True)
        copiados = 0
        for _entrada, rel_path in pares:
            # `rel_path` ya es canonico: relativo y sin `..`, unirlo no sale del payload.
            origen_archivo = raiz_origen / pathlib.PurePosixPath(rel_path)
            destino_archivo = raiz_destino / pathlib.PurePosixPath(rel_path)
            destino_archivo.parent.mkdir(parents=True, exist_ok=True)
            # P3-P / P3-R: revalidar AMBOS lados justo antes de tocar el disco. La
            # contencion del payload se chequeo una sola vez, mucho antes de que
            # este loop llegue a un directorio nido; y el probe de fuente miraba
            # solo el leaf, ciego a un ancestro redirigido.
            _exigir_contencion_del_padre_destino(raiz_contenedora, destino_archivo.parent)
            _exigir_ancestros_del_source(raiz_origen, origen_archivo)
            copiar_archivo(origen_archivo, destino_archivo)
            copiados += 1
    except CandidateCopyError:
        raise
    except OSError as exc:
        raise CandidateCopyError(f"fallo la copia de '{raiz_origen}' -> '{raiz_destino}': {exc}") from exc
    return copiados
