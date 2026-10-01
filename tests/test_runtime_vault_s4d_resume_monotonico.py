"""Recuperación monotónica desde CADA fase durable de S4-D.

Regresión de un P1 bloqueante que los tests previos no podían ver: **todos**
arrancaban desde ``APPLYING``, así que el tramo de reanudación —que existe
precisamente para cuando el proceso anterior muere— nunca se ejercitaba.

El defecto: la cadena usaba ``if estado_durable is not FASE: entrar(FASE)`` por
fase. Desde ``VERIFYING_NODE_SET`` eso pedía ``enter(VERIFYING_RV2)``, una arista
que el FSM de §19.2 no admite, y el resultado era ``INDETERMINATE`` con **cero**
gates ejecutados. El Golden quedaba sin verificar y sin rollback, que es el peor
resultado posible: parece un problema de infraestructura y es una pérdida de
evidencia.

Este archivo enumera los seis estados durables de entrada y comprueba, para cada
uno, la SECUENCIA EXACTA de gates. No alcanza con que "llegue a COMMITTED": una
reanudación que re-ejecuta un gate ya superado es correcta, y una que se salta
uno por debajo de ella es un agujero. La enumeración es lo que distingue ambas.
"""

from __future__ import annotations

import pathlib
import tempfile
from typing import Any

import pytest

from sky_claw.local.runtime_vault.finalization_orchestrator import (
    FinalizationDisposition,
    GateVerdict,
)
from sky_claw.local.runtime_vault.golden_mutation_lock import GoldenLockPhase, deserialize_golden_lock_metadata
from sky_claw.local.runtime_vault.protection_journal import ProtectionTransactionState
from tests.test_runtime_vault_s4d_finalization import (
    _finalize,
    _Harness,
    _lock_path,
    _obs,
)

#: Orden normativo de §19.2. La enumeración completa del tramo es lo que hace
#: este archivo valuable: un estado nuevo, o un salto en el orden, cambia la tabla.
_TRAMO: tuple[ProtectionTransactionState, ...] = (
    ProtectionTransactionState.VERIFYING_GP1,
    ProtectionTransactionState.VERIFYING_RV2,
    ProtectionTransactionState.VERIFYING_NODE_SET,
    ProtectionTransactionState.ARCHIVING_BACKUP,
)

#: Secuencia esperada de gates al reanudar desde cada fase, INCLUDING la que
#: corresponde a esa fase. Se escribe completa y literal para que una
#: reordenación —que a veces es correcta— obligue a passingar por acá.
_ESPERADO: dict[ProtectionTransactionState, tuple[str, ...]] = {
    # Desde el principio: la cadena completa.
    ProtectionTransactionState.APPLYING: (
        "gp1",
        "rv2",
        "node_set",
        "quiescence",
        "rv2",
        "rv2",
        "archive",
    ),
    ProtectionTransactionState.VERIFYING_GP1: (
        "gp1",
        "rv2",
        "node_set",
        "quiescence",
        "rv2",
        "rv2",
        "archive",
    ),
    ProtectionTransactionState.VERIFYING_RV2: (
        "rv2",
        "node_set",
        "quiescence",
        "rv2",
        "rv2",
        "archive",
    ),
    # >>> El caso del P1. NO aparece ``gp1`` ni el ``rv2`` anterior: reanudar
    # desde aquí no puede retroceder el FSM para volver a verificarlos.
    ProtectionTransactionState.VERIFYING_NODE_SET: (
        "node_set",
        "quiescence",
        "rv2",
        "rv2",
        "archive",
    ),
    # Desde ARCHIVING_BACKUP §20 C8 dice "continúa el archivado". Lo único que se
    # re-observa es el contenido justo antes de archivar (el contenido pudo
    # cambiar mientras el proceso estuvo muerto).
    ProtectionTransactionState.ARCHIVING_BACKUP: (
        "rv2",
        "archive",
    ),
}


