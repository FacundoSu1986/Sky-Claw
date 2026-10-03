"""GP2-S4E / P5-B — mappings terminales: exhaustivos y congelados.

El defecto que estos tests cierran: `_proyeccion_de_recovery` mapeaba TODO
terminal que no fuera COMMITTED a `ROLLED_BACK`, que es una afirmación forense
falsa — `CANCELLED`, `ELEVATION_REJECTED`, `REFUSE_TO_PLAN` y
`REFUSE_TO_APPLY` significan que NO hubo restauración física.

Se congela por IGUALDAD LITERAL del diccionario completo, no por muestreo: un
estado terminal nuevo en el enum rompe estos tests hasta que alguien decida
explícitamente qué disposición le corresponde. Ese es el punto — un default
silencioso es cómo el bug original pasó desapercibido.
"""

from __future__ import annotations

import pytest

from sky_claw.local.runtime_vault import protection_service as svc
from sky_claw.local.runtime_vault.protection_journal import (
    TERMINAL_TRANSACTION_STATES,
    ProtectionTransactionState,
)
from sky_claw.local.runtime_vault.recovery_orchestrator import RecoveryDisposition

_OP = "3f2b1c8e-9a4d-4f5e-8b7a-1c2d3e4f5a6b"


# ===========================================================================
# La tabla de estados terminales del journal
# ===========================================================================


def test_la_tabla_terminal_esta_congelada() -> None:
    """Igualdad literal del mapeo completo estado terminal -> disposición.

    Cada fila tiene su razón, y ninguna afirma un rollback que no ocurrió:

    * ``COMMITTED`` -> ALREADY_COMMITTED: el efecto buscado ya es durable.
    * ``ROLLED_BACK`` -> ROLLED_BACK: acá SÍ hubo restauración, y es el ÚNICO
      caso donde afirmarlo es correcto.
    * ``CANCELLED`` -> NOT_APPLICABLE: la operación se abandonó sin mutar.
    * ``ELEVATION_REJECTED`` / ``REFUSE_TO_PLAN`` / ``REFUSE_TO_APPLY`` ->
      REFUSED: se negó antes de tocar el Golden.
    * ``INDETERMINATE`` / ``ROLLBACK_FAILED`` -> INDETERMINATE: fail-closed,
      exigen operador; NO son terminales limpios.
    """
    esperado = {
        ProtectionTransactionState.COMMITTED: svc.ProtectionDisposition.ALREADY_COMMITTED,
        ProtectionTransactionState.ROLLED_BACK: svc.ProtectionDisposition.ROLLED_BACK,
        ProtectionTransactionState.CANCELLED: svc.ProtectionDisposition.NOT_APPLICABLE,
        ProtectionTransactionState.ELEVATION_REJECTED: svc.ProtectionDisposition.REFUSED,
        ProtectionTransactionState.REFUSE_TO_PLAN: svc.ProtectionDisposition.REFUSED,
        ProtectionTransactionState.REFUSE_TO_APPLY: svc.ProtectionDisposition.REFUSED,
        ProtectionTransactionState.INDETERMINATE: svc.ProtectionDisposition.INDETERMINATE,
        ProtectionTransactionState.ROLLBACK_FAILED: svc.ProtectionDisposition.INDETERMINATE,
    }
    assert esperado == svc._DISPOSICION_POR_ESTADO_TERMINAL


def test_la_tabla_terminal_cubre_todos_los_terminales_del_enum() -> None:
    """Exhaustividad sobre el enum VIVO: un terminal nuevo rompe acá.

    Es la mitad mecánica del anchor anterior. Sin esto, un estado terminal
    agregado al FSM caería en el ``.get(..., INDETERMINATE)`` y nadie se
    enteraría de que le falta una decisión explícita.
    """
    assert set(svc._DISPOSICION_POR_ESTADO_TERMINAL) == set(TERMINAL_TRANSACTION_STATES)


def test_solo_rolled_back_afirma_rollback() -> None:
    """La propiedad forense, enunciada como tal.

    De todos los estados terminales, el único que autoriza a decir "se
    restauró el PRE" es ``ROLLED_BACK``. Es la contrapositiva exacta del bug:
    afirmar rollback en un estado donde no lo hubo.
    """
    afirman_rollback = {
        estado
        for estado, disp in svc._DISPOSICION_POR_ESTADO_TERMINAL.items()
        if disp is svc.ProtectionDisposition.ROLLED_BACK
    }
    assert afirman_rollback == {ProtectionTransactionState.ROLLED_BACK}


