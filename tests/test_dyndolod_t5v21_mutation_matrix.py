"""T5-v2.1 — matriz de mutación M9-M18 del wiring UIA.

Cada test de acá ancla la PROPIEDAD ESTRUCTURAL que su mutación rompería, y el
docstring dice cuál es el test de conducta que también se pone rojo. La
mutación se aplica a mano, se corre, se verifica RED y se revierte: eso es lo
que se reportó en el informe, no un verde permanente.

  M9  expected de TexGen = family_root            → este archivo + W1/W3
  M10 expected de DynDOLOD = texgen_root          → este archivo + W2/W4
  M11 UNAVAILABLE ⇒ success                       → W8 + este archivo
  M12 bypass del initial gate                     → W5/W6 + este archivo
  M13 bypass del final gate                       → W11/W12 + este archivo
  M14 auto-approve de la categoría nueva          → H2 (tests/test_hitl.py)
  M15 esperar el gate antes de crear los drains   → W5
  M16 no matar el proceso en deny/timeout         → W9/W10 + este archivo
  M17 tragar CancelledError                       → W15 + este archivo
  M18 reusar el resultado del initial como final  → W11 (rondas == 2) + este archivo
"""

from __future__ import annotations

import ast
import pathlib

RAIZ = pathlib.Path(__file__).resolve().parents[1]
RUNNER = RAIZ / "sky_claw" / "local" / "tools" / "dyndolod_runner.py"


def _arbol() -> ast.Module:
    return ast.parse(RUNNER.read_text(encoding="utf-8"))


