"""GP2-S4E / P6 — contrato del E02 estricto (mutantes M6-1..M6-5).

Este módulo NO repite el RIG: congela las dos cosas que el RIG no puede
probar sobre sí mismo.

1. **Anti-relajación (M6-5)**: el test físico de E02
   (``test_e02_mid_apply_crash_revierte_fisicamente_el_golden``) acepta
   EXACTAMENTE ``rolled_back`` como desenlace. La forma laxa anterior
   (``disposition in ("rolled_back", "rollback_required", "indeterminate")``,
   o ``committed is False`` como única evidencia) tiene que romper este
   archivo, no pasar en silencio. Se verifica por AST, no por texto libre.

2. **Wiring de la proyección (M6-1..M4)**: las assertions del RIG sólo muerden
   si ``ProtectionOutcome.rollback_executed`` viene de
   ``physical_restoration_completed`` y ``lock_retained`` viene de
   ``ACQUIRED_RETAINED``. Una proyección que afirme un rollback sin restaurar,
   o que oculte un lock retenido, tiene que romper acá aunque el RIG esté
   gated por elevación en el runner local.
"""

from __future__ import annotations

import ast
import pathlib
from typing import Any

from sky_claw.local.runtime_vault.authorized_plan_store import DurableWriteOutcome
from sky_claw.local.runtime_vault.protection_journal import ProtectionTransactionState
from sky_claw.local.runtime_vault.protection_journal_store import ProtectionJournalClassification
from sky_claw.local.runtime_vault.protection_service import (
    _DISPOSICION_POR_DESENECHO_DE_S4C,
    ProtectionDisposition,
    _proyeccion_de_recovery,
)
from sky_claw.local.runtime_vault.recovery_orchestrator import (
    RecoveryDisposition,
    RecoveryForensicReport,
    RecoveryIdentitySource,
    RecoveryLockOutcome,
)

_RIG_TEST = pathlib.Path(__file__).resolve().parent / "test_runtime_vault_s4e_windows_rig.py"
_WORKER = pathlib.Path(__file__).resolve().parent / "s4e_crash_worker.py"
_E02_FISICO = "test_e02_mid_apply_crash_revierte_fisicamente_el_golden"
_E02_CONTRACTUAL = "test_e02_estado_durable_y_fail_closed_sin_elevacion"


def _funcion_del_rig(nombre: str) -> ast.FunctionDef:
    arbol = ast.parse(_RIG_TEST.read_text(encoding="utf-8"))
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.FunctionDef) and nodo.name == nombre:
            return nodo
    raise AssertionError(f"no se encontró '{nombre}' en {_RIG_TEST.name}: el anchor se rompió a propósito")


def _comparaciones_sobre_disposition(funcion: ast.FunctionDef) -> list[ast.Compare]:
    """Toda comparación cuyo lado izquierdo es ``b[\"disposition\"]``."""
    encontradas: list[ast.Compare] = []
    for nodo in ast.walk(funcion):
        if not isinstance(nodo, ast.Compare) or not nodo.ops:
            continue
        izquierda = nodo.left
        if (
            isinstance(izquierda, ast.Subscript)
            and isinstance(izquierda.value, ast.Name)
            and izquierda.value.id == "b"
            and isinstance(izquierda.slice, ast.Constant)
            and izquierda.slice.value == "disposition"
        ):
            encontradas.append(nodo)
    return encontradas


