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

import ast
import re
import subprocess
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
    # Adjudicación de la revisión automática de Codex (§14.4). Repiten el cierre en otra
    # superficie: si §4 dice DESIGN_CLOSED y §14 dice PARTIALLY_ACCEPTED sin cierre, el
    # documento se contradice.
    "GATE_REJECTS_RUNTIME_MISMATCH": "YES",
    "HISTORICAL_CONSUMERS_ALLOWLISTED": "YES",
    "ANCHOR_HAS_FROZEN_ALLOWLIST": "YES",
    "RANGO_ES_PRECONDICION": "NO",
    "RANGO_ES_POSTCONDICION": "YES",
    "R7_WORST_CASE_PREMISE": "CONJECTURE_NOT_FALSIFIED",
    "CODEX_REJECTED": "0",
    "CODEX_PARTIALLY_ACCEPTED": "1",
    # Adjudicación de la revisión de CodeRabbit sobre 14daff53 (§14.7). Repiten el cierre en
    # otra superficie (§13 y §14.6): si §13 dice CLOSED y §14.7 dice otra cosa, el documento
    # se contradice.
    "CR3_ADJUDICATION": "ACCEPTED",
    "CR4_ADJUDICATION": "ACCEPTED",
    "CR5_ADJUDICATION": "ACCEPTED",
    "CR6_ADJUDICATION": "ACCEPTED",
    "CR_REJECTED": "0",
    "CR_ADJUDICATION_AMPLIADA": "2",
    # CR-3: contrato del camino identidad (§3.4.1).
    "IDENTITY_NOOP_ONLY_FOR_ADMISSIBLE": "YES",
    "POSTCONDITION_HAS_IDENTITY_EXCEPTION": "NO",
    "RANGO_ES_POSTCONDICION_UNIVERSAL": "YES",
    "IDENTITY_RANGE_POLICY": "CLIP_DECLARED",
    # CR-4: el censo histórico es de SIETE, no seis (N-13).
    "HISTORICAL_CONSUMERS_TOTAL": "7",
    "ANCHOR_COMPARES_EXACT_SETS": "YES",
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
    r"|F1_ADJUDICATION|F2_ADJUDICATION|CONDITION_9_STATUS"
    r"|GATE_REJECTS_RUNTIME_MISMATCH|HISTORICAL_CONSUMERS_ALLOWLISTED"
    r"|ANCHOR_HAS_FROZEN_ALLOWLIST|ANCHOR_COMPARES_EXACT_SETS"
    r"|RANGO_ES_PRECONDICION|RANGO_ES_POSTCONDICION_UNIVERSAL|RANGO_ES_POSTCONDICION"
    r"|R7_WORST_CASE_PREMISE|CODEX_REJECTED|CODEX_PARTIALLY_ACCEPTED"
    r"|CR3_ADJUDICATION|CR4_ADJUDICATION|CR5_ADJUDICATION|CR6_ADJUDICATION"
    r"|CR_REJECTED|CR_ADJUDICATION_AMPLIADA|HISTORICAL_CONSUMERS_TOTAL"
    r"|IDENTITY_NOOP_ONLY_FOR_ADMISSIBLE|POSTCONDITION_HAS_IDENTITY_EXCEPTION"
    r"|IDENTITY_RANGE_POLICY)"
    r"\s*[=:]\s*[\"'`]?(?P<value>[A-Z0-9][A-Z0-9_]*)"
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


def _tabla_por_encabezado(prefijo_encabezado: str) -> list[list[str]]:
    """Extrae las filas de datos de la **primera** tabla cuyo encabezado empieza con el prefijo.

    Se localiza la tabla por su encabezado, no por posición absoluta: anclar por posición
    no congela el orden de ejecución ni sobrevive a inserciones. Devuelve las filas **de
    datos** (sin encabezado ni separador); `[]` si el encabezado no aparece.
    """
    lines = _lineas()
    for i, line in enumerate(lines):
        if line.strip().startswith(prefijo_encabezado):
            filas: list[list[str]] = []
            for j in range(i + 2, len(lines)):
                if not lines[j].strip().startswith("|"):
                    break
                celdas = [c.strip() for c in lines[j].strip().strip("|").split("|")]
                filas.append(celdas)
            return filas
    return []


def _filas_tabla_f1() -> list[list[str]]:
    """Tabla de comportamiento medido de §10.1 (probe ejecutado, 18 filas).

    El encabezado es `| Entrada hostil |`. Es la tabla **más completa**: enumera los casos
    hostiles efectivamente ejercitados en este slice.
    """
    return _tabla_por_encabezado("| Entrada hostil")


def _filas_tabla_contrato_34() -> list[list[str]]:
    """Tabla de comportamiento medido de §3.4 (encabezado `| Entrada |`).

    Es una superficie **distinta** de la de §10.1: la de §3.4 es la que declara la
    disposición por caso (`REJECT`/`TOLERADO`) junto al contrato. Hasta la corrección del
    review de #717, el helper anterior tomaba la **primera** tabla que empezara con
    `| Entrada`, que resultó ser la de §10.1: §3.4 quedaba **sin anclar**. Eso es el patrón
    "arreglar un hermano y no al otro" — se anclan **ambas**.
    """
    return _tabla_por_encabezado("| Entrada |")


