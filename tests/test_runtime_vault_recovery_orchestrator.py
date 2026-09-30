"""Tests causales GP2-S4C: recovery tras crash con evidencia durable.

La autoridad de recuperación es EXCLUSIVAMENTE durable y observable:

    authorized_plan.json (protegido) + protection_journal.json (protegido)
    + metadata del GoldenMutationLock + estado físico fresco del Golden

Prohibido como fuente: staging, objetos Python previos, reportes en memoria,
assertions del caller. Prohibido como identidad: path desnudo o PID solo.

La matriz R01..R20 (mapeada a ADR 0010 §20 y al encargo S4-C) se congela con
kernels y puertos falsos deterministas: los escenarios reales de proceso muerto
viven en ``test_runtime_vault_s4c_windows_rig.py`` (procesos REALES, sólo
Windows, árbol descartable en %TEMP%).

Contrato central de idempotencia (C8): el recovery NO conoce qué nodos restauró
un proceso anterior; por cada nodo candidato revalida identidad física y hace
una sonda SEMÁNTICA contra el PRE autorizado — si ya está en PRE, salta sin
escribir; si no, restaura y verifica. Re-ejecutable desde cero tras otro crash.
"""

from __future__ import annotations

import base64
import hashlib
import json
import pathlib
import uuid
from typing import Any

import pytest

from sky_claw.local.runtime_vault.authorized_plan import (
    AUTHORIZED_PLAN_SCHEMA_VERSION,
    AuthorizedPlan,
    AuthorizedPlanError,
    serialize_authorized_plan,
)
from sky_claw.local.runtime_vault.authorized_plan_store import (
    _MINT_PROOF,
    DurableAuthorizedPlan,
    DurableWriteOutcome,
)
from sky_claw.local.runtime_vault.golden_mutation_lock import (
    GoldenLockMetadata,
    GoldenLockOrphanedOperationMismatchError,
    GoldenLockPhase,
    derive_golden_lock_key,
    derive_golden_lock_path,
    serialize_golden_lock_metadata,
)
from sky_claw.local.runtime_vault.golden_protection_plan import (
    GoldenProtectionNodeKind,
    NodeSecurityBackup,
)
from sky_claw.local.runtime_vault.models import TreeDigest
from sky_claw.local.runtime_vault.mutation_executor import NodeIdentity, rollback_order
from sky_claw.local.runtime_vault.operation_lock_binding import (
    build_operation_lock_binding,
    derive_operation_lock_binding_path,
    serialize_operation_lock_binding,
)
from sky_claw.local.runtime_vault.protection_journal import (
    NodeWalState,
    ProtectionTransactionState,
    parse_journal_bytes,
)
from sky_claw.local.runtime_vault.protection_journal_store import (
    ProtectionJournalClassification,
    create_protection_journal,
    derive_protection_journal_path,
)
from sky_claw.local.runtime_vault.recovery_orchestrator import (
    NodeWalSummary,
    RecoveryDisposition,
    RecoveryForensicReport,
    RecoveryIdentitySource,
    RecoveryLockOutcome,
    RecoveryOrchestratorError,
    recover_interrupted_protection,
)

_OPERACION = "3f6b0be2-1c2a-4d3e-8f4a-9b7c6d5e4f3a"
_OTRA_OPERACION = "7a1c9e55-4b2d-4f60-9c31-2d8e5f6a7b90"
_RAIZ_CANONICA = "C:\\Games\\Skyrim"
_VOLUME_SERIAL = 0xA1B2C3D4
_ROOT_FILE_ID = 0x1122334455667788
_POLICY_VERSION = "golden-policy-v1"

_RUTAS = (
    ".",
    "Data",
    "Data/Scripts",
    "Data/Skyrim.esm",
)

#: PIDs/creation-time del kernel fake: el proceso "actual" que hace recovery.
_PID_ACTUAL = 4242
_CREATION_ACTUAL = 133_456_789_012_345_678
_PID_DUENO_MUERTO = 777
_PID_DUENO_VIVO = 9999


# ============================================================================
# Fábricas: plan durable + journal + lock + puerto
# ============================================================================


def _node(rel_path: str, file_id: int, *, sd_suffix: str = "") -> NodeSecurityBackup:
    sd = b"\x01\x00\x04\x80" + rel_path.encode("utf-8") + sd_suffix.encode("utf-8") + b"\x00" * 8
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


def _operator_evidence() -> Any:
    from sky_claw.local.runtime_vault.authorization_context import OperatorTokenEvidence

    return OperatorTokenEvidence(
        operator_sid="S-1-5-21-1001-1002-1003-1001",
        token_type="primary",
        acquired_via="same_account_coordinator_extraction",
    )


def _plan(
    *,
    operation_id: str = _OPERACION,
    root: str = _RAIZ_CANONICA,
    rutas: tuple[str, ...] = _RUTAS,
) -> AuthorizedPlan:
    nodos = tuple(_node(r, _ROOT_FILE_ID + i) for i, r in enumerate(rutas))
    return AuthorizedPlan(
        schema_version=AUTHORIZED_PLAN_SCHEMA_VERSION,
        operation_id=operation_id,
        canonical_root=root,
        volume_serial_number=_VOLUME_SERIAL,
        root_file_id=_ROOT_FILE_ID,
        tree_digest=TreeDigest(digest="c" * 64, files=len(rutas), bytes=1024),
        node_count=len(rutas),
        policy_version=_POLICY_VERSION,
        staging_digest="d" * 64,
        operator_identity=_operator_evidence(),
        nodes=nodos,
    )


def _resolver(raiz: pathlib.Path) -> Any:
    return lambda: raiz


def _raiz(tmp_path: pathlib.Path, operation_id: str = _OPERACION) -> pathlib.Path:
    operacion = tmp_path / "Sky-Claw" / "runtime_vault" / "operations" / operation_id
    operacion.mkdir(parents=True)
    return tmp_path


def _plan_path(raiz: pathlib.Path, operation_id: str = _OPERACION) -> pathlib.Path:
    return derive_protection_journal_path(operation_id, programdata_resolver=_resolver(raiz)).with_name(
        "authorized_plan.json"
    )


def _plan_durable(raiz: pathlib.Path, **kwargs: Any) -> DurableAuthorizedPlan:
    plan = _plan(**kwargs)
    destino = _plan_path(raiz, plan.operation_id)
    destino.write_bytes(serialize_authorized_plan(plan))
    return DurableAuthorizedPlan(plan, destino, _proof=_MINT_PROOF)


def _journal_path(raiz: pathlib.Path, operation_id: str = _OPERACION) -> pathlib.Path:
    return derive_protection_journal_path(operation_id, programdata_resolver=_resolver(raiz))


def _lock_path(raiz: pathlib.Path) -> pathlib.Path:
    return pathlib.Path(derive_golden_lock_path(_VOLUME_SERIAL, _ROOT_FILE_ID, programdata_resolver=_resolver(raiz)))


class _KernelDiario:
    """Kernel de durabilidad falso: append real sobre disco, flush conmutable.

    ``fallar_append_de``: permite que el append de UNA transición específica
    falle (crash durante la transición terminal, §49). El payload NO llega a
    disco: la evidencia durable queda en el estado anterior, que es exactamente
    lo que un tercer proceso debe poder releer.
    """

    def __init__(self, *, flush_ok: bool = True, fallar_append_de: frozenset[str] = frozenset()) -> None:
        self.flush_ok = flush_ok
        self.fallar_append_de = fallar_append_de
        self.eventos: list[str] = []
        self._handles: dict[int, pathlib.Path] = {}
        self._provisionales: dict[int, pathlib.Path] = {}
        self._siguiente = 500

    def exists(self, path: pathlib.PurePath) -> bool:
        return pathlib.Path(path).exists()

    def stage(self, dest: pathlib.PurePath) -> int:
        destino = pathlib.Path(dest)
        destino.parent.mkdir(parents=True, exist_ok=True)
        provisional = destino.parent / f".tmp_journal_{uuid.uuid4().hex}.{destino.name}"
        provisional.touch()
        self._siguiente += 1
        self._handles[self._siguiente] = provisional
        self._provisionales[self._siguiente] = provisional
        self.eventos.append("stage")
        return self._siguiente

    def publish(self, handle: int, dest: pathlib.PurePath) -> None:
        destino = pathlib.Path(dest)
        if destino.exists():
            from sky_claw.local.runtime_vault.protection_journal_store import (
                ProtectionJournalAlreadyExistsError,
            )

            raise ProtectionJournalAlreadyExistsError(f"ya existe: '{destino}'")
        self.eventos.append("publish-flush")
        if not self.flush_ok:
            from sky_claw.local.runtime_vault.protection_journal_store import (
                DurableJournalFlushError,
            )

            raise DurableJournalFlushError("flush no confirmado: no se publica")
        provisional = self._provisionales[handle]
        destino.write_bytes(pathlib.Path(provisional).read_bytes())
        self.eventos.append("publish")

    def discard(self, handle: int) -> None:
        provisional = self._provisionales.pop(handle, None)
        if provisional is not None:
            pathlib.Path(provisional).unlink(missing_ok=True)
        self.eventos.append("discard")

    def open_append(self, path: pathlib.PurePath) -> int:
        destino = pathlib.Path(path)
        if not destino.exists():
            from sky_claw.local.runtime_vault.protection_journal_store import (
                ProtectionJournalNotFoundError,
            )

            raise ProtectionJournalNotFoundError(f"no existe: '{destino}'")
        self._siguiente += 1
        self._handles[self._siguiente] = destino
        self.eventos.append("open")
        return self._siguiente

    def append(self, handle: int, payload: bytes) -> None:
        for estado in self.fallar_append_de:
            if f'"state":"{estado}"'.encode() in payload:
                self.eventos.append(f"append-fallido:{estado}")
                raise OSError(f"append simulado falló para '{estado}'")
        with self._handles[handle].open("ab") as fh:
            fh.write(payload)
        self.eventos.append("append")

    def flush(self, handle: int) -> bool:
        self.eventos.append("flush-wal")
        return self.flush_ok

    def read_all(self, path: pathlib.PurePath) -> bytes:
        return pathlib.Path(path).read_bytes()

    def close(self, handle: int) -> None:
        self._handles.pop(handle, None)
        self.eventos.append("close")

    def remove(self, path: pathlib.PurePath) -> None:
        pathlib.Path(path).unlink(missing_ok=True)


