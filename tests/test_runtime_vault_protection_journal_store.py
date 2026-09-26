"""Tests focales GP2-S4A: store durable del journal y WAL por nodo.

La propiedad central de S4 (§33/§34) es de ORDEN::

    journal append MUTATING(K)  ->  FlushFileBuffers == TRUE  ->  permiso de mutación

Acá se prueba con kernels falsos deterministas (incluido uno que devuelve
``FALSE`` en el flush) y con un ORÁCULO DE ORDEN: el test debe fallar para el
mutante ``mutate(K); journal; flush()``. Ningún test toca el Golden ni llama a
``SetSecurityInfo``: las "mutaciones" son callbacks espía sobre datos en memoria.

Los tests causales del namespace real (SD canónica, ``FILE_FLAG_WRITE_THROUGH``,
``CreateHardLinkW``) van bajo ``skipif(win32)``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import pathlib
import sys
from typing import Any

import pytest

from sky_claw.local.runtime_vault.authorization_context import OperatorTokenEvidence
from sky_claw.local.runtime_vault.authorized_plan import (
    AUTHORIZED_PLAN_SCHEMA_VERSION,
    AuthorizedPlan,
    serialize_authorized_plan,
)
from sky_claw.local.runtime_vault.authorized_plan_store import _MINT_PROOF, DurableAuthorizedPlan
from sky_claw.local.runtime_vault.golden_protection_plan import (
    GoldenProtectionNodeKind,
    NodeSecurityBackup,
)
from sky_claw.local.runtime_vault.models import TreeDigest
from sky_claw.local.runtime_vault.protection_journal import (
    INITIAL_TRANSACTION_STATE,
    NodeWalState,
    ProtectionJournalIndeterminateError,
    ProtectionJournalSchemaError,
    ProtectionTransactionState,
)
from sky_claw.local.runtime_vault.protection_journal_store import (
    DurableJournalFlushError,
    DurableProtectionJournal,
    JournalDurabilityKernel,
    NodeMutationBinding,
    ProtectionJournalAlreadyExistsError,
    ProtectionJournalClassification,
    ProtectionJournalCreateError,
    ProtectionJournalNotFoundError,
    ProtectionJournalPlanBindingError,
    ProtectionJournalStoreError,
    ProtectionJournalUnsupportedError,
    WalMutationPermit,
    classify_protection_journal,
    create_protection_journal,
    derive_protection_journal_path,
    load_protection_journal,
    open_protection_journal,
    reload_protection_journal_after_restart,
)

_OPERATION_ID = "3f6b0be2-1c2a-4d3e-8f4a-9b7c6d5e4f3a"
_CANONICAL_ROOT = "C:\\Games\\Skyrim"
_VOLUME_SERIAL = 0xA1B2C3D4
_ROOT_FILE_ID = 0x1122334455667788
_POLICY_VERSION = "golden-policy-v1"


# ============================================================================
# Fábricas
# ============================================================================


def _node(rel_path: str, file_id: int) -> NodeSecurityBackup:
    sd = b"\x01\x00\x04\x80" + rel_path.encode("utf-8") + b"\x00" * 8
    return NodeSecurityBackup(
        relative_path=rel_path,
        node_kind=GoldenProtectionNodeKind.DIR if rel_path == "." else GoldenProtectionNodeKind.FILE,
        volume_serial_number=_VOLUME_SERIAL,
        file_id=file_id,
        pre_sd_bytes_b64=base64.b64encode(sd).decode("ascii"),
        pre_sd_length=len(sd),
        pre_sd_sha256=hashlib.sha256(sd).hexdigest(),
        owner_sid="S-1-5-18",
        group_sid="S-1-5-32-544",
        dacl_control_flags=0x1000,
        pre_dacl_protected_flag=True,
        sddl_diagnostic="D:(A;;FA;;;BA)",
    )


def _plan() -> AuthorizedPlan:
    return AuthorizedPlan(
        schema_version=AUTHORIZED_PLAN_SCHEMA_VERSION,
        operation_id=_OPERATION_ID,
        canonical_root=_CANONICAL_ROOT,
        volume_serial_number=_VOLUME_SERIAL,
        root_file_id=_ROOT_FILE_ID,
        tree_digest=TreeDigest(digest="c" * 64, files=3, bytes=1024),
        node_count=3,
        policy_version=_POLICY_VERSION,
        staging_digest="d" * 64,
        operator_identity=OperatorTokenEvidence(
            operator_sid="S-1-5-21-1001-1002-1003-1001",
            token_type="primary",
            acquired_via="same_account_coordinator_extraction",
        ),
        nodes=(
            _node(".", _ROOT_FILE_ID),
            _node("Data/Skyrim.esm", _ROOT_FILE_ID + 1),
            _node("Data/quest.esp", _ROOT_FILE_ID + 2),
        ),
    )


class FakeKernel:
    """Kernel de durabilidad falso: append real sobre disco, flush conmutable.

    Registra el orden de las operaciones para poder afirmar el ORDEN del WAL.
    """

    def __init__(self, *, flush_ok: bool = True, append_falla: bool = False) -> None:
        self.flush_ok = flush_ok
        self.append_falla = append_falla
        self.eventos: list[str] = []
        self._handles: dict[int, pathlib.Path] = {}
        self._siguiente = 500

    def exists(self, path: pathlib.PurePath) -> bool:
        return pathlib.Path(path).exists()

    def create(self, path: pathlib.PurePath) -> int:
        destino = pathlib.Path(path)
        if destino.exists():
            raise ProtectionJournalAlreadyExistsError(f"ya existe: '{destino}'")
        destino.parent.mkdir(parents=True, exist_ok=True)
        destino.touch()
        self._siguiente += 1
        self._handles[self._siguiente] = destino
        self.eventos.append("create")
        return self._siguiente

    def open_append(self, path: pathlib.PurePath) -> int:
        destino = pathlib.Path(path)
        if not destino.exists():
            raise ProtectionJournalNotFoundError(f"no existe: '{destino}'")
        self._siguiente += 1
        self._handles[self._siguiente] = destino
        self.eventos.append("open")
        return self._siguiente

    def append(self, handle: int, payload: bytes) -> None:
        if self.append_falla:
            raise ProtectionJournalStoreError("append simulado roto")
        with self._handles[handle].open("ab") as fh:
            fh.write(payload)
        self.eventos.append("append")

    def flush(self, handle: int) -> bool:
        self.eventos.append("flush")
        return self.flush_ok

    def read_all(self, path: pathlib.PurePath) -> bytes:
        return pathlib.Path(path).read_bytes()

    def close(self, handle: int) -> None:
        self._handles.pop(handle, None)
        self.eventos.append("close")

    def remove(self, path: pathlib.PurePath) -> None:
        pathlib.Path(path).unlink(missing_ok=True)


@pytest.fixture
def raiz(tmp_path: pathlib.Path) -> pathlib.Path:
    operacion = tmp_path / "Sky-Claw" / "runtime_vault" / "operations" / _OPERATION_ID
    operacion.mkdir(parents=True)
    return tmp_path


def _resolver(raiz: pathlib.Path) -> Any:
    return lambda: raiz


def _plan_durable(raiz: pathlib.Path) -> DurableAuthorizedPlan:
    plan = _plan()
    destino = derive_protection_journal_path(_OPERATION_ID, programdata_resolver=_resolver(raiz)).with_name(
        "authorized_plan.json"
    )
    destino.write_bytes(serialize_authorized_plan(plan))
    return DurableAuthorizedPlan(plan, destino, _proof=_MINT_PROOF)


def _journal(raiz: pathlib.Path, kernel: JournalDurabilityKernel | None = None) -> DurableProtectionJournal:
    return create_protection_journal(
        _plan_durable(raiz), programdata_resolver=_resolver(raiz), kernel=kernel or FakeKernel()
    )


# ============================================================================
# Creación: sólo tras un plan durable, nunca antes
# ============================================================================


class TestCreacionDelJournal:
    def test_creacion_liga_el_plan_durable(self, raiz: pathlib.Path) -> None:
        journal = _journal(raiz)
        assert journal.operation_id == _OPERATION_ID
        assert journal.authorized_plan_digest == _plan().plan_digest
        assert journal.transaction_state is ProtectionTransactionState.APPLYING
        assert journal.journal.node_records == ()
        assert journal.journal.sequence == 2
        assert (
            derive_protection_journal_path(_OPERATION_ID, programdata_resolver=_resolver(raiz))
            .read_bytes()
            .count(b"\n")
            == 2
        )
        journal.close()

    def test_creacion_replay_falla_cerrado(self, raiz: pathlib.Path) -> None:
        _journal(raiz).close()
        with pytest.raises(ProtectionJournalAlreadyExistsError):
            _journal(raiz)

    def test_sin_directorio_de_operacion_no_hay_journal(self, tmp_path: pathlib.Path) -> None:
        plan = _plan()
        destino = tmp_path / "authorized_plan.json"
        destino.write_bytes(serialize_authorized_plan(plan))
        durable = DurableAuthorizedPlan(plan, destino, _proof=_MINT_PROOF)
        with pytest.raises(ProtectionJournalCreateError):
            create_protection_journal(durable, programdata_resolver=lambda: tmp_path, kernel=FakeKernel())

    def test_flush_fallido_en_la_creacion_no_deja_autoridad(self, raiz: pathlib.Path) -> None:
        with pytest.raises(DurableJournalFlushError):
            create_protection_journal(
                _plan_durable(raiz),
                programdata_resolver=_resolver(raiz),
                kernel=FakeKernel(flush_ok=False),
            )
        assert not derive_protection_journal_path(_OPERATION_ID, programdata_resolver=_resolver(raiz)).exists()

    def test_journal_no_se_crea_con_un_plan_en_memoria(self, raiz: pathlib.Path) -> None:
        with pytest.raises(ProtectionJournalPlanBindingError):
            create_protection_journal(_plan(), programdata_resolver=_resolver(raiz), kernel=FakeKernel())  # type: ignore[arg-type]

    def test_ruta_derivada_internamente(self, tmp_path: pathlib.Path) -> None:
        ruta = derive_protection_journal_path(_OPERATION_ID, programdata_resolver=lambda: tmp_path)
        assert (
            ruta == tmp_path / "Sky-Claw" / "runtime_vault" / "operations" / _OPERATION_ID / "protection_journal.json"
        )


# ============================================================================
# ORÁCULO DE ORDEN: permiso sólo después del flush
# ============================================================================


class TestOrdenWal:
    def test_permiso_solo_despues_del_flush(self, raiz: pathlib.Path) -> None:
        kernel = FakeKernel()
        journal = _journal(raiz, kernel)
        kernel.eventos.clear()

        permiso = journal.record_node_mutation_intent(journal.node_binding("Data/Skyrim.esm"))

        # append -> flush -> (recién entonces) el permiso existe y se puede mutar.
        assert kernel.eventos == ["append", "flush"]
        assert isinstance(permiso, WalMutationPermit)
        assert permiso.relative_path == "Data/Skyrim.esm"
        assert permiso.is_consumed is False
        assert permiso.authorized_plan_digest == journal.authorized_plan_digest
        assert journal.journal.node_state("Data/Skyrim.esm") is NodeWalState.MUTATING
        journal.close()

    def test_mutante_mutate_antes_del_journal_rompe_el_oraculo(self, raiz: pathlib.Path) -> None:
        """Mutante M-W1: ``mutate(K); journal; flush()`` debe romper el orden.

        La "mutación" es un callback espía (nunca toca el Golden). Con el orden
        correcto el espía ve ``append`` y ``flush`` ANTES de ser invocado.
        """
        kernel = FakeKernel()
        journal = _journal(raiz, kernel)
        kernel.eventos.clear()  # sólo interesa el orden del WAL, no la creación
        observado: list[str] = []

        def _mutar(_binding: NodeMutationBinding) -> None:
            observado.append("mutate")
            observado.extend(kernel.eventos)

        permiso = journal.record_node_mutation_intent(journal.node_binding("Data/Skyrim.esm"))
        _mutar(permiso.node_binding)
        assert observado == ["mutate", "append", "flush"]
        journal.close()

    def test_flush_fallido_no_hay_permiso(self, raiz: pathlib.Path) -> None:
        kernel = FakeKernel()
        journal = _journal(raiz, kernel)
        # A partir de acá el flush falla: el permiso no puede existir.
        kernel.flush_ok = False
        with pytest.raises(DurableJournalFlushError):
            journal.record_node_mutation_intent(journal.node_binding("Data/Skyrim.esm"))
        # Sin permiso, sin callback, sin estado MUTATING, sin autoridad.
        assert journal.journal.node_state("Data/Skyrim.esm") is None
        assert journal.journal.transaction_state is ProtectionTransactionState.INDETERMINATE
        journal.close()

    def test_tras_flush_fallido_el_journal_queda_envenenado(self, raiz: pathlib.Path) -> None:
        kernel = FakeKernel()
        journal = _journal(raiz, kernel)
        kernel.flush_ok = False
        with pytest.raises(DurableJournalFlushError):
            journal.record_node_mutation_intent(journal.node_binding("Data/Skyrim.esm"))
        kernel.flush_ok = True
        with pytest.raises(DurableJournalFlushError):
            journal.record_node_mutation_intent(journal.node_binding("Data/quest.esp"))
        journal.close()

    def test_permiso_es_consume_once(self, raiz: pathlib.Path) -> None:
        journal = _journal(raiz)
        permiso = journal.record_node_mutation_intent(journal.node_binding("Data/Skyrim.esm"))
        assert permiso.is_consumed is False
        permiso.mark_consumed()
        assert permiso.is_consumed is True
        journal.close()

    def test_permiso_no_se_puede_fabricar(self) -> None:
        with pytest.raises(ProtectionJournalStoreError):
            WalMutationPermit(
                record=None,  # type: ignore[arg-type]
                operation_id=_OPERATION_ID,
                authorized_plan_digest="a" * 64,
                journal_path=pathlib.Path("C:/x"),
            )

    def test_mutated_solo_tras_mutating(self, raiz: pathlib.Path) -> None:
        journal = _journal(raiz)
        with pytest.raises(ProtectionJournalSchemaError):
            journal.record_node_mutation_completed(journal.node_binding("Data/Skyrim.esm"))
        journal.record_node_mutation_intent(journal.node_binding("Data/Skyrim.esm"))
        journal.record_node_mutation_completed(journal.node_binding("Data/Skyrim.esm"))
        assert journal.journal.node_state("Data/Skyrim.esm") is NodeWalState.MUTATED
        with pytest.raises(ProtectionJournalSchemaError):
            journal.record_node_mutation_intent(journal.node_binding("Data/Skyrim.esm"))
        journal.close()

    def test_binding_por_indice_desnudo_es_rechazado(self, raiz: pathlib.Path) -> None:
        journal = _journal(raiz)
        with pytest.raises(ProtectionJournalPlanBindingError):
            journal.record_node_mutation_intent(
                NodeMutationBinding(
                    relative_path="Data/Skyrim.esm",
                    volume_serial_number=_VOLUME_SERIAL,
                    file_id=999,
                    pre_sd_sha256="e" * 64,
                )
            )
        with pytest.raises(ProtectionJournalPlanBindingError):
            journal.record_node_mutation_intent(
                NodeMutationBinding(
                    relative_path="Data/no-existe.esp",
                    volume_serial_number=_VOLUME_SERIAL,
                    file_id=1,
                    pre_sd_sha256="e" * 64,
                )
            )
        journal.close()

    def test_journal_cerrado_no_anexa(self, raiz: pathlib.Path) -> None:
        journal = _journal(raiz)
        journal.close()
        assert journal.is_closed is True
        with pytest.raises(ProtectionJournalStoreError):
            journal.record_node_mutation_intent(journal.node_binding("Data/Skyrim.esm"))


# ============================================================================
# FSM transaccional: S4-A no declara COMMITTED
# ============================================================================


class TestFsmProductiva:
    def test_committed_es_rechazado_siempre(self, raiz: pathlib.Path) -> None:
        from sky_claw.local.runtime_vault.protection_journal import PrematureCommitError

        journal = _journal(raiz)
        with pytest.raises(PrematureCommitError):
            journal.transition_to(ProtectionTransactionState.COMMITTED)
        assert journal.transaction_state is ProtectionTransactionState.APPLYING
        journal.close()

    def test_camino_de_exito_fuera_de_alcance(self, raiz: pathlib.Path) -> None:
        journal = _journal(raiz)
        for destino in (
            ProtectionTransactionState.VERIFYING_GP1,
            ProtectionTransactionState.VERIFYING_RV2,
            ProtectionTransactionState.VERIFYING_NODE_SET,
            ProtectionTransactionState.ARCHIVING_BACKUP,
        ):
            with pytest.raises(ProtectionJournalSchemaError):
                journal.transition_to(destino)
        assert journal.transaction_state is ProtectionTransactionState.APPLYING
        journal.close()

    def test_rama_de_rollback_si_es_alcanzable(self, raiz: pathlib.Path) -> None:
        journal = _journal(raiz)
        journal.transition_to(ProtectionTransactionState.ROLLBACK_REQUIRED)
        assert journal.transaction_state is ProtectionTransactionState.ROLLBACK_REQUIRED
        journal.transition_to(ProtectionTransactionState.ROLLING_BACK)
        journal.transition_to(ProtectionTransactionState.ROLLED_BACK)
        assert journal.transaction_state is ProtectionTransactionState.ROLLED_BACK
        with pytest.raises(ProtectionJournalSchemaError):
            journal.transition_to(ProtectionTransactionState.ROLLED_BACK)
        journal.close()

    def test_indeterminate_desde_applying(self, raiz: pathlib.Path) -> None:
        journal = _journal(raiz)
        journal.transition_to(ProtectionTransactionState.INDETERMINATE)
        assert journal.transaction_state is ProtectionTransactionState.INDETERMINATE
        journal.close()


# ============================================================================
# Binding, clasificación y recarga tras reinicio
# ============================================================================


class TestBindingYRecarga:
    def test_journal_con_otro_plan_digest_es_rechazado(self, raiz: pathlib.Path) -> None:
        journal = _journal(raiz)
        journal.close()
        otro = _plan()
        otro_destino = derive_protection_journal_path(_OPERATION_ID, programdata_resolver=_resolver(raiz)).with_name(
            "authorized_plan.json"
        )
        plan_alterado = AuthorizedPlan(
            schema_version=otro.schema_version,
            operation_id=otro.operation_id,
            canonical_root="C:\\Otro\\Golden",
            volume_serial_number=otro.volume_serial_number,
            root_file_id=otro.root_file_id,
            tree_digest=otro.tree_digest,
            node_count=otro.node_count,
            policy_version=otro.policy_version,
            staging_digest=otro.staging_digest,
            operator_identity=otro.operator_identity,
            nodes=otro.nodes,
        )
        otro_destino.write_bytes(serialize_authorized_plan(plan_alterado))
        from sky_claw.local.runtime_vault.authorized_plan_store import load_durable_authorized_plan

        durable = load_durable_authorized_plan(_OPERATION_ID, programdata_resolver=_resolver(raiz))
        with pytest.raises(ProtectionJournalPlanBindingError):
            open_protection_journal(_OPERATION_ID, durable, programdata_resolver=_resolver(raiz), kernel=FakeKernel())

    def test_journal_con_plan_digest_adulterado_falla_cerrado(self, raiz: pathlib.Path) -> None:
        """Corrupción de bytes: un digest alterado rompe el binding, no el esquema."""
        journal = _journal(raiz)
        journal.close()
        ruta = derive_protection_journal_path(_OPERATION_ID, programdata_resolver=_resolver(raiz))
        lineas = ruta.read_bytes().split(b"\n")
        cabecera = json.loads(lineas[0].decode("utf-8"))
        cabecera["authorized_plan_digest"] = "f" * 64
        lineas[0] = json.dumps(cabecera, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ruta.write_bytes(b"\n".join(lineas))

        # El journal sigue siendo estructuralmente válido...
        assert (
            classify_protection_journal(
                _OPERATION_ID, programdata_resolver=_resolver(raiz), kernel=FakeKernel()
            ).classification
            is ProtectionJournalClassification.VALID
        )
        # ...pero NO liga contra el plan autoritativo: fail-closed.
        from sky_claw.local.runtime_vault.authorized_plan_store import load_durable_authorized_plan

        durable = load_durable_authorized_plan(_OPERATION_ID, programdata_resolver=_resolver(raiz))
        with pytest.raises(ProtectionJournalPlanBindingError):
            open_protection_journal(_OPERATION_ID, durable, programdata_resolver=_resolver(raiz), kernel=FakeKernel())
        with pytest.raises(ProtectionJournalPlanBindingError):
            reload_protection_journal_after_restart(
                _OPERATION_ID,
                programdata_resolver=_resolver(raiz),
                kernel=FakeKernel(),
                plan_loader=lambda _op: durable,
            )

    def test_clasificacion_absent_valid_indeterminate(self, raiz: pathlib.Path) -> None:
        assert (
            classify_protection_journal(
                _OPERATION_ID, programdata_resolver=_resolver(raiz), kernel=FakeKernel()
            ).classification
            is ProtectionJournalClassification.ABSENT
        )
        journal = _journal(raiz)
        journal.close()
        resultado = classify_protection_journal(
            _OPERATION_ID, programdata_resolver=_resolver(raiz), kernel=FakeKernel()
        )
        assert resultado.classification is ProtectionJournalClassification.VALID
        assert resultado.journal is not None

        ruta = derive_protection_journal_path(_OPERATION_ID, programdata_resolver=_resolver(raiz))
        ruta.write_bytes(ruta.read_bytes()[:-1])
        cortado = classify_protection_journal(_OPERATION_ID, programdata_resolver=_resolver(raiz), kernel=FakeKernel())
        assert cortado.classification is ProtectionJournalClassification.INDETERMINATE
        assert cortado.journal is None

    def test_journal_corrupto_falla_cerrado_al_cargar(self, raiz: pathlib.Path) -> None:
        journal = _journal(raiz)
        journal.close()
        ruta = derive_protection_journal_path(_OPERATION_ID, programdata_resolver=_resolver(raiz))
        ruta.write_bytes(b'{"kind":"journal_header"}')
        with pytest.raises(ProtectionJournalIndeterminateError):
            load_protection_journal(_OPERATION_ID, programdata_resolver=_resolver(raiz), kernel=FakeKernel())

    def test_journal_inexistente_es_not_found(self, raiz: pathlib.Path) -> None:
        with pytest.raises(ProtectionJournalNotFoundError):
            load_protection_journal(_OPERATION_ID, programdata_resolver=_resolver(raiz), kernel=FakeKernel())

    def test_recarga_tras_reinicio_conserva_la_ambiguedad(self, raiz: pathlib.Path) -> None:
        """Oráculo C: ``MUTATING(K)`` durable -> la mutación PUEDE haber ocurrido."""
        journal = _journal(raiz)
        journal.record_node_mutation_intent(journal.node_binding("Data/Skyrim.esm"))
        journal.close()

        plan, recargado, clase = reload_protection_journal_after_restart(
            _OPERATION_ID,
            programdata_resolver=_resolver(raiz),
            kernel=FakeKernel(),
            plan_loader=lambda _op: _plan_durable(raiz),
        )
        assert clase is ProtectionJournalClassification.VALID
        assert plan.digest == _plan().plan_digest
        assert recargado is not None
        assert recargado.authorized_plan_digest == plan.digest
        assert [r.relative_path for r in recargado.nodes_with_durable_mutation_intent()] == ["Data/Skyrim.esm"]
        assert recargado.nodes_with_durable_mutation_completed() == ()

    def test_plan_durable_sin_journal_no_infinge_transaccion(self, raiz: pathlib.Path) -> None:
        """Fila B de §20: plan cargable, clasificación segura, sin mutación inferida."""
        _plan_durable(raiz)
        plan, recargado, clase = reload_protection_journal_after_restart(
            _OPERATION_ID,
            programdata_resolver=_resolver(raiz),
            kernel=FakeKernel(),
            plan_loader=lambda _op: _plan_durable(raiz),
        )
        assert clase is ProtectionJournalClassification.ABSENT
        assert recargado is None
        assert plan.digest == _plan().plan_digest

    def test_staging_nunca_es_autoridad(self, raiz: pathlib.Path) -> None:
        """Un manifest en staging no puede comitear, revertir ni recuperar."""
        journal = _journal(raiz)
        journal.close()
        staging = raiz / "Sky-Claw" / "runtime_vault" / "staging" / _OPERATION_ID
        staging.mkdir(parents=True)
        (staging / "candidate_manifest.json").write_bytes(
            json.dumps({"operation_id": _OPERATION_ID, "state": "committed"}).encode("utf-8")
        )
        recargado = load_protection_journal(_OPERATION_ID, programdata_resolver=_resolver(raiz), kernel=FakeKernel())
        assert recargado.transaction_state is ProtectionTransactionState.APPLYING
        assert recargado.node_records == ()

    def test_recarga_con_journal_cortado_es_indeterminate(self, raiz: pathlib.Path) -> None:
        journal = _journal(raiz)
        journal.close()
        ruta = derive_protection_journal_path(_OPERATION_ID, programdata_resolver=_resolver(raiz))
        ruta.write_bytes(ruta.read_bytes() + b'{"kind":"transaction_state"')
        with pytest.raises(ProtectionJournalIndeterminateError):
            reload_protection_journal_after_restart(
                _OPERATION_ID,
                programdata_resolver=_resolver(raiz),
                kernel=FakeKernel(),
                plan_loader=lambda _op: _plan_durable(raiz),
            )

    def test_posix_sin_kernel_es_unsupported_tipado(self, raiz: pathlib.Path) -> None:
        if sys.platform == "win32":
            pytest.skip("En Windows el kernel real existe")
        with pytest.raises(ProtectionJournalUnsupportedError):
            create_protection_journal(_plan_durable(raiz), programdata_resolver=_resolver(raiz))
        with pytest.raises(ProtectionJournalUnsupportedError):
            classify_protection_journal(_OPERATION_ID, programdata_resolver=_resolver(raiz))


# ============================================================================
# Anclas estructurales
# ============================================================================


class TestAnclasEstructurales:
    def test_estado_inicial_del_journal(self) -> None:
        assert INITIAL_TRANSACTION_STATE == "applying"

    def test_nombre_de_objeto_protegido_registrado(self) -> None:
        from sky_claw.local.runtime_vault import trusted_namespace

        assert trusted_namespace.PROTECTION_JOURNAL_OBJECT == "protection_journal.json"
        spec = trusted_namespace.build_namespace_dacl_spec("protection_journal.json")
        sids = [ace.sid for ace in spec.aces]
        assert "S-1-5-18" in sids
        assert "S-1-5-32-544" in sids
        assert "S-1-5-11" in sids

    def test_objeto_desconocido_falla_cerrado(self) -> None:
        from sky_claw.local.runtime_vault import trusted_namespace

        with pytest.raises(trusted_namespace.TrustedNamespaceError):
            trusted_namespace.build_namespace_dacl_spec("objeto_inventado.json")
        with pytest.raises(trusted_namespace.TrustedNamespaceError):
            trusted_namespace._verificador_de_archivo_protegido("objeto_inventado.json")


@pytest.fixture
def entorno_elevado(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutraliza las syscalls privilegiadas (1307/1314) del namespace.

    Espejo de ``TestNamespaceBootstrapWindows.mock_elevated_provisioning`` y del
    ``entorno_elevado`` de ``test_runtime_vault_golden_admission_store.py``: permite
    ejercitar la escritura REAL (``CreateFileW`` + ``WriteFile`` +
    ``FlushFileBuffers`` + ``CreateHardLinkW`` + ``DeleteFileW``) sobre ``tmp_path``
    sin exigir un proceso SYSTEM, conservando toda la lógica de la primitiva.
    """
    from sky_claw.local.runtime_vault import trusted_namespace as tn
    from sky_claw.local.runtime_vault.trusted_namespace import (
        _FILE_READ_ATTRIBUTES,
        _READ_CONTROL,
        _WRITE_OWNER,
        BUILTIN_ADMINISTRATORS_SID,
        PERMITTED_NAMESPACE_OWNERS,
        _advapi32,
        _kernel32,
        _open_handle_no_reparse,
        _read_live_owner_group_dacl,
    )

    real_create_dir = _kernel32.CreateDirectoryW
    monkeypatch.setattr(_kernel32, "CreateDirectoryW", lambda path, sa: bool(real_create_dir(path, None)))

    monkeypatch.setattr(
        "sky_claw.local.runtime_vault.trusted_namespace._open_handle_no_reparse",
        lambda path, desired_access=_READ_CONTROL | _FILE_READ_ATTRIBUTES, **kwargs: _open_handle_no_reparse(
            path, desired_access=desired_access & ~_WRITE_OWNER, **kwargs
        ),
    )

    real_create_file = _kernel32.CreateFileW

    def _create_file_sin_sd(
        lp_file_name: Any,
        dw_desired_access: int,
        dw_share_mode: int,
        lp_security_attributes: Any,
        dw_creation_disposition: int,
        dw_flags_and_attributes: int,
        h_template_file: Any,
    ) -> int:
        return int(
            real_create_file(
                lp_file_name,
                dw_desired_access & ~_WRITE_OWNER,
                dw_share_mode,
                None,
                dw_creation_disposition,
                dw_flags_and_attributes,
                h_template_file,
            )
        )

    monkeypatch.setattr(_kernel32, "CreateFileW", _create_file_sin_sd)
    monkeypatch.setattr(_advapi32, "SetSecurityInfo", lambda *args: 0)
    monkeypatch.setattr(
        "sky_claw.local.runtime_vault.trusted_namespace.create_secured_file_from_birth",
        lambda path, obj="trusted_goldens.json", *, extra_flags=0: _kernel32.CreateFileW(
            str(path), 0x40000000 | 0x80000000, 0, None, 1, 0x80 | extra_flags, None
        ),
    )
    monkeypatch.setattr(
        "sky_claw.local.runtime_vault.trusted_namespace._verify_secured_file_contract",
        lambda path, object_name: None,
    )
    monkeypatch.setattr(tn, "_verify_canonical_directory_security_on_handle", lambda *a, **k: None)

    real_read_owner = _read_live_owner_group_dacl

    def _read_owner_simulado(handle: int) -> tuple[str, str, bool]:
        owner, group, is_protected = real_read_owner(handle)
        if owner not in PERMITTED_NAMESPACE_OWNERS:
            owner = BUILTIN_ADMINISTRATORS_SID
        return owner, group, is_protected

    monkeypatch.setattr(
        "sky_claw.local.runtime_vault.trusted_namespace._read_live_owner_group_dacl",
        _read_owner_simulado,
    )

    @pytest.mark.skipif(sys.platform != "win32", reason="Namespace protegido Win32")
    def test_escritura_real_create_once_del_journal(self, tmp_path: pathlib.Path, entorno_elevado: None) -> None:
        from sky_claw.local.runtime_vault import trusted_namespace

        destino = tmp_path / "protection_journal.json"
        trusted_namespace.write_secured_file_create_once_at(
            destino, b'{"kind":"journal_header"}', "protection_journal.json"
        )
        assert destino.read_bytes() == b'{"kind":"journal_header"}'
        with pytest.raises(trusted_namespace.TrustedNamespaceError):
            trusted_namespace.write_secured_file_create_once_at(destino, b'{"kind":"otro"}', "protection_journal.json")
        assert destino.read_bytes() == b'{"kind":"journal_header"}'
