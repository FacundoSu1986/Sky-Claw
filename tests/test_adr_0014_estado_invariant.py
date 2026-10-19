"""Ancla: el estado del ADR 0014 se declara en tres lugares y los tres coinciden.

Defecto que ancla: un ADR cambia de estado en su encabezado, pero el índice (``docs/adr/README.md``) y la
tabla de fuentes canónicas (``docs/documentation/source_of_truth.md``) repiten ese estado por su cuenta.
El mismo patrón ya se vio hoy con el estado de la ratificación de H4 (declarado en tres archivos, desactualizado
en los tres): nada obligaba a actualizar los hermanos juntos.

Qué se congela:

- el estado del encabezado del ADR 0014 es ``Aceptada``;
- el índice y la tabla de fuentes declaran el MISMO estado que el encabezado;
- el CONJUNTO de documentos de ``docs/`` que declaran el estado del 0014 entre backticks es exactamente esos
  dos (por igualdad: un tercero rompe el test hasta que se decida su receta).

Límite: el ancla lee el estado por la FORMA de la declaración (``el 0014 está en estado `X```, ``ADR 0014 (`X`...``).
Una declaración en otra forma, o en prosa, no la ve; si la forma de las existentes cambia, el lector devuelve
``None`` y el test falla cerrado.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1]

ADR = "docs/adr/0014-native-parallax-producto-brief-y-dependencias.md"
INDICE = "docs/adr/README.md"
FUENTES = "docs/documentation/source_of_truth.md"

ESTADO_ESPERADO = "Aceptada"
VOCABULARIO = ("Propuesta", "Aceptada", "Rechazada", "Reemplazada")

_ENCABEZADO = re.compile(r"^\*\*Estado:\*\*\s+(\w+)", re.MULTILINE)
_INDICE = re.compile(r"el 0014 está en estado `(\w+)`")
_FUENTES = re.compile(r"ADR 0014 \(`(\w+)`")
_ESTADO_EN_BACKTICKS = re.compile("`(?:" + "|".join(VOCABULARIO) + ")`")


def _primero(patron: re.Pattern[str], texto: str) -> str | None:
    coincidencia = patron.search(texto)
    return coincidencia.group(1) if coincidencia else None


def _leer(ruta: str) -> str:
    return (RAIZ / ruta).read_text(encoding="utf-8")


def _documentos_que_declaran_el_estado_del_0014(raiz: Path = RAIZ) -> frozenset[str]:
    """Documentos ``.md`` de ``docs/`` con una línea que nombra el 0014 y un estado entre backticks."""
    encontrados: set[str] = set()
    for ruta in (raiz / "docs").rglob("*.md"):
        for linea in ruta.read_text(encoding="utf-8", errors="replace").splitlines():
            if "0014" in linea and _ESTADO_EN_BACKTICKS.search(linea):
                encontrados.add(ruta.relative_to(raiz).as_posix())
    return frozenset(encontrados)


def test_encabezado_del_adr_0014_declara_el_estado_esperado() -> None:
    assert _primero(_ENCABEZADO, _leer(ADR)) == ESTADO_ESPERADO


@pytest.mark.parametrize(("ruta", "patron"), [(INDICE, _INDICE), (FUENTES, _FUENTES)], ids=["indice", "fuentes"])
def test_indice_y_fuentes_declaran_el_mismo_estado_que_el_encabezado(ruta: str, patron: re.Pattern[str]) -> None:
    del_encabezado = _primero(_ENCABEZADO, _leer(ADR))

    declarado = _primero(patron, _leer(ruta))

    assert declarado is not None, f"{ruta}: no se pudo leer el estado del 0014 (¿cambió la forma de la declaración?)"
    assert declarado == del_encabezado


def test_documentos_que_declaran_el_estado_del_0014_son_exactamente_el_indice_y_las_fuentes() -> None:
    assert _documentos_que_declaran_el_estado_del_0014() == {INDICE, FUENTES}


# ---------------------------------------------------------------------------
# Casos sintéticos de los lectores (fallan cerrado)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("patron", "texto", "esperado"),
    [
        (_ENCABEZADO, "# ADR\n\n**Estado:** Aceptada el 2026-10-19.\n", "Aceptada"),
        (_ENCABEZADO, "**Estado:** Propuesta. Texto.", "Propuesta"),
        (_ENCABEZADO, "Estado: Aceptada\n", None),
        (_ENCABEZADO, "texto **Estado:** Aceptada en medio de línea", None),
        (_INDICE, "ADR 0001–0014 (el 0014 está en estado `Aceptada`).", "Aceptada"),
        (_INDICE, "(el 0014 está Aceptada)", None),
        (_FUENTES, "| x | ADR 0014 (`Propuesta`: sin precedencia), otro |", "Propuesta"),
        (_FUENTES, "| x | ADR 0013 (`Aceptada`) |", None),
    ],
)
def test_lectores_extraen_el_estado_o_devuelven_none(patron: re.Pattern[str], texto: str, esperado: str | None) -> None:
    assert _primero(patron, texto) == esperado


def test_censo_ve_una_declaracion_nueva_en_otro_documento(tmp_path: Path) -> None:
    (tmp_path / "docs" / "adr").mkdir(parents=True)
    (tmp_path / "docs" / "adr" / "README.md").write_text("el 0014 está en estado `Aceptada`\n", encoding="utf-8")
    (tmp_path / "docs" / "otro.md").write_text("ver ADR 0014 (`Propuesta`)\n", encoding="utf-8")
    (tmp_path / "docs" / "ajeno.md").write_text("el ADR 0013 está `Aceptada`\n", encoding="utf-8")
    (tmp_path / "docs" / "sin_estado.md").write_text("ver el ADR 0014 para el detalle\n", encoding="utf-8")

    assert _documentos_que_declaran_el_estado_del_0014(tmp_path) == {"docs/adr/README.md", "docs/otro.md"}
