"""T5-v2.1 — matriz de mutación del wiring UIA (M9-M18 + AM1-AM4 de Fase 3).

Cada test de acá ancla la PROPIEDAD ESTRUCTURAL que su mutación rompería, y el
docstring dice cuál es el test de conducta que también se pone rojo. La mutación
se aplica a mano, se corre, se verifica RED y se revierte: eso es lo que se
reporta en el informe, no un verde permanente.

  M9  expected de TexGen = family_root            → este archivo + W1/W3
  M10 expected de DynDOLOD = texgen_root          → este archivo + W2/W4
  M11 UNAVAILABLE ⇒ success                       → W8/A6 + este archivo
  M12 bypass del initial gate                     → W5/W6 + este archivo
  M13 bypass del final gate                       → W11/W12 + este archivo
  M14 auto-approve de la categoría nueva          → H2 (tests/test_hitl.py)
  M15 esperar el gate antes de crear los drains   → W5
  M16 no matar el proceso en deny/timeout         → W9/W10 + este archivo
  M17 tragar CancelledError                       → W15 + este archivo
  M18 reusar el resultado del initial como final  → W11/W12 + este archivo

Fase 3 (post-rig real):

  AM1 volver a bloquear el initial MISMATCH       → A1/A3 + la policy congelada
  AM2 hacer permisivo el MISMATCH final           → A4 + la ancla del MATCH final
  AM3 reusar el veredicto/policy inicial como final → A4 + la ancla del MATCH final
  AM4 quitar el expected del prompt HITL          → A9 (tests/test_hitl.py)
  AM5 omitir kill/reap + Job Object del helper → hard-hang del ejecutor

Nota de AM5 verificada: deshabilitar SÓLO ``kill_and_reap`` deja el hard-hang en
VERDE en Windows porque ``close_job`` mata igual al helper (Job Object
kill-on-close) — dos capas del mismo mecanismo. El rojo aparece al omitir las
dos, que es la mutación que se reporta.
"""

from __future__ import annotations

import ast
import pathlib

from sky_claw.local.tools.dyndolod_uia_gate import (
    RAZONES_TRANSITORIAS_POR_POLITICA,
    PoliticaDeReintento,
    VeredictoInitial,
    clasificar_initial,
)

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
# M11 / AM1 — la observación NO decide; la policy del initial es explícita
# ---------------------------------------------------------------------------


def test_m11_la_observacion_no_autoriza_y_el_initial_no_exige_match() -> None:
    """M11/AM1 — ``_observar_output_con_gate`` no decide y el initial no exige MATCH.

    La versión anterior del gate hacía ``if estado is not MATCH: raise`` dentro de
    la observación. El rig real refutó esa política para el INITIAL: un MISMATCH
    concluyente tiene que llegar al operador. Lo que el ancla congela: la
    observación devuelve el veredicto tal cual (sin MATCH ni policy embebida), y
    la decisión del initial vive en ``clasificar_initial`` — fail-closed por
    enumeración, con el MISMATCH en la caja configurable.
    """
    observacion = _metodo("_observar_output_con_gate")
    fuente = ast.unparse(observacion)
    tiene_match = any(isinstance(nodo, ast.Attribute) and nodo.attr == "MATCH" for nodo in ast.walk(observacion))
    assert not tiene_match, "la observación volvió a decidir por MATCH"
    tiene_policy = any(isinstance(nodo, ast.Name) and nodo.id == "clasificar_initial" for nodo in ast.walk(observacion))
    assert not tiene_policy, "la observación volvió a aplicar la policy del initial"
    assert "return observacion.result()" in fuente
    assert "resultado_proceso_muerto" in fuente, "la muerte del proceso dejó de cortar la observación"

    veredicto_mismatch = clasificar_initial(_resultado_de(estado="MISMATCH", razon="OUTPUT_DIFIERE"))
    assert veredicto_mismatch is VeredictoInitial.CONFIGURABLE_MISMATCH, (
        "AM1: el MISMATCH inicial volvió a ser bloqueante; el operador ya no puede corregir"
    )
    assert clasificar_initial(_resultado_de(estado="MATCH", razon="OUTPUT_COINCIDE")) is VeredictoInitial.MATCH