def _fila_por_entrada(fragmento: str, tabla: str = "10.1") -> list[str] | None:
    """Busca la fila cuya primera celda contiene `fragmento`, ignorando backticks.

    La celda mezcla código y prosa (`` `(H, W)` 2D ``): se normaliza quitando backticks
    para que el ancla no dependa del marcado inline.

    `tabla` selecciona la superficie: `"10.1"` (probe ejecutado) o `"3.4"` (declaración de
    contrato). Ambas se enumeran por separado — no se asume que una esté contenida en la
    otra.
    """
    normalizado = fragmento.replace("`", "")
    filas = _filas_tabla_f1() if tabla == "10.1" else _filas_tabla_contrato_34()
    for fila in filas:
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
    `TOLERATED` para la entrada válida). `H != W` —lo único diferido— no tiene fila acá
    porque no se midió como entrada hostil: está en las tablas de contrato de §3.1/§3.4.

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
        assert ("REJECT" in regla) or ("TOLERATED" in regla) or ("CLIP" in regla), (
            f"la fila {entrada!r} debe declarar REJECT, TOLERATED o CLIP, tiene {regla!r}"
        )


def _bloque_reglas_f1() -> dict[str, str]:
    """Extrae las reglas **normativas** del bloque de §3.4 (el fence con `-> REJECT`).

    Es una superficie distinta de la tabla de comportamiento medido: la tabla dice qué
    hace hoy el código; el bloque dice qué debe hacer el contrato. Mutar el bloque sin
    tocar la tabla dejaba los tests verdes (hallazgo RC-2 de la revisión). Se devuelve
    `{sujeto: disposición}` con las líneas `sujeto -> DISPOSICION`.
    """
    lineas = _lineas()
    reglas: dict[str, str] = {}
    # El separador es ` -> ` (con espacios). El sujeto puede contener `-` (p. ej.
    # `shape[-1] != 3`), así que NO se puede cortar en el primer `-`.
    #
    # Sólo se aceptan líneas con **dos espacios iniciales** y sujeto que arranque en
    # columna 3 sin espacio: es la forma de las reglas del bloque normativo de §3.4. Así se
    # excluyen (a) las citas históricas de §9/§14, que empiezan con `|`, y (b) las líneas
    # de otros fences con indentación de 4+ espacios (p. ej. los comentarios del pseudocódigo
    # de §3.4.1), que de otro modo entrarían como reglas espurias.
    patron = re.compile(r"^  (?P<sujeto>\S.*?)\s+->\s+(?P<disp>REJECT|DEFERRED|TOLERADO)\b")
    for line in lineas:
        m = patron.match(line)
        if m:
            reglas[m.group("sujeto").strip()] = m.group("disp")
    return reglas


# CR-6: cada caso estructural debe tener su PROPIA regla, con su disposición explícita.
# El mapa es el conjunto **exacto** esperado: ni `H == 0` combinado con `W == 0` en una
# sola línea, ni un recuento mínimo que toleraría perder una regla.
_REGLAS_NORMATIVAS_ESPERADAS = {
    "ndim != 3": "REJECT",
    "shape[-1] != 3": "REJECT",
    "H == 0": "REJECT",
    "W == 0": "REJECT",
    "NaN": "REJECT",
    "size no-entero": "REJECT",
    "size < 1": "REJECT",
    "H != W": "DEFERRED",
}


def test_f1_el_bloque_normativo_enumera_cada_caso_con_su_disposicion():
    """CR-6: el bloque normativo se ancla por **regla enumerada**, no por recuento mínimo.

    La versión anterior verificaba `len(reglas) >= 7` y buscaba `H == 0` dentro de una
    regla **combinada** con `W == 0`; además no exigía `size` no-entero. Un mutante podía
    cambiar `W == 0` o `size` no-entero a `TOLERADO` y el test seguía verde.

    Acá se exige: (a) el número **exacto** de reglas (una por caso, sin combinar);
    (b) cada caso estructural con **su propia** regla y la disposición esperada; (c) que el
    conjunto de sujetos `REJECT` sea exactamente el enumerado; (d) exactamente una
    `DEFERRED`, la de cuadratura.
    """
    reglas = _bloque_reglas_f1()
    assert reglas, "falta el bloque normativo de reglas `sujeto -> DISPOSICION` en §3.4"
    assert len(reglas) == len(_REGLAS_NORMATIVAS_ESPERADAS), (
        f"el bloque debe tener exactamente {len(_REGLAS_NORMATIVAS_ESPERADAS)} reglas "
        f"(una por caso, sin combinar); tiene {len(reglas)}: {sorted(reglas)}"
    )

    for sujeto, disp in _REGLAS_NORMATIVAS_ESPERADAS.items():
        coincidencias = [s for s in reglas if sujeto in s]
        assert len(coincidencias) == 1, (
            f"debe haber EXACTAMENTE una regla que mencione {sujeto!r} (CR-6); hay {len(coincidencias)}: {coincidencias}"
        )
        assert reglas[coincidencias[0]] == disp, (
            f"la regla {coincidencias[0]!r} debe ser {disp}, es {reglas[coincidencias[0]]!r}"
        )

    # El conjunto de sujetos REJECT debe ser exactamente el enumerado (ni más ni menos).
    reject = sorted(s for s, d in reglas.items() if d == "REJECT")
    esperados_reject = sorted(s for s, d in _REGLAS_NORMATIVAS_ESPERADAS.items() if d == "REJECT")
    assert len(reject) == len(esperados_reject), (
        f"el bloque debe tener exactamente {len(esperados_reject)} reglas REJECT; tiene {len(reject)}: {reject}"
    )

    # Y exactamente una DEFERRED, la de cuadratura (V-03).
    diferidas = [s for s, d in reglas.items() if d == "DEFERRED"]
    assert len(diferidas) == 1, f"debe haber exactamente una regla DEFERRED, hay {len(diferidas)}: {diferidas}"
    assert "H != W" in diferidas[0] or "H ≠ W" in diferidas[0], (
        f"la única regla diferida debe ser la de cuadratura (H != W), es {diferidas[0]!r}"
    )

    # Ninguna regla con finitud puede diferirse (V-04 está cerrado).
    for sujeto, disp in reglas.items():
        if "NaN" in sujeto or "Inf" in sujeto:
            assert disp == "REJECT", f"la regla de finitud {sujeto!r} no puede diferirse: {disp!r}"


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


