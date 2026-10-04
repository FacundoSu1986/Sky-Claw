"""Independencia física Generation ↔ Managed Source (SFR-18) — P2.

Contrato verificable que P3 usará tras su copia:

- Sin aliasing de root (mismo path resuelto).
- Sin solapamiento de contención (uno dentro del otro, en ningún sentido).
- Sin escapes por symlink/junction/reparse (root, ancestros ni árbol interno).
- Sin objetos de archivo compartidos (hardlinks): índice O(N) por
  ``(st_dev, st_ino)`` — en Windows ``st_dev`` es el número de serie del
  volumen y ``st_ino`` el índice de archivo NTFS, así que la tupla identifica
  el objeto físico. Dos paths distintos pueden compartir el MISMO objeto
  (hardlink): distinto path NO implica archivo independiente.

Resultado tipado (nunca bool opaco). Sin privilegios; sin ACL mutation.
"""

from __future__ import annotations

import os
import pathlib
import stat

from sky_claw.app.security.links import link_kind_or_raise
from sky_claw.local.frozen_runtime.errors import FrozenRuntimeStorageError
from sky_claw.local.frozen_runtime.storage_models import (
    IndependenceState,
    PhysicalIndependenceResult,
    SharedObjectEvidence,
)


class PhysicalReparseError(FrozenRuntimeStorageError):
    """Reparse point (symlink/junction) dentro de un árbol inspeccionado."""

    def __init__(self, message: str, *, etiqueta: str) -> None:
        super().__init__(message)
        self.etiqueta = etiqueta


def descripcion_de_enlace(ruta: pathlib.Path) -> str | None:
    """None si ningún componente (hasta la raíz del volumen) es enlace/reparse.

    Devuelve una descripción legible del primer componente enlazado, o del
    OSError que impidió inspeccionarlo (fail-closed). Primitive compartida
    por discovery (P1) y storage (P2).
    """
    actual = pathlib.Path(ruta)
    while True:
        try:
            kind = link_kind_or_raise(actual)
        except OSError as exc:
            return f"no se pudo inspeccionar '{actual}': {exc}"
        if kind is not None:
            return f"'{actual}' es un enlace ({kind})"
        if actual.anchor == str(actual):
            break
        actual = actual.parent
    return None


