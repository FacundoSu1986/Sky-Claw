"""Ancla: el estado vigente de H4 (ratificación de F01, aceptación de R7) no queda a medias.

Defecto que ancla: el estado de la ratificación se declara en tres lugares —el comentario de
producción de ``authored_dataset.py`` y los dos documentos de H4 del PR #723—. Tras ratificar el
operador F01 = R2-lite y fusionarse #723, los tres siguieron diciendo
``F01_CONTRACT_AMENDMENT = PENDING_TECH_LEAD`` y ``H4_CODE_MERGE_AUTHORIZED = NO``: nada obligaba a
actualizar los hermanos juntos.

Qué se congela:

- el CONJUNTO de archivos de ``docs/`` y ``sky_claw/`` que declaran esos tokens (por igualdad: un
  archivo nuevo que los declare rompe el primer test hasta que se decida su receta);
- el código de producción declara el estado vigente, sin ``PENDING_TECH_LEAD``;
- cada documento abre con un bloque «Estado vigente» con los valores actuales. Los bloques de tokens
  más abajo son fotos fechadas del PR y se conservan como historial: no se reescriben.

Límite: el ancla ve los tokens por su NOMBRE. Un estado declarado con otro nombre, o en prosa sin
token, no lo ve.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1]
DIRECTORIOS_CENSADOS = ("docs", "sky_claw")
SUFIJOS_CENSADOS = (".md", ".py")

TOKENS_DE_ESTADO = ("F01_CONTRACT_AMENDMENT", "PENDING_TECH_LEAD", "H4_CODE_MERGE_AUTHORIZED")

DOCUMENTOS = (
    "docs/design/research/native-parallax/h4-resize-normal-contract-reconciliation.md",
    "docs/design/research/native-parallax/h4-resize-normal-f01-f02-adjudication.md",
)
CODIGO = "sky_claw/local/native_parallax/research/authored_dataset.py"

DECLARANTES_ESPERADOS = frozenset((*DOCUMENTOS, CODIGO))

MARCA_DEL_BLOQUE = "**Estado vigente"

# Las fotos históricas del PR: el estado viejo del contrato, tal como lo declaraban los registros.
FOTO_VIEJA = re.compile(r"PENDING_TECH_LEAD|H4_CODE_MERGE_AUTHORIZED\s*=\s*NO")

# Valores vigentes tras la ratificación del operador y el merge de #723. El prefijo basta: la
# prosa que acompaña al valor (entre paréntesis) puede cambiar sin tocar el estado.
ESTADO_VIGENTE = {
    "F01_CONTRACT_AMENDMENT": "RATIFIED",
    "H4_CODE_MERGE_AUTHORIZED": "YES",
    "R7_THRESHOLD_KIND": "HEURISTIC (RMS)",
    "R7_NUMERIC_DERIVATION": "BLOCKED",
    "M6_IMPLEMENTATION_BLOCKED": "YES",
}


def _declarantes() -> frozenset[str]:
    """Archivos de los directorios censados que mencionan alguno de los tokens de estado."""
    encontrados: set[str] = set()
    for directorio in DIRECTORIOS_CENSADOS:
        for ruta in (RAIZ / directorio).rglob("*"):
            if ruta.suffix not in SUFIJOS_CENSADOS or "__pycache__" in ruta.parts or not ruta.is_file():
                continue
            texto = ruta.read_text(encoding="utf-8", errors="replace")
            if any(token in texto for token in TOKENS_DE_ESTADO):
                encontrados.add(ruta.relative_to(RAIZ).as_posix())
    return frozenset(encontrados)


def _tokens_del_bloque_vigente(texto: str) -> dict[str, str]:
    """Tokens ``CLAVE = valor`` de la primera cerca ```text que sigue a la marca del bloque.

    Falla cerrado: sin marca, sin cerca de apertura o sin cierre devuelve ``{}``, que no cumple
    ningún valor esperado.
    """
    lineas = [linea.lstrip("> ").rstrip() for linea in texto.splitlines()]
    try:
        inicio = next(i for i, linea in enumerate(lineas) if MARCA_DEL_BLOQUE in linea)
        apertura = next(i for i in range(inicio, len(lineas)) if lineas[i].startswith("```text"))
        cierre = next(i for i in range(apertura + 1, len(lineas)) if lineas[i].startswith("```"))
    except StopIteration:
        return {}
    tokens: dict[str, str] = {}
    for linea in lineas[apertura + 1 : cierre]:
        clave, separador, valor = linea.partition("=")
        if separador:
            tokens[clave.strip()] = valor.strip()
    return tokens


def test_los_archivos_que_declaran_el_estado_de_h4_son_exactamente_estos() -> None:
    assert _declarantes() == DECLARANTES_ESPERADOS


def test_el_codigo_de_produccion_declara_el_estado_vigente() -> None:
    texto = (RAIZ / CODIGO).read_text(encoding="utf-8")

    assert "PENDING_TECH_LEAD" not in texto
    lineas = [linea for linea in texto.splitlines() if "F01_CONTRACT_AMENDMENT" in linea]
    assert len(lineas) == 1
    assert lineas[0].partition("=")[2].split()[0] == "RATIFIED"


@pytest.mark.parametrize("documento", DOCUMENTOS)
def test_cada_documento_abre_con_el_bloque_de_estado_vigente(documento: str) -> None:
    texto = (RAIZ / documento).read_text(encoding="utf-8")

    tokens = _tokens_del_bloque_vigente(texto)

    assert set(tokens) == set(ESTADO_VIGENTE)
    for clave, esperado in ESTADO_VIGENTE.items():
        assert tokens[clave].startswith(esperado), f"{clave}: {tokens[clave]!r} no empieza por {esperado!r}"


@pytest.mark.parametrize("documento", DOCUMENTOS)
def test_el_bloque_vigente_precede_a_las_fotos_historicas(documento: str) -> None:
    """El estado vigente tiene que leerse ANTES que cualquier foto del PR con valores viejos."""
    texto = (RAIZ / documento).read_text(encoding="utf-8")

    assert MARCA_DEL_BLOQUE in texto
    posicion_vigente = texto.index(MARCA_DEL_BLOQUE)
    fotos_viejas = [m.start() for m in FOTO_VIEJA.finditer(texto)]

    assert fotos_viejas, "el documento conserva sus fotos históricas con los valores viejos"
    assert posicion_vigente < min(fotos_viejas)


# ---------------------------------------------------------------------------
# Casos sintéticos del lector del bloque (falla cerrado)
# ---------------------------------------------------------------------------

_BLOQUE_OK = """\
> **Estado vigente (2026-10-19).** Texto.
>
> ```text
> F01_CONTRACT_AMENDMENT = RATIFIED   (operador)
> H4_CODE_MERGE_AUTHORIZED = YES
> ```
"""


def test_el_lector_extrae_clave_y_valor_de_una_cerca_en_cita() -> None:
    assert _tokens_del_bloque_vigente(_BLOQUE_OK) == {
        "F01_CONTRACT_AMENDMENT": "RATIFIED   (operador)",
        "H4_CODE_MERGE_AUTHORIZED": "YES",
    }


@pytest.mark.parametrize(
    "texto",
    [
        "sin marca\n```text\nA = B\n```\n",
        "**Estado vigente** sin cerca\n",
        "**Estado vigente**\n```text\nA = B\n",
        "",
    ],
    ids=["sin_marca", "sin_cerca", "cerca_sin_cierre", "vacio"],
)
def test_el_lector_falla_cerrado_ante_un_bloque_incompleto(texto: str) -> None:
    assert _tokens_del_bloque_vigente(texto) == {}