def _fence_por_marca(marca: str) -> str:
    """Devuelve el cuerpo del fence ```text ... ``` que contiene `marca`.

    Necesario porque un token puede aparecer **declarado** en un bloque normativo y
    **citado** en las tablas de §9/§14. Anclar sobre el documento entero deja que la cita
    sostenga el ancla cuando la declaración cambia (lección M-18: mutar el token de §5.3
    sobrevivía porque §14.3 lo cita). Se mide el bloque que **declara**.
    """
    lineas = _lineas()
    for i, ln in enumerate(lineas):
        if ln.strip().startswith("```"):
            for j in range(i + 1, len(lineas)):
                if lineas[j].strip().startswith("```"):
                    cuerpo = "\n".join(lineas[i + 1 : j])
                    if marca in cuerpo:
                        return cuerpo
                    break
    return ""


def test_f2_la_fachada_resize_normal_es_fail_closed():
    """RC-1: la fachada no puede devolver un array degenerado en silencio.

    La primera versión dejaba que `resize_normal` retornara `res.array` sin inspeccionar
    `res.degenerate`: un llamador directo recibía vectores nulos sin señal.

    No basta con verificar que el token existe: hay que verificar que el **código del
    contrato** (el fence del Slice 2) tenga (a) la condición `if res.degenerate`, (b) el
    `raise DegenerateNormalError` dentro de esa rama, y (c) el `return res.array` en la
    rama contraria. Un mutante que ponga `if True:` elimina la distinción de ramas y este
    ancla lo rechaza.

    El token se busca **en su fence de declaración** (el que define la fachada), no en el
    documento entero: §14.3 lo cita y esa cita no debe sostener el ancla (lección M-18).
    """
    texto = _texto()

    # Extrae el cuerpo del fence que define la fachada y ancla el token **ahí**.
    lineas = _lineas()
    inicio = next(
        (i for i, ln in enumerate(lineas) if "def resize_normal(n, size)" in ln),
        None,
    )
    assert inicio is not None, "falta la firma de la fachada `resize_normal` en el contrato"
    cuerpo = lineas[inicio : inicio + 16]

    # El token declarativo debe estar en la **misma región declarativa** (§5.3), y valer YES.
    assert re.search(
        r"RESIZE_NORMAL_FACADE_FAIL_CLOSED\s*[=:]\s*YES",
        _fence_por_marca("RESIZE_NORMAL_FACADE_FAIL_CLOSED"),
    ), (
        "el contrato debe declarar RESIZE_NORMAL_FACADE_FAIL_CLOSED = YES **en su fence "
        "normativo** (RC-1); una cita en §14 no cuenta como declaración"
    )

    condicion = [ln for ln in cuerpo if re.search(r"if\s+res\.degenerate\s*:", ln)]
    assert condicion, (
        "la fachada debe condicionar en `res.degenerate` (no en un literal): "
        f"encontrado {[ln.strip() for ln in cuerpo if 'if ' in ln]}"
    )
    # Dentro de la rama condicional debe haber un raise, y fuera un return del array.
    idx = cuerpo.index(condicion[0])
    rama = cuerpo[idx : idx + 3]
    assert any("raise DegenerateNormalError" in ln for ln in rama), (
        f"la rama `if res.degenerate` debe lanzar DegenerateNormalError: {rama}"
    )
    assert any(re.search(r"return\s+res\.array", ln) for ln in cuerpo), (
        "la fachada debe devolver `res.array` cuando no es degenerado"
    )
    # Y el test sintético que discrimina la pérdida silenciosa debe estar exigido.
    assert "test_fachada_no_devuelve_array_degenerado_en_silencio" in texto, (
        "el contrato debe exigir el test que discrimina la fachada silenciosa"
    )
    assert "test_fachada_sobre_entrada_valida_sigue_siendo_no_op_valido" in texto, (
        "el contrato debe exigir el test que protege el comportamiento válido de la fachada"
    )


def test_f2_la_regla_de_simetria_de_hermanos_esta_declarada():
    """RC-1 generaliza: proteger sólo `load_asset` deja a la fachada como hermano no cubierto."""
    texto = _texto()
    assert "Simetría de hermanos" in texto or "simetría de hermanos" in texto, (
        "el contrato debe declarar la regla de simetría de hermanos (RC-1)"
    )


def test_la_adjudicacion_adversarial_registra_f1_y_f2_como_aceptados():
    """§14 no puede declarar cerrado un hallazgo sin registrar la adjudicación."""
    texto = _texto()
    assert "6113684862" in texto, "el registro debe citar el comentario de revisión de #717"
    assert "ACCEPTED" in texto, "los hallazgos materiales deben quedar adjudicados"
    # El veredicto de la revisión debe quedar registrado, no parafraseado.
    assert "CHANGES_REQUESTED_FOR_CONTRACT_COMPLETENESS" in texto, (
        "el veredicto de la revisión de #717 debe quedar registrado textualmente"
    )


# --- Familia 6: revisión de Codex (N-09 … N-12) ----------------------------------