def _harness_hasta(destino: ProtectionTransactionState) -> _Harness:
    """Harness con el journal durable AVANZADO hasta ``destino`` inclusive.

    Usa las transiciones reales (``enter_finalization_phase``), no una
    construcción artificial del journal: así el fixture no puede面料ar un
    estado que el FSM jamás produciría.
    """
    h = _Harness(pathlib.Path(tempfile.mkdtemp()))
    for fase in _TRAMO[: _TRAMO.index(destino) + 1]:
        h.journal.enter_finalization_phase(fase)
    assert h.journal_estado() is destino, f"el fixture no llegó a {destino.value}: quedó en {h.journal_estado().value}"
    return h


# ============================================================================
# La property que importaba: la reanudación es MONOTÓNICA
# ============================================================================


@pytest.mark.parametrize("destino", list(_TRAMO), ids=lambda estado: estado.value)
def test_la_reanudacion_desde_cada_fase_termina_en_committed(destino: ProtectionTransactionState) -> None:
    """Desde CADA fase durable, S4-D llega a COMMITTED y libera el lock."""
    h = _harness_hasta(destino)

    reporte = _finalize(h)

    assert reporte.disposition is FinalizationDisposition.COMMITTED, (
        f"reanudar desde {destino.value} no debe terminar en {reporte.disposition.value}: {reporte.fail_closed_reason}"
    )
    assert h.journal_estado() is ProtectionTransactionState.COMMITTED
    assert h.backup_path().exists()
    assert reporte.archive_digest is not None
    assert reporte.lock.released is True
    metadata = deserialize_golden_lock_metadata(h.lock_kernel.files[str(_lock_path(h.raiz))])
    assert metadata.phase == GoldenLockPhase.RELEASED.value
    assert metadata.operation_id == h.plan.operation_id


@pytest.mark.parametrize("destino", list(_TRAMO), ids=lambda estado: estado.value)
def test_la_secuencia_de_gates_es_exactamente_la_normativa(destino: ProtectionTransactionState) -> None:
    """Cada fase reanuda en SU fase y avanza. Ni retrocede ni se salta gates.

    Ésta es la mitad que faltaba del P1: que "llegue a COMMITTED" no distingue una
    reanudación correcta de una que re-ejecuta de más o que se salta una
    verificación por debajo.
    """
    h = _harness_hasta(destino)

    _finalize(h)

    # ``archive`` no es una llamada al puerto de verificación sino un paso del
    # store, así que siempre se suma a la secuencia de gates observados.
    ejecutados = tuple(h.puerto.llamadas) + ("archive",)
    assert ejecutados == _ESPERADO[destino], (
        f"reanudar desde {destino.value} ejecutó {ejecutados} y se esperaba {_ESPERADO[destino]}"
    )


def test_desde_verifying_node_set_no_pide_la_arista_ilegal_hacia_verifying_rv2() -> None:
    """El P1 exacto: ``VERIFYING_NODE_SET`` no puede pedir ``VERIFYING_RV2``.

    Si el arreglo se revierte, el ``enter_finalization_phase`` lanza
    ``FinalizationPhaseNotAdmittedError`` y el orquestador devuelve
    INDETERMINATE. Este test falla en ese caso, y falla CON el motivo del
    defecto en el `fail_closed_reason`.
    """
    h = _harness_hasta(ProtectionTransactionState.VERIFYING_NODE_SET)
    assert h.journal_estado() is ProtectionTransactionState.VERIFYING_NODE_SET

    reporte = _finalize(h)

    assert reporte.disposition is not FinalizationDisposition.INDETERMINATE, (
        f"la reanudación desde VERIFYING_NODE_SET cayó a INDETERMINATE: {reporte.fail_closed_reason}"
    )
    assert "verifying_rv2" not in reporte.fail_closed_reason
    # Y el journal no contiene ninguna arista hacia atrás.
    crudo = _journal_path(h).read_text(encoding="utf-8").replace(" ", "")
    assert '"state":"verifying_node_set"' in crudo
    estados = _estados_del_journal(h)
    rangos = [_RANGO[estado] for estado in estados]
    assert rangos == sorted(rangos), f"transiciones no monotónicas: {estados}"