class _KernelLock:
    """Kernel fake del GoldenMutationLock con evidencia en memoria por path."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.busy_paths: set[str] = set()
        self.open_handles: dict[int, str] = {}
        self.owner_alive: bool | None = False
        self.close_calls: list[int] = []
        self.flush_calls: list[str] = []
        self.escrituras: list[tuple[str, bytes]] = []
        self.on_open: Any = None
        self._siguiente = 700

    def seed(self, path: pathlib.PurePath, metadata: GoldenLockMetadata) -> None:
        self.files[str(path)] = serialize_golden_lock_metadata(metadata)

    def open_lock_file(self, path: pathlib.PurePath) -> int:
        path_str = str(path)
        if path_str in self.busy_paths:
            from sky_claw.local.runtime_vault.golden_mutation_lock import GoldenLockBusyError

            raise GoldenLockBusyError("ocupado", win32_error=32)
        if self.on_open is not None:
            self.on_open()
        self._siguiente += 1
        self.open_handles[self._siguiente] = path_str
        self.busy_paths.add(path_str)
        self.files.setdefault(path_str, b"")
        return self._siguiente

    def get_reparse_tag(self, handle: int) -> int:
        return 0

    def read_lock_bytes(self, handle: int) -> bytes:
        return self.files[self.open_handles[handle]]

    def write_lock_bytes(self, handle: int, payload: bytes) -> None:
        path_str = self.open_handles[handle]
        self.files[path_str] = payload
        self.escrituras.append((path_str, payload))

    def flush_lock(self, handle: int) -> None:
        self.flush_calls.append(self.open_handles[handle])

    def is_owner_alive(self, owner_pid: int, owner_process_creation_time: int) -> bool | None:
        return self.owner_alive

    def current_process_identity(self) -> tuple[int, int, int]:
        return (_PID_ACTUAL, _CREATION_ACTUAL, 1)

    def current_epoch_seconds(self) -> int:
        return 1_758_499_200

    def close_handle(self, handle: int) -> None:
        self.close_calls.append(handle)
        self.busy_paths.discard(self.open_handles.pop(handle, ""))

    def metadata_de(self, path: pathlib.PurePath) -> GoldenLockMetadata | None:
        raw = self.files.get(str(path), b"")
        if not raw:
            return None
        from sky_claw.local.runtime_vault.golden_mutation_lock import deserialize_golden_lock_metadata

        return deserialize_golden_lock_metadata(raw)


class _PuertoRecuperacion:
    """Puerto de recuperación falso: estado semántico por nodo, sin Win32.

    Contrato causal:
    - ``estado_live[rel] == "pre"`` -> la sonda semántica PASA (nodo en PRE).
    - ``estado_live[rel] == "pre_reserializado"`` -> la sonda PASA pero los BYTES
      serían distintos (SetSecurityInfo reserializa): un recovery que exija
      igualdad raw de bytes restauraría de más y el test lo detecta.
    - cualquier otro valor -> la sonda FALLA (drift contra el PRE autorizado).

    El puerto NO implementa ``apply_target_dacl`` ni ``read_live_pre_sd_sha256``:
    si el engine intentara usarlos, el test falla con AttributeError.
    """

    def __init__(self, plan: AuthorizedPlan) -> None:
        self._nodos = {n.relative_path: n for n in plan.nodes}
        self._raiz = plan.canonical_root.replace("\\", "/").rstrip("/")
        self.estado_live: dict[str, str] = {n.relative_path: "pre" for n in plan.nodes}
        self.drift_identidad: dict[str, NodeIdentity] = {}
        self.open_falla: set[str] = set()
        self.restore_falla: set[str] = set()
        self.verificacion_falla: set[str] = set()
        self.abiertos: list[str] = []
        self.cerrados: dict[int, int] = {}
        self.restaurados: list[NodeSecurityBackup] = []
        self.orden_restauracion: list[str] = []
        self.orden_escaneo: list[str] = []
        self.sondas: list[str] = []
        self._siguiente = 9000
        self._handles: dict[int, str] = {}

    def _rel(self, path: pathlib.Path) -> str:
        texto = str(path).replace("\\", "/")
        if texto == self._raiz:
            return "."
        prefijo = f"{self._raiz}/"
        assert texto.startswith(prefijo), f"ruta fuera del canonical_root: {texto}"
        return texto[len(prefijo) :]

    def open(self, path: pathlib.Path, node_kind: GoldenProtectionNodeKind) -> int:
        rel = self._rel(path)
        if rel in self.open_falla:
            raise OSError(f"no se pudo abrir '{rel}'")
        self._siguiente += 1
        self._handles[self._siguiente] = rel
        self.cerrados.setdefault(self._siguiente, 0)
        self.abiertos.append(rel)
        return self._siguiente

    def close(self, handle: int) -> None:
        self.cerrados[handle] = self.cerrados.get(handle, 0) + 1

    def read_identity(self, handle: int) -> NodeIdentity:
        rel = self._handles[handle]
        self.orden_escaneo.append(rel)
        if rel in self.drift_identidad:
            return self.drift_identidad[rel]
        nodo = self._nodos[rel]
        return NodeIdentity(
            volume_serial_number=nodo.volume_serial_number,
            file_id=nodo.file_id,
            reparse_tag=0,
            number_of_links=1,
        )

    def verify_restored_pre_sd(self, handle: int, node: NodeSecurityBackup) -> None:
        rel = self._handles[handle]
        self.sondas.append(rel)
        if rel in self.verificacion_falla:
            raise RuntimeError(f"verificación semántica forzada a fallar en '{rel}'")
        if self.estado_live.get(rel, "pre") in {"pre", "pre_reserializado"}:
            return
        raise RuntimeError(f"drift semántico contra el PRE autorizado en '{rel}'")

    def restore_pre_sd(self, handle: int, node: NodeSecurityBackup) -> None:
        rel = self._handles[handle]
        if rel in self.restore_falla:
            raise OSError(f"SetSecurityInfo de restauración falló en '{rel}'")
        self.restaurados.append(node)
        self.orden_restauracion.append(rel)
        self.estado_live[rel] = "pre"

    @property
    def setsecurityinfo_calls(self) -> int:
        return len(self.restaurados)


def _recover(
    raiz: pathlib.Path,
    *,
    operation_id: str = _OPERACION,
    journal_kernel: _KernelDiario | None = None,
    lock_kernel: _KernelLock | None = None,
    port: _PuertoRecuperacion | None = None,
) -> RecoveryForensicReport:
    return recover_interrupted_protection(
        operation_id=operation_id,
        programdata_resolver=_resolver(raiz),
        journal_kernel=journal_kernel,
        lock_kernel=lock_kernel,
        port=port,
    )


def _lineas_validas(raw: bytes) -> list[dict[str, Any]]:
    """Sólo líneas JSON completas: una escritura cortada no es evidencia."""
    lineas: list[dict[str, Any]] = []
    for linea in raw.split(b"\n"):
        if not linea:
            continue
        try:
            registro = json.loads(linea)
        except json.JSONDecodeError:
            continue
        if isinstance(registro, dict):
            lineas.append(registro)
    return lineas


def _estados_transaccionales(raw: bytes) -> list[str]:
    return [registro["state"] for registro in _lineas_validas(raw) if registro.get("kind") == "transaction_state"]


def _registros_de_nodo(raw: bytes) -> list[tuple[str, str]]:
    return [
        (registro["relative_path"], registro["state"])
        for registro in _lineas_validas(raw)
        if registro.get("kind") == "node_state"
    ]


def _estado_durable(raiz: pathlib.Path, operation_id: str = _OPERACION) -> ProtectionTransactionState:
    raw = _journal_path(raiz, operation_id).read_bytes()
    resultado = parse_journal_bytes(raw)
    assert resultado.journal is not None, resultado.detail
    return resultado.journal.transaction_state


def _crear_journal(
    raiz: pathlib.Path,
    durable: DurableAuthorizedPlan,
    kernel: _KernelDiario,
) -> Any:
    return create_protection_journal(durable, programdata_resolver=_resolver(raiz), kernel=kernel)


def _mutar_todo(journal: Any, durable: DurableAuthorizedPlan) -> None:
    """Asienta MUTATING+MUTATED durables para todos los nodos (estado C5)."""
    from sky_claw.local.runtime_vault.mutation_executor import apply_order

    for nodo in apply_order(durable):
        journal.record_node_mutation_intent(journal.node_binding(nodo.relative_path))
        journal.record_node_mutation_completed(journal.node_binding(nodo.relative_path))


# ============================================================================
# R14/R15/R16 + takeover de lock (unidad del módulo de lock)
# ============================================================================


class TestTakeoverDeLockHuerfano:
    """El lock huérfano se toma SÓLO con evidencia de dueño muerto/PID reusado."""

    def _metadata(self, *, operation_id: str, owner_pid: int) -> GoldenLockMetadata:
        return GoldenLockMetadata(
            lock_key=derive_golden_lock_key(_VOLUME_SERIAL, _ROOT_FILE_ID),
            owner_pid=owner_pid,
            owner_process_creation_time=555,
            session_id=2,
            operation_id=operation_id,
            phase=GoldenLockPhase.AUTHORIZATION_BOUNDARY.value,
            created_at=1_758_400_000,
        )

    def _adquirir_recovery(self, raiz: pathlib.Path, kernel: _KernelLock, operation_id: str = _OPERACION) -> Any:
        from sky_claw.local.runtime_vault.golden_mutation_lock import (
            acquire_golden_mutation_lock_for_recovery,
        )

        return acquire_golden_mutation_lock_for_recovery(
            _VOLUME_SERIAL,
            _ROOT_FILE_ID,
            operation_id,
            kernel=kernel,
            programdata_resolver=_resolver(raiz),
        )

    def test_r15_takeover_de_huerfano_mismo_operation_id(self, tmp_path: pathlib.Path) -> None:
        raiz = _raiz(tmp_path)
        kernel = _KernelLock()
        kernel.seed(_lock_path(raiz), self._metadata(operation_id=_OPERACION, owner_pid=_PID_DUENO_MUERTO))
        kernel.owner_alive = False

        adquisicion = self._adquirir_recovery(raiz, kernel)

        assert adquisicion.preexisting_disposition.value == "orphaned_lock"
        assert adquisicion.previous_metadata is not None
        assert adquisicion.previous_metadata.owner_pid == _PID_DUENO_MUERTO
        metadata = kernel.metadata_de(_lock_path(raiz))
        assert metadata is not None
        assert metadata.owner_pid == _PID_ACTUAL
        assert metadata.operation_id == _OPERACION
        assert metadata.phase != GoldenLockPhase.RELEASED.value
        adquisicion.handle.release()
        assert kernel.metadata_de(_lock_path(raiz)).phase == GoldenLockPhase.RELEASED.value

    def test_r14_dueno_vivo_no_se_roba_nunca(self, tmp_path: pathlib.Path) -> None:
        from sky_claw.local.runtime_vault.golden_mutation_lock import GoldenLockBusyError

        raiz = _raiz(tmp_path)
        kernel = _KernelLock()
        kernel.seed(_lock_path(raiz), self._metadata(operation_id=_OPERACION, owner_pid=_PID_DUENO_VIVO))
        kernel.owner_alive = True
        antes = dict(kernel.files)

        with pytest.raises(GoldenLockBusyError):
            self._adquirir_recovery(raiz, kernel)

        assert kernel.files == antes, "un dueño vivo JAMÁS permite robar/reescribir el lock"
        assert kernel.busy_paths == set()

    def test_r16_huerfano_de_otra_operacion_se_rechaza(self, tmp_path: pathlib.Path) -> None:
        raiz = _raiz(tmp_path)
        kernel = _KernelLock()
        kernel.seed(_lock_path(raiz), self._metadata(operation_id=_OTRA_OPERACION, owner_pid=_PID_DUENO_MUERTO))
        kernel.owner_alive = False

        with pytest.raises(GoldenLockOrphanedOperationMismatchError):
            self._adquirir_recovery(raiz, kernel)

        metadata = kernel.metadata_de(_lock_path(raiz))
        assert metadata is not None and metadata.operation_id == _OTRA_OPERACION

    def test_r16_owner_ilegible_jamas_se_roba(self, tmp_path: pathlib.Path) -> None:
        from sky_claw.local.runtime_vault.golden_mutation_lock import GoldenLockBusyError

        raiz = _raiz(tmp_path)
        kernel = _KernelLock()
        kernel.seed(_lock_path(raiz), self._metadata(operation_id=_OPERACION, owner_pid=_PID_DUENO_VIVO))
        kernel.owner_alive = None  # ambigüedad fail-closed: nunca se roba

        with pytest.raises(GoldenLockBusyError):
            self._adquirir_recovery(raiz, kernel)

    def test_retencion_para_inspeccion_cierra_sin_released(self, tmp_path: pathlib.Path) -> None:
        raiz = _raiz(tmp_path)
        kernel = _KernelLock()
        adquisicion = self._adquirir_recovery(raiz, kernel)
        handle = adquisicion.handle

        assert handle.retain_for_inspection() is True
        assert handle.retain_for_inspection() is False  # cierre exactamente una vez
        metadata = kernel.metadata_de(_lock_path(raiz))
        assert metadata is not None
        assert metadata.phase != GoldenLockPhase.RELEASED.value, "retener = NO escribir RELEASED"
        assert metadata.owner_pid == _PID_ACTUAL
        assert len(kernel.close_calls) == 1

    def test_residual_released_se_reutiliza_sin_takeover(self, tmp_path: pathlib.Path) -> None:
        raiz = _raiz(tmp_path)
        kernel = _KernelLock()
        residual = self._metadata(operation_id=_OPERACION, owner_pid=_PID_DUENO_MUERTO)
        kernel.seed(
            _lock_path(raiz),
            GoldenLockMetadata(
                lock_key=residual.lock_key,
                owner_pid=residual.owner_pid,
                owner_process_creation_time=residual.owner_process_creation_time,
                session_id=residual.session_id,
                operation_id=residual.operation_id,
                phase=GoldenLockPhase.RELEASED.value,
                created_at=residual.created_at,
            ),
        )
        kernel.owner_alive = True  # irrelevante para residual

        adquisicion = self._adquirir_recovery(raiz, kernel)
        assert adquisicion.preexisting_disposition.value == "residual_released"
        assert adquisicion.previous_metadata is not None
        adquisicion.handle.release()


# ============================================================================
# R01 — C3: APPLYING con cero nodos
# ============================================================================


class TestC3AplicandoSinNodos:
    def test_r01_c3_rollback_vacio_por_transiciones_declaradas(self, tmp_path: pathlib.Path) -> None:
        raiz = _raiz(tmp_path)
        durable = _plan_durable(raiz)
        kernel = _KernelDiario()
        journal = _crear_journal(raiz, durable, kernel)
        journal.close()
        lock_kernel = _KernelLock()
        port = _PuertoRecuperacion(durable.plan)

        reporte = _recover(raiz, journal_kernel=kernel, lock_kernel=lock_kernel, port=port)

        assert reporte.disposition is RecoveryDisposition.ROLLED_BACK
        assert reporte.observed_transaction_state is ProtectionTransactionState.APPLYING
        assert reporte.nodes_restored == ()
        assert reporte.setsecurityinfo_calls == 0
        # El rollback vacío recorre SÓLO transiciones declaradas (no CANCELLED).
        raw = _journal_path(raiz).read_bytes()
        assert _estados_transaccionales(raw) == [
            "applying",
            "rollback_required",
            "rolling_back",
            "rolled_back",
        ]
        assert _estado_durable(raiz) is ProtectionTransactionState.ROLLED_BACK
        assert lock_kernel.metadata_de(_lock_path(raiz)).phase == GoldenLockPhase.RELEASED.value
        assert reporte.lock_outcome is RecoveryLockOutcome.ACQUIRED_RELEASED
        # Todos los nodos fueron escaneados en modo lectura: quedan en skipped.
        assert set(reporte.nodes_skipped) == {n.relative_path for n in durable.plan.nodes}


# ============================================================================
# R02/R04 — C4: MUTATING(K) durable sin MUTATED(K)
# ============================================================================


class TestC4Mutating:
    def _preparar(self, tmp_path: pathlib.Path, *, estado_vivo_k: str) -> tuple[Any, ...]:
        raiz = _raiz(tmp_path)
        durable = _plan_durable(raiz)
        kernel = _KernelDiario()
        journal = _crear_journal(raiz, durable, kernel)
        journal.record_node_mutation_intent(journal.node_binding("Data/Skyrim.esm"))
        journal.close()
        port = _PuertoRecuperacion(durable.plan)
        port.estado_live["Data/Skyrim.esm"] = estado_vivo_k
        return raiz, durable, kernel, port

    def test_r02_mutating_con_drift_se_restaura(self, tmp_path: pathlib.Path) -> None:
        raiz, durable, kernel, port = self._preparar(tmp_path, estado_vivo_k="target")
        lock_kernel = _KernelLock()

        reporte = _recover(raiz, journal_kernel=kernel, lock_kernel=lock_kernel, port=port)

        assert reporte.disposition is RecoveryDisposition.ROLLED_BACK
        assert reporte.nodes_restored == ("Data/Skyrim.esm",)
        assert reporte.setsecurityinfo_calls == 1
        assert _estado_durable(raiz) is ProtectionTransactionState.ROLLED_BACK
        # El restaurado es EXACTAMENTE el nodo autorizado por el plan.
        restaurado = port.restaurados[0]
        autorizado = durable.plan.node_for("Data/Skyrim.esm")
        assert restaurado == autorizado
        # El journal NUNCA gana registros de nodo nuevos (schema congelado:
        # no se inventa RESTORED/RESTORING).
        raw = _journal_path(raiz).read_bytes()
        assert _registros_de_nodo(raw) == [("Data/Skyrim.esm", "mutating")]

    def test_r04_mutating_sin_mutacion_fisica_se_salta(self, tmp_path: pathlib.Path) -> None:
        """MUTATING(K) durable NO prueba mutación: si el PRE sigue vivo, skip."""
        raiz, durable, kernel, port = self._preparar(tmp_path, estado_vivo_k="pre")
        lock_kernel = _KernelLock()

        reporte = _recover(raiz, journal_kernel=kernel, lock_kernel=lock_kernel, port=port)

        assert reporte.disposition is RecoveryDisposition.ROLLED_BACK
        assert reporte.nodes_restored == ()
        assert port.setsecurityinfo_calls == 0
        assert "Data/Skyrim.esm" in reporte.nodes_skipped
        assert _estado_durable(raiz) is ProtectionTransactionState.ROLLED_BACK


# ============================================================================
# R03/R05 — C4b: torn tail (write-vs-flush) e insuficiencia de evidencia
# ============================================================================


class TestC4bTornTail:
    def test_r03_torn_tail_es_indeterminate_sin_tocar_nada(self, tmp_path: pathlib.Path) -> None:
        raiz = _raiz(tmp_path)
        durable = _plan_durable(raiz)
        kernel = _KernelDiario()
        journal = _crear_journal(raiz, durable, kernel)
        journal.record_node_mutation_intent(journal.node_binding("Data/Skyrim.esm"))
        journal.close()
        # Escritura cortada: sin newline final y último registro incompleto.
        path = _journal_path(raiz)
        raw = path.read_bytes()
        path.write_bytes(raw[:-7])
        lock_kernel = _KernelLock()
        port = _PuertoRecuperacion(durable.plan)

        reporte = _recover(raiz, journal_kernel=kernel, lock_kernel=lock_kernel, port=port)

        assert reporte.disposition is RecoveryDisposition.INDETERMINATE
        assert reporte.journal_classification is ProtectionJournalClassification.INDETERMINATE
        assert reporte.operator_intervention_required is True
        assert reporte.nodes_restored == ()
        assert port.abiertos == [], "evidencia ambigua: CERO aperturas de nodos"
        assert reporte.setsecurityinfo_calls == 0
        # El lock queda RETENIDO para inspección: nunca RELEASED.
        metadata = lock_kernel.metadata_de(_lock_path(raiz))
        assert metadata is not None and metadata.phase != GoldenLockPhase.RELEASED.value
        assert reporte.lock_outcome is RecoveryLockOutcome.ACQUIRED_RETAINED

    def test_r05_torn_tail_jamas_se_interpreta_como_no_ocurrida(self, tmp_path: pathlib.Path) -> None:
        """El torn tail NO produce 'operación no ocurrió' ni COMMITTED."""
        raiz = _raiz(tmp_path)
        durable = _plan_durable(raiz)
        kernel = _KernelDiario()
        journal = _crear_journal(raiz, durable, kernel)
        journal.close()
        path = _journal_path(raiz)
        path.write_bytes(path.read_bytes()[:-3])
        lock_kernel = _KernelLock()
        port = _PuertoRecuperacion(durable.plan)

        reporte = _recover(raiz, journal_kernel=kernel, lock_kernel=lock_kernel, port=port)

        assert reporte.disposition is RecoveryDisposition.INDETERMINATE
        assert reporte.operator_intervention_required is True
        assert "rolled_back" not in _estados_transaccionales(path.read_bytes())
        assert reporte.setsecurityinfo_calls == 0


# ============================================================================
# R06/R07 — C4c: plan autoritativo perdido/corrupto
# ============================================================================


class TestC4cPlanPerdido:
    def _journal_mutating(self, raiz: pathlib.Path) -> tuple[DurableAuthorizedPlan, _KernelDiario]:
        durable = _plan_durable(raiz)
        kernel = _KernelDiario()
        journal = _crear_journal(raiz, durable, kernel)
        journal.record_node_mutation_intent(journal.node_binding("Data/Skyrim.esm"))
        journal.close()
        return durable, kernel

    def test_r06_plan_ausente_con_mutacion_posible_es_indeterminate(self, tmp_path: pathlib.Path) -> None:
        raiz = _raiz(tmp_path)
        durable, kernel = self._journal_mutating(raiz)
        _plan_path(raiz).unlink()
        # Staging envenenado presente: NUNCA es fuente de recuperación.
        staging = tmp_path / "Sky-Claw" / "runtime_vault" / "staging" / _OPERACION
        staging.mkdir(parents=True)
        (staging / "candidate_manifest.json").write_bytes(b'{"PRE-ENVENENADO": true}')
        lock_kernel = _KernelLock()
        port = _PuertoRecuperacion(durable.plan)

        reporte = _recover(raiz, journal_kernel=kernel, lock_kernel=lock_kernel, port=port)

        assert reporte.disposition is RecoveryDisposition.INDETERMINATE
        assert reporte.plan_classification is DurableWriteOutcome.NOT_DURABLE
        assert reporte.operator_intervention_required is True
        assert reporte.nodes_restored == ()
        assert port.abiertos == []
        assert reporte.setsecurityinfo_calls == 0
        # Lock retenido: la evidencia queda preservada para el operador.
        metadata = lock_kernel.metadata_de(_lock_path(raiz))
        assert metadata is not None and metadata.phase != GoldenLockPhase.RELEASED.value
        # El journal queda intacto: ni COMMITTED ni ROLLED_BACK inventados.
        assert _estado_durable(raiz) is ProtectionTransactionState.APPLYING

    def test_r07_plan_corrupto_con_mutacion_posible_es_indeterminate(self, tmp_path: pathlib.Path) -> None:
        raiz = _raiz(tmp_path)
        durable, kernel = self._journal_mutating(raiz)
        _plan_path(raiz).write_bytes(b"\x00\xff no-es-un-plan")
        lock_kernel = _KernelLock()
        port = _PuertoRecuperacion(durable.plan)

        reporte = _recover(raiz, journal_kernel=kernel, lock_kernel=lock_kernel, port=port)

        assert reporte.disposition is RecoveryDisposition.INDETERMINATE
        assert reporte.plan_classification is DurableWriteOutcome.INDETERMINATE
        assert reporte.operator_intervention_required is True
        assert reporte.setsecurityinfo_calls == 0
        assert port.abiertos == []

    def test_sin_plan_y_sin_journal_no_hay_transaccion(self, tmp_path: pathlib.Path) -> None:
        raiz = _raiz(tmp_path)
        lock_kernel = _KernelLock()  # owner_alive=False por defecto: huérfano permitido

        reporte = _recover(raiz, journal_kernel=_KernelDiario(), lock_kernel=lock_kernel, port=None)

        assert reporte.disposition is RecoveryDisposition.NO_TRANSACTION
        assert reporte.plan_classification is DurableWriteOutcome.NOT_DURABLE
        assert reporte.journal_classification is ProtectionJournalClassification.ABSENT
        assert reporte.operator_intervention_required is False

    def test_plan_sin_journal_normaliza_el_lock_sin_mutar(self, tmp_path: pathlib.Path) -> None:
        raiz = _raiz(tmp_path)
        _plan_durable(raiz)
        lock_kernel = _KernelLock()

        reporte = _recover(raiz, journal_kernel=_KernelDiario(), lock_kernel=lock_kernel)

        assert reporte.disposition is RecoveryDisposition.NO_TRANSACTION
        assert reporte.setsecurityinfo_calls == 0
        assert lock_kernel.metadata_de(_lock_path(raiz)).phase == GoldenLockPhase.RELEASED.value


# ============================================================================
# R08 — C5: todos los nodos MUTATED => handoff a post-verificación (S4-D)
# ============================================================================


class TestC5Handoff:
    def test_r08_todos_mutated_no_muta_ni_comitea(self, tmp_path: pathlib.Path) -> None:
        raiz = _raiz(tmp_path)
        durable = _plan_durable(raiz)
        kernel = _KernelDiario()
        journal = _crear_journal(raiz, durable, kernel)
        _mutar_todo(journal, durable)
        journal.close()
        bytes_antes = _journal_path(raiz).read_bytes()
        lock_kernel = _KernelLock()
        port = _PuertoRecuperacion(durable.plan)

        reporte = _recover(raiz, journal_kernel=kernel, lock_kernel=lock_kernel, port=port)

        assert reporte.disposition is RecoveryDisposition.POST_VERIFICATION_REQUIRED
        assert reporte.observed_transaction_state is ProtectionTransactionState.APPLYING
        assert reporte.nodes_restored == ()
        assert port.abiertos == [], "C5 no re-escanea nodos: el apply físico ya terminó"
        assert reporte.setsecurityinfo_calls == 0
        # El FSM durable NO gana NINGÚN registro: VERIFYING_GP1 pertenece a S4-D.
        assert _journal_path(raiz).read_bytes() == bytes_antes
        # §47: el lock NO se libera antes de la transición durable terminal (S4-D).
        assert reporte.lock_outcome is RecoveryLockOutcome.ACQUIRED_RETAINED
        metadata = lock_kernel.metadata_de(_lock_path(raiz))
        assert metadata is not None and metadata.phase != GoldenLockPhase.RELEASED.value
        assert reporte.stale_lock_takeover is False
        assert reporte.node_wal_summary == tuple(
            NodeWalSummary(relative_path=n.relative_path, wal_state=NodeWalState.MUTATED.value)
            for n in durable.plan.nodes
        )


# ============================================================================
# R09/R10 — C8: crash durante rollback y reanudación idempotente
# ============================================================================


class TestC8Reanudacion:
    def _journal_mutados_y_estado(
        self,
        raiz: pathlib.Path,
        *,
        transicion: ProtectionTransactionState | None = None,
        mutados: tuple[str, ...] = ("Data/Skyrim.esm", "Data"),
    ) -> tuple[DurableAuthorizedPlan, _KernelDiario]:
        durable = _plan_durable(raiz)
        kernel = _KernelDiario()
        journal = _crear_journal(raiz, durable, kernel)
        for rel in mutados:
            journal.record_node_mutation_intent(journal.node_binding(rel))
            journal.record_node_mutation_completed(journal.node_binding(rel))
        if transicion is not None:
            # El path durable recorre SÓLO transiciones declaradas.
            if transicion is ProtectionTransactionState.ROLLING_BACK:
                journal.transition_to(ProtectionTransactionState.ROLLBACK_REQUIRED)
            journal.transition_to(transicion)
        journal.close()
        return durable, kernel

    def test_r09_rollback_required_durable_se_retoma(self, tmp_path: pathlib.Path) -> None:
        raiz = _raiz(tmp_path)
        durable, kernel = self._journal_mutados_y_estado(raiz, transicion=ProtectionTransactionState.ROLLBACK_REQUIRED)
        port = _PuertoRecuperacion(durable.plan)
        port.estado_live["Data"] = "target"
        port.estado_live["Data/Skyrim.esm"] = "target"

        reporte = _recover(raiz, journal_kernel=kernel, lock_kernel=_KernelLock(), port=port)

        assert reporte.disposition is RecoveryDisposition.ROLLED_BACK
        assert set(reporte.nodes_restored) == {"Data", "Data/Skyrim.esm"}
        assert _estado_durable(raiz) is ProtectionTransactionState.ROLLED_BACK
        # Orden top-down: "Data" (profundidad 1) antes que "Data/Skyrim.esm" (2).
        assert port.orden_restauracion == ["Data", "Data/Skyrim.esm"]

    def test_r10_rolling_back_con_primer_nodo_restaurado_se_reanuda(self, tmp_path: pathlib.Path) -> None:
        """Process B restauró 'Data' y murió: Process C no lo sabe y no lo necesita.

        La sonda semántica reconoce el nodo restaurado aunque SetSecurityInfo
        haya reserializado los bytes ("pre_reserializado"): se salta SIN escribir
        y el resto se restaura top-down.
        """
        raiz = _raiz(tmp_path)
        durable, kernel = self._journal_mutados_y_estado(raiz, transicion=ProtectionTransactionState.ROLLING_BACK)
        port = _PuertoRecuperacion(durable.plan)
        port.estado_live["Data"] = "pre_reserializado"  # ya restaurado por B
        port.estado_live["Data/Skyrim.esm"] = "target"  # B no llegó: sigue mutado

        reporte = _recover(raiz, journal_kernel=kernel, lock_kernel=_KernelLock(), port=port)

        assert reporte.disposition is RecoveryDisposition.ROLLED_BACK
        assert reporte.nodes_restored == ("Data/Skyrim.esm",)
        assert "Data" in reporte.nodes_skipped
        assert port.orden_restauracion == ["Data/Skyrim.esm"], (
            "un nodo semánticamente restaurado no se re-escribe (mutante de igualdad raw de bytes)"
        )
        assert _estado_durable(raiz) is ProtectionTransactionState.ROLLED_BACK

    def test_orden_de_restauracion_es_top_down_del_plan(self, tmp_path: pathlib.Path) -> None:
        raiz = _raiz(tmp_path)
        durable, kernel = self._journal_mutados_y_estado(raiz, mutados=(".", "Data", "Data/Skyrim.esm"))
        port = _PuertoRecuperacion(durable.plan)
        for rel in (".", "Data", "Data/Skyrim.esm"):
            port.estado_live[rel] = "target"

        reporte = _recover(raiz, journal_kernel=kernel, lock_kernel=_KernelLock(), port=port)

        assert port.orden_restauracion == [
            n.relative_path for n in rollback_order(durable.plan, (".", "Data", "Data/Skyrim.esm"))
        ]
        assert reporte.nodes_restored[0] == "."


# ============================================================================
# R11/R12/R13 — C9: FileId / reparse / hardlink drift durante recovery
# ============================================================================


class TestC9Drift:
    def _escenario(self, tmp_path: pathlib.Path) -> tuple[Any, ...]:
        raiz = _raiz(tmp_path)
        durable = _plan_durable(raiz)
        kernel = _KernelDiario()
        journal = _crear_journal(raiz, durable, kernel)
        journal.record_node_mutation_intent(journal.node_binding("Data/Skyrim.esm"))
        journal.record_node_mutation_completed(journal.node_binding("Data/Skyrim.esm"))
        journal.close()
        port = _PuertoRecuperacion(durable.plan)
        port.estado_live["Data/Skyrim.esm"] = "target"
        return raiz, durable, kernel, port

    def test_r11_fileid_drift_detiene_sin_escribir(self, tmp_path: pathlib.Path) -> None:
        raiz, durable, kernel, port = self._escenario(tmp_path)
        port.drift_identidad["Data/Skyrim.esm"] = NodeIdentity(
            volume_serial_number=_VOLUME_SERIAL,
            file_id=_ROOT_FILE_ID + 999,
            reparse_tag=0,
            number_of_links=1,
        )

        reporte = _recover(raiz, journal_kernel=kernel, lock_kernel=_KernelLock(), port=port)

        assert reporte.disposition is RecoveryDisposition.INDETERMINATE
        assert port.restaurados == []
        assert reporte.setsecurityinfo_calls == 0
        assert reporte.operator_intervention_required is True
        assert "Data/Skyrim.esm" in reporte.nodes_pending

    def test_r12_reparse_drift_detiene_sin_escribir(self, tmp_path: pathlib.Path) -> None:
        raiz, durable, kernel, port = self._escenario(tmp_path)
        port.drift_identidad["Data/Skyrim.esm"] = NodeIdentity(
            volume_serial_number=_VOLUME_SERIAL,
            file_id=_ROOT_FILE_ID + 4,
            reparse_tag=0xA000000C,
            number_of_links=1,
        )

        reporte = _recover(raiz, journal_kernel=kernel, lock_kernel=_KernelLock(), port=port)

        assert reporte.disposition is RecoveryDisposition.INDETERMINATE
        assert port.restaurados == []
        assert reporte.setsecurityinfo_calls == 0

    def test_r13_hardlink_drift_detiene_sin_escribir(self, tmp_path: pathlib.Path) -> None:
        raiz, durable, kernel, port = self._escenario(tmp_path)
        port.drift_identidad["Data/Skyrim.esm"] = NodeIdentity(
            volume_serial_number=_VOLUME_SERIAL,
            file_id=_ROOT_FILE_ID + 4,
            reparse_tag=0,
            number_of_links=2,
        )
        lock_kernel = _KernelLock()

        reporte = _recover(raiz, journal_kernel=kernel, lock_kernel=lock_kernel, port=port)

        assert reporte.disposition is RecoveryDisposition.INDETERMINATE
        assert port.restaurados == []
        assert reporte.setsecurityinfo_calls == 0
        metadata = lock_kernel.metadata_de(_lock_path(raiz))
        assert metadata is not None and metadata.phase != GoldenLockPhase.RELEASED.value

    def test_drift_de_nodo_sin_registro_wal_es_indeterminate(self, tmp_path: pathlib.Path) -> None:
        """Un nodo sin permiso durable NUNCA debió mutar: si muestra drift, no
        se certifica ROLLED_BACK con evidencia contradictoria."""
        raiz = _raiz(tmp_path)
        durable = _plan_durable(raiz)
        kernel = _KernelDiario()
        journal = _crear_journal(raiz, durable, kernel)
        journal.record_node_mutation_intent(journal.node_binding("Data/Skyrim.esm"))
        journal.record_node_mutation_completed(journal.node_binding("Data/Skyrim.esm"))
        journal.close()
        port = _PuertoRecuperacion(durable.plan)
        port.estado_live["Data/Skyrim.esm"] = "target"
        port.estado_live["Data/Scripts"] = "ajeno"  # drift sin registro durable

        reporte = _recover(raiz, journal_kernel=kernel, lock_kernel=_KernelLock(), port=port)

        assert reporte.disposition is RecoveryDisposition.INDETERMINATE
        assert port.restaurados == []
        assert reporte.setsecurityinfo_calls == 0
        assert "Data/Scripts" in reporte.nodes_pending

    def test_drift_detectado_antes_de_cualquier_restauracion(self, tmp_path: pathlib.Path) -> None:
        """El escaneo es READ-ONLY y COMPLETO antes del primer SetSecurityInfo.

        El drift está en "." (la raíz, profundidad 0: se escanea AL FINAL en orden
        bottom-up) y hay nodos restaurables que se escanean ANTES. Un engine que
        restaurara durante el escaneo dejaría ``restaurados`` no vacío.
        """
        raiz = _raiz(tmp_path)
        durable = _plan_durable(raiz)
        kernel = _KernelDiario()
        journal = _crear_journal(raiz, durable, kernel)
        journal.record_node_mutation_intent(journal.node_binding("Data"))
        journal.record_node_mutation_completed(journal.node_binding("Data"))
        journal.record_node_mutation_intent(journal.node_binding("Data/Skyrim.esm"))
        journal.record_node_mutation_completed(journal.node_binding("Data/Skyrim.esm"))
        journal.close()
        port = _PuertoRecuperacion(durable.plan)
        port.estado_live["Data"] = "target"
        port.estado_live["Data/Skyrim.esm"] = "target"
        port.drift_identidad["."] = NodeIdentity(
            volume_serial_number=_VOLUME_SERIAL,
            file_id=_ROOT_FILE_ID + 998,
            reparse_tag=0,
            number_of_links=1,
        )

        reporte = _recover(raiz, journal_kernel=kernel, lock_kernel=_KernelLock(), port=port)

        assert reporte.disposition is RecoveryDisposition.INDETERMINATE
        assert port.restaurados == [], "ninguna restauración puede anteceder a la evidencia completa"
        assert reporte.setsecurityinfo_calls == 0
        assert "." in reporte.nodes_pending


# ============================================================================
# R14 — lock vivo a nivel engine
# ============================================================================


class TestLockBusyEngine:
    def _metadata(self, *, phase: str) -> GoldenLockMetadata:
        return GoldenLockMetadata(
            lock_key=derive_golden_lock_key(_VOLUME_SERIAL, _ROOT_FILE_ID),
            owner_pid=_PID_DUENO_VIVO,
            owner_process_creation_time=555,
            session_id=2,
            operation_id=_OPERACION,
            phase=phase,
            created_at=1_758_400_000,
        )

    def test_r14_dueno_vivo_no_se_toca_nada(self, tmp_path: pathlib.Path) -> None:
        raiz = _raiz(tmp_path)
        durable = _plan_durable(raiz)
        kernel = _KernelDiario()
        journal = _crear_journal(raiz, durable, kernel)
        journal.record_node_mutation_intent(journal.node_binding("Data/Skyrim.esm"))
        journal.close()
        journal_antes = _journal_path(raiz).read_bytes()
        lock_kernel = _KernelLock()
        lock_kernel.seed(_lock_path(raiz), self._metadata(phase=GoldenLockPhase.AUTHORIZATION_BOUNDARY.value))
        lock_kernel.owner_alive = True
        port = _PuertoRecuperacion(durable.plan)

        reporte = _recover(raiz, journal_kernel=kernel, lock_kernel=lock_kernel, port=port)

        assert reporte.disposition is RecoveryDisposition.LOCK_BUSY
        assert reporte.operator_intervention_required is False
        assert port.abiertos == []
        assert reporte.setsecurityinfo_calls == 0
        assert _journal_path(raiz).read_bytes() == journal_antes
        assert lock_kernel.metadata_de(_lock_path(raiz)).owner_pid == _PID_DUENO_VIVO

    def test_r15_huerfano_muerto_permite_recovery_completo(self, tmp_path: pathlib.Path) -> None:
        raiz = _raiz(tmp_path)
        durable = _plan_durable(raiz)
        kernel = _KernelDiario()
        journal = _crear_journal(raiz, durable, kernel)
        journal.record_node_mutation_intent(journal.node_binding("Data/Skyrim.esm"))
        journal.close()
        lock_kernel = _KernelLock()
        lock_kernel.seed(_lock_path(raiz), self._metadata(phase=GoldenLockPhase.AUTHORIZATION_BOUNDARY.value))
        lock_kernel.owner_alive = False
        port = _PuertoRecuperacion(durable.plan)
        port.estado_live["Data/Skyrim.esm"] = "target"

        reporte = _recover(raiz, journal_kernel=kernel, lock_kernel=lock_kernel, port=port)

        assert reporte.disposition is RecoveryDisposition.ROLLED_BACK
        assert reporte.stale_lock_takeover is True
        assert reporte.nodes_restored == ("Data/Skyrim.esm",)
        assert lock_kernel.metadata_de(_lock_path(raiz)).phase == GoldenLockPhase.RELEASED.value

    def test_plan_sustituido_entre_clasificacion_y_lock_es_indeterminate(self, tmp_path: pathlib.Path) -> None:
        """El plan que DERIVÓ la identidad del lock debe ser el que decide.

        Una sustitución del plan autoritativo entre la clasificación y la
        exclusividad dejaría al lock ligado a un Golden y al dispatch actuando
        sobre otro: la comparación de digest + identidad física lo corta antes
        de abrir el journal, con cero aperturas de nodo.
        """
        import dataclasses

        raiz = _raiz(tmp_path)
        durable = _plan_durable(raiz)
        kernel = _KernelDiario()
        journal = _crear_journal(raiz, durable, kernel)
        journal.record_node_mutation_intent(journal.node_binding("Data/Skyrim.esm"))
        journal.close()

        def _sustituir() -> None:
            hostil = dataclasses.replace(durable.plan, staging_digest="e" * 64)
            _plan_path(raiz).write_bytes(serialize_authorized_plan(hostil))

        lock_kernel = _KernelLock()
        lock_kernel.on_open = _sustituir
        port = _PuertoRecuperacion(durable.plan)

        reporte = _recover(raiz, journal_kernel=kernel, lock_kernel=lock_kernel, port=port)

        assert reporte.disposition is RecoveryDisposition.INDETERMINATE
        assert reporte.operator_intervention_required is True
        assert port.abiertos == []
        assert reporte.setsecurityinfo_calls == 0
        metadata = lock_kernel.metadata_de(_lock_path(raiz))
        assert metadata is not None and metadata.phase != GoldenLockPhase.RELEASED.value


# ============================================================================
# §48/§49 — fallo del flush terminal y crash en la transición terminal
# ============================================================================


class TestFalloTerminal:
    def _escenario(self, tmp_path: pathlib.Path, kernel: _KernelDiario) -> tuple[Any, ...]:
        raiz = _raiz(tmp_path)
        durable = _plan_durable(raiz)
        journal = _crear_journal(raiz, durable, kernel)
        journal.record_node_mutation_intent(journal.node_binding("Data/Skyrim.esm"))
        journal.close()
        port = _PuertoRecuperacion(durable.plan)
        port.estado_live["Data/Skyrim.esm"] = "target"
        return raiz, durable, kernel, port

    def test_restauracion_fisica_ok_con_flush_terminal_fallido_no_afirma_rolled_back(
        self, tmp_path: pathlib.Path
    ) -> None:
        raiz = _raiz(tmp_path)
        kernel = _KernelDiario()
        durable = _plan_durable(raiz)
        journal = _crear_journal(raiz, durable, kernel)
        journal.record_node_mutation_intent(journal.node_binding("Data/Skyrim.esm"))
        journal.close()
        # El append de ROLLED_BACK falla: crash durante la transición terminal.
        kernel.fallar_append_de = frozenset({"rolled_back"})
        port = _PuertoRecuperacion(durable.plan)
        port.estado_live["Data/Skyrim.esm"] = "target"
        lock_kernel = _KernelLock()

        reporte = _recover(raiz, journal_kernel=kernel, lock_kernel=lock_kernel, port=port)

        assert reporte.disposition is RecoveryDisposition.INDETERMINATE
        assert reporte.physical_restoration_completed is True, "la restauración física SÍ se observó"
        assert reporte.nodes_restored == ("Data/Skyrim.esm",)
        assert reporte.operator_intervention_required is True
        assert _estado_durable(raiz) is ProtectionTransactionState.ROLLING_BACK
        metadata = lock_kernel.metadata_de(_lock_path(raiz))
        assert metadata is not None and metadata.phase != GoldenLockPhase.RELEASED.value

    def test_recovery_posterior_desde_evidencia_observable_concluye(self, tmp_path: pathlib.Path) -> None:
        raiz = _raiz(tmp_path)
        kernel = _KernelDiario()
        durable = _plan_durable(raiz)
        journal = _crear_journal(raiz, durable, kernel)
        journal.record_node_mutation_intent(journal.node_binding("Data/Skyrim.esm"))
        journal.close()
        kernel.fallar_append_de = frozenset({"rolled_back"})
        port = _PuertoRecuperacion(durable.plan)
        port.estado_live["Data/Skyrim.esm"] = "target"
        _recover(raiz, journal_kernel=kernel, lock_kernel=_KernelLock(), port=port)

        # Proceso nuevo: evidencia fresca, sin memoria del anterior.
        kernel2 = _KernelDiario()
        port2 = _PuertoRecuperacion(durable.plan)
        port2.estado_live["Data/Skyrim.esm"] = "pre"  # físicamente ya restaurado
        lock_kernel2 = _KernelLock()

        reporte = _recover(raiz, journal_kernel=kernel2, lock_kernel=lock_kernel2, port=port2)

        assert reporte.disposition is RecoveryDisposition.ROLLED_BACK
        assert reporte.nodes_restored == ()
        assert "Data/Skyrim.esm" in reporte.nodes_skipped
        assert _estado_durable(raiz) is ProtectionTransactionState.ROLLED_BACK


# ============================================================================
# R20 — idempotencia y terminal
# ============================================================================


class TestIdempotencia:
    def test_r20_segunda_ejecucion_sobre_rolled_back_es_terminal_sin_toques(self, tmp_path: pathlib.Path) -> None:
        raiz = _raiz(tmp_path)
        durable = _plan_durable(raiz)
        kernel = _KernelDiario()
        journal = _crear_journal(raiz, durable, kernel)
        journal.record_node_mutation_intent(journal.node_binding("Data/Skyrim.esm"))
        journal.close()
        port = _PuertoRecuperacion(durable.plan)
        port.estado_live["Data/Skyrim.esm"] = "target"
        primera = _recover(raiz, journal_kernel=kernel, lock_kernel=_KernelLock(), port=port)
        assert primera.disposition is RecoveryDisposition.ROLLED_BACK
        bytes_tras_primera = _journal_path(raiz).read_bytes()
        port2 = _PuertoRecuperacion(durable.plan)

        segunda = _recover(raiz, journal_kernel=_KernelDiario(), lock_kernel=_KernelLock(), port=port2)

        assert segunda.disposition is RecoveryDisposition.TERMINAL
        assert segunda.observed_transaction_state is ProtectionTransactionState.ROLLED_BACK
        assert port2.abiertos == []
        assert segunda.setsecurityinfo_calls == 0
        assert _journal_path(raiz).read_bytes() == bytes_tras_primera, "una operación terminal no gana registros"

    def test_recovery_repetido_tras_indeterminate_sigue_indeterminate(self, tmp_path: pathlib.Path) -> None:
        raiz = _raiz(tmp_path)
        durable = _plan_durable(raiz)
        kernel = _KernelDiario()
        journal = _crear_journal(raiz, durable, kernel)
        journal.record_node_mutation_intent(journal.node_binding("Data/Skyrim.esm"))
        journal.close()
        path = _journal_path(raiz)
        path.write_bytes(path.read_bytes()[:-4])
        primera = _recover(
            raiz, journal_kernel=kernel, lock_kernel=_KernelLock(), port=_PuertoRecuperacion(durable.plan)
        )
        assert primera.disposition is RecoveryDisposition.INDETERMINATE

        segunda = _recover(
            raiz, journal_kernel=_KernelDiario(), lock_kernel=_KernelLock(), port=_PuertoRecuperacion(durable.plan)
        )

        assert segunda.disposition is RecoveryDisposition.INDETERMINATE
        assert segunda.operator_intervention_required is True

    def test_terminal_failure_retiene_lock_y_marca_operador(self, tmp_path: pathlib.Path) -> None:
        raiz = _raiz(tmp_path)
        durable = _plan_durable(raiz)
        kernel = _KernelDiario()
        journal = _crear_journal(raiz, durable, kernel)
        journal.record_node_mutation_intent(journal.node_binding("Data/Skyrim.esm"))
        journal.record_node_mutation_completed(journal.node_binding("Data/Skyrim.esm"))
        journal.transition_to(ProtectionTransactionState.ROLLBACK_REQUIRED)
        journal.transition_to(ProtectionTransactionState.ROLLING_BACK)
        journal.transition_to(ProtectionTransactionState.ROLLBACK_FAILED)
        journal.close()
        lock_kernel = _KernelLock()

        reporte = _recover(raiz, journal_kernel=kernel, lock_kernel=lock_kernel, port=_PuertoRecuperacion(durable.plan))

        assert reporte.disposition is RecoveryDisposition.TERMINAL
        assert reporte.operator_intervention_required is True
        metadata = lock_kernel.metadata_de(_lock_path(raiz))
        assert metadata is not None and metadata.phase != GoldenLockPhase.RELEASED.value
        assert reporte.lock_outcome is RecoveryLockOutcome.ACQUIRED_RETAINED


# ============================================================================
# R17/R18/R19 — staging, TGR y COMMITTED
# ============================================================================


class TestFronterasDuras:
    def test_r17_staging_nunca_es_fuente_de_recuperacion(self, tmp_path: pathlib.Path, monkeypatch: Any) -> None:
        raiz = _raiz(tmp_path)
        durable = _plan_durable(raiz)
        kernel = _KernelDiario()
        journal = _crear_journal(raiz, durable, kernel)
        journal.record_node_mutation_intent(journal.node_binding("Data/Skyrim.esm"))
        journal.close()
        # Canario runtime: si el engine abriera el candidate manifest, explota.
        from sky_claw.local.runtime_vault import authorized_plan_store

        def _explota(*args: Any, **kwargs: Any) -> bytes:
            raise AssertionError("el recovery leyó el staging como fuente")

        monkeypatch.setattr(authorized_plan_store, "read_candidate_manifest_bytes", _explota)
        monkeypatch.setattr(authorized_plan_store, "derive_candidate_manifest_path", _explota)
        port = _PuertoRecuperacion(durable.plan)
        port.estado_live["Data/Skyrim.esm"] = "target"

        reporte = _recover(raiz, journal_kernel=kernel, lock_kernel=_KernelLock(), port=port)

        assert reporte.disposition is RecoveryDisposition.ROLLED_BACK
        autorizado = durable.plan.node_for("Data/Skyrim.esm")
        assert port.restaurados == [autorizado], "el PRE restaurado sale del PLAN, nunca de staging"

    def test_r18_tgr_no_se_escribe(self, tmp_path: pathlib.Path) -> None:
        raiz = _raiz(tmp_path)
        durable = _plan_durable(raiz)
        kernel = _KernelDiario()
        journal = _crear_journal(raiz, durable, kernel)
        journal.record_node_mutation_intent(journal.node_binding("Data/Skyrim.esm"))
        journal.close()
        tgr = raiz / "Sky-Claw" / "runtime_vault" / "trusted_goldens.json"
        tgr.write_bytes(b'{"goldens": [], "centinela": true}')
        port = _PuertoRecuperacion(durable.plan)
        port.estado_live["Data/Skyrim.esm"] = "target"

        reporte = _recover(raiz, journal_kernel=kernel, lock_kernel=_KernelLock(), port=port)

        assert reporte.disposition is RecoveryDisposition.ROLLED_BACK
        assert tgr.read_bytes() == b'{"goldens": [], "centinela": true}'

    def test_r19_ningun_escenario_produce_committed(self, tmp_path: pathlib.Path) -> None:
        """Sweep: el estado durable final JAMÁS es COMMITTED en S4-C."""
        escenarios: list[RecoveryForensicReport] = []

        # (a) C3
        raiz_a = _raiz(tmp_path / "a")
        durable_a = _plan_durable(raiz_a)
        kernel_a = _KernelDiario()
        _crear_journal(raiz_a, durable_a, kernel_a).close()
        escenarios.append(
            _recover(
                raiz_a, journal_kernel=kernel_a, lock_kernel=_KernelLock(), port=_PuertoRecuperacion(durable_a.plan)
            )
        )
        # (b) C5
        raiz_b = _raiz(tmp_path / "b")
        durable_b = _plan_durable(raiz_b)
        kernel_b = _KernelDiario()
        journal_b = _crear_journal(raiz_b, durable_b, kernel_b)
        _mutar_todo(journal_b, durable_b)
        journal_b.close()
        escenarios.append(
            _recover(
                raiz_b, journal_kernel=kernel_b, lock_kernel=_KernelLock(), port=_PuertoRecuperacion(durable_b.plan)
            )
        )
        # (c) C4 con rollback
        raiz_c = _raiz(tmp_path / "c")
        durable_c = _plan_durable(raiz_c)
        kernel_c = _KernelDiario()
        journal_c = _crear_journal(raiz_c, durable_c, kernel_c)
        journal_c.record_node_mutation_intent(journal_c.node_binding("Data/Skyrim.esm"))
        journal_c.close()
        port_c = _PuertoRecuperacion(durable_c.plan)
        port_c.estado_live["Data/Skyrim.esm"] = "target"
        escenarios.append(_recover(raiz_c, journal_kernel=kernel_c, lock_kernel=_KernelLock(), port=port_c))

        for raiz, reporte in zip((raiz_a, raiz_b, raiz_c), escenarios, strict=True):
            if reporte.observed_transaction_state is not None:
                assert _estado_durable(raiz) is not ProtectionTransactionState.COMMITTED
                assert "committed" not in _estados_transaccionales(_journal_path(raiz).read_bytes())


# ============================================================================
# Reporte forense (§32)
# ============================================================================


class TestPrePlanLockHuerfano:
    """P1#1: crash entre adquirir el lock (§12.2 paso 6) y publicar el plan (paso 7).

    Sin binding, el recovery no tenía de dónde derivar la identidad física (la
    clave del lock es ``vol+file_id``, no ``operation_id``) y devolvía
    ``NO_TRANSACTION`` sin tocar el lock: el Golden quedaba bloqueado para
    siempre (toda nueva operación recibía ``GoldenLockOrphanedError``). El
    binding PRE-plan cierra esa ventana.
    """

    def _binding_durable(self, raiz: pathlib.Path, *, operation_id: str = _OPERACION) -> None:
        binding = build_operation_lock_binding(
            operation_id=operation_id,
            volume_serial_number=_VOLUME_SERIAL,
            root_file_id=_ROOT_FILE_ID,
            created_at="2026-09-30T12:00:00.000Z",
        )
        destino = derive_operation_lock_binding_path(operation_id, programdata_resolver=_resolver(raiz))
        destino.parent.mkdir(parents=True, exist_ok=True)
        destino.write_bytes(serialize_operation_lock_binding(binding))

    def _metadata_lock(
        self,
        *,
        phase: str,
        operation_id: str = _OPERACION,
        owner_pid: int = _PID_DUENO_MUERTO,
        lock_key: str | None = None,
    ) -> GoldenLockMetadata:
        return GoldenLockMetadata(
            lock_key=lock_key or derive_golden_lock_key(_VOLUME_SERIAL, _ROOT_FILE_ID),
            owner_pid=owner_pid,
            owner_process_creation_time=555,
            session_id=2,
            operation_id=operation_id,
            phase=phase,
            created_at=1_758_400_000,
        )

    def test_r21_binding_durable_normaliza_el_lock_huerfano_sin_tocar_acl(self, tmp_path: pathlib.Path) -> None:
        raiz = _raiz(tmp_path)
        self._binding_durable(raiz)
        lock_kernel = _KernelLock()
        lock_kernel.seed(_lock_path(raiz), self._metadata_lock(phase=GoldenLockPhase.AUTHORIZATION_BOUNDARY.value))
        lock_kernel.owner_alive = False
        port = _PuertoRecuperacion(_plan())

        reporte = _recover(raiz, journal_kernel=_KernelDiario(), lock_kernel=lock_kernel, port=port)

        assert reporte.disposition is RecoveryDisposition.PRE_PLAN_LOCK_RECOVERED
        assert reporte.physical_identity_source is RecoveryIdentitySource.OPERATION_LOCK_BINDING
        assert reporte.stale_lock_takeover is True
        assert reporte.lock_outcome is RecoveryLockOutcome.ACQUIRED_RELEASED
        assert reporte.operator_intervention_required is False
        # CERO escrituras sobre el Golden: sin plan no hay PRE y no hay rollback.
        assert port.abiertos == []
        assert reporte.setsecurityinfo_calls == 0
        assert reporte.nodes_restored == ()
        metadata = lock_kernel.metadata_de(_lock_path(raiz))
        assert metadata is not None
        assert metadata.phase == GoldenLockPhase.RELEASED.value
        assert metadata.owner_pid == _PID_ACTUAL

    def test_r21b_pre_plan_es_idempotente(self, tmp_path: pathlib.Path) -> None:
        raiz = _raiz(tmp_path)
        self._binding_durable(raiz)
        lock_kernel = _KernelLock()
        lock_kernel.seed(_lock_path(raiz), self._metadata_lock(phase=GoldenLockPhase.AUTHORIZATION_BOUNDARY.value))
        lock_kernel.owner_alive = False
        primera = _recover(raiz, journal_kernel=_KernelDiario(), lock_kernel=lock_kernel, port=None)
        assert primera.disposition is RecoveryDisposition.PRE_PLAN_LOCK_RECOVERED

        # Segundo proceso, misma evidencia: el lock quedó RELEASED (residual) y
        # la clasificación se repite sin tocar nada.
        segunda = _recover(raiz, journal_kernel=_KernelDiario(), lock_kernel=lock_kernel, port=None)

        assert segunda.disposition is RecoveryDisposition.PRE_PLAN_LOCK_RECOVERED
        assert segunda.stale_lock_takeover is False
        metadata = lock_kernel.metadata_de(_lock_path(raiz))
        assert metadata is not None and metadata.phase == GoldenLockPhase.RELEASED.value

    def test_r21i_binding_sin_lock_nunca_llegado_a_adquirirse(self, tmp_path: pathlib.Path) -> None:
        """Caso B: crash DESPUÉS del binding pero ANTES del lock.

        No hay lock huérfano que normalizar, pero el recovery debe resolver la
        identidad, tomar el lock (limpio), comprobar que no hay plan ni journal y
        devolver la misma clasificación sin escribir nada.
        """
        raiz = _raiz(tmp_path)
        self._binding_durable(raiz)
        lock_kernel = _KernelLock()
        port = _PuertoRecuperacion(_plan())

        reporte = _recover(raiz, journal_kernel=_KernelDiario(), lock_kernel=lock_kernel, port=port)

        assert reporte.disposition is RecoveryDisposition.PRE_PLAN_LOCK_RECOVERED
        assert reporte.stale_lock_takeover is False, "no había lock: nada que tomar"
        assert reporte.lock_outcome is RecoveryLockOutcome.ACQUIRED_RELEASED
        assert port.abiertos == [] and reporte.setsecurityinfo_calls == 0

    def test_r21j_plan_corrupto_sin_journal_no_normaliza_el_lock(self, tmp_path: pathlib.Path) -> None:
        """Caso D: crash durante la publicación del plan (presente, no utilizable).

        §12.2 7c/7e tratan el flush fallido como fail-closed: sin journal no hubo
        mutación posible, pero un plan presente e ilegible es evidencia
        contradictoria. Se rehusa con el lock RETENIDO para el operador, en vez de
        normalizarlo como si fuera un pre-plan limpio.
        """
        raiz = _raiz(tmp_path)
        self._binding_durable(raiz)
        _plan_path(raiz).write_bytes(b"\x00\xff plan truncado a mitad de publicacion")
        lock_kernel = _KernelLock()
        lock_kernel.seed(
            _lock_path(raiz), self._metadata_lock(phase=GoldenLockPhase.AUTHORIZATION_BOUNDARY.value)
        )
        lock_kernel.owner_alive = False

        reporte = _recover(raiz, journal_kernel=_KernelDiario(), lock_kernel=lock_kernel, port=None)

        assert reporte.disposition is RecoveryDisposition.INDETERMINATE
        assert reporte.operator_intervention_required is True
        assert reporte.lock_outcome is RecoveryLockOutcome.ACQUIRED_RETAINED
        metadata = lock_kernel.metadata_de(_lock_path(raiz))
        assert metadata is not None and metadata.phase != GoldenLockPhase.RELEASED.value

    def test_r21c_lock_de_otra_operacion_no_se_roba(self, tmp_path: pathlib.Path) -> None:
        raiz = _raiz(tmp_path)
        self._binding_durable(raiz)
        lock_kernel = _KernelLock()
        lock_kernel.seed(
            _lock_path(raiz),
            self._metadata_lock(phase=GoldenLockPhase.AUTHORIZATION_BOUNDARY.value, operation_id=_OTRA_OPERACION),
        )
        lock_kernel.owner_alive = False

        reporte = _recover(raiz, journal_kernel=_KernelDiario(), lock_kernel=lock_kernel, port=None)

        assert reporte.disposition is RecoveryDisposition.INDETERMINATE
        assert reporte.operator_intervention_required is True
        assert reporte.setsecurityinfo_calls == 0
        metadata = lock_kernel.metadata_de(_lock_path(raiz))
        assert metadata is not None and metadata.operation_id == _OTRA_OPERACION, "no hubo robo"

    def test_r21d_dueno_vivo_no_se_roba_en_pre_plan(self, tmp_path: pathlib.Path) -> None:
        raiz = _raiz(tmp_path)
        self._binding_durable(raiz)
        lock_kernel = _KernelLock()
        lock_kernel.seed(
            _lock_path(raiz),
            self._metadata_lock(phase=GoldenLockPhase.AUTHORIZATION_BOUNDARY.value, owner_pid=_PID_DUENO_VIVO),
        )
        lock_kernel.owner_alive = True

        reporte = _recover(raiz, journal_kernel=_KernelDiario(), lock_kernel=lock_kernel, port=None)

        assert reporte.disposition is RecoveryDisposition.LOCK_BUSY
        assert reporte.setsecurityinfo_calls == 0

    def test_r21e_binding_corrupto_es_indeterminate_sin_tocar_lock(self, tmp_path: pathlib.Path) -> None:
        raiz = _raiz(tmp_path)
        destino = derive_operation_lock_binding_path(_OPERACION, programdata_resolver=_resolver(raiz))
        destino.parent.mkdir(parents=True, exist_ok=True)
        destino.write_bytes(b"\x00\xff binding corrupto")
        lock_kernel = _KernelLock()

        reporte = _recover(raiz, journal_kernel=_KernelDiario(), lock_kernel=lock_kernel, port=None)

        assert reporte.disposition is RecoveryDisposition.INDETERMINATE
        assert reporte.operator_intervention_required is True
        assert reporte.lock_outcome is RecoveryLockOutcome.NOT_ATTEMPTED
        assert lock_kernel.open_handles == {}, "sin identidad válida no se ni abre el lock"

    def test_r21f_binding_con_lock_key_incoherente_es_indeterminate(self, tmp_path: pathlib.Path) -> None:
        """Metadata del lock cuyo ``lock_key`` no corresponde a su ruta = sustitución."""
        raiz = _raiz(tmp_path)
        self._binding_durable(raiz)
        lock_kernel = _KernelLock()
        lock_kernel.seed(
            _lock_path(raiz),
            self._metadata_lock(
                phase=GoldenLockPhase.AUTHORIZATION_BOUNDARY.value,
                lock_key=derive_golden_lock_key(_VOLUME_SERIAL, _ROOT_FILE_ID + 1),
            ),
        )
        lock_kernel.owner_alive = False

        reporte = _recover(raiz, journal_kernel=_KernelDiario(), lock_kernel=lock_kernel, port=None)

        assert reporte.disposition is RecoveryDisposition.INDETERMINATE
        assert reporte.lock_outcome is RecoveryLockOutcome.REFUSED
        assert reporte.operator_intervention_required is True

    def test_r21g_sin_binding_plan_ni_journal_es_no_transaction(self, tmp_path: pathlib.Path) -> None:
        raiz = _raiz(tmp_path)
        lock_kernel = _KernelLock()

        reporte = _recover(raiz, journal_kernel=_KernelDiario(), lock_kernel=lock_kernel, port=None)

        assert reporte.disposition is RecoveryDisposition.NO_TRANSACTION
        assert reporte.physical_identity_source is RecoveryIdentitySource.NONE
        assert lock_kernel.open_handles == {}

    def test_r21h_binding_no_puede_habilitar_rollback(self, tmp_path: pathlib.Path) -> None:
        """El binding NO es PRE: con journal presente la evidencia es contradictoria."""
        raiz = _raiz(tmp_path)
        durable = _plan_durable(raiz)
        self._binding_durable(raiz)
        kernel = _KernelDiario()
        journal = _crear_journal(raiz, durable, kernel)
        journal.record_node_mutation_intent(journal.node_binding("Data/Skyrim.esm"))
        journal.close()
        # El plan se borra: la identidad podría venir del binding, pero hay
        # journal => evidencia transaccional contradictoria con "pre-plan".
        _plan_path(raiz).unlink()
        lock_kernel = _KernelLock()

        reporte = _recover(raiz, journal_kernel=kernel, lock_kernel=lock_kernel, port=None)

        assert reporte.disposition is RecoveryDisposition.INDETERMINATE
        assert reporte.operator_intervention_required is True
        assert reporte.setsecurityinfo_calls == 0


class TestReporteForense:
    def test_el_reporte_no_lleva_bytes_de_sd_ni_campos_de_autoridad(self) -> None:
        import dataclasses

        nombres = {campo.name for campo in dataclasses.fields(RecoveryForensicReport)}
        for prohibido in ("pre_sd", "sd_bytes", "canonical_root", "path", "raw"):
            assert all(prohibido not in nombre for nombre in nombres), sorted(nombres)

    def test_jerarquia_de_excepciones(self) -> None:
        from sky_claw.local.runtime_vault.models import RuntimeVaultError

        assert issubclass(RecoveryOrchestratorError, RuntimeVaultError)

    def test_disposiciones_congeladas_por_igualdad_literal(self) -> None:
        assert {d.value for d in RecoveryDisposition} == {
            "no_transaction",
            "rolled_back",
            "post_verification_required",
            "pre_plan_lock_recovered",
            "indeterminate",
            "lock_busy",
            "terminal",
        }
        assert {o.value for o in RecoveryLockOutcome} == {
            "not_attempted",
            "acquired_released",
            "acquired_retained",
            "busy",
            "refused",
        }
        assert {s.value for s in RecoveryIdentitySource} == {
            "authorized_plan",
            "protection_journal",
            "operation_lock_binding",
            "none",
        }

    def test_servicio_de_identidad_reportado(self, tmp_path: pathlib.Path) -> None:
        raiz = _raiz(tmp_path)
        durable = _plan_durable(raiz)
        kernel = _KernelDiario()
        journal = _crear_journal(raiz, durable, kernel)
        journal.close()
        port = _PuertoRecuperacion(durable.plan)

        reporte = _recover(raiz, journal_kernel=kernel, lock_kernel=_KernelLock(), port=port)

        assert reporte.physical_identity_source is RecoveryIdentitySource.AUTHORIZED_PLAN


# ============================================================================
# Validez de la API: sin paths, sin root, sin SD del caller (§45/§46)
# ============================================================================


class TestApiDificilDeUsarMal:
    def test_firma_solo_keyword_con_operation_id(self) -> None:
        import inspect

        firma = inspect.signature(recover_interrupted_protection)
        parametros = list(firma.parameters.values())
        assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in parametros)
        nombres = {p.name for p in parametros}
        assert "operation_id" in nombres
        for prohibido in ("path", "root", "canonical_root", "pre_sd", "sd_bytes", "journal_path", "backup"):
            assert prohibido not in nombres, f"la API no acepta '{prohibido}' desde el caller"

    def test_operation_id_invalido_no_ejecuta_nada(self, tmp_path: pathlib.Path) -> None:
        raiz = _raiz(tmp_path)
        with pytest.raises(AuthorizedPlanError):
            _recover(raiz, operation_id="no-es-un-uuid")


# ============================================================================
# Superficie mínima del puerto: el engine NO aplica ni lee sha raw
# ============================================================================


class TestPuertoMinimo:
    def test_el_engine_no_llama_apply_ni_read_sha_en_el_puerto(self, tmp_path: pathlib.Path) -> None:
        """El puerto falso no implementa ``apply_target_dacl`` ni
        ``read_live_pre_sd_sha256``: si el engine los usara, AttributeError."""
        raiz = _raiz(tmp_path)
        durable = _plan_durable(raiz)
        kernel = _KernelDiario()
        journal = _crear_journal(raiz, durable, kernel)
        journal.record_node_mutation_intent(journal.node_binding("Data/Skyrim.esm"))
        journal.close()
        port = _PuertoRecuperacion(durable.plan)
        port.estado_live["Data/Skyrim.esm"] = "target"

        reporte = _recover(raiz, journal_kernel=kernel, lock_kernel=_KernelLock(), port=port)

        assert reporte.disposition is RecoveryDisposition.ROLLED_BACK
        assert not hasattr(port, "apply_target_dacl")
        assert not hasattr(port, "read_live_pre_sd_sha256")

    def test_handles_cerrados_exactamente_una_vez(self, tmp_path: pathlib.Path) -> None:
        raiz = _raiz(tmp_path)
        durable = _plan_durable(raiz)
        kernel = _KernelDiario()
        journal = _crear_journal(raiz, durable, kernel)
        journal.record_node_mutation_intent(journal.node_binding("Data/Skyrim.esm"))
        journal.close()
        port = _PuertoRecuperacion(durable.plan)
        port.estado_live["Data/Skyrim.esm"] = "target"

        _recover(raiz, journal_kernel=kernel, lock_kernel=_KernelLock(), port=port)

        assert port.cerrados, "el recovery debe abrir handles para evaluar nodos"
        assert all(c == 1 for c in port.cerrados.values())
