"""Ancla contra la reaparición del namespace `docs/superpowers`.

Por qué existe: PR #446 eliminó `docs/superpowers` —residuo del harness
agéntico que generó esos documentos, no un concepto de Sky-Claw— moviendo su
contenido a `docs/design/{specs,plans}`. Un día después, dos commits
recrearon exactamente esa estructura (`docs/superpowers/specs/...`,
`docs/superpowers/plans/...`) sin que ningún test lo detectara, porque no
existía ningún guard automatizado sobre esa ruta.

Enumera en vez de muestrear (ver "La regla que más se viola" en `AGENTS.md`):
recorre **todos** los archivos trackeados por git, no una lista de rutas
conocidas, así que un directorio nuevo bajo cualquiera de las variantes de
naming (`superpowers`, `super-powers`, `super_powers`) rompe el test tanto
como una reaparición literal de `docs/superpowers`.

Deliberadamente no se barre la palabra suelta "superpowers" en el *contenido*
de todo el repo: el plan movido conserva un banner de harness
(`superpowers:subagent-driven-development`) que no es una ruta sino un nombre
de skill, fuera del alcance de esta limpieza. Este ancla protege el
*namespace de rutas* (nombres de directorio/archivo y menciones textuales a
`docs/superpowers` como ruta), no cada mención léxica de la palabra.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
ESTE_ARCHIVO = Path(__file__).resolve()

VARIANTES_DE_NAMESPACE = ("superpowers", "super-powers", "super_powers")
RUTA_VIEJA = "docs/superpowers"


def _archivos_trackeados() -> list[str]:
    resultado = subprocess.run(
        ["git", "ls-files"],
        cwd=RAIZ,
        check=True,
        capture_output=True,
        text=True,
    )
    return [linea for linea in resultado.stdout.splitlines() if linea]


def _segmento_es_namespace_viejo(segmento: str) -> bool:
    """True si el segmento (carpeta, o archivo sin extensión) ES el namespace viejo.

    Igualdad exacta, no subcadena: un archivo cuyo nombre solo *contiene* la
    palabra (p. ej. `2026-08-09-superpowers-related.md`) no vive bajo el
    namespace y no debe romper el test — eso fue un falso positivo real que
    dejó pasar la primera versión de este ancla (ver revisión de PR #459).
    """
    normalizado = segmento.lower()
    stem = normalizado.rsplit(".", 1)[0] if "." in normalizado else normalizado
    return normalizado in VARIANTES_DE_NAMESPACE or stem in VARIANTES_DE_NAMESPACE


def test_detector_de_segmento_exige_igualdad_exacta_no_subcadena() -> None:
    """Regresión del falso positivo señalado por el revisor: subcadena no alcanza."""
    assert _segmento_es_namespace_viejo("superpowers") is True
    assert _segmento_es_namespace_viejo("Super-Powers") is True
    assert _segmento_es_namespace_viejo("super_powers") is True
    # Archivo (con extensión) cuyo nombre completo es el namespace.
    assert _segmento_es_namespace_viejo("superpowers.md") is True
    # Namespace como prefijo/sufijo de un nombre más largo: no es el namespace.
    assert _segmento_es_namespace_viejo("2026-08-09-superpowers-related.md") is False
    assert _segmento_es_namespace_viejo("non-superpowers-stuff") is False


def test_ningun_archivo_vive_bajo_el_namespace_superpowers() -> None:
    """Ninguna ruta trackeada tiene un segmento que SEA el namespace viejo.

    Se excluye este propio archivo: su nombre necesita mencionar el namespace
    para poder documentarlo y buscarlo, pero no vive bajo `docs/superpowers`.
    """
    ofensores = sorted(
        ruta
        for ruta in _archivos_trackeados()
        if (RAIZ / ruta).resolve() != ESTE_ARCHIVO
        and any(_segmento_es_namespace_viejo(segmento) for segmento in ruta.split("/"))
    )
    assert not ofensores, (
        f"Reapareció el namespace `superpowers` en rutas trackeadas: {ofensores}. "
        "Ese contenido va en docs/design/{specs,plans}/, no en docs/superpowers/ "
        "(ver PR #446)."
    )


def test_ningun_archivo_referencia_la_ruta_vieja_docs_superpowers() -> None:
    """Ninguna mención textual apunta a `docs/superpowers` como ruta."""
    ofensores = []
    for relativa in _archivos_trackeados():
        ruta = RAIZ / relativa
        if ruta.resolve() == ESTE_ARCHIVO or not ruta.is_file():
            continue
        contenido = ruta.read_text(encoding="utf-8", errors="ignore")
        if RUTA_VIEJA in contenido.lower():
            ofensores.append(relativa)
    assert not ofensores, (
        f"Referencia textual a la ruta vieja `{RUTA_VIEJA}` en: {sorted(ofensores)}. "
        "Actualizar al equivalente bajo docs/design/{specs,plans}/."
    )


def test_design_readme_indexa_el_spec_y_el_plan_movidos() -> None:
    """El índice no puede quedar desactualizado tras el move (PR #446 lo dejó así una vez)."""
    indice = (RAIZ / "docs/design/README.md").read_text(encoding="utf-8")
    assert "specs/2026-08-09-fomod-profile-and-tool-output-security-design.md" in indice
    assert "plans/2026-08-09-fomod-profile-and-tool-output-security.md" in indice