def test_e02_fisico_exige_rolled_back_por_igualdad_estricta() -> None:
    """M6-5: la aserción de desenlace es EXACTA, no un conjunto de admisión.

    ``rollback_required``, ``indeterminate`` y ``operator_required`` son
    desenlaces fail-closed legítimos en OTROS escenarios; en E02 significan
    "no se demostró rollback automático" y tienen que FALLAR el test.
    """
    funcion = _funcion_del_rig(_E02_FISICO)
    comparaciones = _comparaciones_sobre_disposition(funcion)
    assert comparaciones, "E02 físico no compara b['disposition']: se relajó la aserción central"
    for comparacion in comparaciones:
        assert all(isinstance(op, ast.NotEq) for op in comparacion.ops) or all(
            isinstance(op, ast.Eq) for op in comparacion.ops
        ), "b['disposition'] sólo se compara por igualdad estricta en el E02 físico"
        if any(isinstance(op, ast.Eq) for op in comparacion.ops):
            valores = [
                c.value for c in comparacion.comparators if isinstance(c, ast.Constant) and isinstance(c.value, str)
            ]
            assert valores == ["rolled_back"], (
                "E02 físico admite otra disposición: "
                f"{valores}; un rollback automático SÓLO es ProtectionDisposition.ROLLED_BACK"
            )


def test_e02_fisico_no_usa_conjuntos_de_admision_para_el_desenlace() -> None:
    """M6-5 (forma estructural): prohibido ``disposition in (...)`` en E02 físico."""
    funcion = _funcion_del_rig(_E02_FISICO)
    for nodo in ast.walk(funcion):
        if not isinstance(nodo, ast.Compare):
            continue
        for op in nodo.ops:
            assert not isinstance(op, (ast.In, ast.NotIn)) or not (
                isinstance(nodo.left, ast.Subscript)
                and isinstance(nodo.left.slice, ast.Constant)
                and nodo.left.slice.value == "disposition"
            ), "E02 físico no puede admitir desenlaces por membresía de conjunto"


def test_e02_fisico_observa_restore_pre_restablecido_y_lock_liberado() -> None:
    """El RIG observa la causalidad física; no se limita a leer la proyección.

    Tres piezas tienen que estar en el cuerpo del test: los breadcrumbs
    ``restore:`` (el rollback lo ejecutó S4-C), la verificación semántica del
    PRE re-observado (``_verificar_sd_igual_a_pre``) y la re-adquisición del
    lock (``acquire_golden_mutation_lock``). Quitar cualquiera baja el test a
    "no se hizo commit", que no demuestra rollback.
    """
    funcion = _funcion_del_rig(_E02_FISICO)
    fuente = ast.get_source_segment(_RIG_TEST.read_text(encoding="utf-8"), funcion)
    assert fuente is not None
    assert 'e.startswith("restore:")' in fuente, "E02 físico no observa los breadcrumbs de restore"
    assert "_verificar_sd_igual_a_pre" in fuente, "E02 físico no re-observa el PRE físicamente"
    assert "_sha_sd_live" in fuente, "E02 físico no compara el SD vivo contra el PRE"
    assert "acquire_golden_mutation_lock" in fuente, "E02 físico no prueba que el lock quedó liberado"


def test_e02_el_seam_de_crash_es_causal_no_temporal() -> None:
    """§6: el crash de E02 NO depende de ``time.sleep`` ni de sondeo por tiempo.

    El borde es el breadcrumb ``mid-apply-mutated:`` emitido tras N
    ``MUTATED`` durables; si alguien lo reemplaza por ``time.sleep`` +
    ``taskkill``, el test vuelve a ser verde/rojo por suerte.
    """
    arbol = ast.parse(_WORKER.read_text(encoding="utf-8"))
    seam = None
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.FunctionDef) and nodo.name == "_probe_con_crash_mid_apply":
            seam = nodo
            break
    assert seam is not None, "el worker perdió el seam causal de E02 (_probe_con_crash_mid_apply)"
    fuente = ast.get_source_segment(_WORKER.read_text(encoding="utf-8"), seam)
    assert fuente is not None
    assert "time.sleep" not in fuente, "el seam de E02 no puede depender de timing"
    assert "mid-apply-mutated:" in fuente, "el seam de E02 tiene que emitir el breadcrumb causal"

    funcion = _funcion_del_rig(_E02_FISICO)
    fuente_rig = ast.get_source_segment(_RIG_TEST.read_text(encoding="utf-8"), funcion)
    assert fuente_rig is not None
    assert "mid-apply-mutated:" in fuente_rig, "el RIG debe esperar el breadcrumb causal, no un tiempo"