def _metodo(nombre: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    for nodo in ast.walk(_arbol()):
        if isinstance(nodo, ast.FunctionDef | ast.AsyncFunctionDef) and nodo.name == nombre:
            return nodo
    raise AssertionError(f"el runner ya no tiene {nombre}: revisá este ancla")


def _llamadas(nodo: ast.AST, nombre: str) -> list[ast.Call]:
    return [
        hijo
        for hijo in ast.walk(nodo)
        if isinstance(hijo, ast.Call)
        and (
            (isinstance(hijo.func, ast.Attribute) and hijo.func.attr == nombre)
            or (isinstance(hijo.func, ast.Name) and hijo.func.id == nombre)
        )
    ]


def _kwarg(llamada: ast.Call, nombre: str) -> ast.AST | None:
    return next((kw.value for kw in llamada.keywords if kw.arg == nombre), None)


# ---------------------------------------------------------------------------
# M9/M10 — el expected sale del layout, por herramienta
# ---------------------------------------------------------------------------


def test_m9_m10_el_expected_no_puede_salir_de_otra_raiz() -> None:
    metodo = _metodo("_expected_output_de")
    atributos = {nodo.attr for nodo in ast.walk(metodo) if isinstance(nodo, ast.Attribute)}
    assert "raiz_de" in atributos, "el expected dejó de derivarse de layout.raiz_de(herramienta)"
    assert "family_root" not in atributos, "el expected usa el family_root: no es output"
    # El mapa herramienta-por-tool es la ÚNICA fuente de identidad (una sola
    # frontera tipada, no un `if tool_name == ...` por call site).
    assert "_HERRAMIENTA_POR_TOOL" in {nodo.id for nodo in ast.walk(metodo) if isinstance(nodo, ast.Name)}


# ---------------------------------------------------------------------------
# M11 — un veredicto no-MATCH nunca autoriza
# ---------------------------------------------------------------------------


def test_m11_el_gate_corta_ante_cualquier_veredicto_que_no_sea_match() -> None:
    metodo = _metodo("_gate_uia_output")
    fuente = ast.unparse(metodo)
    assert "EstadoPreflight.MATCH" in fuente, "el gate dejó de exigir MATCH"
    assert "raise DynDOLODPreflightUIAError" in fuente, "el gate no corta ante un veredicto no-MATCH"
    # El UNKNOWN sintetizado por falta de backend también pasa por el mismo corte:
    # no hay una rama que devuelva un resultado "autorizante" por excepción.
    assert "return resultado" in fuente
    assert fuente.count("return ") >= 1


# ---------------------------------------------------------------------------
# M12/M13/M18 — los DOS gates, en orden, con política distinta
# ---------------------------------------------------------------------------


def test_m12_m13_m18_el_protocolo_corre_los_dos_gates_y_no_reusa_el_resultado() -> None:
    protocolo = _metodo("_protocolo_de_readiness")
    gates = _llamadas(protocolo, "_gate_uia_output")
    assert len(gates) == 2, f"el protocolo debe correr DOS gates (initial + final); corre {len(gates)}"

    transitorias = [ast.unparse(_kwarg(gate, "razones_transitorias")) for gate in gates]
    assert transitorias == ["RAZONES_TRANSITORIAS_DE_INICIO", "RAZONES_TRANSITORIAS_FINAL"], (
        f"la política de cada gate dejó de distinguirse: {transitorias}"
    )

    # El initial gate precede a la confirmación humana, y el final la sucede:
    # reusar el resultado del initial como final (M18) rompería este orden.
    llamadas = _llamadas(protocolo, "_confirmar_configuracion")
    assert len(llamadas) == 1
    assert gates[0].lineno < llamadas[0].lineno < gates[1].lineno, "el orden de los gates cambió"


# ---------------------------------------------------------------------------
# M16 — todo rechazo pre-generación mata el proceso y cierra el job
# ---------------------------------------------------------------------------


def test_m16_el_rechazo_del_protocolo_limpia_proceso_y_job() -> None:
    """La rama ``except DynDOLODExecutionError`` de ``_execute_process`` limpia.

    Sin ella, un deny/timeout/mismatch dejaría la GUI abierta y los drains
    corriendo: es exactamente la mutación M16.
    """
    ejecutar = _metodo("_execute_process")
    maneja = [
        nodo
        for nodo in ast.walk(ejecutar)
        if isinstance(nodo, ast.ExceptHandler)
        and nodo.type is not None
        and "DynDOLODExecutionError" in ast.unparse(nodo.type)
    ]
    assert maneja, "desapareció la rama que limpia el rechazo del protocolo"
    cuerpo = ast.unparse(maneja[0])
    for obligatorio in (
        "kill_and_reap",
        "close_job",
        "heartbeat.cancel",
        "drain_out.cancel",
        "drain_err.cancel",
        "raise",
    ):
        assert obligatorio in cuerpo, f"el cleanup del rechazo no hace {obligatorio}"


# ---------------------------------------------------------------------------
# M17 — la cancelación nunca se traga
# ---------------------------------------------------------------------------


def test_m17_las_esperas_del_protocolo_repropagan_cancelled_error() -> None:
    """Todo ``except`` del protocolo que atrapa lo amplio re-lanza la cancelación.

    ``_confirmar_configuracion`` no captura nada: su ``finally`` cancela y espera
    las dos tasks, y el ``CancelledError`` sube solo. Las otras dos sí tienen un
    ``except`` amplio (canal humano / aviso) y por eso están obligadas a
    distinguir la cancelación antes de aplastar el fallo.
    """
    for nombre in ("_confirmar_acotado", "_informar_operador"):
        metodo = _metodo(nombre)
        maneja = [
            nodo
            for nodo in ast.walk(metodo)
            if isinstance(nodo, ast.ExceptHandler)
            and nodo.type is not None
            and "CancelledError" in ast.unparse(nodo.type)
        ]
        assert maneja, f"{nombre} dejó de distinguir la cancelación"
        assert "raise" in ast.unparse(maneja[0]), f"{nombre} traga la cancelación"

    # El `finally` que cancela y espera las tasks no puede dejar huérfanas.
    confirmar = _metodo("_confirmar_configuracion")
    fuente = ast.unparse(confirmar)
    assert "_vigilar_proceso" in fuente
    assert "gather" in fuente
    assert ".cancel()" in fuente


# ---------------------------------------------------------------------------
# Orden load-bearing: drains antes del gate
# ---------------------------------------------------------------------------


def test_los_drains_y_el_gate_estan_en_la_misma_rama_try() -> None:
    """El gate corre DENTRO del ``try`` que ya limpia: si no, un rechazo no se limpia."""
    ejecutar = _metodo("_execute_process")
    trys = [nodo for nodo in ast.walk(ejecutar) if isinstance(nodo, ast.Try)]
    principal = next((nodo for nodo in trys if "_protocolo_de_readiness" in ast.unparse(nodo)), None)
    assert principal is not None, "el protocolo salió del try: su rechazo no se limpiaría"
    cuerpo = ast.unparse(principal)
    assert "proc.wait()" in cuerpo
    assert any(
        isinstance(nodo, ast.ExceptHandler)
        and nodo.type is not None
        and "DynDOLODExecutionError" in ast.unparse(nodo.type)
        for nodo in principal.handlers
    ), "el try del protocolo perdió su rama de cleanup"
