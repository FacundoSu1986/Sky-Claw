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
token, no lo ve. El censo mira TODOS los archivos de esos directorios, sea cual sea su formato
(``.json``, ``.txt``, ``.yaml``...): el estado de decisión de native-parallax ya vive también en JSON.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1]
DIRECTORIOS_CENSADOS = ("docs", "sky_claw")

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


def _declarantes(raiz: Path = RAIZ) -> frozenset[str]:
    """Archivos de los directorios censados que mencionan alguno de los tokens de estado.

    Mira TODOS los archivos, sin filtrar por sufijo ni decodificar: se busca el token en los bytes,
    así que un JSON, un TXT o un binario que lo contenga también cuenta.
    """
    buscados = tuple(token.encode("ascii") for token in TOKENS_DE_ESTADO)
    encontrados: set[str] = set()
    for directorio in DIRECTORIOS_CENSADOS:
        for ruta in (raiz / directorio).rglob("*"):
            if "__pycache__" in ruta.parts or not ruta.is_file():
                continue
            contenido = ruta.read_bytes()
            if any(token in contenido for token in buscados):
                encontrados.add(ruta.relative_to(raiz).as_posix())
    return frozenset(encontrados)


def _valor_es(valor: str, esperado: str) -> bool:
    """El valor ES el esperado: igual, o seguido de un espacio y su anotación entre paréntesis.

    Un prefijo no alcanza: ``RATIFIED_PENDING`` o ``YES_BUT_REVOKED`` no declaran el estado esperado.
    """
    return re.fullmatch(re.escape(esperado) + r"(?:\s.*)?", valor) is not None


def _extension_del_bloque_vigente(texto: str) -> tuple[int, int] | None:
    """Offsets ``[inicio, fin)`` del bloque vigente: de su marca al cierre de su primera cerca.

    La prosa del propio bloque nombra los tokens viejos para explicar cuál rige; por eso las fotos
    históricas se buscan FUERA de este rango. ``None`` si falta la marca o la cerca.
    """
    inicio = texto.find(MARCA_DEL_BLOQUE)
    if inicio < 0:
        return None
    apertura = texto.find("```text", inicio)
    if apertura < 0:
        return None
    cierre = texto.find("```", apertura + len("```text"))
    if cierre < 0:
        return None
    return inicio, cierre + len("```")


def _fotos_historicas(texto: str) -> list[int]:
    """Posiciones de las fotos del PR con valores viejos, fuera del bloque vigente."""
    extension = _extension_del_bloque_vigente(texto)
    if extension is None:
        return []
    inicio, fin = extension
    return [m.start() for m in FOTO_VIEJA.finditer(texto) if not inicio <= m.start() < fin]


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
        assert _valor_es(tokens[clave], esperado), f"{clave}: {tokens[clave]!r} no declara {esperado!r}"


@pytest.mark.parametrize("documento", DOCUMENTOS)
def test_el_bloque_vigente_precede_a_las_fotos_historicas(documento: str) -> None:
    """El estado vigente se lee ANTES que cualquier foto del PR, y las fotos siguen ahí.

    Las fotos se buscan fuera del bloque vigente: su prosa nombra los tokens viejos, así que contarla
    haría pasar el test aunque se borrara todo el historial.
    """
    texto = (RAIZ / documento).read_text(encoding="utf-8")

    extension = _extension_del_bloque_vigente(texto)
    assert extension is not None
    fotos_viejas = _fotos_historicas(texto)

    assert fotos_viejas, "el documento conserva sus fotos históricas con los valores viejos"
    assert min(fotos_viejas) > extension[1], "una foto histórica precede al bloque vigente"


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
    assert _extension_del_bloque_vigente(texto) is None


@pytest.mark.parametrize(
    ("valor", "esperado", "coincide"),
    [
        ("RATIFIED", "RATIFIED", True),
        ("RATIFIED          (operador, 2026-10-19)", "RATIFIED", True),
        ("HEURISTIC (RMS)   (aceptado)", "HEURISTIC (RMS)", True),
        ("RATIFIED_PENDING", "RATIFIED", False),
        ("YES_BUT_REVOKED", "YES", False),
        ("YESTERDAY", "YES", False),
        ("HEURISTIC (RMS)X", "HEURISTIC (RMS)", False),
        ("", "YES", False),
    ],
)
def test_el_valor_debe_ser_el_esperado_y_no_solo_empezar_por_el(valor: str, esperado: str, coincide: bool) -> None:
    assert _valor_es(valor, esperado) is coincide


def test_las_fotos_historicas_no_cuentan_la_prosa_del_propio_bloque_vigente() -> None:
    sin_historial = (
        "> **Estado vigente (2026-10-19).** Donde digan `PENDING_TECH_LEAD` o\n"
        "> `H4_CODE_MERGE_AUTHORIZED = NO`, rige este bloque.\n"
        ">\n"
        "> ```text\n"
        "> F01_CONTRACT_AMENDMENT = RATIFIED\n"
        "> ```\n"
    )
    con_historial = sin_historial + "\nH4_CODE_MERGE_AUTHORIZED   = NO\n"

    assert _fotos_historicas(sin_historial) == []
    assert len(_fotos_historicas(con_historial)) == 1


def test_el_censo_ve_los_tokens_en_cualquier_formato_y_ignora_el_cache(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "sky_claw" / "__pycache__").mkdir(parents=True)
    (tmp_path / "docs" / "a.json").write_text('{"F01_CONTRACT_AMENDMENT": "X"}', encoding="utf-8")
    (tmp_path / "docs" / "b.txt").write_text("PENDING_TECH_LEAD", encoding="utf-8")
    (tmp_path / "docs" / "c.bin").write_bytes(b"\x00\xff H4_CODE_MERGE_AUTHORIZED \x00")
    (tmp_path / "docs" / "d.md").write_text("sin tokens", encoding="utf-8")
    (tmp_path / "sky_claw" / "__pycache__" / "e.pyc").write_bytes(b"F01_CONTRACT_AMENDMENT")

    assert _declarantes(tmp_path) == {"docs/a.json", "docs/b.txt", "docs/c.bin"}