def test_e02_los_breadcrumbs_se_aislan_por_nonce_de_corrida() -> None:
    """§24: un breadcrumb de otra corrida NO satisface una espera del ciclo actual.

    ``_eventos_de`` agrupa por el nonce de la corrida que abrió con la marca:
    se le inyecta un log con dos corridas y el restore viejo tiene que quedar
    fuera del conjunto observado de la nueva.
    """
    import sys
    import tempfile

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    from test_runtime_vault_s4e_windows_rig import _eventos_de

    with tempfile.TemporaryDirectory(prefix="SkyClaw-S4E-RIG-") as tmp:
        rig = pathlib.Path(tmp)
        (rig / "s4e-events.log").write_text(
            "nonce-viejo|resume:begin\n"
            "nonce-viejo|restore:Data/Skyrim.esm\n"
            "nonce-nuevo|resume:begin\n"
            "nonce-nuevo|resume:fin:indeterminate\n",
            encoding="utf-8",
        )
        eventos = _eventos_de(rig, "resume:begin")
    assert "restore:Data/Skyrim.esm" not in eventos, (
        "un restore de una corrida ANTERIOR satisfizo la observación actual: el nonce no aísla"
    )
    assert eventos == ["resume:begin", "resume:fin:indeterminate"], eventos


def test_e02_post_restore_verifica_semantica_no_identidad_raw_del_sd() -> None:
    """P6.1: E02 valida el restore SEMÁNTICAMENTE, nunca por SHA raw de bytes.

    El contrato de restauración del repo es ``verify_restored_security_descriptor_by_handle``
    (ADR 0010 §12.2; ``target_dacl.py``:1697-1703): owner, group, DACL semántica
    exhaustiva y ``SE_DACL_PROTECTED`` — y explícitamente NO
    ``POST_RESTORE_SD_BYTES == PRE_SD_BYTES``, porque Windows puede reserializar
    el descriptor con layout/padding equivalente (observado en CI de
    ``af3d414f``: restore semántico PASS, raw SHA distinto en py3.11 y py3.12).

    La precondición opuesta SÍ queda congelada: antes del crash,
    ``_sha_sd_live(mutado) != pre_sd_sha256`` es lo que demuestra que había
    algo real que restaurar. El anchor chequea ambas mitades por AST.
    """
    funcion = _funcion_del_rig(_E02_FISICO)

    def _es_llamada_sha_vivo(expr: ast.expr) -> bool:
        return isinstance(expr, ast.Call) and isinstance(expr.func, ast.Name) and expr.func.id == "_sha_sd_live"

    def _es_pre_sd_hash(expr: ast.expr) -> bool:
        return isinstance(expr, ast.Attribute) and expr.attr == "pre_sd_sha256"

    # La frontera causal es la corrida del resume: toda comparación raw con
    # ``==`` ANTES abarca nodos nunca escritos (sin SetSecurityInfo no hay
    # reserialización, así que la igualdad cruda es correcta en la
    # precondición); DESPUÉS del resume cualquier ``== _sha_sd_live(...)
    # == pre_sd_sha256`` exige identidad de bytes que Windows no garantiza.
    resume_linea: int | None = None
    for nodo in ast.walk(funcion):
        if (
            isinstance(nodo, ast.Assign)
            and isinstance(nodo.value, ast.Call)
            and isinstance(nodo.value.func, ast.Name)
            and nodo.value.func.id == "_leer"
            and any(isinstance(arg, ast.Constant) and arg.value == "s4e-result-b.json" for arg in nodo.value.args)
        ):
            resume_linea = nodo.lineno
    assert resume_linea is not None, "E02 físico perdió la llamada al resume del proceso B"

    raw_post_restore: list[str] = []
    usa_semantica = False
    precondicion_raw = False
    for nodo in ast.walk(funcion):
        if isinstance(nodo, ast.Assert) and isinstance(nodo.test, ast.Compare):
            comparacion = nodo.test
            if (
                comparacion.ops
                and isinstance(comparacion.left, ast.Call)
                and _es_llamada_sha_vivo(comparacion.left)
                and comparacion.comparators
                and _es_pre_sd_hash(comparacion.comparators[0])
            ):
                op = comparacion.ops[0]
                if isinstance(op, ast.Eq) and nodo.lineno > resume_linea:
                    raw_post_restore.append(ast.unparse(comparacion))
                elif isinstance(op, ast.NotEq) and nodo.lineno < resume_linea:
                    precondicion_raw = True
        if isinstance(nodo, ast.Expr) and isinstance(nodo.value, ast.Call):
            llamada = nodo.value
            if (
                isinstance(llamada.func, ast.Name)
                and llamada.func.id == "_verificar_sd_igual_a_pre"
                and nodo.lineno > resume_linea
            ):
                usa_semantica = True

    assert not raw_post_restore, (
        "E02 volvió a exigir igualdad RAW del SD serializado tras el restore: "
        f"{raw_post_restore}. El contrato es semántico (owner/group/DACL/"
        "SE_DACL_PROTECTED); Windows reserializa los bytes con layout equivalente."
    )
    assert usa_semantica, "E02 perdió la verificación semántica post-restore (_verificar_sd_igual_a_pre)"
    assert precondicion_raw, (
        "E02 perdió la precondición física: antes del crash el nodo MUTATED debe "
        "diferir del PRE (sha vivo != pre_sd_sha256); sin ella no hay nada que restaurar"
    )


