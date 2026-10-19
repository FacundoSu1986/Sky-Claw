"""Ancla: el estado vigente de H4 (ratificación de F01, aceptación de R7) no queda a medias.

Defecto que ancla: el estado de la ratificación se declara en tres lugares —el comentario de
producción de ``authored_dataset.py`` y los dos documentos de H4 del PR #723—. Tras ratificar el
operador F01 = R2-lite y fusionarse #723, los tres siguieron diciendo
``F01_CONTRACT_AMENDMENT = PENDING_TECH_LEAD`` y ``H4_CODE_MERGE_AUTHORIZED = NO``: nada obligaba a
actualizar los hermanos juntos.

Qué se congela:

- el CONJUNTO de archivos de ``docs/`` y ``sky_claw/`` que declaran esos tokens (por igualdad: un
  archivo nuevo que los declare rompe el primer test hasta que se decida su receta);
- el código de producción declara el estado vigente (F01 ratificado, sin ``PENDING_TECH_LEAD``);
- cada documento abre con un bloque «Estado vigente» con los valores actuales. Los bloques de tokens
  más abajo son fotos fechadas del PR y se conservan como historial: no se reescriben. Una foto es una
  DECLARACIÓN (``CLAVE = valor``) dentro de una cerca ``text``; una mención suelta en prosa no cuenta.

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

# Las fotos históricas del PR: el estado viejo del contrato, declarado como ``CLAVE = valor``. Cada
# documento tiene que conservar al menos una de CADA tipo (borrar todas las de un tipo se detecta).
DECLARACIONES_VIEJAS = {
    "F01_PENDIENTE": re.compile(r"(?:H4_)?F01_CONTRACT_AMENDMENT\s*=\s*PENDING_TECH_LEAD\b"),
    "MERGE_NO_AUTORIZADO": re.compile(r"H4_CODE_MERGE_AUTHORIZED\s*=\s*NO\b"),
}

# Valores vigentes tras la ratificación del operador y el merge de #723. El valor tiene que ser ESE,
# seguido a lo sumo de una anotación entre paréntesis (que puede cambiar sin tocar el estado).
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
    """El valor ES el esperado: igual, o seguido de espacio y una anotación ENTRE PARÉNTESIS.

    Un sufijo cualquiera no alcanza: ``RATIFIED_PENDING``, ``YES_BUT_REVOKED`` o ``YES REVOKED`` no
    declaran el estado esperado.
    """
    return re.fullmatch(re.escape(esperado) + r"(?:\s+\(.*\))?", valor) is not None


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


def _declaraciones_viejas(texto: str) -> list[tuple[int, str]]:
    """``(posición, tipo)`` de las declaraciones del estado viejo dentro de cercas ``text``.

    Excluye el bloque vigente. Una mención en prosa, o en una cerca que no es ``text``, no es una foto
    histórica: por eso borrar el bloque y dejar la prosa se detecta.
    """
    extension = _extension_del_bloque_vigente(texto)
    if extension is None:
        return []
    inicio, fin = extension
    encontradas: list[tuple[int, str]] = []
    dentro = False
    posicion = 0
    for linea in texto.splitlines(keepends=True):
        limpia = linea.lstrip("> ").rstrip()
        if limpia.startswith("```"):
            dentro = (not dentro) and limpia.startswith("```text")
        elif dentro and not inicio <= posicion < fin:
            for tipo, patron in DECLARACIONES_VIEJAS.items():
                if patron.match(limpia):
                    encontradas.append((posicion, tipo))
        posicion += len(linea)
    return encontradas


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


def test_declarantes_conjunto_del_repo_coincide_con_el_congelado() -> None:
    assert _declarantes() == DECLARANTES_ESPERADOS


def test_codigo_de_produccion_estado_vigente_declara_f01_ratificado() -> None:
    texto = (RAIZ / CODIGO).read_text(encoding="utf-8")

    assert "PENDING_TECH_LEAD" not in texto
    enmienda = [linea for linea in texto.splitlines() if "F01_CONTRACT_AMENDMENT" in linea]
    assert len(enmienda) == 1
    assert enmienda[0].partition("=")[2].split()[0] == "RATIFIED"
    resolucion = [linea for linea in texto.splitlines() if "F01_RESOLUTION" in linea]
    assert len(resolucion) == 1
    assert resolucion[0].partition("=")[2].split()[0] == "R2_LITE_IMPLEMENTED"
    assert "ratificada por el operador" in resolucion[0]


@pytest.mark.parametrize("documento", DOCUMENTOS)
def test_bloque_vigente_de_cada_documento_declara_los_valores_esperados(documento: str) -> None:
    texto = (RAIZ / documento).read_text(encoding="utf-8")

    tokens = _tokens_del_bloque_vigente(texto)

    assert set(tokens) == set(ESTADO_VIGENTE)
    for clave, esperado in ESTADO_VIGENTE.items():
        assert _valor_es(tokens[clave], esperado), f"{clave}: {tokens[clave]!r} no declara {esperado!r}"


@pytest.mark.parametrize("documento", DOCUMENTOS)
def test_bloque_vigente_de_cada_documento_precede_y_conserva_las_fotos_historicas(documento: str) -> None:
    """El estado vigente se lee ANTES que cualquier foto del PR, y las fotos siguen ahí.

    Una foto es una declaración dentro de una cerca ``text`` fuera del bloque vigente: ni la prosa de
    ese bloque (que nombra los tokens viejos) ni una mención suelta cuentan como historial.
    """
    texto = (RAIZ / documento).read_text(encoding="utf-8")

    extension = _extension_del_bloque_vigente(texto)
    assert extension is not None
    fotos = _declaraciones_viejas(texto)

    assert {tipo for _, tipo in fotos} == set(DECLARACIONES_VIEJAS), "falta una foto histórica de algún tipo"
    assert min(posicion for posicion, _ in fotos) > extension[1], "una foto histórica precede al bloque vigente"


# ---------------------------------------------------------------------------
# Casos sintéticos de los lectores (fallan cerrado)
# ---------------------------------------------------------------------------

_BLOQUE_OK = """\
> **Estado vigente (2026-10-19).** Texto.
>
> ```text
> F01_CONTRACT_AMENDMENT = RATIFIED   (operador)
> H4_CODE_MERGE_AUTHORIZED = YES
> ```
"""


def test_tokens_del_bloque_vigente_cerca_en_cita_extrae_clave_y_valor() -> None:
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
def test_bloque_vigente_incompleto_no_tiene_tokens_ni_extension(texto: str) -> None:
    assert _tokens_del_bloque_vigente(texto) == {}
    assert _extension_del_bloque_vigente(texto) is None
    assert _declaraciones_viejas(texto) == []


@pytest.mark.parametrize(
    ("valor", "esperado", "coincide"),
    [
        ("RATIFIED", "RATIFIED", True),
        ("RATIFIED          (operador, 2026-10-19)", "RATIFIED", True),
        ("HEURISTIC (RMS)   (aceptado)", "HEURISTIC (RMS)", True),
        ("RATIFIED_PENDING", "RATIFIED", False),
        ("YES_BUT_REVOKED", "YES", False),
        ("YES REVOKED", "YES", False),
        ("YES (sin cierre", "YES", False),
        ("YES(sin espacio)", "YES", False),
        ("YESTERDAY", "YES", False),
        ("HEURISTIC (RMS)X", "HEURISTIC (RMS)", False),
        ("", "YES", False),
    ],
)
def test_valor_es_exige_el_valor_esperado_y_solo_una_anotacion_entre_parentesis(
    valor: str, esperado: str, coincide: bool
) -> None:
    assert _valor_es(valor, esperado) is coincide


_VIGENTE_CON_PROSA = (
    "> **Estado vigente (2026-10-19).** Donde digan `PENDING_TECH_LEAD` o\n"
    "> `H4_CODE_MERGE_AUTHORIZED = NO`, rige este bloque.\n"
    ">\n"
    "> ```text\n"
    "> F01_CONTRACT_AMENDMENT = RATIFIED\n"
    "> ```\n"
)
_FOTO_EN_CERCA = "\n```text\nF01_CONTRACT_AMENDMENT = PENDING_TECH_LEAD\nH4_CODE_MERGE_AUTHORIZED = NO\n```\n"


def test_declaraciones_viejas_la_prosa_del_propio_bloque_vigente_no_cuenta() -> None:
    assert _declaraciones_viejas(_VIGENTE_CON_PROSA) == []


def test_declaraciones_viejas_una_mencion_en_prosa_fuera_del_bloque_no_es_una_foto() -> None:
    con_prosa = _VIGENTE_CON_PROSA + "\nAntes decía `F01_CONTRACT_AMENDMENT = PENDING_TECH_LEAD` en el registro.\n"

    assert _declaraciones_viejas(con_prosa) == []


def test_declaraciones_viejas_una_cerca_que_no_es_text_no_es_una_foto() -> None:
    en_cerca_plana = _VIGENTE_CON_PROSA + "\n```\nF01_CONTRACT_AMENDMENT = PENDING_TECH_LEAD\n```\n"

    assert _declaraciones_viejas(en_cerca_plana) == []


def test_declaraciones_viejas_ve_las_dos_clases_de_foto_dentro_de_una_cerca_text() -> None:
    fotos = _declaraciones_viejas(_VIGENTE_CON_PROSA + _FOTO_EN_CERCA)

    assert [tipo for _, tipo in fotos] == ["F01_PENDIENTE", "MERGE_NO_AUTORIZADO"]
    assert all(posicion > len(_VIGENTE_CON_PROSA) for posicion, _ in fotos)


def test_declaraciones_viejas_ve_la_foto_en_cerca_en_cita_con_valor_viejo_anotado() -> None:
    en_cita = _VIGENTE_CON_PROSA + (
        "\n> ```text\n> F01_CONTRACT_AMENDMENT     = PENDING_TECH_LEAD    (falta ratificar)\n> ```\n"
    )

    assert [tipo for _, tipo in _declaraciones_viejas(en_cita)] == ["F01_PENDIENTE"]


def test_declarantes_cualquier_formato_incluye_binarios_y_ignora_el_cache(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "sky_claw" / "__pycache__").mkdir(parents=True)
    (tmp_path / "docs" / "a.json").write_text('{"F01_CONTRACT_AMENDMENT": "X"}', encoding="utf-8")
    (tmp_path / "docs" / "b.txt").write_text("PENDING_TECH_LEAD", encoding="utf-8")
    (tmp_path / "docs" / "c.bin").write_bytes(b"\x00\xff H4_CODE_MERGE_AUTHORIZED \x00")
    (tmp_path / "docs" / "d.md").write_text("sin tokens", encoding="utf-8")
    (tmp_path / "sky_claw" / "__pycache__" / "e.pyc").write_bytes(b"F01_CONTRACT_AMENDMENT")

    assert _declarantes(tmp_path) == {"docs/a.json", "docs/b.txt", "docs/c.bin"}
