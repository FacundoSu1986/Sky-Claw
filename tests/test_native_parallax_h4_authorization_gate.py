"""Invariantes documentales del cierre de autorización H4 `resize_normal`.

Congela la **clase** de defecto, no un caso: el documento de cierre de autorización no
puede publicar una autorización que los contratos no sostienen. Un cierre que declarara
`H4_CODE_IMPLEMENTATION_AUTHORIZED = YES` (o `READY_TO_IMPLEMENT`) mientras la condición 9
de §17 sigue `PENDING` rompe el ancla.

Cinco familias, todas enumeradas (no muestreadas):

1. **Tablas Markdown** — se verifica con las funciones del checker del repo
   (`test_native_parallax_pr700_corrective_docs_invariants`) **importadas**, no con una
   regex propia. La lección es explícita: el instrumento de medición debe ser el
   autoritativo.
2. **Tokens de veredicto** — los valores declarados en §8 son exactamente los esperados,
   y ninguna asignación contradictoria aparece en el documento.
3. **Prohibición de autorización prematura** — no puede coexistir una aprobación con un
   bloqueante abierto.
4. **F1 — envolvente estructural** — `V-03` (cuadratura) no puede volver a usarse como
   bolsa para diferir formas malformadas. Se ancla que la regla distingue `REJECT` de
   `DEFERRED` y que la restricción de V-03 es explícita.
5. **F2 — vehículo de la señal** — el contrato debe nombrar el vehículo tipado y sus
   campos; declarar el vehículo sin nombrar los campos no cierra la propagación.

Tests RESEARCH-ONLY: sin red; sólo leen el documento del PR.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

# --- instrumento autoritativo (importado, NO reimplementado) -------------------
from tests.test_native_parallax_pr700_corrective_docs_invariants import (
    _SEPARATOR,
    _inconsistent_tables,
)

_REPO = Path(__file__).resolve().parents[1]
_DOC = _REPO / "docs" / "design" / "research" / "native-parallax" / "h4-resize-normal-authorization-gate.md"

# --- familia de tokens de veredicto (§8 del documento) -------------------------
_VERDICTOS_ESPERADOS = {
    "H4_R1_NUMERIC_CONTRACT": "PASS",
    "H4_R5_PROVENANCE_CONTRACT": "PASS",
    "H4_R6_CONSUMER_COVERAGE": "PASS",
    "H4_D06": "CONTRACT_CLOSED",
    "H4_D07": "CONTRACT_CLOSED",
    "H4_TECH_LEAD_APPROVAL": "PENDING",
    "H4_CODE_IMPLEMENTATION_AUTHORIZED": "NO",
    "M6_IMPLEMENTATION_BLOCKED": "YES",
    "REAL_CORPUS_RERUN": "NO",
}

# Predicados DERIVADOS del bloque de cierre (§13). Repiten la misma condición que §8
# en otra superficie del documento: si §8 dice PENDING y §13 dice APPROVED, el
# documento se contradice. Se ancla cada superficie, no una muestra.
#
# `TECH_LEAD_SCOPE_APPROVAL` también aparece **citado** en §7 (el dictamen de #711, con
# comillas invertidas) como `=NOT_RECORDED`. Eso es una cita histórica, no una
# declaración de estado. `_declaraciones` filtra las citas entre backticks para que el
# ancla mida la superficie que declara, no la que cita.
_VERDICTOS_DERIVADOS = {
    "TECH_LEAD_SCOPE_APPROVAL": "PENDING",
    # Adjudicación adversarial (§14): los dos hallazgos materiales de la revisión de #717.
    # Repiten el estado de cierre en otra superficie; si §13 dice CLOSED y §14 dice ACCEPTED
    # pero no cerrado, el documento se contradice.
    "F1_SHAPE_CHANNEL_SEMANTICS": "CLOSED",
    "F2_DEGENERATE_TRANSPORT": "CLOSED",
    "F1_ADJUDICATION": "ACCEPTED",
    "F2_ADJUDICATION": "ACCEPTED",
    "CONDITION_9_STATUS": "PENDING",
}

# Tokens cuya presencia activa exige ausencia de bloqueantes abiertos.
_TOKENS_DE_AUTORIZACION = (
    "H4_CODE_IMPLEMENTATION_AUTHORIZED",
    "READY_TO_IMPLEMENT",
)

# --- F1: la envolvente estructural no puede diferirse invocando V-03 -------------
# El defecto de la primera versión: `forma != (H,W,3) -> DEFERRED (V-03)`.
# V-03 trata SOLO H != W. Estos son los casos estructurales que deben aparecer como
# REJECT explícito, no como DEFERRED.
_CASOS_ESTRUCTURALES_REJECT = (
    "ndim",
    "shape[-1]",
    "NaN",
    "Inf",
    "size",
)

# --- F2: el vehículo de la señal debe estar nombrado, con sus campos --------------
_VEHICULO_F2 = "NormalResizeResult"
_API_F2 = "resize_normal_diagnostic"
_CAMPOS_F2 = (
    "degenerate_count",
    "total_pixels",
    "threshold",
    "degenerate",
)
_EXCEPCION_F2 = "DegenerateNormalError"

_ASSIGNMENT = re.compile(
    r"(?P<key>H4_R1_NUMERIC_CONTRACT|H4_R5_PROVENANCE_CONTRACT|H4_R6_CONSUMER_COVERAGE"
    r"|H4_D06|H4_D07|H4_TECH_LEAD_APPROVAL|H4_CODE_IMPLEMENTATION_AUTHORIZED"
    r"|M6_IMPLEMENTATION_BLOCKED|REAL_CORPUS_RERUN|READY_TO_IMPLEMENT"
    r"|PRODUCTION_CHANGED|SCIENTIFIC_ARTIFACTS_CHANGED|PR675_CHANGED"
    r"|THRESHOLDS_TUNED|CORPUS_ACCESSED|TECH_LEAD_SCOPE_APPROVAL"
    r"|F1_SHAPE_CHANNEL_SEMANTICS|F2_DEGENERATE_TRANSPORT"
    r"|F1_ADJUDICATION|F2_ADJUDICATION|CONDITION_9_STATUS)"
    r"\s*[=:]\s*[\"'`]?(?P<value>[A-Z][A-Z0-9_]*)"
)

# Una cita entre comillas invertidas (p.ej. `` `TECH_LEAD_SCOPE_APPROVAL=NOT_RECORDED` ``)
# referencia el dictamen de #711: no declara el estado vigente. Se neutraliza antes de
# buscar declaraciones, para que el ancla mida la superficie que declara y no la que cita.
_CITA_BACKTICK = re.compile(r"`[^`\n]*`")


def _declaraciones() -> list[re.Match[str]]:
    texto = _CITA_BACKTICK.sub(lambda m: " " * len(m.group(0)), _texto())
    return list(_ASSIGNMENT.finditer(texto))


def _valores(token: str) -> set[str]:
    return {m.group("value") for m in _declaraciones() if m.group("key") == token}


def _texto() -> str:
    return _DOC.read_text(encoding="utf-8")


def _lineas() -> list[str]:
    return _texto().split("\n")


def test_el_documento_de_cierre_existe():
    assert _DOC.is_file(), f"falta el documento de cierre: {_DOC}"


def test_todas_las_tablas_markdown_son_consistentes():
    """Usa el checker del repo (importado) sobre el documento de cierre."""
    bad = _inconsistent_tables(_DOC)
    assert bad == [], f"filas con conteo de celdas inconsistente: {bad}"


def test_no_hay_barras_escapadas_en_celdas():
    """El checker del repo prohíbe `\\|` dentro de una celda. Misma regla acá."""
    ofensivas: list[tuple[int, str]] = []
    lines = _lineas()
    for i, line in enumerate(lines):
        if line.strip().startswith("|") and i + 1 < len(lines) and _SEPARATOR.match(lines[i + 1]):
            for j in range(i, len(lines)):
                if not lines[j].strip().startswith("|"):
                    break
                if r"\|" in lines[j]:
                    ofensivas.append((j + 1, lines[j].strip()))
    assert ofensivas == [], f"celdas con barra escapada: {ofensivas}"


@pytest.mark.parametrize(("token", "esperado"), sorted({**_VERDICTOS_ESPERADOS, **_VERDICTOS_DERIVADOS}.items()))
def test_los_veredictos_son_los_declarados(token: str, esperado: str):
    """Cada token de §8 tiene exactamente el valor declarado, en algún bloque del doc."""
    valores = _valores(token)
    assert valores, f"el token {token} no aparece en el documento"
    assert valores == {esperado}, f"{token}: esperado {{{esperado}}}, encontrado {sorted(valores)}"


def test_no_declara_autorizacion_con_el_contrato_abierto():
    """Falla cerrado: si la autorización es YES, no puede haber bloqueantes abiertos.

    El estado válido hoy es `H4_CODE_IMPLEMENTATION_AUTHORIZED = NO` con la condición 9
    `PENDING`. Declarar `YES` mientras `H4_TECH_LEAD_APPROVAL = PENDING` es una
    contradicción que este test rechaza.
    """
    auth = _valores("H4_CODE_IMPLEMENTATION_AUTHORIZED")
    aprobacion = _valores("H4_TECH_LEAD_APPROVAL")
    # El bloque de cierre (§13) repite la condición en otra superficie: ambas deben decir PENDING.
    derivada = _valores("TECH_LEAD_SCOPE_APPROVAL")
    assert aprobacion == {"PENDING"}, f"H4_TECH_LEAD_APPROVAL inesperado: {sorted(aprobacion)}"
    assert derivada == {"PENDING"}, (
        f"TECH_LEAD_SCOPE_APPROVAL (§13) contradice a §8 con el contrato abierto: {sorted(derivada)}"
    )
    assert auth == {"NO"}, (
        "H4_CODE_IMPLEMENTATION_AUTHORIZED debe ser NO mientras "
        f"H4_TECH_LEAD_APPROVAL = PENDING; encontrado {sorted(auth)}"
    )


def test_prohibe_declarar_ready_to_implement():
    """`READY_TO_IMPLEMENT` sólo puede aparecer en su forma prohibida (NO / negada)."""
    afirmaciones = sorted(_valores("READY_TO_IMPLEMENT"))
    assert afirmaciones, "el documento debe declarar explícitamente que READY_TO_IMPLEMENT es NO"
    assert all(v == "NO" for v in afirmaciones), f"READY_TO_IMPLEMENT afirmado con el contrato abierto: {afirmaciones}"


def test_el_cierre_declara_que_no_toco_produccion():
    """El write-set documental no puede haber alterado producción ni artefactos."""
    for clave, valor in (
        ("PRODUCTION_CHANGED", "NO"),
        ("SCIENTIFIC_ARTIFACTS_CHANGED", "NO"),
        ("PR675_CHANGED", "NO"),
        ("THRESHOLDS_TUNED", "NO"),
        ("CORPUS_ACCESSED", "NO"),
    ):
        encontrados = _valores(clave)
        assert encontrados == {valor}, f"{clave}: esperado {{{valor}}}, encontrado {sorted(encontrados)}"


def test_la_condicion_9_sigue_pendiente_y_se_cita():
    """El bloqueante debe ser explícito: la condición 9 de §17, no una vaga 'aprobación'."""
    texto = _texto()
    assert "condición 9" in texto or "condicion 9" in texto
    assert "TECH_LEAD_SCOPE_APPROVAL" in texto
    assert "PENDING" in texto


# --- Familia 4: F1 — envolvente estructural vs. V-03 ----------------------------


def _filas_tabla_f1() -> list[list[str]]:
    """Extrae las filas de la tabla de comportamiento medido de §3.4.

    Se localiza la tabla por su encabezado (`| Entrada hostil |`), no por posición
    absoluta: anclar por posición no congela el orden de ejecución ni sobrevive a
    inserciones. Devuelve las filas **de datos** (sin encabezado ni separador).
    """
    lines = _lineas()
    for i, line in enumerate(lines):
        if line.strip().startswith("| Entrada hostil"):
            filas: list[list[str]] = []
            for j in range(i + 2, len(lines)):
                if not lines[j].strip().startswith("|"):
                    break
                celdas = [c.strip() for c in lines[j].strip().strip("|").split("|")]
                filas.append(celdas)
            return filas
    return []


def _fila_por_entrada(fragmento: str) -> list[str] | None:
    """Busca la fila cuya primera celda contiene `fragmento`, ignorando backticks.

    La celda mezcla código y prosa (`` `(H, W)` 2D ``): se normaliza quitando backticks
    para que el ancla no dependa del marcado inline.
    """
    normalizado = fragmento.replace("`", "")
    for fila in _filas_tabla_f1():
        if fila and normalizado in fila[0].replace("`", ""):
            return fila
    return None


def test_f1_la_tabla_de_comportamiento_existe_y_tiene_filas():
    """Sin la tabla medida, el contrato F1 no está fundado en comportamiento observado."""
    filas = _filas_tabla_f1()
    assert filas, "falta la tabla 'Entrada hostil' de §3.4"
    assert len(filas) >= 10, f"la tabla F1 debe enumerar los casos medidos; tiene {len(filas)}"


def test_f1_cada_caso_estructural_se_declara_reject_no_deferred():
    """Ancla por **fila**, no por presencia de token en el documento.

    El defecto que la revisión marcó como material fue asociar una forma malformada a
    `DEFERRED (V-03)`. Verificar que la palabra 'REJECT' aparece en algún lado del
    documento no lo detecta: hay que verificar que **cada fila estructural** lleva
    `REJECT` como su valor de regla. Se enumera por entrada, no se muestrea.
    """
    entradas_reject = (
        "(H, W, 2)",
        "(H, W, 4)",
        "(H, W) 2D",
        "(H, W, 3, 1) 4D",
        "(0, W, 3)",
        "(H, 0, 3)",
        "NaN",
    )
    for entrada in entradas_reject:
        fila = _fila_por_entrada(entrada)
        assert fila is not None, f"falta la fila de la entrada hostil {entrada!r}"
        regla = fila[-1]
        assert "REJECT" in regla, f"la fila {entrada!r} debe declarar REJECT, tiene {regla!r}"
        assert "DEFERRED" not in regla, f"la fila {entrada!r} no puede diferirse con V-03 (hallazgo F1): {regla!r}"


def test_f1_la_fila_de_size_invalido_se_declara_reject():
    """`size` inválido es envolvente estructural, no una deferencia."""
    for entrada in ("size = 0", "size = −1", "size = 2.5"):
        fila = _fila_por_entrada(entrada)
        assert fila is not None, f"falta la fila de {entrada!r}"
        assert "REJECT" in fila[-1], f"{entrada!r} debe ser REJECT, tiene {fila[-1]!r}"


def test_f1_v03_esta_restringido_explicitamente_a_cuadratura():
    """V-03 debe quedar declarado como **únicamente** `H != W`.

    El mutante que borra 'exclusivamente' (o reemplaza la frase) sobrevive si el ancla
    sólo busca `H != W` en la vecindad: basta con que quede la mención de 'no cuadradas'.
    Se ancla la **frase de restricción completa**, no un fragmento.
    """
    texto = _texto()
    assert re.search(
        r"V-03\s+trata\s+\*\*exclusivamente\*\*\s+dimensiones\s+no\s+cuadradas",
        texto,
    ), "V-03 debe declarar explícitamente que trata **exclusivamente** dimensiones no cuadradas"


def test_f1_ninguna_fila_de_la_tabla_declara_deferred():
    """Ninguna fila de la tabla de comportamiento de §3.4 puede declarar `DEFERRED`.

    La tabla mide **envolvente estructural**: todo caso hostil medido es `REJECT` (o
    `TOLERATED` para la entrada válida de referencia). `H != W` —lo único diferido— no
    tiene fila acá porque no se midió como entrada hostil: está en la tabla de contrato
    de §3.1. Este test cierra el mutante que repone `forma != (H,W,3) -> DEFERRED (V-03)`
    en la fila del `ndim`: esa fila pasaría a declarar `DEFERRED` y el ancla lo rechaza.

    Se **enumera la tabla entera**, no se muestrean filas: muestrear es la trampa.
    """
    filas = _filas_tabla_f1()
    assert filas, "falta la tabla F1"
    for fila in filas:
        assert len(fila) >= 4, f"fila malformada en la tabla F1: {fila}"
        entrada, regla = fila[0], fila[-1]
        assert "DEFERRED" not in regla, (
            f"la fila {entrada!r} declara DEFERRED; la envolvente estructural de §3.4 "
            f"sólo admite REJECT/TOLERATED (hallazgo F1): {regla!r}"
        )
        assert ("REJECT" in regla) or ("TOLERATED" in regla), (
            f"la fila {entrada!r} debe declarar REJECT o TOLERATED, tiene {regla!r}"
        )


def test_f1_ninguna_forma_malformada_aparece_como_deferred_por_v03():
    """Ninguna fila de la tabla F1 puede diferir una forma malformada vía V-03.

    El patrón prohibido es exactamente el de la primera versión:
    `forma != (H,W,3) -> DEFERRED (V-03)`. Se rechaza sobre el texto **declarativo**.

    Las citas entre comillas invertidas se neutralizan antes de buscar, por la misma
    razón que en `_ASSIGNMENT`: §9 (N-07) y §14 (adjudicación F1) **registran** el
    defecto corregido citándolo, y registrar lo que se arregló es trazabilidad, no una
    declaración vigente. El ancla debe medir la superficie que declara, no la que cita.
    """
    texto = _CITA_BACKTICK.sub(lambda m: " " * len(m.group(0)), _texto())
    prohibido = re.compile(
        r"forma\s*!=\s*\(\s*H\s*,\s*W\s*,\s*3\s*\)[^\n]{0,40}DEFERRED",
        re.IGNORECASE,
    )
    assert not prohibido.search(texto), (
        "el documento no puede diferir 'forma != (H,W,3)' invocando V-03 (hallazgo F1) en una posición declarativa"
    )


def test_f1_la_tabla_de_contrato_de_entrada_declara_los_casos_estructurales():
    """La tabla de contrato de §3.1 debe declarar los casos estructurales como cerrados.

    El mutante que reintroduce `forma != (H,W,3) -> DEFERRED (V-03)` ataca la fila del
    `ndim` de **esta** tabla (no la de comportamiento medido). El ancla debe cubrir las
    dos superficies: §3.1 declara el contrato, §3.4 mide el comportamiento.
    """
    lines = _lineas()
    filas: list[list[str]] = []
    for i, line in enumerate(lines):
        if line.strip().startswith("| Atributo |"):
            for j in range(i + 2, len(lines)):
                if not lines[j].strip().startswith("|"):
                    break
                filas.append([c.strip() for c in lines[j].strip().strip("|").split("|")])
            break
    assert filas, "falta la tabla de contrato de entrada (§3.1)"

    por_atributo = {f[0].replace("`", "").strip(): f for f in filas if f}
    for atributo in ("ndim", "Nº de canales", "H > 0 y W > 0", "Finitud", "size destino"):
        assert atributo in por_atributo, f"la tabla §3.1 debe declarar el atributo {atributo!r}"

    # Las filas estructurales cierran (no difieren). `H ≠ W` es la única diferida.
    for atributo in ("ndim", "Nº de canales", "H > 0 y W > 0"):
        estado = por_atributo[atributo][-1]
        assert "DEFERRED" not in estado, (
            f"la fila estructural {atributo!r} no puede diferirse (hallazgo F1): {estado!r}"
        )

    # Y explícitamente: ninguna fila declara la forma malformada como DEFERRED (V-03).
    for fila in filas:
        assert "DEFERRED (V-03)" not in fila[1], f"ninguna fila estructural puede diferirse con V-03: {fila[1]!r}"


# --- Familia 5: F2 — el vehículo de la señal de degeneración --------------------


def test_f2_el_vehiculo_de_la_senal_esta_nombrado_con_sus_campos():
    """F2: declarar 'el consumidor decide' sin vehículo no cierra la propagación.

    Se ancla que el contrato nombra el resultado tipado **y** cada campo que debe
    viajar. Un vehículo sin campos declarados no transporta el conteo.
    """
    texto = _texto()
    assert _VEHICULO_F2 in texto, f"falta el vehículo tipado {_VEHICULO_F2}"
    assert _API_F2 in texto, f"falta la API de diagnóstico {_API_F2}"
    faltantes = [campo for campo in _CAMPOS_F2 if campo not in texto]
    assert not faltantes, f"el vehículo F2 no declara los campos: {faltantes}"


def test_f2_la_senal_tiene_excepcion_propia_y_no_se_confunde_con_la_de_integridad():
    """La señal de degeneración debe ser distinguible de un fallo de integridad."""
    texto = _texto()
    assert _EXCEPCION_F2 in texto, f"falta la excepción diferenciada {_EXCEPCION_F2}"
    # El punto crítico de M3 (el `continue` que excluye en silencio) debe aparecer.
    assert "continue" in texto, "el contrato debe tratar el `continue` de M3 explícitamente"


def test_f2_el_contrato_declara_que_el_vehiculo_no_esta_implementado():
    """Especificar el vehículo ≠ implementarlo. Debe declararse como pendiente del Slice 2."""
    texto = _texto()
    assert re.search(
        r"NORMAL_RESIZE_RESULT_IMPLEMENTED\s*[=:]\s*NO",
        texto,
    ), "el contrato debe declarar NORMAL_RESIZE_RESULT_IMPLEMENTED = NO (Slice 2)"


def test_la_adjudicacion_adversarial_registra_f1_y_f2_como_aceptados():
    """§14 no puede declarar cerrado un hallazgo sin registrar la adjudicación."""
    texto = _texto()
    assert "6113684862" in texto, "el registro debe citar el comentario de revisión de #717"
    assert "ACCEPTED" in texto, "los hallazgos materiales deben quedar adjudicados"
    # El veredicto de la revisión debe quedar registrado, no parafraseado.
    assert "CHANGES_REQUESTED_FOR_CONTRACT_COMPLETENESS" in texto, (
        "el veredicto de la revisión de #717 debe quedar registrado textualmente"
    )
