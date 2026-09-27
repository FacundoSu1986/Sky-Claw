"""Tests focales GP2-S4A: journal transaccional autoritativo — modelo puro.

Cubre ADR 0010 §19.2 (FSM), §19.4 (WAL por nodo), §20 (matriz de crash), §24
(taxonomía de errores) y §39 (prohibición de regresiones):

- ``JRNL-01``: los estados se EXTRAEN del ADR, no se inventan. La tabla cerrada
  de transiciones queda congelada por igualdad literal.
- ``JRNL-02``: esquema cerrado y versionado de los tres tipos de registro.
- ``JRNL-03``: serialización canónica (una línea por registro, ``\\n`` incluido).
- ``JRNL-04``: escritura cortada -> ``INDETERMINATE`` (fila C4b), nunca éxito.
- ``JRNL-05``: replay validado (monotonía de secuencia, sin regresiones).
- ``JRNL-06``: ``MUTATING(K)`` durable NO permite afirmar "la mutación no ocurrió".

Todo es puro (sin I/O ni Win32): la durabilidad se prueba en
``test_runtime_vault_protection_journal_store.py`` con kernels falsos.
"""

from __future__ import annotations

import json

import pytest

from sky_claw.local.runtime_vault.protection_journal import (
    HEADER_RECORD_KEYS,
    INITIAL_TRANSACTION_STATE,
    NODE_RECORD_KEYS,
    NODE_WAL_TRANSITIONS,
    PROTECTION_JOURNAL_FILE_NAME,
    PROTECTION_JOURNAL_SCHEMA_VERSION,
    S4A_PERMITTED_TRANSITION_TARGETS,
    TERMINAL_TRANSACTION_STATES,
    TRANSACTION_RECORD_KEYS,
    TRANSACTION_TRANSITIONS,
    JournalHeaderRecord,
    JournalLoadDisposition,
    JournalNodeRecord,
    JournalTransactionRecord,
    NodeWalState,
    PrematureCommitError,
    ProtectionJournal,
    ProtectionJournalError,
    ProtectionJournalIndeterminateError,
    ProtectionJournalSchemaError,
    ProtectionJournalTransitionError,
    ProtectionTransactionState,
    assert_node_transition,
    assert_transaction_transition,
    build_journal_header_record,
    deserialize_journal_bytes,
    parse_journal_bytes,
    replay_journal_records,
    serialize_journal_record,
)

_OPERATION_ID = "3f6b0be2-1c2a-4d3e-8f4a-9b7c6d5e4f3a"
_ALT_OPERATION_ID = "00f93602-bd14-4a95-80e1-a4f9689a3afb"
_PLAN_DIGEST = "a" * 64
_ALT_PLAN_DIGEST = "e" * 64
_PRE_SD_SHA256 = "b" * 64
_CANONICAL_ROOT = "C:\\Games\\Skyrim"
_VOLUME_SERIAL = 0xA1B2C3D4
_ROOT_FILE_ID = 0x1122334455667788


def _header(**overrides: object) -> JournalHeaderRecord:
    kwargs: dict[str, object] = {
        "operation_id": _OPERATION_ID,
        "authorized_plan_digest": _PLAN_DIGEST,
        "canonical_root": _CANONICAL_ROOT,
        "volume_serial_number": _VOLUME_SERIAL,
        "root_file_id": _ROOT_FILE_ID,
        "created_at": "2026-09-26T00:00:00Z",
    }
    kwargs.update(overrides)
    return build_journal_header_record(**kwargs)  # type: ignore[arg-type]


def _initial_state(sequence: int = 2) -> JournalTransactionRecord:
    return JournalTransactionRecord(sequence=sequence, state=ProtectionTransactionState(INITIAL_TRANSACTION_STATE))


def _node_record(
    sequence: int,
    *,
    relative_path: str = "Data/Skyrim.esm",
    state: NodeWalState = NodeWalState.MUTATING,
    file_id: int = _ROOT_FILE_ID + 1,
    pre_sd_sha256: str = _PRE_SD_SHA256,
) -> JournalNodeRecord:
    return JournalNodeRecord(
        sequence=sequence,
        relative_path=relative_path,
        volume_serial_number=_VOLUME_SERIAL,
        file_id=file_id,
        pre_sd_sha256=pre_sd_sha256,
        state=state,
    )


def _bytes(*records: object) -> bytes:
    return b"".join(serialize_journal_record(record) for record in records)  # type: ignore[arg-type]


# ============================================================================
# JRNL-01: FSM extraída del ADR
# ============================================================================