def test_desde_verifying_node_set_el_nodeset_se_re_observa() -> None:
    """El NodeSet se VUELVE a observar en su propia fase; no se hereda de S4-B.

    Es el fondo del P1: sin re-observación, un nodo que apareció entre el apply y
    el crash se commitearía sin que nadie lo mirara.
    """
    h = _harness_hasta(ProtectionTransactionState.VERIFYING_NODE_SET)
    h.puerto.node_set = _obs(False, "un_nodo_aparecio_despues_del_apply", "node_set")

    reporte = _finalize(h)

    assert reporte.disposition is FinalizationDisposition.ROLLBACK_REQUIRED
    assert "node_set" in h.puerto.llamadas
    assert not h.backup_path().exists()
    assert h.journal_estado() is ProtectionTransactionState.ROLLBACK_REQUIRED


@pytest.mark.parametrize("destino", list(_TRAMO), ids=lambda estado: estado.value)
def test_la_reanudacion_nunca_escribe_una_transicion_hacia_atras(destino: ProtectionTransactionState) -> None:
    """Ninguna fase durable puede reaparecer DESPUÉS de una fase superior.

    Es la invariante que el P1 violaba. Se comprueba sobre el journal completo,
    no sobre el estado final: un `VERIFYING_RV2` re-emitido después de un
    `VERIFYING_NODE_SET` dejaría el estado final correcto y el journal con una
    arista ilegal en medio.
    """
    h = _harness_hasta(destino)

    _finalize(h)

    estados = _estados_del_journal(h)
    rangos = [_RANGO[estado] for estado in estados]
    assert rangos == sorted(rangos), (
        f"el journal de la reanudación desde {destino.value} contiene transiciones no monotónicas: {estados}"
    )


#: Orden total del FSM de §19.2, para detectar cualquier salto hacia atrás.
_RANGO: dict[ProtectionTransactionState, int] = {
    ProtectionTransactionState.APPLYING: 0,
    ProtectionTransactionState.VERIFYING_GP1: 1,
    ProtectionTransactionState.VERIFYING_RV2: 2,
    ProtectionTransactionState.VERIFYING_NODE_SET: 3,
    ProtectionTransactionState.ARCHIVING_BACKUP: 4,
    ProtectionTransactionState.COMMITTED: 5,
    ProtectionTransactionState.ROLLBACK_REQUIRED: 6,
    ProtectionTransactionState.ROLLING_BACK: 7,
    ProtectionTransactionState.ROLLED_BACK: 8,
    ProtectionTransactionState.ROLLBACK_FAILED: 9,
    ProtectionTransactionState.INDETERMINATE: 10,
}


def _estados_del_journal(h: _Harness) -> list[ProtectionTransactionState]:
    """Estados de transacción en orden de aparición en el journal.

    Se leen del ARCHIVO, no de `ProtectionJournal`: ese dataclass expone el estado
    derivado y los registros de nodo, pero no la secuencia de transiciones — y lo
    que esta property necesita verificar es precisamente la secuencia.
    """
    import json

    estados: list[ProtectionTransactionState] = []
    for linea in _journal_path(h).read_text(encoding="utf-8").splitlines():
        registro = json.loads(linea)
        if registro.get("kind") == "transaction_state":
            estados.append(ProtectionTransactionState(registro["state"]))
    return estados


def _journal_path(h: _Harness) -> pathlib.Path:
    from sky_claw.local.runtime_vault.protection_journal_store import derive_protection_journal_path

    return pathlib.Path(
        str(
            derive_protection_journal_path(
                h.plan.operation_id,
                programdata_resolver=lambda: h.raiz,
            )
        )
    )


def test_desde_committed_no_se_re_ejecuta_ningun_gate() -> None:
    """COMMITTED es un hecho consumado: re-verificarlo no lo haría más cierto."""
    h = _Harness(pathlib.Path(tempfile.mkdtemp()))
    _finalize(h)
    h.puerto.llamadas.clear()

    reporte = _finalize(h)

    assert reporte.disposition is FinalizationDisposition.ALREADY_COMMITTED
    assert h.puerto.llamadas == []
    assert h.journal_estado() is ProtectionTransactionState.COMMITTED