def _normcase_abspath(path: pathlib.Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _contenida(interior: pathlib.Path, contenedor: pathlib.Path) -> bool:
    i = _normcase_abspath(interior)
    c = _normcase_abspath(contenedor)
    if i == c:
        return True
    return i.startswith(c.rstrip(os.sep) + os.sep)


def _indice_fisico(root: pathlib.Path, *, etiqueta: str) -> dict[tuple[int, int], str]:
    """Índice ``(st_dev, st_ino) → relpath`` de los archivos propios del árbol.

    Rechaza fail-closed los reparse points dentro del árbol (no se sigue ni se
    ignora: el árbol deja de ser un objeto físico controlable). Sin links en
    el root tampoco hay índice que afirmar.
    """
    motivo_raiz = descripcion_de_enlace(root)
    if motivo_raiz is not None:
        raise FrozenRuntimeStorageError(f"{etiqueta}: {motivo_raiz}")
    if not root.is_dir():
        raise FrozenRuntimeStorageError(f"{etiqueta}: '{root}' no es un directorio")
    indice: dict[tuple[int, int], str] = {}
    pendientes: list[pathlib.Path] = [root]
    while pendientes:
        actual = pendientes.pop()
        try:
            with os.scandir(actual) as entries:
                for entry in entries:
                    entrada = pathlib.Path(entry.path)
                    if entry.is_symlink():
                        raise PhysicalReparseError(
                            f"{etiqueta}: reparse/symlink dentro del árbol: '{entrada}' (fail-closed)",
                            etiqueta=etiqueta,
                        )
                    # os.stat (no DirEntry.stat): en Windows DirEntry.stat()
                    # no trae el file index NTFS (st_ino=0) y el índice físico
                    # quedaría inservible para detectar hardlinks.
                    st = os.stat(entrada, follow_symlinks=False)
                    attrs = getattr(st, "st_file_attributes", 0)
                    if attrs & 0x00000400:  # FILE_ATTRIBUTE_REPARSE_POINT
                        raise PhysicalReparseError(
                            f"{etiqueta}: reparse point dentro del árbol: '{entrada}' (fail-closed)",
                            etiqueta=etiqueta,
                        )
                    if entry.is_dir(follow_symlinks=False):
                        pendientes.append(entrada)
                        continue
                    if not stat.S_ISREG(st.st_mode):
                        raise FrozenRuntimeStorageError(
                            f"{etiqueta}: entrada inesperada (no archivo regular ni directorio): '{entrada}'"
                        )
                    if st.st_dev <= 0 or st.st_ino <= 0:
                        raise FrozenRuntimeStorageError(
                            f"{etiqueta}: identidad física no disponible (st_dev={st.st_dev}, st_ino={st.st_ino}) "
                            f"en '{entrada}': no se puede afirmar independencia (fail-closed)"
                        )
                    rel = entrada.relative_to(root).as_posix()
                    indice[(st.st_dev, st.st_ino)] = rel
        except OSError as exc:
            if isinstance(exc, FrozenRuntimeStorageError):
                raise
            raise FrozenRuntimeStorageError(f"{etiqueta}: no se pudo recorrer '{actual}': {exc}") from exc
    return indice


def verify_generation_independence(
    generation_root: pathlib.Path,
    managed_source_root: pathlib.Path,
) -> PhysicalIndependenceResult:
    """Verifica que una Generation no comparta objetos mutables con la Managed Source.

    Es la primitive que P3 ejecutará contra su copia publicada. Fail-closed:
    cualquier condición inobservable es INDETERMINATE, nunca INDEPENDENT.
    """
    gen = pathlib.Path(generation_root)
    fuente = pathlib.Path(managed_source_root)
    if not fuente.is_dir():
        return PhysicalIndependenceResult(
            state=IndependenceState.INDETERMINATE,
            message=f"la Managed Source '{fuente}' no existe o no es un directorio: no hay comparación que afirmar",
        )
    if _contenida(gen, fuente):
        return PhysicalIndependenceResult(
            state=IndependenceState.VIOLATED,
            message=f"la Generation está dentro de (o es) la Managed Source: '{gen}'",
        )
    if _contenida(fuente, gen):
        return PhysicalIndependenceResult(
            state=IndependenceState.VIOLATED,
            message=f"la Managed Source está dentro de la Generation: '{fuente}'",
        )
    motivo = descripcion_de_enlace(gen)
    if motivo is not None:
        return PhysicalIndependenceResult(
            state=IndependenceState.VIOLATED, message=f"root de Generation redirigido: {motivo}"
        )
    try:
        indice_gen = _indice_fisico(gen, etiqueta="generation")
        indice_fuente = _indice_fisico(fuente, etiqueta="managed source")
    except PhysicalReparseError as exc:
        # Un reparse DENTRO de la Generation es una violación del contrato
        # SFR-18 (nuestro árbol debe ser físico y plano); en la Managed Source
        # es una condición inobservable (no es nuestra para sanear).
        estado = IndependenceState.VIOLATED if exc.etiqueta == "generation" else IndependenceState.INDETERMINATE
        return PhysicalIndependenceResult(state=estado, message=str(exc))
    except FrozenRuntimeStorageError as exc:
        return PhysicalIndependenceResult(state=IndependenceState.INDETERMINATE, message=str(exc))
    compartidos: list[SharedObjectEvidence] = []
    for clave, rel_gen in indice_gen.items():
        rel_fuente = indice_fuente.get(clave)
        if rel_fuente is not None:
            volumen, file_index = clave
            compartidos.append(
                SharedObjectEvidence(
                    rel_path_generation=rel_gen,
                    rel_path_source=rel_fuente,
                    volume_serial=volumen,
                    file_index=file_index,
                )
            )
    if compartidos:
        return PhysicalIndependenceResult(
            state=IndependenceState.VIOLATED,
            message=(
                f"{len(compartidos)} objeto(s) de archivo compartido(s) entre Generation y Managed Source "
                "(hardlink): la mutación del proveedor propagaría a la Generation (SFR-18)"
            ),
            shared_objects=tuple(sorted(compartidos, key=lambda e: (e.rel_path_generation, e.rel_path_source))),
        )
    return PhysicalIndependenceResult(
        state=IndependenceState.INDEPENDENT,
        message="sin aliasing, sin contención, sin reparse y sin objetos de archivo compartidos",
    )