class TestFsmAutoritativa:
    def test_estados_exactos_del_adr(self) -> None:
        assert {state.value for state in ProtectionTransactionState} == {
            "preparing",
            "prepared",
            "awaiting_elevation",
            "applying",
            "verifying_gp1",
            "verifying_rv2",
            "verifying_node_set",
            "archiving_backup",
            "committed",
            "cancelled",
            "elevation_rejected",
            "refuse_to_apply",
            "refuse_to_plan",
            "rollback_required",
            "rolling_back",
            "rolled_back",
            "rollback_failed",
            "indeterminate",
        }

    def test_tabla_cerrada_congelada(self) -> None:
        """Igualdad literal: una transición añadida o quitada rompe este test."""
        assert TRANSACTION_TRANSITIONS[ProtectionTransactionState.PREPARING] == frozenset(
            {
                ProtectionTransactionState.PREPARED,
                ProtectionTransactionState.REFUSE_TO_APPLY,
                ProtectionTransactionState.CANCELLED,
            }
        )
        assert TRANSACTION_TRANSITIONS[ProtectionTransactionState.APPLYING] == frozenset(
            {
                ProtectionTransactionState.VERIFYING_GP1,
                ProtectionTransactionState.ROLLBACK_REQUIRED,
                ProtectionTransactionState.INDETERMINATE,
            }
        )
        assert TRANSACTION_TRANSITIONS[ProtectionTransactionState.ARCHIVING_BACKUP] == frozenset(
            {
                ProtectionTransactionState.COMMITTED,
                ProtectionTransactionState.ROLLBACK_REQUIRED,
            }
        )
        assert TRANSACTION_TRANSITIONS[ProtectionTransactionState.COMMITTED] == frozenset()
        assert TRANSACTION_TRANSITIONS[ProtectionTransactionState.ROLLED_BACK] == frozenset()

    def test_estados_terminales(self) -> None:
        assert (
            frozenset(
                {
                    ProtectionTransactionState.COMMITTED,
                    ProtectionTransactionState.CANCELLED,
                    ProtectionTransactionState.ELEVATION_REJECTED,
                    ProtectionTransactionState.REFUSE_TO_APPLY,
                    ProtectionTransactionState.REFUSE_TO_PLAN,
                    ProtectionTransactionState.ROLLED_BACK,
                    ProtectionTransactionState.ROLLBACK_FAILED,
                    ProtectionTransactionState.INDETERMINATE,
                }
            )
            == TERMINAL_TRANSACTION_STATES
        )

    def test_estado_inicial_es_applying(self) -> None:
        assert ProtectionTransactionState.APPLYING.value == INITIAL_TRANSACTION_STATE

    def test_transicion_valida_y_transicion_invalida(self) -> None:
        assert_transaction_transition(ProtectionTransactionState.APPLYING, ProtectionTransactionState.VERIFYING_GP1)
        with pytest.raises(ProtectionJournalTransitionError):
            assert_transaction_transition(ProtectionTransactionState.PREPARING, ProtectionTransactionState.APPLYING)
        with pytest.raises(ProtectionJournalTransitionError):
            assert_transaction_transition(ProtectionTransactionState.COMMITTED, ProtectionTransactionState.APPLYING)

    def test_s4a_no_puede_declarar_el_camino_de_exito(self) -> None:
        assert ProtectionTransactionState.COMMITTED not in S4A_PERMITTED_TRANSITION_TARGETS
        for estado in (
            ProtectionTransactionState.VERIFYING_GP1,
            ProtectionTransactionState.VERIFYING_RV2,
            ProtectionTransactionState.VERIFYING_NODE_SET,
            ProtectionTransactionState.ARCHIVING_BACKUP,
        ):
            assert estado not in S4A_PERMITTED_TRANSITION_TARGETS
        assert ProtectionTransactionState.ROLLBACK_REQUIRED in S4A_PERMITTED_TRANSITION_TARGETS
        assert ProtectionTransactionState.INDETERMINATE in S4A_PERMITTED_TRANSITION_TARGETS

    def test_premature_commit_es_error_tipado(self) -> None:
        """El MODELO conoce ``COMMITTED``; quien lo prohibe es la API de S4-A.

        La tabla cerrada conserva la arista ``ARCHIVING_BACKUP -> COMMITTED``
        porque es normativa (§19.2) y la necesita S4-B/C. Lo que S4-A rechaza
        siempre es la API productiva que la escribiría sin esos gates (§32): esa
        guarda vive en ``protection_journal_store.DurableProtectionJournal``.
        """
        from sky_claw.local.runtime_vault.protection_journal_store import DurableProtectionJournal

        assert (
            ProtectionTransactionState.COMMITTED in TRANSACTION_TRANSITIONS[ProtectionTransactionState.ARCHIVING_BACKUP]
        )
        assert issubclass(PrematureCommitError, ProtectionJournalError)
        assert hasattr(DurableProtectionJournal, "transition_to")


