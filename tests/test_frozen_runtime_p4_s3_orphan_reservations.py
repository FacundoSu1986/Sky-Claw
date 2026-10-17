"""CR-R3-02 — CREATE y START_NEXT deben rechazar reservas huérfanas ajenas.

Finding de CodeRabbit sobre `014fec6c` (`journal.py`, líneas 1429-1431 y
1469-1475), verificado contra el código antes de aceptarlo.

**El defecto.** `_crear_pendiente_locked` no audita reservas en absoluto, y
`_start_next_locked` llama a `auditar_reservas_finalizacion` pero **descarta**
`huerfanas`: esa función sólo levanta para `inconsistentes`. La reconciliación
de arranque, en cambio, ya clasifica el mismo estado durable como inadmisible:

| Estado | `reconciliar_journal` | Mutadores (antes) |
|---|---|---|
| journal `ABSENT` + cualquier reserva | `FAIL_CLOSED` / `HISTORIAL_O_RESERVAS_SIN_JOURNAL` | escribe `PENDING` |
| journal `NONE` + reserva huérfana | `REQUIRE_OWNER` / `RESERVA_HUERFANA_SIN_TRANSICION_PENDIENTE` | escribe `PENDING` |

**La consecuencia.** Una reserva huérfana en el `NEXT_FINALIZATION_SEQ` que
pertenece a otra transición `Tx` no es `inconsistente` (no hay registro que la
contradiga), así que `_finalizar_locked` también la deja pasar hasta
`_reservar_seq_locked`, que recién ahí levanta `JournalCasError`. El `PENDING`
recién escrito queda imposible de cerrar por la vía normal y, por
`AT_MOST_ONE_PENDING_PER_ROOT`, bloquea todo `CREATE` posterior.

**La propiedad que se ancla acá:** una transición nueva NO puede escribirse
cuando existe una reserva huérfana, porque el journal (`ABSENT`/`NONE`) prueba
que no hay `PENDING` que la reclame ⇒ la reserva es ajena y el estado exige
dueño (§38.3). El rechazo ocurre ANTES de la primera mutación y la evidencia
durable queda byte-idéntica.

`test_recovery_legitimo_del_mismo_pending_se_preserva` es el contrapeso: el
mismo mecanismo NO debe bloquear el caso `RECOVER` de §38.3, donde la reserva
huérfana sí corresponde al `PENDING` vigente.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from sky_claw.local.frozen_runtime import journal as journal_module
from sky_claw.local.frozen_runtime.errors import (
    JournalCorruptError,
    JournalIdentityError,
)
from sky_claw.local.frozen_runtime.storage import (
    active_state_path,
    finalization_seqs_state_dir,
    transition_journal_path,
    transitions_state_dir,
)
from tests.test_frozen_runtime_p4_s3_durable_journal import (
    T1,
    T2,
    T3,
    _aprobacion,
    _crear_pendiente,
    _dump_estado,
    _escribir_json,
    _finalizar,
    _observacion,
    _payload_reserva,
    _post_para,
    _propuesta,
    _root,
    _start_next,
)


def _estado_durable(root: pathlib.Path) -> dict:
    """Huella de TODO lo que un mutador podría tocar: journal, store y Desired."""
    estado = _dump_estado(root)
    desired = active_state_path(root)
    estado["desired_existe"] = desired.exists()
    estado["desired_bytes"] = desired.read_bytes() if desired.exists() else None
    return estado


def _registros(root: pathlib.Path) -> list[str]:
    return sorted(p.name for p in transitions_state_dir(root).glob("*.json"))


def _reservas(root: pathlib.Path) -> list[str]:
    return sorted(p.name for p in finalization_seqs_state_dir(root).glob("*.json"))


def _store_con_t1_finalizado(root: pathlib.Path) -> None:
    """Store SANO: registro `T1`(seq 1) + su reserva + journal `NONE` con testigo."""
    _crear_pendiente(root)
    _finalizar(root)
    assert _registros(root) == [f"{T1}.json"]
    assert _reservas(root) == ["1.json"]
    assert journal_module.cargar_journal_transicion(root).disposition.value == "NONE"


# ============================================================================
# T1 — CREATE con reserva huérfana (journal ABSENT)
# ============================================================================


def test_create_rechaza_reserva_huerfana_sin_pending(tmp_path: pathlib.Path) -> None:
    root = _root(tmp_path)
    # Bootstrap limpio (journal ABSENT) + reserva de seq=1 que pertenece a T3.
    _escribir_json(
        finalization_seqs_state_dir(root) / "1.json",
        _payload_reserva(root, seq=1, transition_id=T3),
    )
    antes = _estado_durable(root)

    # La reconciliación YA declara inadmisible este estado: el mutador debe coincidir.
    veredicto = journal_module.reconciliar_journal(root)
    assert veredicto.veredicto.value == "FAIL_CLOSED"
    assert "HISTORIAL_O_RESERVAS_SIN_JOURNAL" in veredicto.motivo

    with pytest.raises(JournalIdentityError) as excinfo:
        _crear_pendiente(root, _propuesta(T2), _aprobacion(T2))

    # El motivo se pincha: un rechazo por otra causa no satisface la propiedad.
    assert "REQUIRE_OWNER" in str(excinfo.value)
    assert str((1, T3)) in str(excinfo.value)

    despues = _estado_durable(root)
    assert despues == antes  # journal, store y Desired byte-idénticos
    assert not transition_journal_path(root).exists()
    assert journal_module.cargar_journal_transicion(root).disposition.value == "ABSENT"
    assert _registros(root) == []


def test_create_rechaza_reserva_huerfana_del_mismo_transition_id(tmp_path: pathlib.Path) -> None:
    """Sin `PENDING` no hay `RECOVER`: la reserva es ajena AUNQUE el id coincida.

    Si el guard se escribiera como "huérfana de OTRA transición", este caso
    pasaría y el `PENDING` volvería a quedar imposible de cerrar.
    """
    root = _root(tmp_path)
    _escribir_json(
        finalization_seqs_state_dir(root) / "1.json",
        _payload_reserva(root, seq=1, transition_id=T2),
    )
    antes = _estado_durable(root)

    with pytest.raises(JournalIdentityError) as excinfo:
        _crear_pendiente(root, _propuesta(T2), _aprobacion(T2))

    assert "REQUIRE_OWNER" in str(excinfo.value)
    assert _estado_durable(root) == antes


def test_create_admite_placeholder_vacio_de_una_f1a_interrumpida(tmp_path: pathlib.Path) -> None:
    """Control positivo: el placeholder de 0 bytes NO es una reserva huérfana.

    `_reservar_seq_locked` lo deja a propósito y `auditar_reservas_finalizacion`
    lo omite porque no compromete evidencia. Un guard que enumerara el
    directorio por su cuenta (en vez de leer la auditoría) rompería acá.
    """
    root = _root(tmp_path)
    ruta = finalization_seqs_state_dir(root) / "1.json"
    ruta.write_bytes(b"")

    assert journal_module.auditar_reservas_finalizacion(root).huerfanas == ()

    journal = _crear_pendiente(root)
    assert journal.transition_id == T1
    assert journal_module.cargar_journal_transicion(root).disposition.value == "PENDING_PROMOTION"
    assert ruta.read_bytes() == b""  # CREATE no toca la reserva ajena al bootstrap


# ============================================================================
# T2 — START_NEXT con reserva huérfana (journal NONE con historial)
# ============================================================================


def test_start_next_rechaza_reserva_huerfana_en_el_next_seq(tmp_path: pathlib.Path) -> None:
    root = _root(tmp_path)
    _store_con_t1_finalizado(root)
    # La reserva huérfana ocupa EXACTAMENTE el NEXT_FINALIZATION_SEQ (2) y
    # pertenece a T3: es el escenario del finding.
    _escribir_json(
        finalization_seqs_state_dir(root) / "2.json",
        _payload_reserva(root, seq=2, transition_id=T3),
    )
    antes = _estado_durable(root)
    revision_antes = journal_module.cargar_journal_transicion(root).journal.journal_revision

    veredicto = journal_module.reconciliar_journal(root)
    assert veredicto.veredicto.value == "REQUIRE_OWNER"
    assert "RESERVA_HUERFANA_SIN_TRANSICION_PENDIENTE" in veredicto.motivo

    with pytest.raises(JournalIdentityError) as excinfo:
        _start_next(root, _propuesta(T2), _aprobacion(T2))

    assert "REQUIRE_OWNER" in str(excinfo.value)
    assert str((2, T3)) in str(excinfo.value)

    despues = _estado_durable(root)
    assert despues == antes
    snapshot = journal_module.cargar_journal_transicion(root)
    assert snapshot.disposition.value == "NONE"  # ningún PENDING(T2) escrito
    assert snapshot.journal is not None
    assert snapshot.journal.journal_revision == revision_antes  # la revisión no avanzó


def test_start_next_rechaza_reserva_huerfana_del_mismo_transition_id(tmp_path: pathlib.Path) -> None:
    """Simétrico de CREATE: con journal `NONE` la reconciliación exige dueño igual."""
    root = _root(tmp_path)
    _store_con_t1_finalizado(root)
    _escribir_json(
        finalization_seqs_state_dir(root) / "2.json",
        _payload_reserva(root, seq=2, transition_id=T2),
    )
    antes = _estado_durable(root)

    with pytest.raises(JournalIdentityError) as excinfo:
        _start_next(root, _propuesta(T2), _aprobacion(T2))

    assert "REQUIRE_OWNER" in str(excinfo.value)
    assert _estado_durable(root) == antes


# ============================================================================
# T3 — el recovery legítimo del MISMO PENDING sigue intacto
# ============================================================================


def test_recovery_legitimo_del_mismo_pending_se_preserva(tmp_path: pathlib.Path) -> None:
    """Crash entre F1a y F1b: la reserva huérfana SÍ corresponde al PENDING vigente.

    §38.3: ese caso es `RECOVER`, no `REQUIRE_OWNER`. El fix no puede
    convertirlo en un error genérico ni borrar la reserva.
    """
    root = _root(tmp_path)
    _crear_pendiente(root)  # PENDING(T1), sin registro
    ruta_reserva = finalization_seqs_state_dir(root) / "1.json"
    _escribir_json(ruta_reserva, _payload_reserva(root, seq=1, transition_id=T1))
    bytes_reserva = ruta_reserva.read_bytes()

    veredicto = journal_module.reconciliar_journal(root, observador_effective=lambda: _observacion())
    assert veredicto.veredicto.value == "RECOVER"
    assert "RESERVA_HUERFANA_PARA_LA_TRANSICION_PENDIENTE" in veredicto.motivo
    assert veredicto.requiere_dueno is False
    assert ruta_reserva.read_bytes() == bytes_reserva

    # El recovery REAL completa la finalización: no borra la reserva, no crea
    # una transición adicional y no reescribe la evidencia.
    registro = _finalizar(root)
    assert registro.transition_id == T1
    assert registro.finalization_seq == 1
    assert ruta_reserva.read_bytes() == bytes_reserva
    assert _reservas(root) == ["1.json"]
    assert _registros(root) == [f"{T1}.json"]
    assert journal_module.cargar_journal_transicion(root).disposition.value == "NONE"


# ============================================================================
# T4 — el store sano sigue funcionando (control positivo)
# ============================================================================


def test_store_sano_crea_finaliza_y_encadena_la_siguiente(tmp_path: pathlib.Path) -> None:
    root = _root(tmp_path)
    _store_con_t1_finalizado(root)

    journal_t2 = _start_next(root, _propuesta(T2), _aprobacion(T2))
    assert journal_t2.transition_id == T2

    registro_t2 = _finalizar(root, _post_para(root, T2))
    assert registro_t2.transition_id == T2
    assert registro_t2.finalization_seq == 2  # secuencia contigua
    assert registro_t2.finalized_from_transition_id == T1  # predecessor coherente
    assert _registros(root) == sorted([f"{T1}.json", f"{T2}.json"])
    assert _reservas(root) == ["1.json", "2.json"]
    assert journal_module.cargar_journal_transicion(root).disposition.value == "NONE"

    # Reintento idempotente: no reescribe registro ni journal.
    bytes_registro = (transitions_state_dir(root) / f"{T2}.json").read_bytes()
    bytes_journal = transition_journal_path(root).read_bytes()
    reintento = _finalizar(root, _post_para(root, T2))
    assert reintento.transition_id == T2
    assert (transitions_state_dir(root) / f"{T2}.json").read_bytes() == bytes_registro
    assert transition_journal_path(root).read_bytes() == bytes_journal


# ============================================================================
# T5 — corrupción y no-clobber: el guard no relaja ningún validador
# ============================================================================


def test_create_rechaza_reserva_de_otro_root(tmp_path: pathlib.Path) -> None:
    root = _root(tmp_path)
    otro_root = _root(tmp_path, "otro-root")
    payload = _payload_reserva(otro_root, seq=1, transition_id=T3)
    assert payload["root_id"] != _payload_reserva(root, seq=1, transition_id=T3)["root_id"]
    _escribir_json(finalization_seqs_state_dir(root) / "1.json", payload)
    antes = _estado_durable(root)

    with pytest.raises(JournalIdentityError):
        _crear_pendiente(root, _propuesta(T2), _aprobacion(T2))
    assert _estado_durable(root) == antes


def test_create_rechaza_reserva_malformada(tmp_path: pathlib.Path) -> None:
    root = _root(tmp_path)
    ruta = finalization_seqs_state_dir(root) / "1.json"
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_text('{"schema_version": 1, "finalization_seq": 1', encoding="utf-8")
    antes = _estado_durable(root)

    with pytest.raises(JournalCorruptError):
        _crear_pendiente(root, _propuesta(T2), _aprobacion(T2))
    assert _estado_durable(root) == antes


def test_start_next_rechaza_reserva_incompatible_con_el_registro(tmp_path: pathlib.Path) -> None:
    """Registro `T1`(seq 1) con reserva de `T3` en la MISMA seq: autoridad incoherente.

    Es el camino `inconsistentes`, que ya fallaba; se ancla a nivel mutador para
    que el nuevo guard no lo degrade a "huérfana" ni lo deje pasar.
    """
    root = _root(tmp_path)
    _store_con_t1_finalizado(root)
    _escribir_json(
        finalization_seqs_state_dir(root) / "1.json",
        _payload_reserva(root, seq=1, transition_id=T3),
    )
    antes = _estado_durable(root)

    with pytest.raises(JournalIdentityError) as excinfo:
        _start_next(root, _propuesta(T2), _aprobacion(T2))

    assert "incoherentes" in str(excinfo.value)
    assert _estado_durable(root) == antes


def test_create_rechaza_reserva_huerfana_con_journal_none_vacio(tmp_path: pathlib.Path) -> None:
    """Variante `NONE` sin historial: mismo rechazo, otro punto de entrada."""
    root = _root(tmp_path)
    from tests.test_frozen_runtime_p4_s3_durable_journal import _payload_none

    _escribir_json(transition_journal_path(root), _payload_none(revision=1, witness=None))
    _escribir_json(
        finalization_seqs_state_dir(root) / "1.json",
        _payload_reserva(root, seq=1, transition_id=T3),
    )
    antes = _estado_durable(root)

    veredicto = journal_module.reconciliar_journal(root)
    assert veredicto.veredicto.value == "REQUIRE_OWNER"
    assert "RESERVA_HUERFANA_SIN_TRANSICION_PENDIENTE" in veredicto.motivo

    with pytest.raises(JournalIdentityError) as excinfo:
        _crear_pendiente(root, _propuesta(T2), _aprobacion(T2))

    assert "REQUIRE_OWNER" in str(excinfo.value)
    assert _estado_durable(root) == antes
    assert json.loads(transition_journal_path(root).read_text(encoding="utf-8"))["state"] == "NONE"
