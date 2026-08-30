"""Identidad durable de un árbol empaquetado (D2, PR #493).

La autoridad del handoff de deployment es el artifact FINAL —``mods/TexGen
Output/textures``— nunca el staging crudo. Este módulo produce el digest de ese
árbol: completo (sin sampling), determinista, y con un recorrido link-aware que
no atraviesa junctions ni symlinks (la misma política de medición que
``texgen_visibility`` — el ancla de ``tests/test_borrado_recursivo.py`` congela
que ambos módulos miden link-aware).

El digest distingue:

- archivo renombrado (el relpath canónico entra al digest);
- bytes redistribuidos entre archivos (el contenido entra por archivo);
- artifact reemplazado o de otra corrida (todo el árbol entra);
- archivos de más/menos (el conteo y los bytes totales van en el registro).

**F-003**: TODO archivo regular propio dentro del scope entra al digest, sin
excepción por nombre. El ``meta.ini`` que MO2 administra vive en
``mods/TexGen Output/`` — HERMANO del subárbol ``textures/`` — así que queda
fuera POR ESTRUCTURA; un ``meta.ini`` dentro de ``textures/`` es propio del
artifact y se incluye.

**F-004**: un enlace (symlink/junction/reparse-point) dentro del subtree
autorizado NO se sigue NI se ignora: el árbol deja de ser autorizable y el
digest FALLA CERRADO. La detección es local a este módulo (no cambia la
semántica global de ``iter_archivos_propios``) y es **best-effort ante
mutación concurrente del filesystem**: se inspecciona entrada por entrada sin
handles ``O_NOFOLLOW``, así que no se promete una garantía TOCTOU-fuerte — una
carrera que reemplace un archivo regular por un enlace entre la inspección y
la apertura no está cubierta por este diseño.
"""

from __future__ import annotations

import hashlib
import os
import pathlib
import stat
from dataclasses import dataclass

from sky_claw.app.security.links import iter_archivos_propios

#: Chunk de lectura del digest (mismo criterio que ``texgen_visibility``: los
#: .dds de LOD llegan a decenas de MB y el árbol a GBs; nunca un archivo entero
#: en memoria).
_CHUNK = 1024 * 1024

_SEP = b"\x00"

#: FILE_ATTRIBUTE_REPARSE_POINT (Windows): junctions y otros reparse points que
#: no son symlinks Python (``entry.is_symlink()`` es False sobre un junction).
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


@dataclass(frozen=True, slots=True)
class TreeDigest:
    """Identidad completa de un árbol: digest + dimensiones.

    La tupla (digest, files, bytes) ES la identidad autorizada del artifact:
    las tres se persisten juntas en el handoff y las tres se comparan juntas en
    el resume. Un digest solo, sin dimensiones, no distingue un árbol vacío de
    un árbol de un archivo vacío.
    """

    digest: str
    files: int
    bytes: int


def _enlaces_dentro(raiz: pathlib.Path) -> list[str]:
    """Enlaces (symlinks/junctions/reparse-points) dentro de ``raiz``, sin
    seguirlos. Walk local, manual, sin mutar la semántica del walker compartido.

    Un árbol ilegible también falla cerrado: no se puede firmar como propio lo
    que no se pudo inspeccionar.

    **Best-effort ante mutación concurrente**: la clasificación es por
    ``DirEntry``/``lstat`` por entrada, sin handles ``O_NOFOLLOW`` — una
    carrera que reemplace una entrada por un enlace entre la inspección y el
    uso posterior no queda cubierta por este recorrido.
    """
    enlaces: list[str] = []
    pendientes: list[pathlib.Path] = [raiz]
    while pendientes:
        actual = pendientes.pop()
        try:
            with os.scandir(actual) as iterador:
                entradas = list(iterador)
        except OSError as e:
            raise OSError(f"No se pudo inspeccionar '{actual}' para identificar el artifact: {e}") from e
        for entry in entradas:
            try:
                es_enlace = entry.is_symlink()
            except OSError:
                es_enlace = False
            if not es_enlace and os.name == "nt":
                try:
                    atributos = entry.stat(follow_symlinks=False).st_file_attributes
                except OSError:
                    atributos = 0
                if atributos & _FILE_ATTRIBUTE_REPARSE_POINT:
                    es_enlace = True
            if es_enlace:
                enlaces.append(entry.path)
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    pendientes.append(pathlib.Path(entry.path))
            except OSError as e:
                raise OSError(f"No se pudo clasificar '{entry.path}' en el artifact: {e}") from e
    return enlaces


def digest_arbol(raiz: pathlib.Path) -> TreeDigest:
    """Digest completo del árbol bajo ``raiz``.

    Fail-closed sobre la raíz: un árbol ausente o no-directorio NO tiene
    identidad que afirmar, así que lanza ``OSError`` en vez de devolver un
    digest vacío que parecería válido (mismo criterio que el gate C para un
    staging sin archivos propios). Igual con un enlace dentro del scope (F-004):
    no se sigue ni se ignora — no hay identidad que firmar. La detección de
    enlaces es best-effort ante mutación concurrente del filesystem (ver el
    docstring del módulo).

    Fórmula (por archivo, en orden lexicográfico del relpath canónico):

        sha256( relpath + NUL + size + NUL + contenido + NUL )

    La concatación por stream —sin materializar el árbol en memoria— y el
    ordenamiento determinista hacen que dos corridas sobre el mismo árbol
    produzcan exactamente el mismo digest en cualquier plataforma.
    """
    if not raiz.is_dir():
        raise OSError(f"La raíz del artifact '{raiz}' no es un directorio: no hay identidad que afirmar")

    enlaces = _enlaces_dentro(raiz)
    if enlaces:
        raise OSError(
            f"El subtree autorizado '{raiz}' contiene enlaces ({enlaces[:3]}{'…' if len(enlaces) > 3 else ''}): "
            "un artifact con links no propios no es autorizable — no se firma ni se sigue."
        )

    entradas: list[tuple[str, int, pathlib.Path]] = []
    for archivo, identidad in iter_archivos_propios(raiz):
        if not stat.S_ISREG(identidad.st_mode):
            continue  # defensa en profundidad: el recorrido ya filtra enlaces
        rel = archivo.relative_to(raiz).as_posix()
        entradas.append((rel, identidad.st_size, archivo))

    if not entradas:
        # Fail-closed: "no hay archivos propios" es la MISMA ausencia de
        # evidencia que el gate C rechaza en el staging. Un artifact vacío no
        # es un artifact.
        raise OSError(f"El árbol bajo '{raiz}' no tiene archivos propios que identificar")

    acumulador = hashlib.sha256()
    total_bytes = 0
    for rel, size, archivo in sorted(entradas, key=lambda e: e[0]):
        acumulador.update(rel.encode("utf-8", "surrogatepass"))
        acumulador.update(_SEP)
        acumulador.update(str(size).encode("ascii"))
        acumulador.update(_SEP)
        with archivo.open("rb") as fh:
            while chunk := fh.read(_CHUNK):
                acumulador.update(chunk)
                total_bytes += len(chunk)
        acumulador.update(_SEP)

    return TreeDigest(digest=acumulador.hexdigest(), files=len(entradas), bytes=total_bytes)


__all__ = ["TreeDigest", "digest_arbol"]
