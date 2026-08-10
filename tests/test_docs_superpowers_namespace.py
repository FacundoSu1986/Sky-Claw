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

import re
import subprocess
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
ESTE_ARCHIVO = Path(__file__).resolve()

VARIANTES_DE_NAMESPACE = ("superpowers", "super-powers", "super_powers")

# Requiere un "/" inmediatamente antes de la variante -eso es lo que distingue
# una referencia de ruta ("docs/superpowers", "../superpowers/plans/x.md",
# "docs/super-powers/...") de una mención léxica suelta ("el namespace
# superpowers", o el banner "superpowers:subagent-driven-development", que va
# precedido por un espacio y seguido de ":", no de "/"). La lookahead negativa
# evita matchear un prefijo dentro de un nombre más largo ("superpowers_backup").
PATRON_RUTA_VIEJA = re.compile(
    "(?<=/)(?:" + "|".join(re.escape(variante) for variante in VARIANTES_DE_NAMESPACE) + r")(?![\w-])",
    re.IGNORECASE,
)


def _archivos_trackeados() -> list[str]:
    resultado = subprocess.run(
        ["git", "ls-files"],
        cwd=RAIZ,
        check=True,
        capture_output=True,
        text=True,
    )
    return [linea for linea in resultado.stdout.splitlines() if linea]


def _segmento_es_namespace_viejo(segmento: str, *, es_archivo: bool = False) -> bool:
    """True si el segmento ES el namespace viejo (carpeta exacta, o archivo final sin extensión).

    Igualdad exacta, no subcadena: un archivo cuyo nombre solo *contiene* la
    palabra (p. ej. `2026-08-09-superpowers-related.md`) no vive bajo el
    namespace y no debe romper el test — eso fue un falso positivo real que
    dejó pasar la primera versión de este ancla (ver revisión de PR #459).

    La extensión solo se quita cuando el segmento es el archivo final de la
    ruta: una carpeta llamada literalmente `superpowers.archive` no es el
    namespace, y tratar ese sufijo como si fuera la extensión de un archivo
    fue otro falso positivo señalado en la misma revisión.
    """
    normalizado = segmento.lower()
    if normalizado in VARIANTES_DE_NAMESPACE:
        return True
    return es_archivo and "." in normalizado and normalizado.rsplit(".", 1)[0] in VARIANTES_DE_NAMESPACE


def _ruta_tiene_segmento_de_namespace_viejo(ruta: str) -> bool:
    segmentos = ruta.split("/")
    ultimo = len(segmentos) - 1
    return any(
        _segmento_es_namespace_viejo(segmento, es_archivo=(indice == ultimo))
        for indice, segmento in enumerate(segmentos)
    )


def test_detector_de_segmento_exige_igualdad_exacta_no_subcadena() -> None:
    """Regresión de los falsos positivos señalados por el revisor: subcadena no alcanza."""
    assert _segmento_es_namespace_viejo("Super-Powers") is True  # case-insensitive
    for variante in VARIANTES_DE_NAMESPACE:
        # Carpeta: igualdad exacta, sin tocar extensión.
        assert _segmento_es_namespace_viejo(variante) is True
        # Archivo final (con extensión) cuyo nombre completo es el namespace.
        assert _segmento_es_namespace_viejo(f"{variante}.md", es_archivo=True) is True
        # Namespace como prefijo/sufijo de un nombre más largo: no es el namespace.
        assert _segmento_es_namespace_viejo(f"2026-08-09-{variante}-related.md", es_archivo=True) is False
        assert _segmento_es_namespace_viejo(f"non-{variante}-stuff") is False
        # Carpeta con un sufijo con punto: no es un archivo, no se le quita "extensión".
        assert _segmento_es_namespace_viejo(f"{variante}.archive", es_archivo=False) is False