def test_am2_am3_el_final_exige_match_despues_de_confirmar_y_no_reusa_el_inicial() -> None:
    """AM2/AM3 — el único MATCH del protocolo está DESPUÉS de la confirmación.

    Un reuso del veredicto inicial (AM3) o un final permisivo (AM2) tendrían que
    romper esta geografía: el initial se clasifica con ``clasificar_initial``
    (que admite MISMATCH), la confirmación humana va en el medio, y recién
    después aparece ``EstadoPreflight.MATCH`` como única autorización.
    """
    protocolo = _metodo("_protocolo_de_readiness")
    fuente = ast.unparse(protocolo)
    assert "clasificar_initial" in fuente, "el initial dejó de clasificarse con la policy explícita"
    assert "VeredictoInitial.BLOCKED" in fuente, "el initial dejó de cortar en BLOCKED"
    assert "EstadoPreflight.MATCH" in fuente, "el final dejó de exigir MATCH"

    confirmaciones = _llamadas(protocolo, "_confirmar_configuracion")
    assert len(confirmaciones) == 1
    primer_match = min(
        nodo.lineno for nodo in ast.walk(protocolo) if isinstance(nodo, ast.Attribute) and nodo.attr == "MATCH"
    )
    assert confirmaciones[0].lineno < primer_match, (
        "hay un MATCH ANTES/EN LUGAR de la confirmación: el initial volvió a autorizar, o el final no re-observa"
    )


# ---------------------------------------------------------------------------
# M12/M13/M18 — los DOS gates, en orden, con política distinta
# ---------------------------------------------------------------------------


def test_m12_m13_m18_el_protocolo_corre_los_dos_gates_y_no_reusa_el_resultado() -> None:
    protocolo = _metodo("_protocolo_de_readiness")
    gates = _llamadas(protocolo, "_observar_output_con_gate")
    assert len(gates) == 2, f"el protocolo debe correr DOS observaciones (initial + final); corre {len(gates)}"

    politicas = [ast.unparse(_kwarg(gate, "politica")) for gate in gates]
    assert politicas == ["PoliticaDeReintento.INICIO", "PoliticaDeReintento.FINAL"], (
        f"la política de cada gate dejó de distinguirse: {politicas}"
    )

    # El initial gate precede a la confirmación humana, y el final la sucede:
    # reusar el resultado del initial como final (M18/AM3) rompería este orden.
    llamadas = _llamadas(protocolo, "_confirmar_configuracion")
    assert len(llamadas) == 1
    assert gates[0].lineno < llamadas[0].lineno < gates[1].lineno, "el orden de los gates cambió"


def test_la_particion_de_politicas_esta_congelada_por_igualdad_literal() -> None:
    """Una política nueva sin entrada no puede degradar a "sin transitorias"."""
    assert {
        PoliticaDeReintento.INICIO: frozenset(
            {
                # Import directo para que el literal sea el contrato, no una referencia.
                _razon("PROCESO_NO_ENCONTRADO"),
                _razon("PID_NO_COINCIDE"),
                _razon("VENTANA_NO_ENCONTRADA"),
                _razon("CONTROL_NO_ENCONTRADO"),
                _razon("VALOR_NO_LEIBLE"),
            }
        ),
        PoliticaDeReintento.FINAL: frozenset({_razon("VALOR_NO_LEIBLE")}),
    } == RAZONES_TRANSITORIAS_POR_POLITICA


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

    # La observación también cancela y espera: sin eso, el helper del gate
    # quedaría vivo detrás de una cancelación externa.
    observar = ast.unparse(_metodo("_observar_output_con_gate"))
    assert ".cancel()" in observar
    assert "gather" in observar


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


# ---------------------------------------------------------------------------
# Helpers de construcción del resultado (el import vive arriba)
# ---------------------------------------------------------------------------


def _resultado_de(*, estado: str, razon: str):
    from sky_claw.local.tools.dyndolod_uia_preflight import (  # noqa: PLC0415
        EstadoPreflight,
        RazonPreflight,
        ResultadoPreflightUIA,
    )

    return ResultadoPreflightUIA(
        estado=EstadoPreflight[estado],
        razon=RazonPreflight[razon],
        tool="TexGen",
        detalle="mutación",
        valor_esperado="E:/out",
    )


def _razon(nombre: str):
    from sky_claw.local.tools.dyndolod_uia_preflight import RazonPreflight  # noqa: PLC0415

    return RazonPreflight[nombre]