def test_e02_contractual_existe_y_es_fail_closed_sin_elevacion() -> None:
    """El hermano contractual del E02 físico no desaparece en silencio."""
    funcion = _funcion_del_rig(_E02_CONTRACTUAL)
    fuente = ast.get_source_segment(_RIG_TEST.read_text(encoding="utf-8"), funcion)
    assert fuente is not None
    assert 'b["disposition"] != "rolled_back"' in fuente, (
        "el E02 contractual tiene que prohibir un rolled_back sin restore posible (M6-3 en runner no elevado)"
    )
    assert 'b["rollback_executed"] is False' in fuente
    assert 'b["operator_intervention_required"] is True' in fuente


# --------------------------------------------------------------------------
# Wiring de la proyección S4-C -> S4-E (M6-1..M6-4)
# --------------------------------------------------------------------------


def test_rolled_back_no_es_rollback_required() -> None:
    """§17 del encargo: la distinción es semántica, no de nombre.

    ``ROLLBACK_REQUIRED`` significa "restauración pendiente/no confirmada";
    afirmarla como éxito es exactamente el falso positivo que E02 cierra.
    """
    assert ProtectionDisposition.ROLLED_BACK != ProtectionDisposition.ROLLBACK_REQUIRED
    assert ProtectionDisposition.ROLLED_BACK.value == "rolled_back"
    assert ProtectionDisposition.ROLLBACK_REQUIRED.value == "rollback_required"


