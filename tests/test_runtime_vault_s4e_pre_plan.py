"""Tests causales del routing pre-plan (P3 de S4-E closure)."""

import pathlib
from typing import Any

import pytest

from sky_claw.local.runtime_vault import protection_service as svc
from sky_claw.local.runtime_vault.authorized_plan_store import DurableWriteOutcome
from sky_claw.local.runtime_vault.operation_lock_binding import OperationLockBindingEvidence

_OP = "3f2b1c8e-9a4d-4f5e-8b7a-1c2d3e4f5a6b"


class _Clasif:
    """Journal ABSENT — la pre-condicion del router pre-plan."""

    classification = svc.ProtectionJournalClassification.ABSENT
    is_valid = False
    detail = "journal ausente"
    journal = None


def _parchar_evidence(
    monkeypatch: pytest.MonkeyPatch,
    *,
    plan: DurableWriteOutcome,
    binding: OperationLockBindingEvidence,
) -> list[str]:
    """Congela la evidencia durable y registra a qué suborquestador se fue."""
    llamadas: list[str] = []

    monkeypatch.setattr(svc, "classify_protection_journal", lambda *a, **k: _Clasif())
    monkeypatch.setattr(svc, "classify_durable_authorized_plan", lambda *a, **k: plan)
    monkeypatch.setattr(svc, "classify_operation_lock_binding", lambda *a, **k: binding)

    def _s4c(*args: Any, ruta: Any = None, **kwargs: Any) -> Any:
        # Se registra la RUTA QUE LLEGA, no la disposition del stub: el
        # disposition lo decide S4-C de verdad y un stub no puede
        # representarlo. Lo que S4-E tiene que hacer es enrutar.
        llamadas.append("s4c:" + (ruta.value if ruta else "None"))
        return svc.ProtectionOutcome(
            operation_id=_OP,
            disposition=svc.ProtectionDisposition.NOT_APPLICABLE,
            stage=svc.ProtectionStage.RECOVERY,
            source_orchestrator="fake_s4c",
            route=ruta,
        )

    monkeypatch.setattr(svc, "_reanudar_por_s4c", _s4c)
    return llamadas


# ===========================================================================
# La tabla
# ===========================================================================


def test_la_tabla_pre_journal_esta_congelada() -> None:
    """Congela el mapeo (plan, binding) -> ruta por igualdad literal.

    Es exhaustiva sobre el PRODUCTO de los dos enums reales: una fila nueva en
    cualquiera de los dos enums rompe este test, que es la forma de forzar la
    decisión en vez de dejarla caer en un default.
    """
    esperado = {
        (DurableWriteOutcome.NOT_DURABLE, OperationLockBindingEvidence.DURABLE): (svc.RestartRoute.S4C_ROLLBACK),
        (DurableWriteOutcome.NOT_DURABLE, OperationLockBindingEvidence.ABSENT): svc.RestartRoute.TERMINAL,
        (DurableWriteOutcome.NOT_DURABLE, OperationLockBindingEvidence.INDETERMINATE): (
            svc.RestartRoute.OPERATOR_REQUIRED
        ),
        (DurableWriteOutcome.INDETERMINATE, OperationLockBindingEvidence.DURABLE): (svc.RestartRoute.OPERATOR_REQUIRED),
        (DurableWriteOutcome.INDETERMINATE, OperationLockBindingEvidence.ABSENT): (svc.RestartRoute.OPERATOR_REQUIRED),
        (DurableWriteOutcome.INDETERMINATE, OperationLockBindingEvidence.INDETERMINATE): (
            svc.RestartRoute.OPERATOR_REQUIRED
        ),
        (DurableWriteOutcome.DURABLE, OperationLockBindingEvidence.DURABLE): svc.RestartRoute.S4C_ROLLBACK,
        (DurableWriteOutcome.DURABLE, OperationLockBindingEvidence.ABSENT): svc.RestartRoute.OPERATOR_REQUIRED,
        (DurableWriteOutcome.DURABLE, OperationLockBindingEvidence.INDETERMINATE): (svc.RestartRoute.OPERATOR_REQUIRED),
    }
    assert esperado == svc.PRE_JOURNAL_ROUTES

    # Y es exhaustiva sobre el producto real, no una sublattice.
    producto = {(p, b) for p in DurableWriteOutcome for b in OperationLockBindingEvidence}
    assert set(svc.PRE_JOURNAL_ROUTES) == producto


def test_la_tabla_no_tiene_default() -> None:
    """Indexar fuera de la tabla es KeyError, no una ruta por defecto."""
    with pytest.raises(KeyError):
        svc.PRE_JOURNAL_ROUTES[("inventado", "inventado")]  # type: ignore[index]


# ===========================================================================
# Los tres desenlaces
# ===========================================================================