def _bloque_identidad_r5() -> str:
    """Extrae el fence `IDENTITY_TUPLE = (...)` de §4.1.

    Se ancla sobre la **tupla misma**, no sobre el documento entero: si sólo se busca
    `numpy_version` en cualquier posición, el token podría sobrevivir en la prosa que
    *explica* por qué hace falta, mientras la tupla que el runner recalcula queda sin él.
    Medir la superficie que obliga, no la que comenta.
    """
    lineas = _lineas()
    inicio = next((i for i, ln in enumerate(lineas) if ln.strip().startswith("IDENTITY_TUPLE")), None)
    assert inicio is not None, "falta la tupla de identidad de R5 (§4.1)"
    cuerpo: list[str] = []
    for ln in lineas[inicio:]:
        cuerpo.append(ln)
        if ln.strip() == ")":
            break
    return "\n".join(cuerpo)


def test_codex_n09_la_tupla_de_identidad_incluye_el_runtime():
    """N-09: la identidad de R5 no puede omitir Python/NumPy/Pillow.

    El `module_sha256_lf` hashea **nuestro** código, no la librería que lo ejecuta. Con la
    estabilidad entre versiones en `NOT_MEASURED` y el kernel dependiente de Pillow, dos
    corridas con distinto runtime no son comparables. Se enumera cada versión exigida.
    """
    tupla = _bloque_identidad_r5()
    for campo in ("python_version", "numpy_version", "pillow_version"):
        assert campo in tupla, f"la IDENTITY_TUPLE de R5 debe incluir `{campo}` (hallazgo N-09); tupla actual:\n{tupla}"


def test_codex_n09_el_gate_rechaza_mismatch_de_runtime():
    """Declarar el campo no basta: el algoritmo del gate debe **rechazar** el mismatch."""
    texto = _texto()
    assert re.search(r"GATE_REJECTS_RUNTIME_MISMATCH\s*[=:]\s*YES", texto), (
        "el contrato debe declarar GATE_REJECTS_RUNTIME_MISMATCH = YES (N-09)"
    )
    # La regla fail-closed debe nombrar las tres versiones y la acción STOP.
    assert re.search(
        r"python_version\s*\|\s*numpy_version\s*\|\s*pillow_version",
        texto,
    ), "la regla fail-closed debe enumerar las tres versiones del runtime (N-09)"
    assert "STOP_M6_EXECUTION" in texto, "la regla de runtime debe abortar con STOP_M6_EXECUTION (N-09)"


def _tabla_allowlist_n10() -> dict[str, str]:
    """Extrae la allowlist histórica de §5.1: `{ruta repo-relativa: símbolo}`.

    La tabla tiene **cuatro** columnas (`Ruta repo-relativa | Símbolo | Localización |
    Tratamiento`). Se localiza por su encabezado y se exige la marca `EXCLUIDO por diseño`
    en la columna de tratamiento. Se ancla la allowlist **entera** (enumerando cada fila), no
    la presencia de un nombre suelto: un nombre puede sobrevivir en la prosa de §14 mientras
    su fila se borra de la allowlist (lección M-23).
    """
    entradas: dict[str, str] = {}
    for fila in _tabla_por_encabezado("| Ruta repo-relativa"):
        if len(fila) >= 4 and "EXCLUIDO por diseño" in fila[3]:
            ruta = fila[0].replace("`", "").strip()
            simbolo = fila[1].replace("`", "").strip()
            entradas[ruta] = simbolo
    return entradas


# --- CR-4 / N-13: censo exacto de consumidores por AST ---------------------------
# El censo congelado. Se compara por IGUALDAD DE CONJUNTOS contra el censo AST real y
# contra la allowlist del documento: agregar un consumidor (producción, histórico o test)
# rompe el ancla hasta que se le declare tratamiento.
# Slice 2 (issue #721): la fachada `resize_normal` delega en `resize_normal_diagnostic`,
# así que `authored_dataset.py` deja de referenciar el nombre `resize_normal` como
# `ast.Name`. El censo NO se recorta: se amplía a la superficie que el contrato §5.4
# manda enumerar ("el conjunto que llama a `resize_normal` **o a la varita
# `resize_normal_diagnostic`**"), de modo que el módulo que define y ejerce el resizer
# sigue cubierto.
_SIMBOLOS_CONSUMIDOS = ("resize_normal", "resize_normal_diagnostic", "load_asset")

_CENSO_PRODUCCION_ESPERADO = frozenset(
    {
        ("sky_claw/local/native_parallax/research/authored_dataset.py", "resize_normal_diagnostic"),
        ("sky_claw/local/native_parallax/research/run_exp_m2.py", "load_asset"),
        ("sky_claw/local/native_parallax/research/run_exp_m3.py", "load_asset"),
        ("sky_claw/local/native_parallax/research/run_exp_m4.py", "load_asset"),
        ("sky_claw/local/native_parallax/research/run_exp_m5.py", "load_asset"),
    }
)

# SIETE, no seis: el review citaba seis y omitía `phase_c2_h4_corpus.py` (hermano no
# correctivo de `phase_c2_h4_corpus_corrective.py`). Hallazgo N-13.
_ALLOWLIST_HISTORICA_ESPERADA = frozenset(
    {
        (
            "docs/validation/native-parallax-h1-h5-falsification-impact-audit-20261007/scripts/h4_magnitude_probe.py",
            "resize_normal",
        ),
        (
            "docs/validation/native-parallax-h1-h5-falsification-impact-audit-20261007/scripts/phase_a_synthetic.py",
            "resize_normal",
        ),
        (
            "docs/validation/native-parallax-h1-h5-falsification-impact-audit-20261007/scripts/phase_c2_h4_corpus.py",
            "resize_normal",
        ),
        (
            "docs/validation/native-parallax-h1-h5-falsification-impact-audit-20261007/scripts/phase_c_real_impact.py",
            "load_asset",
        ),
        (
            "docs/validation/native-parallax-h1-h5-falsification-impact-audit-20261007/"
            "corrective-20261008/scripts/h4_magnitude_probe_corrective.py",
            "resize_normal",
        ),
        (
            "docs/validation/native-parallax-h1-h5-falsification-impact-audit-20261007/"
            "corrective-20261008/scripts/phase_c2_h4_corpus_corrective.py",
            "resize_normal",
        ),
        (
            "docs/validation/native-parallax-h1-h5-falsification-impact-audit-20261007/"
            "corrective-20261008/scripts/phase_c_corrective.py",
            "load_asset",
        ),
    }
)