def test_el_mapa_s4c_a_s4e_esta_congelado_literal() -> None:
    """M6-1: S4-C sólo reporta ``ROLLED_BACK`` tras rollback físico completo.

    ``POST_VERIFICATION_REQUIRED`` se proyecta como ``ROLLBACK_REQUIRED`` (el
    apply quedó completo, no revertido): cambiar cualquiera de las dos celdas
    convierte un handoff en un rollback fantasma o viceversa.
    """
    assert _DISPOSICION_POR_DESENECHO_DE_S4C == {
        RecoveryDisposition.ROLLED_BACK: ProtectionDisposition.ROLLED_BACK,
        RecoveryDisposition.POST_VERIFICATION_REQUIRED: ProtectionDisposition.ROLLBACK_REQUIRED,
        RecoveryDisposition.PRE_PLAN_LOCK_RECOVERED: ProtectionDisposition.NOT_APPLICABLE,
        RecoveryDisposition.LOCK_BUSY: ProtectionDisposition.LOCK_BUSY,
        RecoveryDisposition.NO_TRANSACTION: ProtectionDisposition.NOT_APPLICABLE,
        RecoveryDisposition.INDETERMINATE: ProtectionDisposition.INDETERMINATE,
        RecoveryDisposition.TERMINAL: ProtectionDisposition.REFUSED,
    }


def _reporte_s4c(**overrides: Any) -> RecoveryForensicReport:
    """Reporte forense de S4-C construido a mano para probar la proyección.

    Los defaults describen un rollback físico COMPLETO: cada override monta
    un mutante concreto.
    """
    campos: dict[str, Any] = {
        "operation_id": "op-e02",
        "plan_classification": DurableWriteOutcome.DURABLE,
        "journal_classification": ProtectionJournalClassification.VALID,
        "observed_transaction_state": ProtectionTransactionState.APPLYING,
        "node_wal_summary": (),
        "physical_identity_source": RecoveryIdentitySource.AUTHORIZED_PLAN,
        "lock_outcome": RecoveryLockOutcome.ACQUIRED_RELEASED,
        "stale_lock_takeover": True,
        "disposition": RecoveryDisposition.ROLLED_BACK,
        "nodes_restored": ("Data/Skyrim.esm",),
        "nodes_skipped": (),
        "nodes_pending": (),
        "physical_restoration_completed": True,
        "operator_intervention_required": False,
        "indeterminate_reason": "",
        "detail": "rollback físico completo",
        "setsecurityinfo_calls": 1,
    }
    campos.update(overrides)
    return RecoveryForensicReport(**campos)


def test_proyeccion_rollback_executed_sale_de_la_restauracion_fisica() -> None:
    """M6-3 (proyección): ROLLED_BACK sin restauración física no puede morder.

    Si alguien desconecta ``rollback_executed`` de
    ``physical_restoration_completed``, el RIG no tiene manera de
    distinguir "restauró" de "dijo que restauró".
    """
    outcome = _proyeccion_de_recovery(_reporte_s4c())
    assert outcome.disposition is ProtectionDisposition.ROLLED_BACK
    assert outcome.rollback_executed is True
    assert outcome.committed is False

    mutante = _proyeccion_de_recovery(_reporte_s4c(physical_restoration_completed=False))
    assert mutante.rollback_executed is False


def test_proyeccion_lock_retained_sale_del_lock_outcome() -> None:
    """M6-4: un lock retenido tras rollback no puede reportarse como liberado."""
    outcome = _proyeccion_de_recovery(_reporte_s4c())
    assert outcome.lock_retained is False

    mutante = _proyeccion_de_recovery(_reporte_s4c(lock_outcome=RecoveryLockOutcome.ACQUIRED_RETAINED))
    assert mutante.lock_retained is True


def test_proyeccion_rollback_requerido_no_es_exito_de_rollback() -> None:
    """M6-1 (proyección): POST_VERIFICATION_REQUIRED no es un rollback."""
    outcome = _proyeccion_de_recovery(
        _reporte_s4c(
            disposition=RecoveryDisposition.POST_VERIFICATION_REQUIRED,
            physical_restoration_completed=False,
            nodes_restored=(),
            lock_outcome=RecoveryLockOutcome.ACQUIRED_RETAINED,
        )
    )
    assert outcome.disposition is ProtectionDisposition.ROLLBACK_REQUIRED
    assert outcome.disposition is not ProtectionDisposition.ROLLED_BACK
    assert outcome.rollback_executed is False
    assert outcome.lock_retained is True