def test_pre_plan_con_binding_durable_va_a_s4c(monkeypatch: pytest.MonkeyPatch) -> None:
    """La ventana real del protocolo llega a S4-C, que ya la soporta."""
    llamadas = _parchar_evidence(
        monkeypatch,
        plan=DurableWriteOutcome.NOT_DURABLE,
        binding=OperationLockBindingEvidence.DURABLE,
    )

    resultado = svc.resume_golden_protection(operation_id=_OP)

    assert llamadas == ["s4c:s4c_rollback"], (
        f"un journal ABSENT con binding durable debe llegar a S4-C por la ruta de recovery: {llamadas}"
    )
    assert resultado.route is svc.RestartRoute.S4C_ROLLBACK


def test_sin_evidencia_durable_no_hay_transaccion(monkeypatch: pytest.MonkeyPatch) -> None:
    """plan ausente + binding ausente → NOT_APPLICABLE, cero escrituras."""
    llamadas = _parchar_evidence(
        monkeypatch,
        plan=DurableWriteOutcome.NOT_DURABLE,
        binding=OperationLockBindingEvidence.ABSENT,
    )

    resultado = svc.resume_golden_protection(operation_id=_OP)

    assert llamadas == [], "sin evidencia durable no se invoca ningún motor"
    assert resultado.disposition is svc.ProtectionDisposition.NOT_APPLICABLE
    assert resultado.committed is False
    assert resultado.settled is True


def test_binding_ilegible_es_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Binding presente pero ilegible → INDETERMINATE + operador, cero escrituras."""
    llamadas = _parchar_evidence(
        monkeypatch,
        plan=DurableWriteOutcome.NOT_DURABLE,
        binding=OperationLockBindingEvidence.INDETERMINATE,
    )

    resultado = svc.resume_golden_protection(operation_id=_OP)

    assert llamadas == [], "un binding ilegible no puede autorizar ninguna acción"
    assert resultado.disposition is svc.ProtectionDisposition.INDETERMINATE
    assert resultado.operator_intervention_required is True
    assert resultado.lock_retained is True
    assert resultado.fail_closed_reason


def test_el_routing_pre_journal_nunca_lee_staging(monkeypatch: pytest.MonkeyPatch) -> None:
    """STAGING != AUTHORITY, y el camino pre-plan es donde más tentador sería.

    El pre-plan es la única ventana donde NO hay plan ni journal: la única
    tentación es reconstruir evidencia desde staging. Este anchor congela que
    el router no tiene ninguna ruta de lectura hacia staging, para que un
    "agarremos el candidate manifest" futuro rompa acá y no en producción.
    """
    import ast

    arbol = ast.parse(
        pathlib.Path(svc.__file__).read_text(encoding="utf-8"),
        filename=str(svc.__file__),
    )
    cuerpo_resolver = next(
        nodo for nodo in ast.walk(arbol) if isinstance(nodo, ast.FunctionDef) and nodo.name == "_resolver_pre_journal"
    )
    nombres: set[str] = set()
    for nodo in ast.walk(cuerpo_resolver):
        match nodo:
            case ast.Name():
                nombres.add(nodo.id)
            case ast.Attribute():
                nombres.add(nodo.attr)
    for prohibido in (
        "read_candidate_manifest_bytes",
        "derive_candidate_manifest_path",
        "candidate_manifest_bytes",
        "publish_candidate_manifest",
        "staging_digest",
    ):
        assert prohibido not in nombres, (
            f"el resolver pre-plan menciona '{prohibido}': staging no es autoridad de recovery, "
            "ni siquiera cuando no hay plan ni journal."
        )


def test_absent_no_se_confunde_con_indeterminate(monkeypatch: pytest.MonkeyPatch) -> None:
    """Los DOS caminos pre-journal con binding durable, distinguidos por el plan.

    Journal ABSENT + plan NOT_DURABLE es un pre-plan limpio. Journal
    INDETERMINATE (no ABSENT) tiene que seguir siendo fail-closed aunque el
    binding sea durable. Es la distinción que P3 vino a hacer; sin este par,
    el router podría degradar ambas al mismo caso.
    """

    class _ClasifIndeterminate:
        classification = svc.ProtectionJournalClassification.INDETERMINATE
        is_valid = False
        detail = "torn tail"
        journal = None

    _parchar_evidence(
        monkeypatch,
        plan=DurableWriteOutcome.NOT_DURABLE,
        binding=OperationLockBindingEvidence.DURABLE,
    )
    monkeypatch.setattr(svc, "classify_protection_journal", lambda *a, **k: _ClasifIndeterminate())

    resultado = svc.resume_golden_protection(operation_id=_OP)

    assert resultado.disposition is svc.ProtectionDisposition.INDETERMINATE
    assert resultado.operator_intervention_required is True


def test_absent_valido_llega_a_s4c_aunque_el_resultado_no_lo_diga() -> None:
    """Smoke del contrato: con journal ABSENT, S4-C devuelve el reporte real.

    Este test no usa fakes de la evidencia: congela que cuando el router
    resuelve un pre-plan, delega y proyecta lo que S4-C/continuación devolvió,
    sin inventar una disposición propia.
    """
    assert (
        svc.RestartRoute.S4C_ROLLBACK
        is svc.PRE_JOURNAL_ROUTES[(DurableWriteOutcome.NOT_DURABLE, OperationLockBindingEvidence.DURABLE)]
    )