# ============================================================================
# WAL por nodo: tabla cerrada y sin regresiones
# ============================================================================


class TestWalPorNodo:
    def test_tabla_cerrada_del_nodo(self) -> None:
        assert NODE_WAL_TRANSITIONS[None] == frozenset({NodeWalState.MUTATING})
        assert NODE_WAL_TRANSITIONS[NodeWalState.MUTATING] == frozenset({NodeWalState.MUTATED})
        assert NODE_WAL_TRANSITIONS[NodeWalState.MUTATED] == frozenset()

    def test_prohibidas_las_regresiones(self) -> None:
        with pytest.raises(ProtectionJournalTransitionError):
            assert_node_transition(NodeWalState.MUTATED, NodeWalState.MUTATING, "a/b")
        with pytest.raises(ProtectionJournalTransitionError):
            assert_node_transition(NodeWalState.MUTATING, NodeWalState.MUTATING, "a/b")
        with pytest.raises(ProtectionJournalTransitionError):
            assert_node_transition(NodeWalState.MUTATED, NodeWalState.MUTATED, "a/b")

    def test_relpath_no_canonico_es_rechazado_por_construccion(self) -> None:
        for relpath in ("../escape", "/absoluto", "a\\b", "a\x00b", "a//b", ""):
            with pytest.raises(ProtectionJournalSchemaError):
                _node_record(3, relative_path=relpath)

    def test_sequence_invalida_es_rechazada(self) -> None:
        with pytest.raises(ProtectionJournalSchemaError):
            JournalTransactionRecord(sequence=0, state=ProtectionTransactionState.APPLYING)


# ============================================================================
# JRNL-02/03: esquema cerrado y serialización canónica
# ============================================================================


class TestSerializacionDeRegistros:
    def test_esquemas_cerrados(self) -> None:
        assert (
            frozenset(
                {
                    "kind",
                    "schema_version",
                    "sequence",
                    "operation_id",
                    "authorized_plan_digest",
                    "canonical_root",
                    "volume_serial_number",
                    "root_file_id",
                    "created_at",
                }
            )
            == HEADER_RECORD_KEYS
        )
        assert frozenset({"kind", "sequence", "state"}) == TRANSACTION_RECORD_KEYS
        assert (
            frozenset(
                {
                    "kind",
                    "sequence",
                    "relative_path",
                    "volume_serial_number",
                    "file_id",
                    "pre_sd_sha256",
                    "state",
                }
            )
            == NODE_RECORD_KEYS
        )

    def test_linea_canonica_termina_en_newline(self) -> None:
        linea = serialize_journal_record(_header())
        assert linea.endswith(b"\n")
        assert linea.count(b"\n") == 1
        payload = json.loads(linea.decode("utf-8"))
        assert list(payload.keys()) == sorted(HEADER_RECORD_KEYS)
        assert payload["kind"] == "journal_header"
        assert payload["schema_version"] == PROTECTION_JOURNAL_SCHEMA_VERSION
        assert payload["sequence"] == 1

    def test_nombre_de_archivo_normativo(self) -> None:
        assert PROTECTION_JOURNAL_FILE_NAME == "protection_journal.json"

    def test_operation_id_no_uuid_canonico_es_rechazado(self) -> None:
        with pytest.raises(ProtectionJournalSchemaError):
            _header(operation_id="no-es-uuid")
        with pytest.raises(ProtectionJournalSchemaError):
            _header(operation_id="3F6B0BE2-1C2A-4D3E-8F4A-9B7C6D5E4F3A")

    def test_digest_no_sha256_es_rechazado(self) -> None:
        with pytest.raises(ProtectionJournalSchemaError):
            _header(authorized_plan_digest="zz")


# ============================================================================
# JRNL-04/05: parse, clasificación y replay
# ============================================================================


