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

from sky_claw.app.security.links import link_kind_and_identity_or_raise
from sky_claw.local.frozen_runtime.errors import CandidateCopyError, FrozenRuntimeStorageError
from sky_claw.local.frozen_runtime.independence import exigir_namespace_escribible
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


def _rechazar_si_es_enlace(ruta: pathlib.Path) -> None:
    tipo, _st = link_kind_and_identity_or_raise(ruta)
    if tipo is not None:
        raise CandidateCopyError(f"'{ruta}' es un enlace ({tipo}) en el source o el destino: no se sigue ni se ignora")


def copiar_archivo(origen: pathlib.Path, destino: pathlib.Path) -> int:
    """Copia el CONTENIDO de un archivo, sin compartir el objeto de filesystem."""
    origen = pathlib.Path(origen)
    destino = pathlib.Path(destino)
    # La fuente se valida justo antes de abrir: un symlink/junction inyectado
    # despues del inventario no debe poder redirigir la lectura fuera de la
    # Managed Source.
    tipo, st = link_kind_and_identity_or_raise(origen)
    if tipo is not None:
        raise CandidateCopyError(f"el origen '{origen}' es un enlace ({tipo}) en el momento de copiar")
    if st is None:
        raise CandidateCopyError(f"el origen '{origen}' desaparecio antes de copiar")
    try:
        with open(origen, "rb") as origen_fh, open(destino, "xb") as destino_fh:
            shutil.copyfileobj(origen_fh, destino_fh, _CHUNK)
            destino_fh.flush()
            os.fsync(destino_fh.fileno())
    except OSError as exc:
        raise CandidateCopyError(f"no se pudo copiar '{origen}' -> '{destino}': {exc}") from exc
    return st.st_size


def copiar_arbol_independiente(
    origen: pathlib.Path,
    destino: pathlib.Path,
    files: tuple[FileIdentity, ...],
    directories: tuple[str, ...],
) -> int:
    """Copia el arbol SELLADO de la Managed Source a un Candidate.

    Crea primero toda la estructura de directorios (incluidos los vacios, que
    son parte de la identidad P3) y despues los archivos, en orden determinista.
    Devuelve la cantidad de archivos copiados.
    """
    raiz_origen = pathlib.Path(origen)
    raiz_destino = pathlib.Path(destino)

    # Re-admision del namespace justo antes de mutar: un ancestor del destino
    # redirigido por junction entre la preparacion y ahora haria que mkdir y la
    # copia escribieran FUERA del FrozenRuntimeRoot. Se admiten UNO POR UNO en
    # vez de confiar en `parents=True`: un ancestro podria ser un enlace y
    # `parents=True` lo seguiria en silencio al crear.
    padre = raiz_destino.parent
    if not padre.exists():
        exigir_namespace_escribible(padre.parent)
        padre.mkdir(parents=False, exist_ok=False)
    exigir_namespace_escribible(padre)
    exigir_payload_vacio(raiz_destino)
    _rechazar_si_es_enlace(raiz_origen)

    try:
        raiz_destino.mkdir(parents=False, exist_ok=False)
        for directorio in directories:
            (raiz_destino / pathlib.PurePosixPath(directorio)).mkdir(parents=True, exist_ok=True)
        copiados = 0
        for entrada in sorted(files, key=lambda f: f.rel_path):
            origen_archivo = raiz_origen / pathlib.PurePosixPath(entrada.rel_path)
            destino_archivo = raiz_destino / pathlib.PurePosixPath(entrada.rel_path)
            destino_archivo.parent.mkdir(parents=True, exist_ok=True)
            copiar_archivo(origen_archivo, destino_archivo)
            copiados += 1
    except CandidateCopyError:
        raise
    except OSError as exc:
        raise CandidateCopyError(f"fallo la copia de '{raiz_origen}' -> '{raiz_destino}': {exc}") from exc
    return copiados