def test_un_gate_que_falla_en_cualquier_fase_va_a_rollback_y_nunca_a_committed() -> None:
    """La matriz completa: por dónde se reanude, un fallo es ROLLBACK_REQUIRED.

    No se prueba un estado suelto: se recorren TODOS. La propiedad ("un gate que
    falla nunca comitea") es la que importa, y comprobarla en un subconjunto de
    puntos de entrada es exactamente el patrón que dejó pasar el P1.
    """
    for destino in _TRAMO:
        for gate in ("gp1", "rv2", "node_set", "quiescence"):
            h = _harness_hasta(destino)
            # Sólo tiene efecto si ese gate se ejecuta en esta reanudación.
            if gate not in h.puerto.llamadas and not any(gate == g for g in _ESPERADO[destino]):
                continue
            setattr(h.puerto, gate, _obs(False, f"{gate}:fallo inyectado", gate))

            reporte = _finalize(h)

            assert reporte.disposition is FinalizationDisposition.ROLLBACK_REQUIRED, (
                f"desde {destino.value}, el fallo de {gate} dio {reporte.disposition.value}"
            )
            assert h.journal_estado() is ProtectionTransactionState.ROLLBACK_REQUIRED
            assert not h.backup_path().exists()
            assert reporte.lock.released is False
            assert reporte.lock.retained_as_orphan is True


def test_el_puerto_simple_por_tupla_conserva_la_identidad_del_gate() -> None:
    """``_evaluar`` no destruye el nombre del gate: ``verdict_for`` funciona.

    El puerto devuelve la forma más simple posible —una tupla ``(passed,
    detail)``— y aun así el reporte tiene que permitir localizar el veredicto por
    su nombre. Antes el fallback usaba ``gate="observacion"``, que hacía
    ``verdict_for("rv2")`` devolver ``None`` sobre un reporte que sí contenía el
    veredicto de RV-2.
    """
    h = _Harness(pathlib.Path(tempfile.mkdtemp()))

    class _PuertoPorTuplas:
        def __init__(self) -> None:
            self.llamadas: list[str] = []

        def observar_gp1(self, **_kwargs: Any) -> tuple[bool, str]:
            self.llamadas.append("gp1")
            return (True, "gp1 por tupla")

        def observar_rv2(self, **_kwargs: Any) -> tuple[bool, str]:
            self.llamadas.append("rv2")
            return (False, "rv2 por tupla: tree mismatch")

        def observar_node_set(self, **_kwargs: Any) -> tuple[bool, str]:
            self.llamadas.append("node_set")
            return (True, "node_set por tupla")

        def observar_quiescence(self, **_kwargs: Any) -> tuple[bool, str]:
            self.llamadas.append("quiescence")
            return (True, "quiescence por tupla")

    h.puerto = _PuertoPorTuplas()

    reporte = _finalize(h)

    assert reporte.disposition is FinalizationDisposition.ROLLBACK_REQUIRED
    veredicto = reporte.verdict_for("rv2")
    assert veredicto is not None, (
        f"verdict_for('rv2') devolvió None; el gate se perdió al normalizar. "
        f"gates presentes={[v.gate for v in reporte.verdicts]}"
    )
    assert veredicto.passed is False
    assert "tree mismatch" in veredicto.detail
    assert reporte.verdict_for("gp1") is not None


def test_el_verdict_de_un_puerto_por_tuplas_conserva_los_datos_requeridos() -> None:
    """El veredicto construido desde una tupla cumple el contrato de ``GateVerdict``."""
    from sky_claw.local.runtime_vault.finalization_orchestrator import _evaluar

    veredicto = _evaluar("rv2", (False, "detalle"))

    assert isinstance(veredicto, GateVerdict)
    assert veredicto.gate == "rv2"
    assert veredicto.passed is False
    assert veredicto.detail == "detalle"
    assert len(veredicto.evidence_digest) == 64
