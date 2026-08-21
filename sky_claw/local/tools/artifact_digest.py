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

``meta.ini`` queda fuera del alcance por ESTRUCTURA (vive en ``mods/TexGen
Output/``, hermano de ``textures/``): el scope de identidad es el subárbol
``textures/``. Dentro del alcance se excluye explícitamente ``meta.ini`` porque
MO2 puede reescribirlo; la exclusión está congelada en
``EXCLUSIONES_DE_DIGEST`` y un test enumerativo la afirma por igualdad literal.
"""

from __future__ import annotations

import hashlib
import pathlib
import stat
from dataclasses import dataclass

from sky_claw.app.security.links import iter_archivos_propios

#: Chunk de lectura del digest (mismo criterio que ``texgen_visibility``: los
#: .dds de LOD llegan a decenas de MB y el árbol a GBs; nunca un archivo entero
#: en memoria).
_CHUNK = 1024 * 1024

#: Archivos excluidos del scope de identidad, congelados por test. Sólo
#: ``meta.ini``: MO2 lo reescribe fuera de nuestro control y no es parte de lo
#: que DynDOLOD lee del Data.
EXCLUSIONES_DE_DIGEST: frozenset[str] = frozenset({"meta.ini"})

_SEP = b"\x00"


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


def digest_arbol(raiz: pathlib.Path) -> TreeDigest:
    """Digest completo del árbol bajo ``raiz``.

    Fail-closed sobre la raíz: un árbol ausente o no-directorio NO tiene
    identidad que afirmar, así que lanza ``OSError`` en vez de devolver un
    digest vacío que parecería válido (mismo criterio que el gate C para un
    staging sin archivos propios).

    Fórmula (por archivo, en orden lexicográfico del relpath canónico):

        sha256( relpath + NUL + size + NUL + contenido + NUL )

    La concatación por stream —sin materializar el árbol en memoria— y el
    ordenamiento determinista hacen que dos corridas sobre el mismo árbol
    produzcan exactamente el mismo digest en cualquier plataforma.
    """
    entradas: list[tuple[str, int, pathlib.Path]] = []
    for archivo, identidad in iter_archivos_propios(raiz):
        if not stat.S_ISREG(identidad.st_mode):
            continue  # defensa en profundidad: el recorrido ya filtra enlaces
        if archivo.name in EXCLUSIONES_DE_DIGEST:
            continue
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


__all__ = ["EXCLUSIONES_DE_DIGEST", "TreeDigest", "digest_arbol"]