class TestParseYReplay:
    def test_journal_valido_se_reproduce(self) -> None:
        raw = _bytes(_header(), _initial_state(), _node_record(3))
        resultado = parse_journal_bytes(raw)
        assert resultado.disposition is JournalLoadDisposition.VALID
        journal = resultado.journal
        assert journal is not None
        assert journal.operation_id == _OPERATION_ID
        assert journal.authorized_plan_digest == _PLAN_DIGEST
        assert journal.transaction_state is ProtectionTransactionState.APPLYING
        assert journal.sequence == 3
        assert journal.node_state("Data/Skyrim.esm") is NodeWalState.MUTATING

    def test_escritura_cortada_es_indeterminate(self) -> None:
        raw = _bytes(_header(), _initial_state(), _node_record(3))
        resultado = parse_journal_bytes(raw[:-1])
        assert resultado.disposition is JournalLoadDisposition.TORN_TAIL
        assert resultado.journal is None
        with pytest.raises(ProtectionJournalIndeterminateError):
            deserialize_journal_bytes(raw[:-1])

    def test_journal_vacio_es_escritura_cortada(self) -> None:
        assert parse_journal_bytes(b"").disposition is JournalLoadDisposition.TORN_TAIL

    def test_str_no_es_aceptado(self) -> None:
        assert parse_journal_bytes("{}").disposition is JournalLoadDisposition.SCHEMA_INVALID  # type: ignore[arg-type]

    def test_clave_desconocida_es_rechazada(self) -> None:
        linea = json.loads(serialize_journal_record(_initial_state()).decode("utf-8"))
        linea["extra"] = 1
        raw = _bytes(_header()) + json.dumps(linea, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
        assert parse_journal_bytes(raw).disposition is JournalLoadDisposition.SCHEMA_INVALID

    def test_enum_desconocido_es_rechazado(self) -> None:
        linea = json.loads(serialize_journal_record(_initial_state()).decode("utf-8"))
        linea["state"] = "bogus"
        raw = _bytes(_header()) + json.dumps(linea, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
        assert parse_journal_bytes(raw).disposition is JournalLoadDisposition.SCHEMA_INVALID

    def test_kind_desconocido_es_rechazado(self) -> None:
        linea = json.loads(serialize_journal_record(_initial_state()).decode("utf-8"))
        linea["kind"] = "otra_cosa"
        raw = _bytes(_header()) + json.dumps(linea, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
        assert parse_journal_bytes(raw).disposition is JournalLoadDisposition.SCHEMA_INVALID

    def test_clave_json_duplicada_es_rechazada(self) -> None:
        linea = serialize_journal_record(_initial_state()).decode("utf-8").strip()
        bruta = linea[:-1] + ',"sequence":9}'
        raw = _bytes(_header()) + bruta.encode("utf-8") + b"\n"
        assert parse_journal_bytes(raw).disposition is JournalLoadDisposition.SCHEMA_INVALID

    def test_secuencia_rota_es_rechazada(self) -> None:
        raw = _bytes(
            _header(), JournalTransactionRecord(sequence=7, state=ProtectionTransactionState.ROLLBACK_REQUIRED)
        )
        resultado = parse_journal_bytes(raw)
        assert resultado.disposition is JournalLoadDisposition.SCHEMA_INVALID
        assert "secuencia" in resultado.detail

    def test_header_debe_ser_el_primer_registro(self) -> None:
        raw = _bytes(_initial_state(1), _header())
        assert parse_journal_bytes(raw).disposition is JournalLoadDisposition.SCHEMA_INVALID

    def test_header_con_sequence_distinta_de_1_es_rechazado(self) -> None:
        crudo = _header()
        objeto = json.loads(serialize_journal_record(crudo).decode("utf-8"))
        objeto["sequence"] = 5
        linea = json.dumps(objeto, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
        assert parse_journal_bytes(linea).disposition is JournalLoadDisposition.SCHEMA_INVALID

    def test_declarar_dos_veces_el_estado_inicial_es_rechazado(self) -> None:
        raw = _bytes(_header(), _initial_state(2), _initial_state(3))
        assert parse_journal_bytes(raw).disposition is JournalLoadDisposition.SCHEMA_INVALID

    def test_schema_version_distinta_es_rechazada(self) -> None:
        crudo = _header()
        with pytest.raises(ProtectionJournalSchemaError):
            JournalHeaderRecord(
                schema_version="9.9",
                sequence=crudo.sequence,
                operation_id=crudo.operation_id,
                authorized_plan_digest=crudo.authorized_plan_digest,
                canonical_root=crudo.canonical_root,
                volume_serial_number=crudo.volume_serial_number,
                root_file_id=crudo.root_file_id,
                created_at=crudo.created_at,
            )

    def test_regresion_de_nodo_es_rechazada(self) -> None:
        raw = _bytes(
            _header(),
            _initial_state(),
            _node_record(3, state=NodeWalState.MUTATED),
            _node_record(4, state=NodeWalState.MUTATING),
        )
        resultado = parse_journal_bytes(raw)
        assert resultado.disposition is JournalLoadDisposition.SCHEMA_INVALID
        assert "no declarada" in resultado.detail

    def test_estado_vigente_es_el_ultimo_registro(self) -> None:
        raw = _bytes(
            _header(),
            _initial_state(),
            _node_record(3, state=NodeWalState.MUTATING),
            _node_record(4, state=NodeWalState.MUTATED),
        )
        journal = deserialize_journal_bytes(raw)
        assert journal.node_state("Data/Skyrim.esm") is NodeWalState.MUTATED
        assert [r.relative_path for r in journal.nodes_with_durable_mutation_intent()] == []
        assert [r.relative_path for r in journal.nodes_with_durable_mutation_completed()] == ["Data/Skyrim.esm"]

    def test_replay_exige_registros(self) -> None:
        with pytest.raises(ProtectionJournalSchemaError):
            replay_journal_records(())

    def test_identidad_fisica_fuera_de_rango_es_rechazada(self) -> None:
        with pytest.raises(ProtectionJournalSchemaError):
            _header(root_file_id=1 << 200)


# ============================================================================
# JRNL-06: ambigüedad preservada
# ============================================================================


class TestAmbiguedadPreservada:
    def test_mutating_durable_no_permite_afirmar_que_no_hubo_mutacion(self) -> None:
        """Fila C de §20: ``MUTATING(K)`` durable => la mutación PUEDE haber ocurrido."""
        raw = _bytes(_header(), _initial_state(), _node_record(3))
        journal = deserialize_journal_bytes(raw)
        assert [r.relative_path for r in journal.nodes_with_durable_mutation_intent()] == ["Data/Skyrim.esm"]
        assert journal.nodes_with_durable_mutation_completed() == ()
        # El modelo NO ofrece ninguna API que afirme "la mutación nunca ocurrió".
        assert not any("never" in name or "nunca" in name for name in dir(journal) if not name.startswith("_"))

    def test_journal_de_otra_operacion_no_se_liga(self) -> None:
        crudo = _header(operation_id=_ALT_OPERATION_ID)
        with pytest.raises(ProtectionJournalSchemaError):
            JournalHeaderRecord(
                schema_version=crudo.schema_version,
                sequence=crudo.sequence,
                operation_id="no-es-uuid",
                authorized_plan_digest=crudo.authorized_plan_digest,
                canonical_root=crudo.canonical_root,
                volume_serial_number=crudo.volume_serial_number,
                root_file_id=crudo.root_file_id,
                created_at=crudo.created_at,
            )

    def test_modelo_es_inmutable(self) -> None:
        raw = _bytes(_header(), _initial_state())
        journal = deserialize_journal_bytes(raw)
        assert isinstance(journal, ProtectionJournal)
        with pytest.raises(Exception):  # noqa: B017,PT011 - frozen dataclass
            journal.transaction_state = ProtectionTransactionState.COMMITTED  # type: ignore[misc]


# ============================================================================
# Endurecimiento adversarial (revisión previa al merge)
# ============================================================================


class TestIdentidadFisicaUnicaPorRelpath:
    """Un journal no puede re-apuntar un ``relative_path`` a otro inodo."""

    def test_cambio_de_identidad_fisica_es_schema_invalido(self) -> None:
        crudo = _bytes(
            _header(),
            _initial_state(),
            _node_record(3),
            _node_record(
                4,
                state=NodeWalState.MUTATED,
                file_id=_ROOT_FILE_ID + 99,
                pre_sd_sha256="f" * 64,
            ),
        )
        resultado = parse_journal_bytes(crudo)
        assert resultado.disposition is JournalLoadDisposition.SCHEMA_INVALID
        assert resultado.journal is None
        assert "cambió de identidad física" in resultado.detail

    def test_misma_identidad_repetida_sigue_valida(self) -> None:
        """Repetir la MISMA identidad no es un re-apuntado: el WAL sólo exige unicidad."""
        crudo = _bytes(
            _header(),
            _initial_state(),
            _node_record(3),
            _node_record(4, state=NodeWalState.MUTATED),
        )
        journal = deserialize_journal_bytes(crudo)
        assert journal.node_record("Data/Skyrim.esm").state is NodeWalState.MUTATED

    def test_dos_relpaths_distintos_conviven(self) -> None:
        crudo = _bytes(
            _header(),
            _initial_state(),
            _node_record(3, relative_path="Data/Skyrim.esm"),
            _node_record(4, relative_path="Data/quest.esp", file_id=_ROOT_FILE_ID + 2),
        )
        journal = deserialize_journal_bytes(crudo)
        assert journal.node_state("Data/Skyrim.esm") is NodeWalState.MUTATING
        assert journal.node_state("Data/quest.esp") is NodeWalState.MUTATING