def test_ningun_archivo_vive_bajo_el_namespace_superpowers() -> None:
    """Ninguna ruta trackeada tiene un segmento que SEA el namespace viejo.

    Se excluye este propio archivo: su nombre necesita mencionar el namespace
    para poder documentarlo y buscarlo, pero no vive bajo `docs/superpowers`.
    """
    ofensores = sorted(
        ruta
        for ruta in _archivos_trackeados()
        if (RAIZ / ruta).resolve() != ESTE_ARCHIVO and _ruta_tiene_segmento_de_namespace_viejo(ruta)
    )
    assert not ofensores, (
        f"Reapareció el namespace `superpowers` en rutas trackeadas: {ofensores}. "
        "Ese contenido va en docs/design/{specs,plans}/, no en docs/superpowers/ "
        "(ver PR #446)."
    )


def test_patron_de_ruta_vieja_exige_barra_no_solo_la_palabra() -> None:
    """Regresión: variantes con separador y enlaces relativos también cuentan.

    Señalado en la revisión de PR #459: la versión anterior solo buscaba el
    substring literal `docs/superpowers`, así que un enlace relativo
    (`../superpowers/plans/foo.md`) o una variante con guion
    (`docs/super-powers/...`) pasaban sin detectarse.
    """
    assert PATRON_RUTA_VIEJA.search("ver docs/superpowers para más detalle")
    assert PATRON_RUTA_VIEJA.search("[plan](../superpowers/plans/foo.md)")
    assert PATRON_RUTA_VIEJA.search("docs/super-powers/x.md")
    assert PATRON_RUTA_VIEJA.search("docs/super_powers/x.md")
    # El banner de harness (precedido por espacio, seguido de ":") no es una ruta.
    assert not PATRON_RUTA_VIEJA.search("Use superpowers:subagent-driven-development")
    # Mención léxica suelta, sin "/" antes: fuera de alcance de este ancla.
    assert not PATRON_RUTA_VIEJA.search("el namespace superpowers ya no existe")
    # Prefijo dentro de un nombre más largo: no es el namespace.
    assert not PATRON_RUTA_VIEJA.search("docs/non-superpowers-stuff/x.md")


def test_ningun_archivo_referencia_la_ruta_vieja_docs_superpowers() -> None:
    """Ninguna mención textual apunta a `docs/superpowers` (o variantes) como ruta."""
    ofensores = []
    for relativa in _archivos_trackeados():
        ruta = RAIZ / relativa
        if ruta.resolve() == ESTE_ARCHIVO or not ruta.is_file():
            continue
        contenido = ruta.read_text(encoding="utf-8", errors="ignore")
        if PATRON_RUTA_VIEJA.search(contenido):
            ofensores.append(relativa)
    assert not ofensores, (
        f"Referencia de ruta al namespace viejo en: {sorted(ofensores)}. "
        "Actualizar al equivalente bajo docs/design/{specs,plans}/."
    )


def _destinos_de_enlaces_markdown(texto: str) -> set[str]:
    """Destinos de enlaces `[texto](destino)`, sin bloques de código."""
    sin_bloques_de_codigo = re.sub(r"```.*?```", "", texto, flags=re.DOTALL)
    return set(re.findall(r"\[[^\]]+\]\(([^)]+)\)", sin_bloques_de_codigo))


def test_design_readme_indexa_el_spec_y_el_plan_movidos() -> None:
    """El índice no puede quedar desactualizado tras el move (PR #446 lo dejó así una vez).

    Compara destinos reales de enlaces Markdown, no una simple pertenencia de
    substring: un `in indice` pasaría igual si la ruta apareciera en texto
    plano o en un bloque de código, sin que el documento esté realmente
    indexado (señalado en la revisión de PR #459).
    """
    indice = (RAIZ / "docs/design/README.md").read_text(encoding="utf-8")
    destinos = _destinos_de_enlaces_markdown(indice)
    esperados = {
        "specs/2026-08-09-fomod-profile-and-tool-output-security-design.md",
        "plans/2026-08-09-fomod-profile-and-tool-output-security.md",
    }
    assert esperados <= destinos