def test_los_fail_closed_no_son_terminales_limpios() -> None:
    """``INDETERMINATE`` y ``ROLLBACK_FAILED`` NO se resuelven solos.

    Mapearlos a una disposición cerrada haría que el arranque los tratara como
    resueltos y el operador nunca se enterara.
    """
    for estado in (
        ProtectionTransactionState.INDETERMINATE,
        ProtectionTransactionState.ROLLBACK_FAILED,
    ):
        assert svc._DISPOSICION_POR_ESTADO_TERMINAL[estado] is (svc.ProtectionDisposition.INDETERMINATE)


def test_ningun_terminal_no_committed_se_proyecta_como_committed() -> None:
    """Sólo ``COMMITTED`` puede producir ``ALREADY_COMMITTED``."""
    commiteados = {
        estado
        for estado, disp in svc._DISPOSICION_POR_ESTADO_TERMINAL.items()
        if disp is svc.ProtectionDisposition.ALREADY_COMMITTED
    }
    assert commiteados == {ProtectionTransactionState.COMMITTED}


# ===========================================================================
# La tabla de disposiciones de S4-C
# ===========================================================================


def test_la_tabla_de_s4c_esta_congelada() -> None:
    """Igualdad literal del mapeo ``RecoveryDisposition`` -> disposición.

    ``PRE_PLAN_LOCK_RECOVERED`` es la fila que motiva la tabla: S4-C normalizó
    el lock huérfano de una operación que nunca alcanzó el publish del plan,
    así que no hubo MUTATING, no hubo rollback y no había nada que revertir.
    Es un desenlace CERRADO y limpio -> ``NOT_APPLICABLE``, no
    ``INDETERMINATE``.
    """
    esperado = {
        RecoveryDisposition.ROLLED_BACK: svc.ProtectionDisposition.ROLLED_BACK,
        RecoveryDisposition.POST_VERIFICATION_REQUIRED: svc.ProtectionDisposition.ROLLBACK_REQUIRED,
        RecoveryDisposition.PRE_PLAN_LOCK_RECOVERED: svc.ProtectionDisposition.NOT_APPLICABLE,
        RecoveryDisposition.LOCK_BUSY: svc.ProtectionDisposition.LOCK_BUSY,
        RecoveryDisposition.NO_TRANSACTION: svc.ProtectionDisposition.NOT_APPLICABLE,
        RecoveryDisposition.INDETERMINATE: svc.ProtectionDisposition.INDETERMINATE,
        RecoveryDisposition.TERMINAL: svc.ProtectionDisposition.REFUSED,
    }
    assert esperado == svc._DISPOSICION_POR_DESENECHO_DE_S4C


def test_la_tabla_de_s4c_cubre_todas_las_disposiciones() -> None:
    """Exhaustividad: una disposición nueva de S4-C rompe acá."""
    assert set(svc._DISPOSICION_POR_DESENECHO_DE_S4C) == set(RecoveryDisposition)


# ===========================================================================
# La proyección real, estado por estado
# ===========================================================================


@pytest.mark.parametrize("estado", sorted(TERMINAL_TRANSACTION_STATES, key=lambda e: e.value))
def test_la_proyeccion_de_un_terminal_usa_su_fila(estado: ProtectionTransactionState) -> None:
    """End-to-end de la tabla: el reporte TERMINAL se proyecta por su estado.

    No alcanza con congelar el diccionario — hay que probar que la proyección
    lo CONSULTA. Una tabla correcta que nadie lee no arregla nada.
    """
    from sky_claw.local.runtime_vault.authorized_plan_store import DurableWriteOutcome
    from sky_claw.local.runtime_vault.protection_journal_store import (
        ProtectionJournalClassification,
    )
    from sky_claw.local.runtime_vault.recovery_orchestrator import (
        RecoveryForensicReport,
        RecoveryIdentitySource,
        RecoveryLockOutcome,
    )

    reporte = RecoveryForensicReport(
        operation_id=_OP,
        plan_classification=DurableWriteOutcome.DURABLE,
        journal_classification=ProtectionJournalClassification.VALID,
        observed_transaction_state=estado,
        node_wal_summary=(),
        physical_identity_source=RecoveryIdentitySource.AUTHORIZED_PLAN,
        lock_outcome=RecoveryLockOutcome.ACQUIRED_RELEASED,
        stale_lock_takeover=False,
        disposition=RecoveryDisposition.TERMINAL,
        nodes_restored=(),
        nodes_skipped=(),
        nodes_pending=(),
        physical_restoration_completed=False,
        operator_intervention_required=False,
        indeterminate_reason="",
        detail="terminal",
        setsecurityinfo_calls=0,
    )

    proyectado = svc._proyeccion_de_recovery(reporte)
    assert proyectado.disposition is svc._DISPOSICION_POR_ESTADO_TERMINAL[estado], (
        f"{estado.value} se proyectó como {proyectado.disposition.value}"
    )
