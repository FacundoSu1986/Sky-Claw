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
        # Fin de la cadena: raíz del volumen (anchor) o path relativo que
        # llegó a '.' (cuyo parent es él mismo: sin este corte, loop infinito).
        if actual.anchor == str(actual) or actual.parent == actual:
            break
        actual = actual.parent
    return None


def exigir_namespace_escribible(path: pathlib.Path) -> None:
    """Fail-closed antes de ESCRIBIR: el directorio y sus ancestros sin enlaces.

    Re-admisión on-demand del namespace (P2-B1 extendido): si `state/` o el
    FrozenRuntimeRoot fueron reemplazados por un junction/symlink DESPUÉS de la
    inicialización, una escritura (mkstemp/os.replace) seguiría el link y
    escaparía del root. Los writer deben llamar esto justo antes de mutar.
    """
    ruta = pathlib.Path(path)
    motivo = descripcion_de_enlace(ruta)
    if motivo is not None:
        raise FrozenRuntimeStorageError(f"namespace de escritura redirigido: {motivo}")
    if not ruta.is_dir():
        raise FrozenRuntimeStorageError(f"namespace de escritura inválido: '{ruta}' no es un directorio")


def _normcase_abspath(path: pathlib.Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _contenida(interior: pathlib.Path, contenedor: pathlib.Path) -> bool:
    i = _normcase_abspath(interior)
    c = _normcase_abspath(contenedor)
    if i == c:
        return True
    return i.startswith(c.rstrip(os.sep) + os.sep)


def _archivos_fisicos(root: pathlib.Path, *, etiqueta: str) -> list[tuple[str, os.stat_result]]:
    """Recorre el árbol y devuelve ``(relpath, lstat)`` de sus archivos regulares.

    Rechaza fail-closed: reparse/symlink dentro del árbol
    (:class:`PhysicalReparseError`), entradas que no sean archivo regular ni
    directorio, e identidad física no disponible (st_dev/st_ino <= 0 ⇒ no se
    puede afirmar nada). Sin links en el root tampoco hay árbol que afirmar.

    Limitación declarada: hay filesystems (ReFS, unidades de red, ciertos
    archivos comprimidos) donde Windows no reporta file index (st_ino=0). En
    ellos la verificación devuelve INDETERMINATE: es la dirección segura
    (nunca un falso INDEPENDENT), pero puede bloquear operaciones legítimas;
    se registrará en el rig P7 con evidencia real.
    """
    motivo_raiz = descripcion_de_enlace(root)
    if motivo_raiz is not None:
        raise FrozenRuntimeStorageError(f"{etiqueta}: {motivo_raiz}")
    if not root.is_dir():
        raise FrozenRuntimeStorageError(f"{etiqueta}: '{root}' no es un directorio")
    archivos: list[tuple[str, os.stat_result]] = []
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
                    archivos.append((entrada.relative_to(root).as_posix(), st))
        except OSError as exc:
            raise FrozenRuntimeStorageError(f"{etiqueta}: no se pudo recorrer '{actual}': {exc}") from exc
    return archivos


def _mencion_multilink(archivos: list[tuple[str, os.stat_result]], *, limite: int = 5) -> list[str]:
    return [f"{rel} (nlink={st.st_nlink})" for rel, st in archivos if st.st_nlink > 1][:limite]


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
        archivos_gen = _archivos_fisicos(gen, etiqueta="generation")
        archivos_fuente = _archivos_fisicos(fuente, etiqueta="managed source")
    except PhysicalReparseError as exc:
        # Un reparse DENTRO de la Generation es una violación del contrato
        # SFR-18 (nuestro árbol debe ser físico y plano); en la Managed Source
        # es una condición inobservable (no es nuestra para sanear).
        estado = IndependenceState.VIOLATED if exc.etiqueta == "generation" else IndependenceState.INDETERMINATE
        return PhysicalIndependenceResult(state=estado, message=str(exc))
    except FrozenRuntimeStorageError as exc:
        return PhysicalIndependenceResult(state=IndependenceState.INDETERMINATE, message=str(exc))
    multlink = _mencion_multilink(archivos_gen)
    indice_gen = {(st.st_dev, st.st_ino): rel for rel, st in archivos_gen}
    indice_fuente = {(st.st_dev, st.st_ino): rel for rel, st in archivos_fuente}
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
    # Multi-link sin par en la fuente: el archivo comparte file object con OTRO
    # árbol (otra Generation u objeto externo) — violación por sí misma.
    if multlink:
        return PhysicalIndependenceResult(
            state=IndependenceState.VIOLATED,
            message=f"archivo(s) multi-link (hardlink) en la Generation: {', '.join(multlink)} (SFR-18)",
        )
    return PhysicalIndependenceResult(
        state=IndependenceState.INDEPENDENT,
        message="sin aliasing, sin contención, sin reparse y sin objetos de archivo compartidos",
    )


def verify_generation_physical_integrity(generation_root: pathlib.Path) -> PhysicalIndependenceResult:
    """SFR-18 como propiedad on-demand de una Generation, SIN Managed Source.

    Una Generation no es segura sólo porque su ``TreeDigest`` coincida: debe
    demostrarse fresca la integridad física de sus archivos —
    sin reparse/symlink/junction dentro del árbol y con ``st_nlink == 1`` en
    todo archivo regular (nlink>1 ⇒ comparte file object con la Managed
    Source, otra Generation u otro árbol externo). No requiere que la Managed
    Source exista: el rollback/la verificación futura no dependen de Steam.

    Veredictos: ``VIOLATED`` (reparse o multi-link observado), ``INDETERMINATE``
    (identidad física no observable/inseccionable), ``INDEPENDENT``. Nunca se
    promueve a válido por metadata: la evidencia es fresca.
    """
    gen = pathlib.Path(generation_root)
    motivo = descripcion_de_enlace(gen)
    if motivo is not None:
        return PhysicalIndependenceResult(
            state=IndependenceState.VIOLATED, message=f"root de Generation redirigido: {motivo}"
        )
    if not gen.is_dir():
        return PhysicalIndependenceResult(
            state=IndependenceState.INDETERMINATE, message=f"la Generation '{gen}' no existe o no es un directorio"
        )
    try:
        archivos = _archivos_fisicos(gen, etiqueta="generation")
    except PhysicalReparseError as exc:
        return PhysicalIndependenceResult(state=IndependenceState.VIOLATED, message=str(exc))
    except FrozenRuntimeStorageError as exc:
        return PhysicalIndependenceResult(state=IndependenceState.INDETERMINATE, message=str(exc))
    multlink = _mencion_multilink(archivos)
    if multlink:
        return PhysicalIndependenceResult(
            state=IndependenceState.VIOLATED,
            message=(
                f"archivo(s) multi-link (hardlink) dentro de la Generation: {', '.join(multlink)} "
                "(SFR-18: la Generation debe ser físicamente independiente)"
            ),
        )
    return PhysicalIndependenceResult(
        state=IndependenceState.INDEPENDENT,
        message=f"sin reparse y st_nlink==1 en {len(archivos)} archivo(s): integridad física de la Generation",
    )