_CENSO_TESTS_ESPERADO = frozenset(
    {
        ("tests/test_native_parallax_exp_m2.py", "resize_normal"),
        ("tests/test_native_parallax_exp_m3.py", "load_asset"),
        # Slice 2 (issue #721): el ancla hace exactamente lo que fue diseñada a hacer —
        # un consumidor nuevo rompe la igualdad hasta que se le declare tratamiento.
        ("tests/test_native_parallax_h4_resize_normal.py", "resize_normal"),
    }
)


def _censo_ast_consumidores() -> dict[str, set[tuple[str, str]]]:
    """Censo por AST de los consumidores de `resize_normal` / `load_asset` en .py tracked.

    Enumera **referencias** (`ast.Name`), no sólo llamadas: dos consumidores históricos
    asignan el símbolo a una variable (`resizer = ... else resize_normal`) y un ancla basada
    en `ast.Call` los perdería — el mismo punto ciego que la lista original (N-13). Los
    nombres en sentencias `import` son nodos `ast.alias`, no `ast.Name`, así que se excluyen
    solos; las definiciones (`def load_asset`) también (el nombre es un atributo del
    `FunctionDef`, no un `Name`).
    """
    listado = subprocess.run(
        ["git", "ls-files", "*.py"],
        cwd=_REPO,
        capture_output=True,
        text=True,
        check=True,
    )
    censo: dict[str, set[tuple[str, str]]] = {"produccion": set(), "historico": set(), "tests": set()}
    for rel in listado.stdout.splitlines():
        rel = rel.strip()
        if not rel:
            continue
        ruta = _REPO / rel
        if not ruta.is_file():
            continue
        try:
            arbol = ast.parse(ruta.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        encontrados = {
            nodo.id for nodo in ast.walk(arbol) if isinstance(nodo, ast.Name) and nodo.id in _SIMBOLOS_CONSUMIDOS
        }
        if not encontrados:
            continue
        if rel.startswith("sky_claw/"):
            destino = "produccion"
        elif rel.startswith("docs/"):
            destino = "historico"
        elif rel.startswith("tests/"):
            destino = "tests"
        else:
            continue
        for simbolo in encontrados:
            censo[destino].add((rel, simbolo))
    return censo


def test_cr4_el_censo_ast_coincide_con_la_allowlist_del_documento():
    """CR-4: el censo real (AST) debe ser **exactamente** la allowlist declarada.

    Tres igualdades de conjuntos: (1) producción AST == censo productivo congelado;
    (2) históricos AST == allowlist del documento == allowlist congelada; (3) tests AST ==
    censo de tests congelado. Un consumidor nuevo en cualquiera de las tres superficies
    rompe el ancla hasta que se le declare tratamiento. Un ancla que sólo comparara
    substrings de nombre de archivo no detectaría ni una ruta nueva ni una fila fusionada.
    """
    censo = _censo_ast_consumidores()

    assert censo["produccion"] == set(_CENSO_PRODUCCION_ESPERADO), (
        "el censo productivo por AST difiere del congelado: "
        f"nuevos={sorted(censo['produccion'] - set(_CENSO_PRODUCCION_ESPERADO))} "
        f"faltantes={sorted(set(_CENSO_PRODUCCION_ESPERADO) - censo['produccion'])}"
    )
    assert censo["tests"] == set(_CENSO_TESTS_ESPERADO), (
        "el censo de tests por AST difiere del congelado: "
        f"nuevos={sorted(censo['tests'] - set(_CENSO_TESTS_ESPERADO))} "
        f"faltantes={sorted(set(_CENSO_TESTS_ESPERADO) - censo['tests'])}"
    )

    allowlist_doc = _tabla_allowlist_n10()
    assert len(allowlist_doc) == 7, (
        f"la allowlist del documento debe tener 7 filas independientes (CR-4); tiene {len(allowlist_doc)}"
    )
    declarado = {(ruta, simbolo) for ruta, simbolo in allowlist_doc.items()}
    assert declarado == set(_ALLOWLIST_HISTORICA_ESPERADA), (
        "la allowlist del documento difiere del conjunto congelado: "
        f"extra={sorted(declarado - set(_ALLOWLIST_HISTORICA_ESPERADA))} "
        f"faltantes={sorted(set(_ALLOWLIST_HISTORICA_ESPERADA) - declarado)}"
    )
    assert censo["historico"] == set(_ALLOWLIST_HISTORICA_ESPERADA), (
        "el censo histórico real difiere del congelado (N-13): "
        f"nuevos={sorted(censo['historico'] - set(_ALLOWLIST_HISTORICA_ESPERADA))} "
        f"faltantes={sorted(set(_ALLOWLIST_HISTORICA_ESPERADA) - censo['historico'])}"
    )
    assert len(_ALLOWLIST_HISTORICA_ESPERADA) == 7, "el censo histórico es de SIETE archivos, no seis (N-13)"


def test_codex_n10_los_consumidores_historicos_estan_allowlistados():
    """N-10: la frontera productivo/histórico debe estar **declarada**, no supuesta.

    Los invocadores de `docs/validation/` son reales y tracked. Excluirlos sin decirlo es
    indistinguible de omitirlos. Se exige que cada uno figure con su razón de exclusión.

    Se ancla la allowlist como **tabla**: se enumera cada fila. Buscar los nombres en el
    documento entero no alcanza — un nombre citado en §14.7 sostendría el ancla aunque su
    fila desapareciera de la allowlist.
    """
    texto = _texto()
    assert re.search(r"HISTORICAL_CONSUMERS_ALLOWLISTED\s*[=:]\s*YES", texto), (
        "el contrato debe declarar HISTORICAL_CONSUMERS_ALLOWLISTED = YES (N-10)"
    )
    assert re.search(r"ANCHOR_HAS_FROZEN_ALLOWLIST\s*[=:]\s*YES", texto), (
        "el ancla de §5.4 debe declarar ANCHOR_HAS_FROZEN_ALLOWLIST = YES (N-10)"
    )

    allowlist = _tabla_allowlist_n10()
    assert allowlist, "falta la tabla de allowlist histórica en §5.1 (N-10)"

    # Se enumera cada consumidor histórico citado **como fila de la allowlist**.
    for consumidor in (
        "h4_magnitude_probe.py",
        "phase_a_synthetic.py",
        "phase_c2_h4_corpus.py",
        "h4_magnitude_probe_corrective.py",
        "phase_c2_h4_corpus_corrective.py",
        "phase_c_real_impact.py",
        "phase_c_corrective.py",
    ):
        assert any(consumidor in archivo for archivo in allowlist), (
            f"la allowlist histórica debe enumerar {consumidor!r} como entrada propia "
            f"(hallazgo N-10); entradas actuales: {sorted(allowlist)}"
        )
    # Y cada entrada declara su símbolo ejercido (no basta con el nombre del archivo).
    assert all(simbolo in ("resize_normal", "load_asset") for simbolo in allowlist.values()), (
        f"cada entrada de la allowlist debe declarar qué símbolo ejerce: {allowlist}"
    )


def test_codex_n11_el_rango_por_canal_es_postcondicion_no_precondicion():
    """N-11: `[-1,1]` no puede leerse a la vez como rechazo y como clip.

    Se anclan los dos lados: la disposición cerrada (postcondición) y el caso medido
    (`1000.0` tolerado) que la hace observable. Sólo anclar el token dejaría pasar un
    cambio de la tabla que reintrodujera el rechazo.
    """
    texto = _texto()
    assert re.search(r"RANGO_ES_PRECONDICION\s*[=:]\s*NO", texto), (
        "el contrato debe declarar RANGO_ES_PRECONDICION = NO (N-11)"
    )
    assert re.search(r"RANGO_ES_POSTCONDICION\s*[=:]\s*YES", texto), (
        "el contrato debe declarar RANGO_ES_POSTCONDICION = YES (N-11)"
    )
    # El caso medido que discrimina: 1000.0 debe seguir siendo tolerado con clip.
    # Vive en la tabla de **§3.4** (la de §10.1 es el probe de formas hostiles y no
    # incluye el caso de rango). Se apunta a la superficie correcta.
    fila = _fila_por_entrada("1000.0", tabla="3.4")
    assert fila is not None, "falta la fila de la entrada `1000.0` en la tabla de §3.4"
    assert "TOLER" in fila[-1].upper() or "clip" in fila[-1].lower(), (
        f"la fila de `1000.0` debe declarar clip tolerado, no rechazo (N-11): {fila[-1]!r}"
    )
    assert "REJECT" not in fila[-1], f"un valor finito fuera de rango NO se rechaza (N-11): {fila[-1]!r}"


def test_codex_n11_la_tabla_34_esta_anclada_por_si_misma():
    """La tabla de §3.4 no puede quedar sin anclar detrás de la de §10.1.

    Defecto descubierto en esta corrección: `_filas_tabla_f1()` tomaba la **primera** tabla
    cuyo encabezado empezara con `| Entrada`, que es la de §10.1. La tabla de §3.4 —que es
    la que declara la disposición por caso— quedaba **sin cubrir**. Se enumera cada fila de
    §3.4 y se exige una disposición explícita, igual que en §10.1.
    """
    filas = _filas_tabla_contrato_34()
    assert filas, "falta la tabla de comportamiento medido de §3.4 (encabezado `| Entrada |`)"
    assert len(filas) >= 8, f"la tabla de §3.4 debe enumerar los casos medidos; tiene {len(filas)}"
    for fila in filas:
        assert len(fila) >= 3, f"fila malformada en la tabla de §3.4: {fila}"
        disposicion = fila[-1]
        assert ("REJECT" in disposicion) or ("No" in disposicion) or ("Sí" in disposicion), (
            f"cada fila de §3.4 debe declarar su disposición explícita: {fila!r}"
        )
    # El caso de rango tolerado debe estar en §3.4 y NO puede decir REJECT.
    rango = _fila_por_entrada("1000.0", tabla="3.4")
    assert rango is not None and "REJECT" not in rango[-1], (
        "§3.4 debe declarar el rango finito fuera de [-1,1] como tolerado, no rechazado (N-11)"
    )


def test_codex_n12_la_premisa_de_nyquist_es_conjetura_no_establecida():
    """N-12: 'Nyquist = peor caso' no puede figurar como resultado sin derivación.

    R7 sigue `DEFERRED` en su valor; lo que se prohíbe es presentar la premisa como
    establecida. Se ancla que queda marcada como conjetura **y** que la obligación de
    falsación por ratio/fase/borde está escrita.
    """
    texto = _texto()
    assert re.search(r"R7_WORST_CASE_PREMISE\s*[=:]\s*CONJECTURE_NOT_FALSIFIED", texto), (
        "el contrato debe declarar R7_WORST_CASE_PREMISE = CONJECTURE_NOT_FALSIFIED (N-12)"
    )
    assert "CONJETURA" in texto, (
        "la premisa de Nyquist debe declararse explícitamente como conjetura, no como resultado (N-12)"
    )
    # La falsación debe cubrir ratio, fase y borde — no sólo Nyquist.
    for eje in ("ratio", "fase", "borde"):
        assert eje in texto, f"la falsación de la cota debe barrer el eje {eje!r} (N-12)"
    # Y el valor del umbral sigue fuera del documento.
    assert re.search(r"R7_NUMERIC_VALUE\s*[=:]\s*DEFERRED", texto), (
        "R7_NUMERIC_VALUE debe seguir DEFERRED a Slice 2 (N-12, sin cambio)"
    )
    # El umbral no puede quedar fijado: se ancla en **su fence normativo** de §6.1, no en
    # el documento entero. `THRESHOLD_FIXED_HERE` aparecía además citado en §7 y §14; mutar
    # la declaración de §6.1 sobrevivía porque las citas seguían diciendo `NO` (lección M-28).
    assert re.search(
        r"THRESHOLD_FIXED_HERE\s*[=:]\s*NO",
        _fence_por_marca("THRESHOLD_FIXED_HERE"),
    ), (
        "el umbral no puede quedar fijado en este documento (N-12); se mide en su fence "
        "normativo de §6.1, no en las citas de §7/§14"
    )


def test_codex_los_cinco_hilos_quedan_adjudicados():
    """§14.4 debe registrar los cinco hilos de Codex con su adjudicación explícita."""
    texto = _texto()
    for tid in ("4243420665", "4243420673", "4243420679", "4243420687", "4243420695"):
        assert tid in texto, f"el registro §14.4 debe citar el hilo de Codex {tid}"
    assert re.search(r"CODEX_THREADS_TOTAL\s*[=:]\s*5", texto), (
        "el bloque de §14.4 debe declarar CODEX_THREADS_TOTAL = 5"
    )
    assert re.search(r"CODEX_REJECTED\s*[=:]\s*0", texto), (
        "ningún hallazgo de Codex se rechaza en bloque; el bloque debe declararlo"
    )
    assert re.search(r"CODEX_PARTIALLY_ACCEPTED\s*[=:]\s*1", texto), (
        "el hilo N-12 es PARTIALLY_ACCEPTED; el bloque debe declararlo (no forzar ACCEPTED)"
    )


# --- Familia 7: CR-3 — camino identidad (rango / no-op) ---------------------------


def test_cr3_el_camino_identidad_valida_la_envolvente():
    """CR-3: el camino identidad no puede ser un bypass del contrato de entrada.

    Medido: con `H == W == size` la implementación vigente devuelve `1000.0` sin clip,
    `NaN`/`Inf`, `(2,2,4)` y `(2,2)` — no ejecuta **ningún** contrato. El contrato debe
    declarar que la envolvente (F1) y la finitud (V-04) corren **antes** de la bifurcación.
    """
    texto = _texto()
    for token, valor in (
        ("IDENTITY_PATH_VALIDATES_ENVELOPE", "YES"),
        ("IDENTITY_PATH_VALIDATES_FINITENESS", "YES"),
        ("IDENTITY_NOOP_ONLY_FOR_ADMISSIBLE", "YES"),
        ("IDENTITY_RANGE_POLICY", "CLIP_DECLARED"),
    ):
        assert re.search(rf"{token}\s*[=:]\s*{valor}", texto), f"el contrato debe declarar {token} = {valor} (CR-3)"


def test_cr3_la_postcondicion_no_tiene_excepcion_de_identidad():
    """CR-3: la postcondición `[-1,1]` vale en ambos caminos, sin excepción.

    Se descartó explícitamente la alternativa de declarar una "excepción de identidad": una
    postcondición con excepciones deja de ser una postcondición. Y el clip aplicado en la
    identidad debe ser el **ya declarado** (N-11), no una corrección numérica nueva.
    """
    texto = _texto()
    assert re.search(r"POSTCONDITION_HAS_IDENTITY_EXCEPTION\s*[=:]\s*NO", texto), (
        "el contrato no puede declarar una excepción de identidad a la postcondición (CR-3)"
    )
    assert re.search(r"RANGO_ES_POSTCONDICION_UNIVERSAL\s*[=:]\s*YES", texto), (
        "el contrato debe declarar RANGO_ES_POSTCONDICION_UNIVERSAL = YES (CR-3)"
    )
    assert re.search(r"IDENTITY_RANGE_POLICY\s*[=:]\s*CLIP_DECLARED", texto), (
        "la identidad debe aplicar el clip ya declarado, no una corrección nueva (CR-3)"
    )


def test_cr3_la_tabla_de_discriminacion_cubre_ambos_caminos():
    """CR-3: la tabla de §3.4.1 debe cubrir las clases de entrada × los dos caminos.

    Se enumera cada fila: una tabla que sólo cubriera el camino resize (o que dejara la
    celda de identidad vacía) no distinguiría la clase de defecto.
    """
    filas = _tabla_por_encabezado("| Clase de entrada |")
    assert filas, "falta la tabla de discriminación de §3.4.1 (CR-3)"

    for fila in filas:
        assert len(fila) == 3, f"la tabla debe declarar identidad y resize por fila: {fila}"
        assert fila[1].strip(), f"celda de identidad vacía: {fila}"
        assert fila[2].strip(), f"celda de resize vacía: {fila}"

    # La fila de fuera de rango debe clipear en identidad (no rechazar) — decisión de CR-3.
    fuera = next((f for f in filas if "Fuera de rango" in f[0]), None)
    assert fuera is not None, "falta la fila de 'fuera de rango' en la tabla de §3.4.1"
    assert "clip" in fuera[1].lower(), (
        f"la identidad debe clipear el fuera de rango finito, no rechazarlo (CR-3): {fuera[1]!r}"
    )
    assert "REJECT" not in fuera[1], (
        f"la identidad NO debe rechazar el fuera de rango finito (contradiría N-11): {fuera[1]!r}"
    )

    # La fila de no finita debe rechazar en AMBOS caminos.
    no_finita = next((f for f in filas if "No finita" in f[0]), None)
    assert no_finita is not None, "falta la fila de 'no finita' en la tabla de §3.4.1"
    assert "REJECT" in no_finita[1] and "REJECT" in no_finita[2], (
        f"la entrada no finita debe rechazarse en ambos caminos (V-04): {no_finita}"
    )

    # La fila válida debe declarar el no-op en identidad.
    valida = next((f for f in filas if f[0].startswith("Válida")), None)
    assert valida is not None, "falta la fila de entrada válida"
    assert "no-op" in valida[1].lower(), f"la identidad debe ser no-op para entrada válida: {valida[1]!r}"


# --- Familia 8: CR-5 — la prosa de la fachada no contradice su contrato ------------


def _seccion_531() -> str:
    """Cuerpo de §5.3.1 (desde su encabezado hasta el siguiente `###`/`####`)."""
    lineas = _lineas()
    inicio = next((i for i, ln in enumerate(lineas) if ln.startswith("#### 5.3.1")), None)
    assert inicio is not None, "falta la subsección §5.3.1"
    fin = len(lineas)
    for i in range(inicio + 1, len(lineas)):
        if lineas[i].startswith("#### ") or lineas[i].startswith("### "):
            fin = i
            break
    return "\n".join(lineas[inicio:fin])


def test_cr5_la_prosa_de_la_fachada_no_contradice_el_contrato():
    """CR-5: §5.3.1 no puede decir que `resize_normal` no lanza y devuelve un tipado.

    La prosa normativa decía «`resize_normal` **no** lanza por degeneración (sigue devolviendo
    un `NDArray`) … devuelve un resultado tipado», mientras el pseudocódigo inmediatamente
    inferior y §5.3.4 prescriben lo contrario. Se anclan **las dos** superficies: la prosa y
    el pseudocódigo, no sólo sus nombres.
    """
    seccion = _seccion_531()

    # (a) La prosa no puede atribuir el "no lanza" a la fachada.
    assert not re.search(r"`resize_normal`\s*\*\*no\*\*\s+lanza", seccion), (
        "§5.3.1 no puede afirmar que `resize_normal` no lanza por degeneración (CR-5)"
    )
    # (b) La superficie de diagnóstico es la que devuelve el resultado tipado.
    assert re.search(r"resize_normal_diagnostic.{0,220}resultado\s+tipado", seccion, re.DOTALL), (
        "§5.3.1 debe declarar que `resize_normal_diagnostic` devuelve el resultado tipado (CR-5)"
    )
    # (c) La fachada lanza DegenerateNormalError.
    assert re.search(r"`resize_normal`.{0,220}lanza\s+`?DegenerateNormalError", seccion, re.DOTALL), (
        "§5.3.1 debe declarar que `resize_normal` lanza `DegenerateNormalError` (CR-5)"
    )
    # (d) La distinción de identidad se preserva: asset_id=None en fachada vs asset en
    #     load_asset. Se ancla en el **fence** que declara cada superficie, no en el
    #     documento entero: la prosa de §5.3.1 *cita* `asset_id = None` y esa cita no debe
    #     sostener el ancla (lección M-41: mutar el fence sobrevivía porque la prosa lo citaba).
    fachada = _fence_por_marca("def resize_normal(n, size)")
    assert fachada, "falta el fence que declara la fachada `resize_normal` (CR-5)"
    assert re.search(r"asset_id\s*=\s*None", fachada), (
        "la fachada debe declarar asset_id=None **en su fence**, no sólo citarlo en la prosa (CR-5)"
    )
    carga = _fence_por_marca("resize_normal_diagnostic(normal, resolution")
    assert carga, "falta el fence que declara `load_asset` (CR-5)"
    assert re.search(r"asset_id\s*=\s*spec\.asset_id", carga), (
        "load_asset debe conservar la identidad del asset **en su fence** (CR-5)"
    )


def test_cr_los_cuatro_hilos_quedan_adjudicados():
    """§14.7 debe registrar los cuatro hilos de CodeRabbit (CR-3…CR-6) con su adjudicación."""
    texto = _texto()
    for tid in ("4243666873", "4243666878", "4243666883", "4243666891"):
        assert tid in texto, f"el registro §14.7 debe citar el hilo de CodeRabbit {tid}"
    assert re.search(r"CR_THREADS_TOTAL\s*[=:]\s*4", texto), "el bloque de §14.7 debe declarar CR_THREADS_TOTAL = 4"
    assert re.search(r"CR_REJECTED\s*[=:]\s*0", texto), "ningún hallazgo de CR se rechaza en bloque"
    assert re.search(r"CR_ADJUDICATION_AMPLIADA\s*[=:]\s*2", texto), (
        "CR-3 y CR-4 se adjudican ampliados; el bloque debe declararlo (no forzar la forma literal)"
    )
